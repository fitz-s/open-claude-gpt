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

    ok, reason = validate(prompt)
    if not ok:
        store.gate_reject(rid, reason)
        return store_mod.GATE_REJECTED

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
    """Reattach to a round that was already `accepted`/`waiting` when a worker died (daemon restart,
    closed tab) and resume polling its existing conversation — the store peer of the spool's orphan
    recovery. It NEVER re-sends; a round with no conversation cannot be resumed and is left uncertain
    for human reconciliation (never auto-resent)."""
    rid = r["rid"]
    spec = json.loads(r["spec_json"]) if r.get("spec_json") else {}
    tid = r.get("thread_id")
    conv = None
    if tid:
        row = store.db.execute("SELECT conversation_id FROM threads WHERE thread_id=?", (tid,)).fetchone()
        conv = row["conversation_id"] if row else None
    if not conv:
        # accepted but no conversation recorded → we cannot address it; do not resend, mark uncertain.
        if r["state"] != store_mod.POSSIBLY_ACCEPTED:
            store.set_state(rid, store_mod.POSSIBLY_ACCEPTED,
                            error_code="accepted but no conversation to reattach — retrieve manually")
        return store_mod.POSSIBLY_ACCEPTED
    if r["state"] == store_mod.ACCEPTED:
        store.mark_waiting(rid)
    return _wait_phase(store, rid, conv, spec, run_cdp)


def _wait_phase(store, rid, conv, spec, run_cdp) -> str:
    out_tmp = os.path.join(store_mod.CGC_STATE_DIR, f"_wait_{rid}.txt")
    res = run_cdp("wait", rid=rid, conversation=conv, out=out_tmp,
                  poll=spec.get("poll"), timeout=spec.get("timeout"))
    code = res.get("code")
    answer_path = res.get("out") or out_tmp
    answer = ""
    if os.path.exists(answer_path):
        try:
            with open(answer_path, encoding="utf-8") as f:
                answer = f.read()
        except OSError:
            answer = ""
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
