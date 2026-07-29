#!/usr/bin/env python3
"""Store-backed control plane — the enqueue / await / worker logic over the SQLite store, the SOLE
round authority (the file-spool control plane it replaced is gone). Kept in ONE module so cgc_spool
(enqueue/await CLI) and cgc_daemon (the worker loop) delegate here, and so the whole round lifecycle
is testable with a stubbed CDP driver — no browser.

The worker's mapping of a submit outcome to a round state is the load-bearing part. begin_send
durably commits `sending` before the click, so:
  - exit 6 (EXIT_NOT_SENT_PRECLICK)        -> failed    (cdp_consult's own pre-click boundary proved
                                                          the click was never issued; auto-stamped
                                                          not-sent-proven, key-retry-eligible with no
                                                          operator step — see mark_send_not_sent)
  - a returned conversation id             -> accepted  (the click landed)
  - a returned PROVEN-not-sent verdict (stderr marker):
        login / captcha / rate-limit       -> blocked   (human acts; terminal, never resent)
        model-not-selectable / composer     -> queued    (retry; proven the click never happened)
  - anything else (unknown_send, a crash)  -> possibly_accepted  (uncertain; NEVER auto-resent)
A crash mid-send leaves the round in `sending`; recover() turns that into possibly_accepted too, so
the anti-duplicate invariant holds on every path.
"""
from __future__ import annotations

import json
import os
import re
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

# cdp_consult.py's EXIT_NOT_SENT_PRECLICK: its pre-click boundary (composer settle/clear/chunked-paste
# /verify) catches EVERY exception raised inside it, so this exit code is trusted directly by NUMBER,
# not by matching a stderr string — a raw traceback (the recorded field incident: a websocket read
# timeout mid-paste) carries no marker text and used to fall through to possibly_accepted uncaught.
_EXIT_NOT_SENT_PRECLICK = 6


class ConversationLeaseRefused(Exception):
    """Another worker holds the per-conversation exclusive mutator lease for this follow-up's target
    thread, so ours may not touch that thread's shared composer. Raised BEFORE begin_send, so the
    round is untouched (still READY) — nothing was sent and no state moved. The worker catches this
    and exits EXIT_LEASE_REFUSED, exactly like a lost rid-lease race: the round is redispatched later
    and the reaper treats the death as inert."""
    def __init__(self, conversation):
        super().__init__(conversation)
        self.conversation = conversation


# A pre-click failure is called transient because a retry MAY fix it — not because it will. Three
# begun sends that all died before the click is no longer evidence of a blip; it is a standing
# condition (the observed one: the thread's model tier had dropped off Pro, which no retry can
# restore). Unbounded, that retry is a hot loop against the daemon poll, and each turn of it opens
# a browser tab — exhausting the one resource whose exhaustion ends every consult. So it is bounded,
# and the round then BLOCKS: terminal, never resent, addressed to the human who can actually fix it.
_MAX_NOT_SENT_RETRIES = 3


def _requeue_or_block(store, rid: str, marker: str) -> str:
    """Route a PROVEN not-sent round: retry while the evidence still reads transient, then block."""
    if store.attempt_count(rid) >= _MAX_NOT_SENT_RETRIES:
        store.set_state(rid, store_mod.BLOCKED, expect=store_mod.SENDING,
                        error_code=(f"{marker} — still failing after {_MAX_NOT_SENT_RETRIES} "
                                    "automatic retries; nothing was ever sent, but this needs a "
                                    "human (check the ChatGPT window: model tier, login, the tab)"))
        return store_mod.BLOCKED
    store.set_state(rid, store_mod.QUEUED, expect=store_mod.SENDING, error_code=marker)
    return store_mod.QUEUED



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
def _request_fingerprint(a, prompt: str) -> str:
    """The identity of one LOGICAL request — canonical JSON over every CALLER-side field that
    routes or shapes it: kind, the rid-independent prompt identity, project, model, and the
    caller's explicit thread selection (--parent / a concrete --conversation). Same key + a
    different ANY of these is a conflict, never a silently-returned old receipt.

    Caller-side fields only, deliberately not the enqueue-time RESOLVED conversation: resolution
    is derived state (and for a bare-auto followup it depends on what is in flight — including the
    original round a retry is trying to recover, which would deadlock the idempotent re-fire that
    is this feature's whole point). The prompt identity is --logical-sha (the prompt rendered with a
    literal <RID> placeholder, so user text containing a rid is never normalized away); enqueue
    REFUSES a --request-key without it, so a keyed round's fingerprint is always logical-sha-based.
    The raw-prompt hash below is only ever reached by a NON-keyed round, whose stored fingerprint is
    never compared — so it must NOT normalize the rid out of user text (the S2 defect: a literal rid
    in caller-supplied content could be erased and forge a false match)."""
    logical = getattr(a, "logical_sha", None) or store_mod.sha256(prompt or "")
    conv = getattr(a, "conversation", None)
    return store_mod.sha256(json.dumps({
        "fp": 1, "kind": a.kind, "prompt": logical,
        "project_url": getattr(a, "project_url", None), "model": getattr(a, "model", "Pro"),
        "parent": getattr(a, "parent", None),
        "conversation": None if conv in (None, "auto", "last") else conv,
    }, sort_keys=True))


