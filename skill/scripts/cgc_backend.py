#!/usr/bin/env python3
"""Store-backed control plane — the enqueue / await / worker logic over the SQLite store, the SOLE
round authority (the file-spool control plane it replaced is gone). Kept in ONE module so cgc_spool
(enqueue/await CLI) and cgc_daemon (the worker loop) delegate here, and so the whole round lifecycle
is testable with a stubbed CDP driver — no browser.

The worker's mapping of a submit outcome to a round state is the load-bearing part. begin_send
durably commits `sending` before the click, so:
  - a returned conversation id            -> accepted  (the click landed)
  - a returned PROVEN-not-sent verdict:
        login / captcha / rate-limit       -> blocked   (human acts; terminal, never resent)
        model-not-selectable / composer     -> queued    (retry; proven the click never happened)
  - anything else (unknown_send, a crash)  -> possibly_accepted  (uncertain; NEVER auto-resent)
A crash mid-send leaves the round in `sending`; recover() turns that into possibly_accepted too, so
the anti-duplicate invariant holds on every path.
"""
from __future__ import annotations

import json
import os
import sys
import time

import cgc_store as store_mod

# stderr markers cdp_consult emits FAIL-CLOSED, before the click — so their presence PROVES this
# attempt did not send. Safe to trust because _run scopes stderr to the current invocation (an
# earlier attempt's marker can no longer leak in): a marker here is always this attempt's own.
#   _NOT_SENT_BLOCK: needs a human (login/captcha/rate-limit) → terminal blocked, never resent.
#   _NOT_SENT_RETRY: a transient pre-click failure (bad tab/composer/model/page) → requeue; retry may
#       immediately succeed. Includes the "browser can't open a working tab" family (attach_failed /
#       new_tab*) that the file-spool path handled via browser-repair — requeuing lets a transient
#       tab-open glitch retry instead of stranding a provably-unsent round as uncertain.
_NOT_SENT_BLOCK = ("login_needed", "CGC_LOGIN", "captcha", "rate_limit", "usage")
_NOT_SENT_RETRY = ("model_not_selectable", "composer_not_ready", "no_page_target",
                   "attach_failed", "new_tab", "wrong_page", "gate_refused")


def _gate(store, rid: str, prompt: str, validate) -> str | None:
    """Run the egress gate on a `ready` round. Two failure kinds, two outcomes:
      - `refused:`     — an AUTHORITATIVE denial (secret detected / no public link / repo confirmed
                         not public). Terminal GATE_REJECTED; the payload must never leave.
      - `unverified:`  — a TRANSIENT gate failure (gh not installed, gh timed out, gh API error). The
                         gate could not CONFIRM the repo is public, not that it is private. Requeue
                         (the round is still `ready`, pre-send — nothing has left) so a temporary gh
                         outage does not permanently strand a legitimate consult behind a terminal
                         reject. The daemon poll paces the retry; gh's own timeout bounds a tight spin.
    Returns the resulting state if the gate did not pass, else None (passed — caller proceeds to send)."""
    ok, reason = validate(prompt)
    if ok:
        return None
    if reason.startswith("unverified:"):
        store.set_state(rid, store_mod.QUEUED, expect=store_mod.READY, error_code=reason)
        return store_mod.QUEUED
    store.gate_reject(rid, reason)
    return store_mod.GATE_REJECTED


