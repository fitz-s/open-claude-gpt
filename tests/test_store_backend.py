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


def test_attach_failure_is_provably_not_sent_requeues(env):
    """A pre-click browser failure — the debug Chrome opened a tab that never answered Runtime.enable
    (cdp_attach_failed), or a new tab could not be created — happens BEFORE the click, so it proves
    this attempt did not send. It must requeue (retry may immediately work), not strand as uncertain.
    Safe because _run scopes stderr to the current invocation, so the marker is this attempt's own."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)

    def cdp(kind, **kw):
        return {"code": 1, "conversation": "",
                "stderr": "CGC_ERROR cdp_attach_failed: opened a tab but it never answered Runtime.enable"}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.QUEUED
    rec = s.recover()
    assert r["rid"] in rec["dispatchable"] and r["rid"] not in rec["uncertain"]


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


def _complete_a_consult(store_mod, s, rid="REQ-20260721-000000-000caf", conv="conv-active"):
    s.create_round(rid, "submit")
    s.set_state(rid, store_mod.READY)
    aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid, conv); s.mark_waiting(rid)
    s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="the first answer")
    return conv


def test_followup_auto_resolves_to_the_last_completed_thread(env):
    """The zero-bookkeeping follow-up: `--conversation auto` on a follow-up is resolved AT ENQUEUE to
    the last completed consult's conversation, so the agent never has to track and pass the id. This
    is what was broken (store had no 'active thread' notion → auto follow-ups failed → agents opened
    fresh conversations instead of continuing the thread)."""
    store_mod, backend, s, tmp = env
    conv = _complete_a_consult(store_mod, s)
    a = types.SimpleNamespace(rid="REQ-20260721-000000-000cf1", kind="followup",
                              project_url="https://chatgpt.com/", model="Pro", conversation="auto",
                              poll=1, timeout=1)
    assert backend.enqueue_round(a, "continuing; the next question", str(tmp / "f.txt")) == 0
    r = s.get_round("REQ-20260721-000000-000cf1")
    assert json.loads(r["spec_json"])["conversation"] == conv, "auto pinned to the last thread"
    assert r["thread_id"] == conv


def test_followup_parent_resolves_causally_not_to_global_latest(env):
    """--parent <rid> pins the follow-up to THAT consult's conversation, even when a DIFFERENT consult
    completed more recently. Global 'last completed' would attach to the wrong thread under concurrent
    consults (diff-review S1); causal parent resolution does not."""
    store_mod, backend, s, tmp = env
    _complete_a_consult(store_mod, s, rid="REQ-20260721-000000-000AAA", conv="conv-A")
    _complete_a_consult(store_mod, s, rid="REQ-20260721-000000-000BBB", conv="conv-B")  # globally latest
    assert s.latest_conversation() == "conv-B"
    a = types.SimpleNamespace(rid="REQ-20260721-000000-000fp1", kind="followup",
                              parent="REQ-20260721-000000-000AAA", project_url="https://chatgpt.com/",
                              model="Pro", conversation="auto", poll=1, timeout=1)
    assert backend.enqueue_round(a, "continuing A specifically", str(tmp / "f.txt")) == 0
    r = s.get_round("REQ-20260721-000000-000fp1")
    assert json.loads(r["spec_json"])["conversation"] == "conv-A", "parent pins causally, not latest"
    assert r["thread_id"] == "conv-A"


def test_stale_raw_sidecar_does_not_downgrade_a_verified_answer(env):
    """A .raw sidecar left by a PRIOR wait (e.g. a timed-out first attempt) must not make a later
    clean sentinel answer complete as unverified — _wait_phase decides confidence from .raw existence,
    so it clears the slate before each wait (diff-review S2)."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    wait_tmp = os.path.join(store_mod.CGC_STATE_DIR, f"_wait_{r['rid']}.txt")
    os.makedirs(store_mod.CGC_STATE_DIR, exist_ok=True)
    with open(wait_tmp + ".raw", "w") as f:
        f.write("stale salvage from an earlier wait")

    def cdp(kind, **kw):
        if kind == "submit":
            return {"code": 0, "conversation": "conv-9", "stderr": ""}
        with open(kw["out"], "w") as f:
            f.write("BEGIN\nthe clean sentinel answer\nEND")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.COMPLETED_VERIFIED, "a stale .raw must not downgrade a verified answer"