def _release_eligible(s, prior: dict) -> bool:
    """Does the store DURABLY prove the prior round's send never reached ChatGPT, so its request-key
    may move to a retry? Three sufficient proofs, and nothing else:
      - GATE_REJECTED — pre-send by construction (the gate runs while `ready`, before begin_send).
      - FAILED with no send attempt EVER — has_send_attempt is False, i.e. the round never entered
        `sending`, so nothing could have been sent (cancel-while-queued, no-thread, gate paths).
      - FAILED carrying a not-sent proof (send_disposition) — an operator's explicit reconcile, OR the
        CDP driver's own pre-click exit (mark_send_not_sent, exit 6: a paste/verify failure proven to
        precede the send click) recorded automatically, no operator step needed.
    A FAILED round that DID cross the send fence (an attempt exists) with no such proof is
    post-send-uncertain — possibly_accepted->failed / waiting->failed are legal reconciles that do
    NOT prove no-send — so its key stays owned. FAILED alone is never sufficient."""
    if prior["state"] == store_mod.GATE_REJECTED:
        return True
    if prior["state"] != store_mod.FAILED:
        return False
    if prior.get("send_disposition") == store_mod.NOT_SENT_PROVEN:
        return True
    return not s.has_send_attempt(prior["rid"])


def _idempotent_receipt(s, prior: dict, fingerprint: str, rkey: str, out: str):
    """Decide what a repeated request-key means. FINGERPRINT FIRST, always: a different logical
    request under the same key is a conflict REGARDLESS of the prior's state — one key names one
    intent, and releasing a key before comparing (the S3 defect) let a different request reuse it
    after a failure. Rows predating the structured fingerprint carry none and so never match —
    conflict, the safe direction.

    With a MATCHING fingerprint:
      - a live/completed prior returns its ORIGINAL receipt (idempotent repeat);
      - a terminally-failed prior releases its key to the retry ONLY when the store durably proves
        no send ever happened (_release_eligible); otherwise the key stays owned and the retry is
        refused as possibly-already-sent.

    Returns 0 (idempotent receipt printed), 2 (conflict), or prior['rid'] (str) — the row to
    transfer the key FROM into a fresh successor."""
    pspec = json.loads(prior["spec_json"]) if prior.get("spec_json") else {}
    if pspec.get("request_fingerprint") != fingerprint:
        sys.stderr.write(f"CGC_ERROR request_key_conflict: request-key {rkey!r} was already "
                         f"used by {prior['rid']} with a DIFFERENT logical request (content, "
                         "kind, parent, conversation, project, or model differ). One key names "
                         "one logical request — use a new key.\n")
        return 2
    if prior["state"] in (store_mod.FAILED, store_mod.GATE_REJECTED):
        if _release_eligible(s, prior):
            sys.stderr.write(f"CGC_KEY_RELEASED request-key {rkey!r}: prior {prior['rid']} "
                             f"({prior['state']}) is durably proven not-sent — moving the key to a "
                             "fresh round.\n")
            return prior["rid"]  # caller performs the atomic transfer
        sys.stderr.write(
            f"CGC_ERROR request_key_locked: request-key {rkey!r} names {prior['rid']}, which "
            "FAILED after its send may have reached ChatGPT (a durable send attempt exists and no "
            "operator not-sent proof was recorded). Re-using the key could send a duplicate consult. "
            "Retrieve/reconcile that round — record a not-sent proof if you have verified it never "
            "sent — or use a new key.\n")
        return 2
    sys.stderr.write(f"CGC_IDEMPOTENT request-key {rkey!r} already enqueued as "
                     f"{prior['rid']} — returning the original receipt, nothing new "
                     "queued.\n")
    print(json.dumps({"queued": True, "rid": prior["rid"],
                      "out": prior["out_path"] or out, "backend": "store",
                      "idempotent_repeat": True}))
    return 0


def _post_create_receipt(a, out: str, queued: bool = True) -> int:
    """The shared tail after a round is created (fresh insert OR key transfer) — and after an
    idempotent repeat, where `queued=False`: the round was already there, so claiming it was queued
    would be a receipt that lies. Warns if no daemon will send it, then prints the one receipt
    unless composed into `fire` (quiet). Either way the caller's next step is the same `await`."""
    import cgc_spool as _spool
    if queued and not _spool.daemon_alive():
        sys.stderr.write("CGC_WARN daemon_down: queued, but nothing will send it until the daemon "
                         "runs. Relay ONE line to the user: cgc install-daemon\n")
    if getattr(a, "quiet", False):
        # Composed into `fire`, which prints the one receipt that matters. Two receipts for one
        # action is pure noise.
        return 0
    print(json.dumps({"queued": queued, "rid": a.rid, "out": out, "backend": "store"}))
    argv = ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cgc_spool.py"),
            "await", "--rid", a.rid, "--out", out]
    import shlex
    sys.stderr.write(f"{'CGC_QUEUED' if queued else 'CGC_ALREADY'} {a.rid} (store). Await it:\n"
                     f"  {' '.join(shlex.quote(x) for x in argv)}\n")
    return 0