# ---- observability -----------------------------------------------------------
def store_status(up: bool, rid: str | None = None) -> int:
    """Post-cutover `cgc queue`/`status`: round counts by state + the active buckets, so the store is
    as inspectable as the spool dirs were. Surfaces `uncertain` rounds prominently — those are the
    only ones that need a human (possibly sent, deliberately not auto-resent)."""
    with store_mod.Store() as s:
        rows = s.db.execute(
            "SELECT state, count(*) c FROM rounds GROUP BY state ORDER BY state").fetchall()
        active = s.recover()
        # possibly_accepted is the only bucket that needs a HUMAN; a `sending` round is just in-flight.
        needs_human = [row["rid"] for row in s.db.execute(
            "SELECT rid FROM rounds WHERE state=?", (store_mod.POSSIBLY_ACCEPTED,))]
        print(f"daemon: {'UP' if up else 'DOWN'}   backend: STORE ({store_mod.db_path()})")
        for r in rows:
            print(f"  {r['state']:20s} {r['c']:3d}")
        print(f"  active: dispatchable={len(active['dispatchable'])} "
              f"reattach={len(active['reattach'])} in-flight/uncertain={len(active['uncertain'])}")
        if needs_human:
            print("  NEEDS RECONCILE (possibly sent, NOT auto-resent — retrieve, don't resend): "
                  + " ".join(needs_human[:8]))
        if rid:
            rr = s.get_round(rid)
            if rr:
                fields = {k: rr[k] for k in ("state", "kind", "error_code", "current_attempt_id")}
                print(f"  round[{rid}]: {json.dumps(fields)}")
            else:
                print(f"  round[{rid}]: (not in store)")
    return 0


def stats_report() -> int:
    """Aggregate reliability metrics from data the store already holds. The raw material was always
    there (rounds + timestamps); this makes it a number someone actually looks at — the difference
    between 'sentinel drift is happening' being an anecdote and being an alarm."""
    import datetime as _dt

    def _secs(a, b):
        try:
            return (_dt.datetime.fromisoformat(b) - _dt.datetime.fromisoformat(a)).total_seconds()
        except (ValueError, TypeError):
            return None

    with store_mod.Store() as s:
        rows = [dict(r) for r in s.db.execute(
            "SELECT state, created_at, updated_at FROM rounds")]
    total = len(rows)
    by = {}
    for r in rows:
        by[r["state"]] = by.get(r["state"], 0) + 1
    ok = by.get(store_mod.COMPLETED_VERIFIED, 0)
    unv = by.get(store_mod.COMPLETED_UNVERIFIED, 0)
    fail = by.get(store_mod.FAILED, 0) + by.get(store_mod.GATE_REJECTED, 0)
    completed = ok + unv
    durations = sorted(d for r in rows
                       if r["state"] in (store_mod.COMPLETED_VERIFIED, store_mod.COMPLETED_UNVERIFIED)
                       for d in [_secs(r["created_at"], r["updated_at"])] if d is not None and d > 0)

    def _pct(p):
        return durations[min(len(durations) - 1, int(len(durations) * p))] if durations else None

    unv_rate = (unv / completed) if completed else None
    report = {
        "rounds": total, "by_state": by,
        "completed": completed, "failed": fail,
        "unverified_rate": round(unv_rate, 3) if unv_rate is not None else None,
        "latency_s": {"p50": _pct(0.50), "p90": _pct(0.90), "n": len(durations)},
    }
    print(json.dumps(report))
    if completed:
        sys.stderr.write(f"CGC_STATS {completed} completed ({ok} verified / {unv} unverified), "
                         f"{fail} failed, of {total} rounds.\n")
        if durations:
            sys.stderr.write(f"CGC_STATS completion latency p50={int(_pct(0.50))}s "
                             f"p90={int(_pct(0.90))}s over {len(durations)} rounds.\n")
        # The drift alarm: the sentinel wrapper failing often is the earliest cheap signal that the
        # ChatGPT UI or model behavior changed under us.
        if unv_rate is not None and unv_rate > 0.25 and completed >= 8:
            sys.stderr.write(
                f"CGC_ALERT unverified-completion rate is {unv_rate:.0%} — the model is frequently "
                "skipping the BEGIN/END sentinel wrapper. That is the early signal of UI/model "
                "drift: check a recent answer file for truncation and consider re-testing the "
                "prompt template.\n")
    return 0


