#!/usr/bin/env python3
"""Store-backed control plane — the enqueue / await / worker logic that runs when CGC_STORE_BACKEND
is on. Kept in ONE module so the cutover is a single delegation point from cgc_spool (enqueue/await)
and cgc_daemon (the worker loop), and so the whole round lifecycle is testable with a stubbed CDP
driver — no browser.

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

# submit stderr markers that PROVE the click did not happen (cdp_consult fails closed before sending).
_NOT_SENT_BLOCK = ("login_needed", "CGC_LOGIN", "captcha", "rate_limit", "usage")
_NOT_SENT_RETRY = ("model_not_selectable", "composer_not_ready", "no_page_target")


def store_enabled() -> bool:
    return store_mod.store_enabled()


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
        print(f"daemon: {'UP' if up else 'DOWN'}   backend: STORE ({store_mod.DB_PATH})")
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


# ---- enqueue -----------------------------------------------------------------
def enqueue_round(a, prompt: str, out: str) -> int:
    """Create a queued round from the CLI args. The prompt BYTES live on the round (no pathname to
    drift); project_url/model/conversation/poll/timeout ride in spec_json for the worker."""
    spec = {"project_url": getattr(a, "project_url", None), "model": getattr(a, "model", "Pro"),
            "conversation": getattr(a, "conversation", None), "poll": getattr(a, "poll", None),
            "timeout": getattr(a, "timeout", None)}
    thread = spec["conversation"] if spec["conversation"] not in (None, "auto") else None
    with store_mod.Store() as s:
        if s.get_round(a.rid) is not None:
            sys.stderr.write(f"CGC_ERROR already_enqueued: {a.rid} already exists in the store.\n")
            return 2
        s.create_round(a.rid, a.kind, thread_id=thread, out_path=out, prompt=prompt,
                       spec_json=json.dumps(spec))
    print(json.dumps({"queued": True, "rid": a.rid, "out": out, "backend": "store"}))
    argv = ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cgc_spool.py"),
            "await", "--rid", a.rid, "--out", out]
    import shlex
    sys.stderr.write(f"CGC_QUEUED {a.rid} (store). Await it:\n  {' '.join(shlex.quote(x) for x in argv)}\n")
    return 0


# ---- await -------------------------------------------------------------------
_TERMINAL_OK = store_mod.COMPLETED_VERIFIED, store_mod.COMPLETED_UNVERIFIED


def await_round(a) -> int:
    """Poll the round until terminal, materialize the stored result to --out, and map to the same
    three-outcome contract as the file-spool await (0 answer / 3 human / 1 broken)."""
    out = os.path.abspath(a.out)
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        with store_mod.Store() as s:
            r = s.get_round(a.rid)
        if r is None:
            sys.stderr.write(f"CGC_BROKEN {a.rid}: no such round in the store.\n")
            return 1
        state = r["state"]
        if state in _TERMINAL_OK:
            text = r["result_text"] or ""
            if not text:
                sys.stderr.write(f"CGC_BROKEN {a.rid}: round is {state} but its result_text is empty.\n")
                return 1
            _materialize(out, text)
            n = len(text.encode("utf-8"))
            tag = "" if state == store_mod.COMPLETED_VERIFIED else " (UNVERIFIED salvage — check it isn't cut off)"
            sys.stderr.write(f"CGC_DONE {a.rid}: answer ready ({n} bytes){tag}. READ IT AT:\n  {out}\n")
            return 0
        if state == store_mod.BLOCKED:
            sys.stderr.write(f"CGC_BLOCKER {a.rid}: {r['error_code'] or 'login/captcha/rate-limit'}\n"
                             "A human must act in the ChatGPT window, then re-enqueue.\n")
            return 3
        if state == store_mod.POSSIBLY_ACCEPTED:
            sys.stderr.write(
                f"CGC_UNCERTAIN {a.rid}: the send may have reached ChatGPT but was not confirmed "
                f"({r['error_code'] or 'unknown'}). NOT auto-resent to avoid a duplicate consult — "
                f"check the ChatGPT window / retrieve by conversation before re-sending.\n")
            return 1
        if state in (store_mod.FAILED, store_mod.GATE_REJECTED):
            sys.stderr.write(f"CGC_BROKEN {a.rid}: {r['error_code'] or 'the daemon could not deliver an answer'}\n")
            return 1
        time.sleep(getattr(a, "poll", None) or store_mod.__dict__.get("POLL_S", 20) or 20)
    sys.stderr.write(f"CGC_STUCK {a.rid}: no terminal state within {a.timeout}s — read the daemon log.\n")
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
        if not conv or conv == "auto":
            conv = _thread_conv(store, r)
        if not conv:
            store.finish(rid, store_mod.FAILED,
                         error_code="followup needs an explicit conversation id — no thread to continue")
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
        # bound the auto-retrieve BEFORE the wait, so a never-landed send is not re-attached forever.
        store.record_auto_retrieve(rid)
        store.set_state(rid, store_mod.WAITING, expect=store_mod.POSSIBLY_ACCEPTED)
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