def _enqueue_transfer(s, a, prompt: str, out: str, prior: dict, fingerprint: str, rkey: str) -> int:
    """Enqueue a same-fingerprint retry that inherits a released key from a proven-not-sent `prior`,
    atomically. Route PRESERVATION (audit req 5): the successor reuses the prior's already-RESOLVED
    conversation/parent rather than re-resolving — two same-fingerprint requests (e.g. a bare-auto
    followup, whose fingerprint excludes the resolved thread) must never diverge to different
    conversations because the 'last completed' thread moved between the two fires. The key transfer
    and successor insert are one transaction; a lost race re-resolves to the winner's receipt."""
    pspec = json.loads(prior["spec_json"]) if prior.get("spec_json") else {}
    inh_conv = pspec.get("conversation")
    inh_parent = pspec.get("parent_rid") or prior.get("parent_rid")
    spec = {"project_url": getattr(a, "project_url", None), "model": getattr(a, "model", "Pro"),
            "conversation": inh_conv, "parent_rid": inh_parent, "poll": getattr(a, "poll", None),
            "timeout": getattr(a, "timeout", None), "request_fingerprint": fingerprint}
    thread = inh_conv if inh_conv not in (None, "auto", "last") else None
    try:
        s.transfer_request_key(prior["rid"], rid=a.rid, kind=a.kind, thread_id=thread,
                               out_path=out, spec_json=json.dumps(spec), prompt=prompt,
                               request_key=rkey, parent_rid=inh_parent)
    except store_mod.RequestKeyRaced:
        # A concurrent retry moved the key first — re-resolve to the current owner (the winner) and
        # take its receipt, so exactly one successor ever owns the key.
        winner = s.round_by_request_key(rkey)
        if winner is None:
            raise
        res = _idempotent_receipt(s, winner, fingerprint, rkey, out)
        if isinstance(res, int):
            return res
        return _enqueue_transfer(s, a, prompt, out, winner, fingerprint, rkey)
    return _post_create_receipt(a, out)