# ---- enqueue -----------------------------------------------------------------
def enqueue_round(a, prompt: str, out: str) -> int:
    """Create a queued round from the CLI args. The prompt BYTES live on the round (no pathname to
    drift); project_url/model/conversation/poll/timeout ride in spec_json for the worker.

    `--request-key K` makes the enqueue LOGICALLY idempotent: repeating the same key with the same
    payload returns the ORIGINAL round's receipt (an agent that lost the output of its first call
    can safely re-run it), while the same key with a DIFFERENT payload is refused — one key, one
    request. RID uniqueness alone only prevents row collisions, not duplicate intents."""
    conv = getattr(a, "conversation", None)
    parent = getattr(a, "parent", None)
    rkey = getattr(a, "request_key", None)
    # The idempotency comparator is the prompt with THIS round's rid normalized out: the rendered
    # bytes embed the rid (sentinel instructions), so raw bytes differ on every re-fire even when
    # the logical request is identical. Same key + same normalized content = the same request.
    norm_sha = store_mod.sha256((prompt or "").replace(a.rid, "<RID>"))
    with store_mod.Store() as s:
        if rkey:
            prior = s.round_by_request_key(rkey)
            if prior is not None:
                pspec = json.loads(prior["spec_json"]) if prior.get("spec_json") else {}
                if pspec.get("request_sha") == norm_sha:
                    sys.stderr.write(f"CGC_IDEMPOTENT request-key {rkey!r} already enqueued as "
                                     f"{prior['rid']} — returning the original receipt, nothing new "
                                     "queued.\n")
                    print(json.dumps({"queued": True, "rid": prior["rid"],
                                      "out": prior["out_path"] or out, "backend": "store",
                                      "idempotent_repeat": True}))
                    return 0
                sys.stderr.write(f"CGC_ERROR request_key_conflict: request-key {rkey!r} was already "
                                 f"used by {prior['rid']} with DIFFERENT content. One key names one "
                                 "logical request — use a new key for new content.\n")
                return 2
        if s.get_round(a.rid) is not None:
            sys.stderr.write(f"CGC_ERROR already_enqueued: {a.rid} already exists in the store.\n")
            return 2
        # Resolve the follow-up's thread NOW, while the agent's intent is fresh (pinning at enqueue,
        # not at process time, keeps a concurrent consult from stealing the selection). Prefer CAUSAL
        # identity — an explicit --parent <rid> resolves to THAT consult's conversation — over the
        # "last completed" heuristic, which under concurrency or near-simultaneous completions is
        # refused as ambiguous (latest_conversation_strict). Either way FAIL CLOSED: if nothing
        # resolves, refuse — never silently open a fresh conversation and lose the thread's context.
        if a.kind == "followup":
            if parent:
                conv = s.conversation_of(parent)
                if not conv:
                    sys.stderr.write(f"CGC_ERROR followup_parent_unresolved: --parent {parent} has no "
                                     "recorded conversation to continue.\n")
                    return 2
            elif conv in (None, "auto", "last"):
                conv, why = s.latest_conversation_strict(exclude_rid=a.rid)
                if not conv:
                    sys.stderr.write(f"CGC_ERROR followup_no_thread: {why}\n")
                    return 2
        spec = {"project_url": getattr(a, "project_url", None), "model": getattr(a, "model", "Pro"),
                "conversation": conv, "parent_rid": parent, "poll": getattr(a, "poll", None),
                "timeout": getattr(a, "timeout", None), "request_sha": norm_sha}
        thread = conv if conv not in (None, "auto", "last") else None
        s.create_round(a.rid, a.kind, thread_id=thread, out_path=out, prompt=prompt,
                       spec_json=json.dumps(spec), request_key=rkey, parent_rid=parent)
    import cgc_spool as _spool
    if not _spool.daemon_alive():
        sys.stderr.write("CGC_WARN daemon_down: queued, but nothing will send it until the daemon "
                         "runs. Relay ONE line to the user: cgc install-daemon\n")
    if getattr(a, "quiet", False):
        # Composed into `fire`, which prints the one receipt that matters. Two receipts for one
        # action is pure noise.
        return 0
    print(json.dumps({"queued": True, "rid": a.rid, "out": out, "backend": "store"}))
    argv = ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cgc_spool.py"),
            "await", "--rid", a.rid, "--out", out]
    import shlex
    sys.stderr.write(f"CGC_QUEUED {a.rid} (store). Await it:\n  {' '.join(shlex.quote(x) for x in argv)}\n")
    return 0