def test_followup_auto_with_no_prior_thread_is_refused_at_enqueue(env):
    """Fail-closed: a follow-up that resolves to no thread is refused BEFORE a round is created — it
    never silently opens a fresh conversation and loses context."""
    store_mod, backend, s, tmp = env
    a = types.SimpleNamespace(rid="REQ-20260721-000000-000cf2", kind="followup",
                              project_url="https://chatgpt.com/", model="Pro", conversation="auto",
                              poll=1, timeout=1)
    assert backend.enqueue_round(a, "continuing", str(tmp / "f.txt")) == 2
    assert s.get_round("REQ-20260721-000000-000cf2") is None, "no doomed round created"


def test_latest_conversation_ignores_inflight_and_self(env):
    store_mod, backend, s, tmp = env
    # an in-flight (waiting) consult is NOT a resolvable thread — you cannot continue an unanswered one
    s.create_round("REQ-20260721-000000-00wait", "submit"); s.set_state("REQ-20260721-000000-00wait", store_mod.READY)
    waid = s.begin_send("REQ-20260721-000000-00wait", "p", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(waid, "conv-inflight"); s.mark_waiting("REQ-20260721-000000-00wait")
    assert s.latest_conversation() is None, "an unanswered thread is not offered"
    conv = _complete_a_consult(store_mod, s, rid="REQ-20260721-000000-00done", conv="conv-done")
    assert s.latest_conversation() == conv
    assert s.latest_conversation(exclude_rid="REQ-20260721-000000-00done") is None, "excludes itself"


def test_await_unverified_is_review_required_not_success(env, tmp_path):
    """completed_unverified (salvaged without the sentinel wrapper) is NOT automatic success: await
    materializes it for a human but returns review-required (3), never 0, and emits no auto-followup
    — an unwrapped salvage can carry the wrong round's answer (diff-review)."""
    store_mod, backend, s, tmp = env
    rid = "REQ-20260721-000000-00unv1"
    s.create_round(rid, "submit")
    s.set_state(rid, store_mod.READY)
    aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid, "conv-u"); s.mark_waiting(rid)
    s.finish(rid, store_mod.COMPLETED_UNVERIFIED, result_text="a salvaged, unwrapped answer")
    out = str(tmp / "u.txt")
    aw = types.SimpleNamespace(rid=rid, out=out, timeout=2, poll=1)
    assert backend.await_round(aw) == 3, "unverified must be review-required, never auto-success (0)"
    assert open(out).read() == "a salvaged, unwrapped answer", "still materialized for the human"


def test_await_verified_is_success(env, tmp_path):
    store_mod, backend, s, tmp = env
    rid = "REQ-20260721-000000-00ver1"
    s.create_round(rid, "submit"); s.set_state(rid, store_mod.READY)
    aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid, "conv-v"); s.mark_waiting(rid)
    s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="the verified answer")
    out = str(tmp / "v.txt")
    aw = types.SimpleNamespace(rid=rid, out=out, timeout=2, poll=1)
    assert backend.await_round(aw) == 0


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


# ---- Phase C: idempotency, cancel, strict followup, outcome envelope ---------

def _enq_ns(rid, prompt_file, **over):
    import argparse
    d = dict(rid=rid, prompt_file=prompt_file, kind="submit",
             project_url="https://chatgpt.com/", conversation="auto", parent=None,
             request_key=None, model="Pro", out=None, poll=1, timeout=5, quiet=False)
    d.update(over)
    return argparse.Namespace(**d)


class TestRequestKeyIdempotency:
    def _enqueue(self, backend, tmp_path, rid, key, body="review https://github.com/acme/w"):
        pf = tmp_path / f"{rid}.md"
        # the rendered prompt embeds its rid (sentinel instructions) — reproduce that
        pf.write_text(f"{body}\nwrap in BEGIN_RESPONSE:{rid}\n", encoding="utf-8")
        prompt = pf.read_text()
        return backend.enqueue_round(_enq_ns(rid, str(pf), request_key=key), prompt,
                                     str(tmp_path / f"a_{rid}.txt"))

    def test_same_key_same_content_returns_original_receipt(self, env, capsys):
        store_mod, backend, s, tmp_path = env
        assert self._enqueue(backend, tmp_path, "REQ-20260707-120000-0aa001", "k1") == 0
        rid1 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["rid"]
        # retry with a NEW rid (as a re-fire does) but the same key + logical content
        assert self._enqueue(backend, tmp_path, "REQ-20260707-120000-0aa002", "k1") == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["rid"] == rid1 and out.get("idempotent_repeat") is True
        assert s.get_round("REQ-20260707-120000-0aa002") is None, "no duplicate round created"

    def test_same_key_different_content_refuses(self, env, capsys):
        store_mod, backend, s, tmp_path = env
        assert self._enqueue(backend, tmp_path, "REQ-20260707-120000-0ab001", "k2") == 0
        capsys.readouterr()
        assert self._enqueue(backend, tmp_path, "REQ-20260707-120000-0ab002", "k2",
                             body="review https://github.com/acme/OTHER") == 2
        assert "request_key_conflict" in capsys.readouterr().err