def enqueue_round(a, prompt: str, out: str) -> int:
    """Create a queued round from the CLI args. The prompt BYTES live on the round (no pathname to
    drift); project_url/model/conversation/poll/timeout ride in spec_json for the worker.

    `--request-key K` makes the enqueue LOGICALLY idempotent: repeating the same key with the same
    logical request returns the ORIGINAL round's receipt (an agent that lost the output of its
    first call can safely re-run it), while the same key with a different one is refused — one key,
    one request. RID uniqueness alone only prevents row collisions, not duplicate intents."""
    import sqlite3
    import cgc_spool as _spool
    conv = getattr(a, "conversation", None)
    parent = getattr(a, "parent", None)
    rkey = getattr(a, "request_key", None)
    # S3-B: an explicit --conversation must ALREADY be a canonical conversation id. A rid/URL/path
    # alias is resolvable, but it would be stored and locked verbatim while the --parent path resolves
    # the same thread to its canonical id and locks THAT — two lock files for one composer. Fail
    # closed (canonical-by-construction): reject the alias here, before it is stored, and name the fix.
    if conv not in (None, "auto", "last") and not _spool.is_canonical_conversation(conv):
        sys.stderr.write(
            "CGC_ERROR conversation_not_canonical: --conversation must be a bare ChatGPT conversation "
            f"id (uuid), got {conv!r}. To target a consult by its rid use --parent <rid>; otherwise "
            "pass the bare conversation id.\n")
        return 2
    # A request-key's idempotency identity is its fingerprint, whose prompt component is --logical-sha
    # (the placeholder render). Without it the low-level enqueue would fall back to hashing the raw
    # prompt — which cannot safely distinguish logical requests when the caller supplies the prompt
    # directly (S2). Refuse rather than key off an unsafe identity; the canonical fire/prep path
    # always passes --logical-sha.
    if rkey and not getattr(a, "logical_sha", None):
        sys.stderr.write(
            "CGC_ERROR request_key_needs_logical_sha: --request-key requires --logical-sha (the "
            "placeholder-rendered content identity from prep). Without it the request-key "
            "fingerprint cannot safely tell one logical request from another. Render via "
            "prep/fire, or pass --logical-sha.\n")
        return 2
    fingerprint = _request_fingerprint(a, prompt)
    with store_mod.Store() as s:
        # Writer-identity fence: if a daemon is LIVE, it must be operating THIS store. An old-code
        # daemon (no identity fields) or one holding a different DB (the pre-cutover /tmp file — the
        # recorded live split-brain) would accept this row into a store nobody serves, or serve a
        # store this row never reaches. Refuse now, in seconds, with the fix.
        import cgc_spool as _spool
        ident = _spool.daemon_identity()
        if ident is not None:
            my_path, my_uuid = os.path.abspath(store_mod.db_path()), s.store_uuid()
            if ident.get("db_path") != my_path or ident.get("store_uuid") != my_uuid:
                sys.stderr.write(
                    "CGC_ERROR store_mismatch: the running daemon is not serving this store "
                    f"(daemon: db={ident.get('db_path')!r} uuid={ident.get('store_uuid')!r} / "
                    f"CLI: db={my_path!r} uuid={my_uuid!r}). It is running old code or holding a "
                    "relocated-away DB. Restart it:\n"
                    "  launchctl kickstart -k gui/$(id -u)/com.open-claude-gpt.daemon\n")
                return 2
        if rkey:
            prior = s.round_by_request_key(rkey)
            if prior is not None:
                res = _idempotent_receipt(s, prior, fingerprint, rkey, out)
                if isinstance(res, int):
                    return res
                # res is the prior rid → move its released key onto this retry, atomically.
                return _enqueue_transfer(s, a, prompt, out, prior, fingerprint, rkey)
        existing = s.get_round(a.rid)
        if existing is not None:
            # A rid is minted per render, so re-enqueuing one is always a repeat of a request the
            # store already owns — and the caller's next move depends entirely on WHICH state it is
            # in. Saying only "already exists" left a retry loop with nothing to act on: the same
            # command failed the same way every time, and the round it was colliding with had
            # already answered. Name the state and the one command that follows from it.
            import cgc_spool as _spool
            st = existing["state"]
            # SAME rid + SAME bytes is the same request, so say so and SUCCEED. The caller's next
            # step (`await`) is what actually delivers the answer, and the canonical invocation
            # chains the two with `&&` — refusing here short-circuited that chain and left an
            # answer sitting completed in the store while the command failed identically on every
            # retry. Nothing is enqueued either way; this only decides whether the chain proceeds.
            # A DIFFERENT prompt under the same rid is a different request and still refuses.
            if existing["prompt_sha256"] == (store_mod.sha256(prompt) if prompt else None):
                sys.stderr.write(
                    f"CGC_IDEMPOTENT {a.rid} is already in the store ('{st}') with these exact "
                    "bytes — nothing new queued; await it for the answer.\n")
                return _post_create_receipt(a, out, queued=False)
            if st in (store_mod.COMPLETED_VERIFIED, store_mod.COMPLETED_UNVERIFIED):
                nxt = (f"read its answer: {existing['out_path']}  (or re-materialize it: python3 "
                       f"{_spool.__file__} await --rid {a.rid} --out {existing['out_path']})\n"
                       "  To ask something NEW on that thread, render a fresh round and continue it:\n"
                       f"  consult.py fire --followup --parent {a.rid} --task \"<...>\"")
            elif st in (store_mod.FAILED, store_mod.GATE_REJECTED, store_mod.BLOCKED):
                nxt = (f"it ended '{st}' ({existing['error_code'] or 'no detail'}) — render a FRESH "
                       "round (new rid) rather than re-enqueuing this one")
            elif st in store_mod.UNCERTAIN:
                nxt = ("its send may already have reached ChatGPT — retrieve it, never re-send: "
                       f"python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cdp_consult.py')}"
                       f" find-conversation --rid {a.rid}")
            else:
                nxt = (f"it is '{st}' and the daemon is working it: python3 {_spool.__file__} "
                       f"await --rid {a.rid} --out {existing['out_path']}")
            sys.stderr.write(f"CGC_ERROR already_enqueued: {a.rid} is already in the store — {nxt}\n")
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
        # A retrieve round recovers ANOTHER round's answer read-only. It must pin THAT round's rid
        # (via --parent) as the source to verify against on the page — never resolve "whatever the
        # conversation's latest turn is" (auto), which silently adopts a later same-thread send's
        # answer if one lands before/during recovery (the causal-substitution bug this fixes).
        if a.kind == "retrieve" and not parent:
            sys.stderr.write(
                "CGC_ERROR retrieve_needs_source_rid: --kind retrieve requires --parent <rid> (the "
                "rid of the uncertain round being recovered) — the recovery path must never "
                "auto-adopt the conversation's latest turn.\n")
            return 2
        spec = {"project_url": getattr(a, "project_url", None), "model": getattr(a, "model", "Pro"),
                "conversation": conv, "parent_rid": parent, "poll": getattr(a, "poll", None),
                "timeout": getattr(a, "timeout", None), "request_fingerprint": fingerprint}
        thread = conv if conv not in (None, "auto", "last") else None
        try:
            s.create_round(a.rid, a.kind, thread_id=thread, out_path=out, prompt=prompt,
                           spec_json=json.dumps(spec), request_key=rkey, parent_rid=parent)
        except sqlite3.IntegrityError:
            # Two concurrent enqueues with the same key both saw "no prior row"; the unique index
            # on request_key made exactly one insert win. The loser resolves DETERMINISTICALLY to
            # the winner's receipt (or a conflict, or a proven-not-sent key transfer) instead of
            # surfacing a raw constraint error.
            prior = s.round_by_request_key(rkey) if rkey else None
            if prior is None:
                raise
            res = _idempotent_receipt(s, prior, fingerprint, rkey, out)
            if isinstance(res, int):
                return res
            # the racing winner had itself terminally failed proven-not-sent — its key is released;
            # move it onto this retry atomically (a second conflict is a real error and propagates).
            return _enqueue_transfer(s, a, prompt, out, prior, fingerprint, rkey)
    return _post_create_receipt(a, out)


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
          parent_rid=None, confidence=None, error=None, source_rid=None, observed_rid=None):
    import cgc_spool as _spool
    # Envelope invariant: retryable=true must always name the retry — an agent told "retryable"
    # with no next_command/human_action has a verdict but no move. Enforced structurally so no
    # future branch can violate it.
    if retryable and not (next_command or human_action):
        human_action = "re-run the SAME fire (same --request-key) — the failure was transient"
    log = _spool.log_path(rid)
    try:
        has_log = os.path.exists(log)
    except OSError:
        has_log = False
    print(json.dumps({
        "schema": 1, "rid": rid, "parent_rid": parent_rid, "state": state,
        "retryable": retryable, "human_action": human_action, "next_command": next_command,
        "answer_path": answer_path, "log_path": log if has_log else None,
        "confidence": confidence, "error": error,
        # source_rid: for a retrieve round, the rid of the round it recovers (transparency — a
        # retrieve's own rid is fresh and means nothing on its own). observed_rid: on a rid-mismatch
        # failure, the rid the CDP layer actually found on the page instead, so a human/agent can
        # see WHAT it refused to substitute, not just that it refused.
        "source_rid": source_rid, "observed_rid": observed_rid,
    }))