# ---- cancel ------------------------------------------------------------------
def cancel_round(rid: str) -> int:
    """Cancel a round BEFORE it sends. Only queued/ready are cancellable — past begin_send the click
    may have reached ChatGPT, and cancelling a possible send is indistinguishable from ignoring its
    answer, so it is refused with the states that explain themselves. Idempotent: cancelling an
    already-cancelled round is a no-op success."""
    with store_mod.Store() as s:
        r = s.get_round(rid)
        if r is None:
            sys.stderr.write(f"CGC_ERROR no_such_round: {rid}\n")
            return 2
        if r["state"] in (store_mod.FAILED,) and (r["error_code"] or "").startswith("cancelled"):
            sys.stderr.write(f"CGC_CANCELLED {rid} (already cancelled — idempotent no-op).\n")
            return 0
        if r["state"] not in (store_mod.QUEUED, store_mod.READY):
            sys.stderr.write(
                f"CGC_ERROR not_cancellable: {rid} is '{r['state']}' — past the send fence, the "
                "click may already have reached ChatGPT, so cancel would not undo anything. Await "
                "it, or reconcile via `cgc queue --rid` if it is uncertain.\n")
            return 2
        try:
            s.set_state(rid, store_mod.FAILED, expect=r["state"],
                        error_code="cancelled by caller before send")
        except store_mod.IllegalTransition:
            sys.stderr.write(f"CGC_ERROR cancel_raced: {rid} changed state while cancelling — "
                             "it is being dispatched. Await it instead.\n")
            return 2
    sys.stderr.write(f"CGC_CANCELLED {rid}: cancelled before send.\n")
    return 0


# ---- the outcome envelope ----------------------------------------------------
# Every await exit prints ONE machine-readable JSON line on stdout (prose stays on stderr): an
# agent should never have to infer state by scraping prose. schema=1 is the envelope's version.
def _emit(rid, state, *, retryable, human_action=None, next_command=None, answer_path=None,
          parent_rid=None, confidence=None, error=None):
    import cgc_spool as _spool
    log = _spool.log_path(rid)
    print(json.dumps({
        "schema": 1, "rid": rid, "parent_rid": parent_rid, "state": state,
        "retryable": retryable, "human_action": human_action, "next_command": next_command,
        "answer_path": answer_path, "log_path": log if os.path.exists(log) else None,
        "confidence": confidence, "error": error,
    }))


# ---- await -------------------------------------------------------------------
# Only a SENTINEL-VERIFIED answer is automatic success. completed_unverified (the model skipped the
# BEGIN/END_RESPONSE:<rid> wrapper, so the answer was salvaged by size/position) is NOT auto-success:
# the diff-review showed its salvage can attribute a DIFFERENT round's answer to this rid under
# concurrent same-thread sends, and handing automation a silent "success" there can corrupt
# downstream autonomous work. Unverified answers are materialized for a human, but await returns
# review-required and never emits the auto-followup nudge.
_TERMINAL_OK = (store_mod.COMPLETED_VERIFIED,)


