# Tests for the store-backed worker (skill/scripts/cgc_backend.py process_round + enqueue/await),
# phase 3b. The CDP driver is stubbed, so the FULL round lifecycle is exercised with no browser —
# the same technique the file-spool daemon tests use. The invariant under test end-to-end: only a
# confirmed conversation completes; every ambiguity leaves the round uncertain, never resent.
import importlib.util
import json
import os
import types

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load(name, db_path):
    import sys
    os.environ["CGC_STORE_DB"] = str(db_path)
    os.environ["CGC_STATE_DIR"] = os.path.dirname(str(db_path))
    sys.path.insert(0, SCRIPTS)
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def env(tmp_path):
    store_mod = _load("cgc_store", tmp_path / "control.db")
    backend = _load("cgc_backend", tmp_path / "control.db")
    s = store_mod.Store(str(tmp_path / "control.db"))
    yield store_mod, backend, s, tmp_path
    s.close()


def _ready_round(store_mod, s, prompt="review https://github.com/acme/x/tree/deadbeef please"):
    s.create_round("REQ-20260721-000000-00000a", "submit", out_path="/tmp/a.txt", prompt=prompt,
                   spec_json=json.dumps({"project_url": "https://chatgpt.com/", "model": "Pro"}))
    s.set_state("REQ-20260721-000000-00000a", store_mod.READY)
    return s.get_round("REQ-20260721-000000-00000a")


_OK_GATE = lambda p: (True, "ok")


def test_happy_path_completes_verified(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)

    def cdp(kind, **kw):
        if kind == "submit":
            return {"code": 0, "conversation": "conv-9", "stderr": ""}
        # wait: write the answer file, return 0
        with open(kw["out"], "w") as f:
            f.write("BEGIN\nthe answer\nEND")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.COMPLETED_VERIFIED
    rr = s.get_round("REQ-20260721-000000-00000a")
    assert rr["state"] == store_mod.COMPLETED_VERIFIED
    assert "the answer" in rr["result_text"]