_OBSERVED_RID_RE = re.compile(r"observed_rid=(\S+)")


def _parse_observed_rid(error_code: str | None) -> str | None:
    """Pull the 'observed_rid=<rid>' token cdp_consult embeds in a retrieve's rid_absent/
    rid_superseded stderr line (see cdp_consult._locate_source_turn) back out of the error_code
    the round stored it under, for the outcome envelope. 'none' means the CDP layer itself found
    nothing to report."""
    if not error_code:
        return None
    m = _OBSERVED_RID_RE.search(error_code)
    if not m or m.group(1) in ("none", "None"):
        return None
    return m.group(1)


def _stderr_error_line(stderr: str) -> str | None:
    """The last CGC_ERROR line in a CDP subprocess's stderr — the actionable verdict (rid_absent,
    rid_superseded, ...), not the CGC_WAIT heartbeat noise around it. None if the subprocess never
    printed one (e.g. a plain timeout)."""
    lines = [ln for ln in (stderr or "").splitlines() if ln.startswith("CGC_ERROR")]
    return lines[-1] if lines else None


# ---- await -------------------------------------------------------------------
# Only a SENTINEL-VERIFIED answer is automatic success. completed_unverified (the model skipped the
# BEGIN/END_RESPONSE:<rid> wrapper, so the answer was salvaged by size/position) is NOT auto-success:
# the diff-review showed its salvage can attribute a DIFFERENT round's answer to this rid under
# concurrent same-thread sends, and handing automation a silent "success" there can corrupt
# downstream autonomous work. Unverified answers are materialized for a human, but await returns
# review-required and never emits the auto-followup nudge.
_TERMINAL_OK = (store_mod.COMPLETED_VERIFIED,)


def await_round(a) -> int:
    """The public await entry. EVERY termination — including a store that cannot open
    (SchemaTooNew, a corrupt file) or an answer that cannot materialize (disk full) — emits exactly
    one JSON envelope on stdout; the traceback goes to stderr. 'Every await exit has an envelope'
    is a contract, not a happy-path property."""
    try:
        return _await_round(a)
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            _emit(a.rid, "error", retryable=False,
                  human_action="await itself failed before reaching a round outcome — read the "
                               "traceback on stderr",
                  error=f"{type(e).__name__}: {e}")
        except Exception:
            print(json.dumps({"schema": 1, "rid": getattr(a, "rid", None), "state": "error",
                              "retryable": False, "error": f"{type(e).__name__}: {e}"}))
        return 1