def await_round(a) -> int:
    """Poll the round until terminal, materialize the stored result to --out, and map to the
    three-outcome contract (0 answer / 3 human / 1 broken).

    Liveness is checked in EVERY non-terminal state: the round only advances while a daemon is
    alive to advance it, so a dead daemon must surface as broken-with-next-step promptly — not as
    90 silent minutes of polling a row nobody is working."""
    import cgc_spool as _spool
    out = os.path.abspath(a.out)
    deadline = time.time() + a.timeout
    down_since = None
    while time.time() < deadline:
        with store_mod.Store() as s:
            r = s.get_round(a.rid)
        if r is None:
            sys.stderr.write(f"CGC_BROKEN {a.rid}: no such round in the store.\n")
            _emit(a.rid, "missing", retryable=False, error="no such round in the store")
            return 1
        state = r["state"]
        parent = r.get("parent_rid")
        if state not in (store_mod.COMPLETED_VERIFIED, store_mod.COMPLETED_UNVERIFIED,
                         store_mod.BLOCKED, store_mod.POSSIBLY_ACCEPTED, store_mod.FAILED,
                         store_mod.GATE_REJECTED):
            if _spool.daemon_alive():
                down_since = None
            else:
                if down_since is None:
                    down_since = time.time()
                    sys.stderr.write("CGC_WAIT no live daemon; holding briefly in case it is restarting…\n")
                elif time.time() - down_since > _spool.DAEMON_GRACE_S:
                    sys.stderr.write(
                        f"CGC_BROKEN {a.rid}: round is '{state}' but no daemon is alive to work it. "
                        "Relay ONE line to the user — `cgc install-daemon` (launchd agent: starts at "
                        "login, respawns if it dies). The round stays in the store and runs as soon "
                        "as the daemon is up.\n")
                    _emit(a.rid, state, retryable=True, parent_rid=parent,
                          human_action="run `cgc install-daemon` (the daemon is not running)",
                          next_command=f"python3 {_spool.__file__} await --rid {a.rid} --out {out}",
                          error="daemon down")
                    return 1
        if state == store_mod.COMPLETED_VERIFIED:
            text = r["result_text"] or ""
            if not text:
                sys.stderr.write(f"CGC_BROKEN {a.rid}: round is {state} but its result_text is empty.\n")
                _emit(a.rid, state, retryable=False, parent_rid=parent,
                      error="completed but result_text empty")
                return 1
            _materialize(out, text)
            n = len(text.encode("utf-8"))
            consult = os.path.join(os.path.dirname(os.path.abspath(__file__)), "consult.py")
            followup_cmd = (f"python3 {consult} fire --followup --parent {a.rid} --no-code "
                            "--task \"<local results + the next question>\" --title \"<what's new>\"")
            sys.stderr.write(
                f"CGC_DONE {a.rid}: answer ready ({n} bytes). READ IT AT:\n  {out}\n"
                "CGC_NEXT to CONTINUE this thread (re-review after your changes, re-check a fix, next "
                "phase) — a FOLLOW-UP keeps ChatGPT's context; a fresh consult throws it away:\n"
                f"  {followup_cmd}\n"
                f"  (--parent {a.rid} pins THIS consult's thread causally; add --refs-file for a fresh diff link.)\n")
            _emit(a.rid, state, retryable=False, parent_rid=parent, answer_path=out,
                  confidence="verified", next_command=followup_cmd)
            return 0
        if state == store_mod.COMPLETED_UNVERIFIED:
            text = r["result_text"] or ""
            _materialize(out, text)  # materialize so a human CAN read it — but this is NOT auto-success
            n = len(text.encode("utf-8"))
            sys.stderr.write(
                f"CGC_REVIEW_REQUIRED {a.rid}: an answer was salvaged WITHOUT the BEGIN/END_RESPONSE:"
                f"{a.rid} wrapper ({n} bytes) written to:\n  {out}\n"
                "A HUMAN must verify it is (a) complete/not cut off AND (b) actually this round's "
                "answer — an unwrapped salvage can pick up a different message if another consult ran "
                "on the same thread. Do NOT auto-chain a follow-up on it; re-run the consult if in "
                "doubt.\n")
            _emit(a.rid, state, retryable=False, parent_rid=parent, answer_path=out,
                  confidence="unverified",
                  human_action="verify the salvaged answer is complete and belongs to this round")
            return 3
        if state == store_mod.BLOCKED:
            # A blocker BEFORE the send (login/model/composer at submit) is safely re-enqueued; a
            # blocker AFTER the send (login/captcha/rate-limit hit during the wait) is NOT — the
            # consult may still be generating or already complete, so re-enqueue would duplicate it.
            # A recorded conversation means the send crossed the fence → retrieve, don't resend.
            with store_mod.Store() as _s:
                conv = _s.conversation_of(a.rid)
            if conv:
                retrieve = (f"python3 {_spool.__file__} enqueue --rid <new-rid> --kind retrieve "
                            f"--conversation {conv}")
                sys.stderr.write(
                    f"CGC_BLOCKER {a.rid} (post-send): {r['error_code'] or 'login/captcha/rate-limit'}\n"
                    f"The consult was already sent to conversation {conv}. Clear the blocker in the "
                    "ChatGPT window, then RETRIEVE it (enqueue --kind retrieve --conversation "
                    f"{conv}) — do NOT re-enqueue a fresh consult, it would duplicate this one.\n")
                _emit(a.rid, state, retryable=False, parent_rid=parent,
                      human_action="clear the blocker in the ChatGPT window",
                      next_command=retrieve, error=r["error_code"])
            else:
                sys.stderr.write(
                    f"CGC_BLOCKER {a.rid} (pre-send): {r['error_code'] or 'login/captcha/rate-limit'}\n"
                    "Nothing was sent. A human must clear it in the ChatGPT window, then re-enqueue.\n")
                _emit(a.rid, state, retryable=True, parent_rid=parent,
                      human_action="clear the blocker in the ChatGPT window, then re-fire",
                      error=r["error_code"])
            return 3
        if state == store_mod.POSSIBLY_ACCEPTED:
            sys.stderr.write(
                f"CGC_UNCERTAIN {a.rid}: the send may have reached ChatGPT but was not confirmed "
                f"({r['error_code'] or 'unknown'}). NOT auto-resent to avoid a duplicate consult — "
                f"check the ChatGPT window / retrieve by conversation before re-sending.\n")
            _emit(a.rid, state, retryable=False, parent_rid=parent,
                  human_action="check the ChatGPT window; retrieve by conversation before re-sending",
                  error=r["error_code"])
            return 1
        if state in (store_mod.FAILED, store_mod.GATE_REJECTED):
            err = r["error_code"] or "the daemon could not deliver an answer"
            sys.stderr.write(f"CGC_BROKEN {a.rid}: {err}\n")
            _spool._point_at_log(a.rid, out)
            # `unverified:` gate failures are transient (gh outage) — the round is re-fireable.
            _emit(a.rid, state, retryable=err.startswith("unverified:"), parent_rid=parent, error=err)
            return 1
        time.sleep(getattr(a, "poll", None) or store_mod.__dict__.get("POLL_S", 20) or 20)
    sys.stderr.write(f"CGC_STUCK {a.rid}: no terminal state within {a.timeout}s — read the daemon log.\n")
    _spool._point_at_log(a.rid, out)
    _emit(a.rid, "stuck", retryable=False, error=f"no terminal state within {a.timeout}s",
          human_action="read the job log; the consult is broken, not slow")
    return 1