class TestCancel:
    def test_cancel_queued_round(self, env, capsys):
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0ac001"
        s.create_round(rid, "submit", prompt="p")
        assert backend.cancel_round(rid) == 0
        r = s.get_round(rid)
        assert r["state"] == store_mod.FAILED
        assert r["error_code"].startswith("cancelled")
        # idempotent repeat
        assert backend.cancel_round(rid) == 0

    def test_cancel_after_send_fence_refuses(self, env, capsys):
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0ac002"
        s.create_round(rid, "submit", prompt="p")
        s.set_state(rid, store_mod.READY)
        s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d")
        assert backend.cancel_round(rid) == 2
        assert "not_cancellable" in capsys.readouterr().err

    def test_cancel_missing_round(self, env, capsys):
        store_mod, backend, s, tmp_path = env
        assert backend.cancel_round("REQ-20260707-120000-0ac003") == 2
        assert "no_such_round" in capsys.readouterr().err


class TestStrictFollowupResolution:
    def _complete(self, store_mod, s, rid, conv):
        s.create_round(rid, "submit", thread_id=conv, prompt="p")
        s.set_state(rid, store_mod.READY)
        aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d")
        s.mark_accepted(aid, conv)
        s.mark_waiting(rid)
        s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="ans")

    def test_single_completed_thread_resolves(self, env):
        store_mod, backend, s, tmp_path = env
        self._complete(store_mod, s, "REQ-20260707-120000-0ad001", "conv-A")
        conv, why = s.latest_conversation_strict()
        assert conv == "conv-A" and why is None

    def test_inflight_consult_makes_auto_ambiguous(self, env):
        store_mod, backend, s, tmp_path = env
        self._complete(store_mod, s, "REQ-20260707-120000-0ae001", "conv-A")
        s.create_round("REQ-20260707-120000-0ae002", "submit", prompt="p")
        s.set_state("REQ-20260707-120000-0ae002", store_mod.READY)
        s.begin_send("REQ-20260707-120000-0ae002", "p", "h" * 64, daemon_instance_id="d")
        conv, why = s.latest_conversation_strict()
        assert conv is None and "in flight" in why

    def test_two_recent_completions_on_different_threads_are_ambiguous(self, env):
        store_mod, backend, s, tmp_path = env
        self._complete(store_mod, s, "REQ-20260707-120000-0af001", "conv-A")
        self._complete(store_mod, s, "REQ-20260707-120000-0af002", "conv-B")
        conv, why = s.latest_conversation_strict()
        assert conv is None and "different" in why and "--parent" in why

    def test_old_second_thread_does_not_block_auto(self, env):
        store_mod, backend, s, tmp_path = env
        self._complete(store_mod, s, "REQ-20260707-120000-0ag001", "conv-A")
        self._complete(store_mod, s, "REQ-20260707-120000-0ag002", "conv-B")
        # age the first completion far beyond the ambiguity window
        s.db.execute("UPDATE rounds SET updated_at='2020-01-01T00:00:00+00:00' "
                     "WHERE rid='REQ-20260707-120000-0ag001'")
        conv, why = s.latest_conversation_strict()
        assert conv == "conv-B" and why is None


class TestOutcomeEnvelope:
    def test_await_verified_emits_schema_1_envelope(self, env, capsys):
        import argparse
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0ah001"
        s.create_round(rid, "submit", prompt="p")
        s.set_state(rid, store_mod.READY)
        aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d")
        s.mark_accepted(aid, "conv-E")
        s.mark_waiting(rid)
        s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="answer!")
        out = tmp_path / "a.txt"
        code = backend.await_round(argparse.Namespace(rid=rid, out=str(out), poll=0.01, timeout=2))
        assert code == 0
        env_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert env_json["schema"] == 1 and env_json["rid"] == rid
        assert env_json["state"] == "completed_verified" and env_json["confidence"] == "verified"
        assert env_json["answer_path"] == str(out)
        assert "fire --followup --parent" in env_json["next_command"]

    def test_await_failed_envelope_carries_retryability(self, env, capsys):
        import argparse
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0ah002"
        s.create_round(rid, "submit", prompt="p")
        s.set_state(rid, store_mod.READY)
        s.gate_reject(rid, "unverified: gh timed out")
        code = backend.await_round(argparse.Namespace(rid=rid, out=str(tmp_path / "a.txt"),
                                                      poll=0.01, timeout=2))
        assert code == 1
        env_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert env_json["retryable"] is True, "an unverified: gate failure is transient — re-fireable"