def _await_round(a) -> int:
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
        # A retrieve round stores the rid it is RECOVERING in the same parent_rid column (its own
        # rid is fresh and means nothing on its own) — surface it in the envelope as source_rid so
        # a human/agent never has to guess which round a retrieve was for.
        src_rid = parent if r.get("kind") == "retrieve" else None
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
                  confidence="verified", next_command=followup_cmd, source_rid=src_rid)
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
                  confidence="unverified", source_rid=src_rid,
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
                # --parent pins the SOURCE rid the retrieve must verify on the page — never let it
                # auto-adopt whatever the conversation's latest turn happens to be by the time a
                # human clears the blocker and runs this.
                retrieve = (f"python3 {_spool.__file__} enqueue --rid <new-rid> --kind retrieve "
                            f"--conversation {conv} --parent {a.rid}")
                sys.stderr.write(
                    f"CGC_BLOCKER {a.rid} (post-send): {r['error_code'] or 'login/captcha/rate-limit'}\n"
                    f"The consult was already sent to conversation {conv}. Clear the blocker in the "
                    "ChatGPT window, then RETRIEVE it (enqueue --kind retrieve --conversation "
                    f"{conv} --parent {a.rid}) — do NOT re-enqueue a fresh consult, it would "
                    "duplicate this one.\n")
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
            # The recovery instruction: a fresh kind=retrieve round, read-only, pinned to THIS
            # round's rid via --parent — never auto-adopt whatever the conversation's latest turn
            # is by the time a human/agent runs it (a later same-thread send would otherwise be
            # mistaken for this round's answer).
            with store_mod.Store() as _s:
                conv = _s.conversation_of(a.rid)
            # With no conversation recorded there is still one thing to try before a human reads the
            # screen: the rid is inside the prompt that was sent, so an open tab carrying it IS this
            # round's thread. That search is read-only and cannot duplicate anything.
            cdp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdp_consult.py")
            retrieve = (f"python3 {_spool.__file__} enqueue --rid <new-rid> --kind retrieve "
                        f"--conversation {conv} --parent {a.rid}") if conv else (
                        f"python3 {cdp} find-conversation --rid {a.rid}")
            sys.stderr.write(
                f"CGC_UNCERTAIN {a.rid}: the send may have reached ChatGPT but was not confirmed "
                f"({r['error_code'] or 'unknown'}). NOT auto-resent to avoid a duplicate consult — "
                + (f"retrieve it:\n  {retrieve}\n" if conv else
                   "no conversation was captured, so first ASK THE BROWSER which thread holds this "
                   f"rid (read-only, sends nothing):\n  {retrieve}\n"
                   "If it names a conversation, retrieve from it (enqueue --kind retrieve "
                   f"--conversation <id> --parent {a.rid}); if it finds none, a human must look at "
                   "the ChatGPT window before any re-send.\n"))
            _emit(a.rid, state, retryable=False, parent_rid=parent,
                  human_action="check the ChatGPT window; retrieve by conversation before re-sending",
                  next_command=retrieve, error=r["error_code"])
            # 3, not 1. The documented contract is 0 answer / 3 a human must act / 1 BROKEN, and an
            # uncertain send is the definition of "a human must act" — nothing is broken, the send
            # may well have landed. Reporting it as broken is what made every one of these read as a
            # tool failure in the caller's log ("failed with exit code 1") instead of as the one
            # action item it is, which is how an hour went into hunting a crash that never happened.
            return 3
        if state in (store_mod.FAILED, store_mod.GATE_REJECTED):
            err = r["error_code"] or "the daemon could not deliver an answer"
            sys.stderr.write(f"CGC_BROKEN {a.rid}: {err}\n")
            _spool._point_at_log(a.rid, out)
            # `unverified:` gate failures are transient (gh outage) — the round is re-fireable.
            _emit(a.rid, state, retryable=err.startswith("unverified:"), parent_rid=parent, error=err,
                  source_rid=src_rid, observed_rid=_parse_observed_rid(err) if src_rid else None)
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
    # It pins the SOURCE round's rid (spec["parent_rid"], from --parent at enqueue) and hands it to
    # the waiter as the exact turn to verify + extract. Auto-adopting whatever the conversation's
    # LATEST turn happens to be (the earlier design here) silently commits a later same-thread
    # send's answer as this recovery's result if one lands before/during it — per-rid leases don't
    # serialize a conversation, and an explicit-parent caller can legitimately fire more than one
    # round on the same thread. Fail closed instead: no source_rid, no retrieve.
    if r["kind"] == "retrieve":
        conv = spec.get("conversation")
        if not conv or conv == "auto":
            store.finish(rid, store_mod.FAILED, error_code="retrieve needs an explicit conversation id")
            return store_mod.FAILED
        source_rid = spec.get("parent_rid") or r.get("parent_rid")
        if not source_rid:
            store.finish(rid, store_mod.FAILED,
                        error_code="retrieve needs an explicit source_rid (--parent <rid>) — the "
                                  "recovery path must never auto-adopt the conversation's latest turn")
            return store_mod.FAILED
        store.set_state(rid, store_mod.WAITING, expect=store_mod.READY)
        return _wait_phase(store, rid, conv, spec, run_cdp, wait_rid=source_rid, is_retrieve=True)

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
        import cgc_spool as _spool
        # S3-B worker boundary: a pre-release queued row may carry a rid-shaped conversation. The lock
        # key MUST be canonical; a non-canonical resolved conversation is a PRE-SEND terminal failure
        # (nothing has touched the browser here), never a mis-keyed lock or a send.
        if not _spool.is_canonical_conversation(conv):
            store.finish(rid, store_mod.FAILED,
                         error_code=(f"followup conversation is not a canonical id ({conv!r}) — "
                                     "re-enqueue with --parent <rid> or a bare conversation id"))
            return store_mod.FAILED
        gated = _gate(store, rid, prompt, validate)
        if gated is not None:
            return gated
        # Per-conversation exclusive mutator lease. The gate above is store-only (no browser), so the
        # mutating region is exactly begin_send + the follow-up CDP subprocess (attach -> clear ->
        # paste -> verify -> click -> landing-rid). Acquire AFTER the conversation is concrete and
        # BEFORE that subprocess; hold until it returns. A second same-conversation follow-up cannot
        # enter this region — it refuses (the round is still READY, redispatched later) rather than
        # racing W1 onto the one shared composer and corrupting the pre-click not-sent proof.
        conv_lease = _spool.acquire_conversation_lease(conv)
        if conv_lease is None:
            raise ConversationLeaseRefused(conv)
        try:
            attempt = store.begin_send(rid, prompt, store_mod.sha256(prompt),
                                       daemon_instance_id=daemon_instance_id)
            # SEND only (not send+wait): so the round reaches `waiting` promptly and is reattach-able on
            # a restart, exactly like submit. A combined followup --watch would leave it `sending` for
            # the whole ~25-min answer, where a restart would strand it as possibly_accepted.
            send = run_cdp("followup", rid=rid, conversation=conv, prompt=prompt)
        finally:
            if hasattr(conv_lease, "close"):
                conv_lease.close()  # mutating region done — the read-only wait below needs no lease
        stderr = (send.get("stderr") or "").lower()
        if send.get("code") == _EXIT_NOT_SENT_PRECLICK:
            store.mark_send_not_sent(attempt, f"followup failed before the click (exit "
                                              f"{_EXIT_NOT_SENT_PRECLICK}): {(send.get('stderr') or '').strip()[:300]}")
            return store_mod.FAILED
        if send.get("code") == 0:
            store.mark_accepted(attempt, conv)       # same thread, confirmed
            store.mark_waiting(rid)
            return _wait_phase(store, rid, conv, spec, run_cdp)
        if any(m.lower() in stderr for m in _NOT_SENT_BLOCK):
            store.set_state(rid, store_mod.BLOCKED, expect=store_mod.SENDING,
                            error_code=_first_marker(stderr, _NOT_SENT_BLOCK))
            return store_mod.BLOCKED
        if any(m.lower() in stderr for m in _NOT_SENT_RETRY):
            # Same evidence, same verdict as a submit's. These markers are emitted FAIL-CLOSED
            # before the click, so the follow-up provably did not send and re-queuing duplicates
            # nothing. Omitting this check here (the submit branch always had it) filed a proven
            # not-sent as UNCERTAIN, which is the costliest possible misfiling: it blocks the free
            # automatic retry, burns a full auto-retrieve and then a manual one hunting an answer
            # that was never asked for, and leaves a human staring at "may have sent" with only a
            # resend left to try — under exactly the uncertainty the invariant exists to prevent.
            return _requeue_or_block(store, rid, _first_marker(stderr, _NOT_SENT_RETRY))
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

    if sub.get("code") == _EXIT_NOT_SENT_PRECLICK:
        store.mark_send_not_sent(attempt, f"submit failed before the click (exit "
                                          f"{_EXIT_NOT_SENT_PRECLICK}): {(sub.get('stderr') or '').strip()[:300]}")
        return store_mod.FAILED

    if conv and sub.get("code") == 0:
        store.mark_accepted(attempt, conv)
        store.mark_waiting(rid)
        return _wait_phase(store, rid, conv, spec, run_cdp)

    # The click did not confirm. Distinguish PROVEN-not-sent from uncertain.
    if any(m.lower() in stderr for m in _NOT_SENT_BLOCK):
        store.set_state(rid, store_mod.BLOCKED, expect=store_mod.SENDING,
                        error_code=_first_marker(stderr, _NOT_SENT_BLOCK))
        return store_mod.BLOCKED
    if any(m.lower() in stderr for m in _NOT_SENT_RETRY):
        # provably not sent → safe to re-queue (this is NOT resending a possible send), bounded
        return _requeue_or_block(store, rid, _first_marker(stderr, _NOT_SENT_RETRY))
    # UNCERTAIN. A conversation reported by a FAILING submit is an address, not a confirmation (the
    # driver reports one whenever the post-click tab sits in a thread, including a reused tab it was
    # already in), so the state stays possibly_accepted — but recording it is what makes this round
    # retrievable read-only (recover()'s `retrievable` bucket, verified by its own END_RESPONSE
    # sentinel) instead of the dead end that forced a human to copy the answer out of the window by
    # hand. Linked ONLY here: on the proven-not-sent branches above the round is re-queued, and a
    # thread pinned from a send that never happened would follow it into its next attempt.
    if conv:
        store.link_conversation(rid, conv)
    store.mark_possibly_accepted(
        attempt,
        f"submit did not confirm the send (exit {sub.get('code')})"
        + (f"; conversation {conv} recorded for read-only retrieval" if conv else
           "; no conversation captured"))
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