def _materialize(out: str, text: str) -> None:
    """Write the store's authoritative result to the derived answer file atomically (0600). The file
    is a rematerializable view of the store, so 'done but file missing' is repairable, not a
    contradiction."""
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = out + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, out)


# ---- the worker --------------------------------------------------------------
def process_round(store, r: dict, run_cdp, *, daemon_instance_id: str, validate) -> str:
    """Drive one claimed (`ready`) round to a terminal or uncertain state. `run_cdp` is injected (the
    daemon wraps cdp_consult subprocesses; tests stub it); `validate(prompt) -> (ok, reason)` is the
    egress gate. Returns the final round state (for logging/tests)."""
    rid = r["rid"]
    prompt = r["rendered_prompt"] or ""
    spec = json.loads(r["spec_json"]) if r.get("spec_json") else {}

    # retrieve: attach to an existing conversation and wait — no gate, no send (nothing leaves).
    if r["kind"] == "retrieve":
        conv = spec.get("conversation")
        if not conv or conv == "auto":
            store.finish(rid, store_mod.FAILED, error_code="retrieve needs an explicit conversation id")
            return store_mod.FAILED
        store.set_state(rid, store_mod.WAITING, expect=store_mod.READY)
        return _wait_phase(store, rid, conv, spec, run_cdp)

    # followup: CONTINUE the same thread — attach to its conversation and send there, never open a
    # new one (a fresh submit would silently lose the thread's context, the worst kind of bug).
    if r["kind"] == "followup":
        conv = spec.get("conversation")
        if not conv or conv in ("auto", "last"):
            # enqueue pins a concrete conversation (from --parent, or the last completed thread).
            # Only fall back to THIS round's own pinned thread — never a process-time GLOBAL resolve:
            # the diff-review flagged that a global "latest completed" at process time can attach to
            # an unrelated conversation that finished after enqueue. Ambiguity refuses, never guesses.
            conv = _thread_conv(store, r)
        if not conv:
            store.finish(rid, store_mod.FAILED,
                         error_code="followup has no pinned thread to continue — pass --parent <rid> "
                                    "or --conversation <id>")
            return store_mod.FAILED
        gated = _gate(store, rid, prompt, validate)
        if gated is not None:
            return gated
        attempt = store.begin_send(rid, prompt, store_mod.sha256(prompt),
                                   daemon_instance_id=daemon_instance_id)
        # SEND only (not send+wait): so the round reaches `waiting` promptly and is reattach-able on a
        # restart, exactly like submit. A combined followup --watch would leave it `sending` for the
        # whole ~25-min answer, where a restart would strand it as possibly_accepted.
        send = run_cdp("followup", rid=rid, conversation=conv, prompt=prompt)
        stderr = (send.get("stderr") or "").lower()
        if send.get("code") == 0:
            store.mark_accepted(attempt, conv)       # same thread, confirmed
            store.mark_waiting(rid)
            return _wait_phase(store, rid, conv, spec, run_cdp)
        if any(m.lower() in stderr for m in _NOT_SENT_BLOCK):
            store.set_state(rid, store_mod.BLOCKED, expect=store_mod.SENDING,
                            error_code=_first_marker(stderr, _NOT_SENT_BLOCK))
            return store_mod.BLOCKED
        store.mark_possibly_accepted(attempt, f"followup send failed (exit {send.get('code')}) — retrieve, don't resend")
        return store_mod.POSSIBLY_ACCEPTED

    gated = _gate(store, rid, prompt, validate)
    if gated is not None:
        return gated

    attempt = store.begin_send(rid, prompt, store_mod.sha256(prompt),
                               daemon_instance_id=daemon_instance_id)
    sub = run_cdp("submit", rid=rid, prompt=prompt,
                  project_url=spec.get("project_url") or "https://chatgpt.com/",
                  model=spec.get("model") or "Pro")
    conv = sub.get("conversation")
    stderr = (sub.get("stderr") or "").lower()

    if conv:
        store.mark_accepted(attempt, conv)
        store.mark_waiting(rid)
        return _wait_phase(store, rid, conv, spec, run_cdp)

    # No conversation → the click did not confirm. Distinguish PROVEN-not-sent from uncertain.
    if any(m.lower() in stderr for m in _NOT_SENT_BLOCK):
        store.set_state(rid, store_mod.BLOCKED, expect=store_mod.SENDING,
                        error_code=_first_marker(stderr, _NOT_SENT_BLOCK))
        return store_mod.BLOCKED
    if any(m.lower() in stderr for m in _NOT_SENT_RETRY):
        # provably not sent → safe to re-queue (this is NOT resending a possible send)
        store.set_state(rid, store_mod.QUEUED, expect=store_mod.SENDING,
                        error_code=_first_marker(stderr, _NOT_SENT_RETRY))
        return store_mod.QUEUED
    store.mark_possibly_accepted(attempt, f"submit gave no conversation (exit {sub.get('code')})")
    return store_mod.POSSIBLY_ACCEPTED