def test_gate_rejection_never_sends(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    sent = []
    final = backend.process_round(s, r, lambda *a, **k: sent.append(k) or {"code": 1},
                                  daemon_instance_id="d1", validate=lambda p: (False, "refused: private repo"))
    assert final == store_mod.GATE_REJECTED
    assert sent == [], "a gate rejection must not call the CDP driver at all"
    assert s.get_round(r["rid"])["state"] == store_mod.GATE_REJECTED


def test_transient_gate_failure_requeues_not_terminal(env):
    """A `refused:` gate reason is authoritative and terminal, but an `unverified:` reason is a
    TRANSIENT failure (gh missing/timed out — could not CONFIRM public, not proven private). It must
    requeue, not terminally reject, so a temporary gh outage does not permanently strand a legit
    consult. The round is `ready` (pre-send) so nothing has left."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    sent = []
    final = backend.process_round(s, r, lambda *a, **k: sent.append(k) or {"code": 1},
                                  daemon_instance_id="d1",
                                  validate=lambda p: (False, "unverified: gh timed out after 8s"))
    assert final == store_mod.QUEUED
    assert sent == [], "a gate that could not confirm must not send"
    assert r["rid"] in s.recover()["dispatchable"], "requeued → safe to retry once gh recovers"


def test_auto_retrieve_recovers_possibly_accepted_with_conversation(env):
    """A possibly_accepted round with a known conversation is re-attached READ-ONLY exactly once: the
    waiter's rid-sentinel check means it completes only if THIS round's own answer is on the thread,
    and it never re-sends. This automates the manual `wait --rid --conversation` recovery."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    aid = s.begin_send(r["rid"], "b", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid, "conv-live")
    s.mark_waiting(r["rid"])
    s.set_state(r["rid"], store_mod.POSSIBLY_ACCEPTED, error_code="wait died — retrieve, don't resend")
    # recover() offers it as retrievable (has conv, not yet auto-retrieved).
    assert r["rid"] in s.recover()["retrievable"]
    calls = []

    def cdp(kind, **kw):
        calls.append(kind)
        with open(kw["out"], "w") as f:
            f.write("the answer that was already generated")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    final = backend.resume_round(s, s.get_round(r["rid"]), cdp)
    assert final == store_mod.COMPLETED_VERIFIED
    assert calls == ["wait"], "auto-retrieve must ONLY wait — never re-send"


def test_auto_retrieve_is_bounded_to_one_attempt(env):
    """A send that never landed has no matching sentinel, so the read-only wait finds nothing and the
    round returns to possibly_accepted. It must NOT be offered for auto-retrieve again — else it would
    re-attach every loop, burning a full wait budget each time. Bounded by the auto_retrieve event."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    aid = s.begin_send(r["rid"], "b", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid, "conv-live")
    s.mark_waiting(r["rid"])
    s.set_state(r["rid"], store_mod.POSSIBLY_ACCEPTED, error_code="wait died")

    def cdp_no_answer(kind, **kw):
        return {"code": 4, "out": kw["out"], "stderr": "no matching sentinel"}

    final = backend.resume_round(s, s.get_round(r["rid"]), cdp_no_answer)
    assert final == store_mod.POSSIBLY_ACCEPTED       # sentinel absent → back to uncertain
    assert s.was_auto_retrieved(r["rid"])
    assert r["rid"] not in s.recover()["retrievable"], "must not auto-retrieve a second time"
    assert r["rid"] in s.recover()["uncertain"], "still needs a human after the one bounded attempt"


def test_login_blocker_is_blocked_not_uncertain(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)

    def cdp(kind, **kw):
        return {"code": 3, "conversation": "", "stderr": "CGC_ERROR login_needed: log into ChatGPT"}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.BLOCKED
    assert r["rid"] not in s.recover()["uncertain"], "a proven-not-sent login is not uncertain"


def test_model_not_selectable_requeues(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)

    def cdp(kind, **kw):
        return {"code": 3, "conversation": "", "stderr": "CGC_ERROR model_not_selectable: no Pro tier"}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.QUEUED  # provably not sent → safe to retry
    assert r["rid"] in s.recover()["dispatchable"]


def test_unknown_send_is_possibly_accepted(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)

    def cdp(kind, **kw):
        # clicked, but no conversation observed and no proven-not-sent marker
        return {"code": 1, "conversation": "", "stderr": "unknown_send: RID never echoed"}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.POSSIBLY_ACCEPTED
    rec = s.recover()
    assert r["rid"] in rec["uncertain"] and r["rid"] not in rec["dispatchable"]


def test_accepted_but_wait_empty_is_uncertain_not_failed(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)

    def cdp(kind, **kw):
        if kind == "submit":
            return {"code": 0, "conversation": "conv-9", "stderr": ""}
        return {"code": 4, "out": kw["out"], "stderr": "no answer"}   # accepted but wait got nothing

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    # the send WAS accepted → the answer may exist in the conversation; retrieve, don't resend
    assert final == store_mod.POSSIBLY_ACCEPTED
    assert r["rid"] in s.recover()["uncertain"]


def test_salvage_marks_unverified(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)

    def cdp(kind, **kw):
        if kind == "submit":
            return {"code": 0, "conversation": "conv-9", "stderr": ""}
        with open(kw["out"], "w") as f:
            f.write("salvaged answer")
        with open(kw["out"] + ".raw", "w") as f:   # the .raw sibling marks an unverified salvage
            f.write("salvaged answer")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.COMPLETED_UNVERIFIED


def test_followup_continues_same_conversation_not_new_submit(env):
    """Regression: the store worker submitted for EVERY kind, so a followup opened a NEW conversation
    and lost the thread. A followup must attach to its conversation via the followup path, never
    submit."""
    store_mod, backend, s, tmp = env
    s.create_round("REQ-20260721-000000-0000ff", "followup", out_path=str(tmp / "f.txt"),
                   prompt="continuing this consult; the next question",
                   spec_json=json.dumps({"conversation": "conv-existing"}))
    s.set_state("REQ-20260721-000000-0000ff", store_mod.READY)
    calls = []

    def cdp(kind, **kw):
        calls.append((kind, kw.get("conversation")))
        if kind == "wait":
            with open(kw["out"], "w") as f:
                f.write("the follow-up answer on the same thread")
        return {"code": 0, "out": kw.get("out"), "stderr": ""}

    final = backend.process_round(s, s.get_round("REQ-20260721-000000-0000ff"), cdp,
                                  daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.COMPLETED_VERIFIED
    # send via followup (attach to the existing conversation), THEN a separate wait — so the round
    # reaches `waiting` and is reattach-able, never a 25-min `sending`.
    assert calls == [("followup", "conv-existing"), ("wait", "conv-existing")], \
        "followup MUST attach to the existing conversation (never submit) and split send from wait"
    assert s.get_round("REQ-20260721-000000-0000ff")["thread_id"] == "conv-existing"


def test_followup_without_conversation_fails_not_new_thread(env):
    store_mod, backend, s, tmp = env
    s.create_round("REQ-20260721-000000-0000fe", "followup", out_path=str(tmp / "f.txt"),
                   prompt="continuing this consult", spec_json=json.dumps({"conversation": "auto"}))
    s.set_state("REQ-20260721-000000-0000fe", store_mod.READY)
    final = backend.process_round(s, s.get_round("REQ-20260721-000000-0000fe"),
                                  lambda *a, **k: pytest.fail("must not send"),
                                  daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.FAILED  # no conversation → fail, never open a new thread


def test_retrieve_attaches_without_sending(env):
    store_mod, backend, s, tmp = env
    s.create_round("REQ-20260721-000000-00000r", "retrieve", out_path="/tmp/r.txt",
                   spec_json=json.dumps({"conversation": "conv-existing"}))
    s.set_state("REQ-20260721-000000-00000r", store_mod.READY)
    r = s.get_round("REQ-20260721-000000-00000r")
    calls = []

    def cdp(kind, **kw):
        calls.append(kind)
        with open(kw["out"], "w") as f:
            f.write("the retrieved answer")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.COMPLETED_VERIFIED
    assert calls == ["wait"], "retrieve must only wait — it must never submit/send"


def test_resume_reattaches_and_never_resends(env):
    store_mod, backend, s, tmp = env
    # a round that was accepted+waiting when its worker died
    r = _ready_round(store_mod, s)
    aid = s.begin_send(r["rid"], "b", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid, "conv-live"); s.mark_waiting(r["rid"])
    calls = []

    def cdp(kind, **kw):
        calls.append(kind)
        with open(kw["out"], "w") as f:
            f.write("resumed answer")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    final = backend.resume_round(s, s.get_round(r["rid"]), cdp)
    assert final == store_mod.COMPLETED_VERIFIED
    assert calls == ["wait"], "resume must ONLY wait — never submit/resend"


def test_resume_without_conversation_is_uncertain(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    # force accepted state with NO conversation recorded (thread-less)
    aid = s.begin_send(r["rid"], "b", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid)  # no conversation
    final = backend.resume_round(s, s.get_round(r["rid"]), lambda *a, **k: pytest.fail("must not send"))
    assert final == store_mod.POSSIBLY_ACCEPTED


def test_enqueue_and_await_roundtrip(env, monkeypatch):
    store_mod, backend, s, tmp = env
    # enqueue via the CLI-facing helper
    a = types.SimpleNamespace(rid="REQ-20260721-000000-00000b", kind="submit",
                              project_url="https://chatgpt.com/", model="Pro", conversation=None,
                              poll=1, timeout=1)
    out = str(tmp / "ans.txt")
    assert backend.enqueue_round(a, "references no code; a maths question", out) == 0
    assert s.get_round("REQ-20260721-000000-00000b")["state"] == store_mod.QUEUED
    # simulate the worker completing it
    s.set_state("REQ-20260721-000000-00000b", store_mod.READY)
    aid = s.begin_send("REQ-20260721-000000-00000b", "x", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid, "conv-b"); s.mark_waiting("REQ-20260721-000000-00000b")
    s.finish("REQ-20260721-000000-00000b", store_mod.COMPLETED_VERIFIED, result_text="here is the answer")
    # await materializes it
    aw = types.SimpleNamespace(rid="REQ-20260721-000000-00000b", out=out, timeout=2, poll=1)
    assert backend.await_round(aw) == 0
    assert open(out).read() == "here is the answer"