def _no_live_worker(rid: str) -> bool:
    """Is nobody else working this round? The retrieve holds only its OWN rid lease, so the round it
    is about to resolve is unfenced: the daemon's one-shot auto-retrieve can be mid-wait on the very
    round a human-enqueued retrieve just recovered. Terminalising it under that worker turns its own
    `finish` into an IllegalTransition traceback — the data stays right, the log looks like
    corruption. Probing the lease is what makes the two paths mutually exclusive."""
    try:
        import cgc_spool as _spool
        return _spool.rid_lease_free(rid)
    except Exception:
        return False   # cannot prove it is free → do not touch it


def _wait_phase(store, rid, conv, spec, run_cdp, wait_rid=None, *, is_retrieve=False) -> str:
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
    res = run_cdp("wait", rid=wait_rid or rid, conversation=conv, out=out_tmp,
                  poll=spec.get("poll"), timeout=spec.get("timeout"))
    code = res.get("code")
    answer_path = res.get("out") or out_tmp
    answer = _read_answer(answer_path)
    if code == 0 and answer.strip():
        unverified = os.path.exists(answer_path + ".raw")
        store.finish(rid, store_mod.COMPLETED_UNVERIFIED if unverified else store_mod.COMPLETED_VERIFIED,
                     result_text=answer)
        if (is_retrieve and wait_rid and wait_rid != rid and not unverified
                and _no_live_worker(wait_rid)):
            # The waiter matched END_RESPONSE:<wait_rid> on the thread — proof that the SOURCE
            # round's send landed and that this is its answer. Close the source with it; leaving it
            # uncertain with its answer already in hand is a lie the next reader has to re-litigate
            # (and, since uncertainty holds the tab sweep, one that keeps costing). Only a verified
            # sentinel may do this: an unwrapped salvage cannot prove which turn it came from.
            if store.adopt_retrieved_answer(wait_rid, answer, conversation_id=conv):
                sys.stderr.write(f"CGC_RECONCILED {wait_rid}: resolved from retrieve {rid} "
                                 "(sentinel-verified on the thread).\n")
        return store_mod.COMPLETED_UNVERIFIED if unverified else store_mod.COMPLETED_VERIFIED
    if code == 3:
        store.set_state(rid, store_mod.BLOCKED, error_code=(res.get("stderr") or "blocker")[:200])
        return store_mod.BLOCKED
    if is_retrieve:
        # retrieve never sends anything, so a wait that cannot confirm/attribute the SOURCE turn's
        # answer (rid_absent — the turn was never sent here; rid_superseded/ambiguous — the thread
        # has since advanced past it and no sentinel-wrapped answer anchors it) is a plain, retryable
        # FAILURE — not "possibly accepted", which would wrongly imply a click that might need
        # reconciling. cdp_consult's CGC_ERROR line (if any) carries the actionable detail, including
        # 'observed_rid=' — the rid it actually found instead, for the envelope.
        detail = _stderr_error_line(res.get("stderr") or "")
        store.finish(rid, store_mod.FAILED,
                    error_code=detail or f"retrieve could not confirm source_rid={wait_rid or rid} "
                                         f"(exit {code})")
        return store_mod.FAILED
    # A wait that returns nothing usable is not a clean failure: the answer may still exist in the
    # conversation. Leave it uncertain so it is retrieved, not resent — but do NOT overwrite WHY the
    # round became uncertain in the first place. This fallback is also where the one-shot
    # auto-retrieve of an already-uncertain round lands, and stamping "accepted but wait produced no
    # answer" there asserted a confirmation that never happened, erasing the real disposition (a
    # proven-not-sent `model_not_selectable`, in the observed case) that a human needs to judge
    # whether a resend would duplicate anything.
    prior_row = store.get_round(rid)
    prior = prior_row["error_code"] if prior_row else None
    store.set_state(rid, store_mod.POSSIBLY_ACCEPTED,
                    error_code=(f"wait produced no answer (exit {code}) — retrieve, don't resend"
                                + (f"; original disposition: {prior}" if prior else "")))
    return store_mod.POSSIBLY_ACCEPTED


def _first_marker(stderr: str, markers) -> str:
    for m in markers:
        if m.lower() in stderr:
            return m
    return "not_sent"