def resume_round(store, r: dict, run_cdp) -> str:
    """Reattach to a round and resume polling its existing conversation — the store peer of the
    spool's orphan recovery. It NEVER re-sends (attach + wait is read-only; the waiter's rid-sentinel
    check means it completes only if THIS round's own answer is on the thread). Handles three inputs:
      - accepted / waiting          — a worker died mid-poll; resume it.
      - possibly_accepted + conv    — a ONE-SHOT auto-retrieve (recover() only offers these once): the
                                      send may have landed; read-only attach recovers it if so, and
                                      falls back to uncertain (for a human) if the sentinel is absent.
    A round with no addressable conversation cannot be resumed and is left uncertain."""
    rid = r["rid"]
    spec = json.loads(r["spec_json"]) if r.get("spec_json") else {}
    conv = store.conversation_of(rid)
    if not conv:
        # no conversation recorded → we cannot address it; do NOT resend, leave uncertain.
        if r["state"] != store_mod.POSSIBLY_ACCEPTED:
            store.set_state(rid, store_mod.POSSIBLY_ACCEPTED,
                            error_code="accepted but no conversation to reattach — retrieve manually")
        return store_mod.POSSIBLY_ACCEPTED
    if r["state"] == store_mod.POSSIBLY_ACCEPTED:
        # Claim the one-shot auto-retrieve ATOMICALLY (marker + possibly_accepted->waiting in one
        # transaction). If we lose the claim (already consumed, or no longer eligible), leave it for
        # a human rather than racing another worker onto the same read-only retrieve.
        if not store.claim_auto_retrieve(rid):
            return store_mod.POSSIBLY_ACCEPTED
    elif r["state"] == store_mod.ACCEPTED:
        store.mark_waiting(rid)
    return _wait_phase(store, rid, conv, spec, run_cdp)


def _thread_conv(store, r) -> str | None:
    tid = r.get("thread_id")
    if not tid:
        return None
    row = store.db.execute("SELECT conversation_id FROM threads WHERE thread_id=?", (tid,)).fetchone()
    return row["conversation_id"] if row else None


def _read_answer(path) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _wait_phase(store, rid, conv, spec, run_cdp) -> str:
    out_tmp = os.path.join(store_mod.CGC_STATE_DIR, f"_wait_{rid}.txt")
    # Clear any stale answer + .raw sidecar from a PRIOR wait on this rid (e.g. a timed-out first
    # wait, before an auto-retrieve). _wait_phase decides completed_verified vs _unverified purely
    # from the .raw sidecar's existence, so a leftover .raw would downgrade a later CLEAN sentinel
    # answer to unverified (diff-review S2). Start each wait from a blank slate.
    for _p in (out_tmp, out_tmp + ".raw"):
        try:
            os.remove(_p)
        except OSError:
            pass
    res = run_cdp("wait", rid=rid, conversation=conv, out=out_tmp,
                  poll=spec.get("poll"), timeout=spec.get("timeout"))
    code = res.get("code")
    answer_path = res.get("out") or out_tmp
    answer = _read_answer(answer_path)
    if code == 0 and answer.strip():
        unverified = os.path.exists(answer_path + ".raw")
        store.finish(rid, store_mod.COMPLETED_UNVERIFIED if unverified else store_mod.COMPLETED_VERIFIED,
                     result_text=answer)
        return store_mod.COMPLETED_UNVERIFIED if unverified else store_mod.COMPLETED_VERIFIED
    if code == 3:
        store.set_state(rid, store_mod.BLOCKED, error_code=(res.get("stderr") or "blocker")[:200])
        return store_mod.BLOCKED
    # a wait that returns nothing usable, while the send WAS accepted, is not a clean failure: the
    # answer may still exist in the conversation. Leave it uncertain so it is retrieved, not resent.
    store.set_state(rid, store_mod.POSSIBLY_ACCEPTED,
                    error_code=f"accepted but wait produced no answer (exit {code}) — retrieve, don't resend")
    return store_mod.POSSIBLY_ACCEPTED


def _first_marker(stderr: str, markers) -> str:
    for m in markers:
        if m.lower() in stderr:
            return m
    return "not_sent"
