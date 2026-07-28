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


def test_preclick_exit_maps_submit_to_failed_not_sent(env):
    """cdp_consult's EXIT_NOT_SENT_PRECLICK (6) is cdp_consult.py's own proof that the pre-click paste
    boundary failed — including a crash — strictly before the send click. It must be trusted by exit
    CODE alone (a raw traceback carries no recognizable stderr marker), map straight to FAILED (not
    the uncertain possibly_accepted a bare crash used to produce), and stamp send_disposition so the
    round is release-eligible with no operator reconcile."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)

    def cdp(kind, **kw):
        return {"code": 6, "conversation": "",
                "stderr": "CGC_ERROR not_sent_preclick: TimeoutError during paste: timed out"}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.FAILED
    rr = s.get_round(r["rid"])
    assert rr["send_disposition"] == store_mod.NOT_SENT_PROVEN
    rec = s.recover()
    assert r["rid"] not in rec["uncertain"], "a pre-click-proven failure must never be uncertain"
    assert s.has_send_attempt(r["rid"]), "begin_send DID write an attempt row before the click"


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
                   spec_json=json.dumps({"conversation": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"}))
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
    assert calls == [("followup", "cccccccc-cccc-4ccc-8ccc-cccccccccccc"), ("wait", "cccccccc-cccc-4ccc-8ccc-cccccccccccc")], \
        "followup MUST attach to the existing conversation (never submit) and split send from wait"
    assert s.get_round("REQ-20260721-000000-0000ff")["thread_id"] == "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def test_preclick_exit_maps_followup_to_failed_not_sent(env):
    """Regression for the recorded incident: it happened on the FOLLOWUP path specifically (re-opening
    a heavy conversation server-side, then a paste whose CDP reply outran the read timeout), where the
    old code only checked _NOT_SENT_BLOCK and fell everything else into possibly_accepted. Exit 6 must
    map to FAILED + not-sent-proven here too, not just on submit."""
    store_mod, backend, s, tmp = env
    s.create_round("REQ-20260721-000000-0000fd", "followup", out_path=str(tmp / "f2.txt"),
                   prompt="continuing this consult", spec_json=json.dumps({"conversation": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"}))
    s.set_state("REQ-20260721-000000-0000fd", store_mod.READY)

    def cdp(kind, **kw):
        return {"code": 6, "stderr": "CGC_ERROR not_sent_preclick: composer holds 40 normalized "
                                     "chars, prompt needs >= 7600 — paste looks truncated"}

    final = backend.process_round(s, s.get_round("REQ-20260721-000000-0000fd"), cdp,
                                  daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.FAILED
    rr = s.get_round("REQ-20260721-000000-0000fd")
    assert rr["send_disposition"] == store_mod.NOT_SENT_PROVEN
    assert "REQ-20260721-000000-0000fd" not in s.recover()["uncertain"]


def test_followup_holds_conversation_lease_across_the_send(env):
    """The mutating region (begin_send + the follow-up CDP subprocess) runs UNDER the per-conversation
    exclusive lease, and the lease is released once the send returns (the read-only wait needs none).
    We prove both by probing the lease from inside the fake CDP driver and again after."""
    import cgc_spool
    store_mod, backend, s, tmp = env
    if cgc_spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    s.create_round("REQ-20260721-000000-0000f1", "followup", out_path=str(tmp / "f1.txt"),
                   prompt="continuing this consult", spec_json=json.dumps({"conversation": "dddddddd-dddd-4ddd-8ddd-dddddddddddd"}))
    s.set_state("REQ-20260721-000000-0000f1", store_mod.READY)
    probed = {}

    def cdp(kind, **kw):
        if kind == "followup":
            probed["held_during_send"] = cgc_spool.acquire_conversation_lease("dddddddd-dddd-4ddd-8ddd-dddddddddddd") is None
        if kind == "wait":
            fh = cgc_spool.acquire_conversation_lease("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
            probed["free_during_wait"] = fh is not None
            if fh is not None:
                fh.close()
            with open(kw["out"], "w") as f:
                f.write("the answer")
        return {"code": 0, "out": kw.get("out"), "stderr": ""}

    final = backend.process_round(s, s.get_round("REQ-20260721-000000-0000f1"), cdp,
                                  daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.COMPLETED_VERIFIED
    assert probed["held_during_send"] is True, "the conversation lease must be held across the send"
    assert probed["free_during_wait"] is True, "and released before the read-only wait"
    assert cgc_spool.acquire_conversation_lease("dddddddd-dddd-4ddd-8ddd-dddddddddddd") is not None, "released after process_round"


def test_second_same_conversation_followup_refuses_without_sending(env):
    """Two follow-ups pinned to the SAME conversation. With the lease held by a stand-in for the first
    worker, the second must raise ConversationLeaseRefused BEFORE any CDP send and BEFORE begin_send —
    the round stays READY, no attempt exists, no not_sent_proven, nothing to reconcile."""
    import cgc_spool
    store_mod, backend, s, tmp = env
    if cgc_spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    s.create_round("REQ-20260721-000000-0000f2", "followup", out_path=str(tmp / "f2.txt"),
                   prompt="continuing this consult", spec_json=json.dumps({"conversation": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"}))
    s.set_state("REQ-20260721-000000-0000f2", store_mod.READY)
    held = cgc_spool.acquire_conversation_lease("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")   # the first worker owns the composer
    assert held is not None
    try:
        with pytest.raises(backend.ConversationLeaseRefused):
            backend.process_round(s, s.get_round("REQ-20260721-000000-0000f2"),
                                  lambda *a, **k: pytest.fail("must not touch the composer"),
                                  daemon_instance_id="d1", validate=_OK_GATE)
    finally:
        held.close()
    rr = s.get_round("REQ-20260721-000000-0000f2")
    assert rr["state"] == store_mod.READY, "the refused round is untouched, still ready"
    assert rr["current_attempt_id"] is None, "no send attempt was ever begun"
    assert rr["send_disposition"] != store_mod.NOT_SENT_PROVEN
    assert "REQ-20260721-000000-0000f2" not in s.recover()["uncertain"]
    # once the first worker releases, the same round proceeds normally
    calls = []

    def cdp(kind, **kw):
        calls.append(kind)
        if kind == "wait":
            with open(kw["out"], "w") as f:
                f.write("the answer")
        return {"code": 0, "out": kw.get("out"), "stderr": ""}

    final = backend.process_round(s, s.get_round("REQ-20260721-000000-0000f2"), cdp,
                                  daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.COMPLETED_VERIFIED
    assert calls == ["followup", "wait"]


def test_different_conversation_followups_do_not_serialize(env):
    """A lease held on conversation A must NOT block a follow-up on conversation B — the fence is
    per-conversation, not global, so distinct threads proceed concurrently (no false serialization)."""
    import cgc_spool
    store_mod, backend, s, tmp = env
    if cgc_spool.fcntl is None:
        pytest.skip("no fcntl on this platform")
    s.create_round("REQ-20260721-000000-0000f3", "followup", out_path=str(tmp / "f3.txt"),
                   prompt="continuing this consult", spec_json=json.dumps({"conversation": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}))
    s.set_state("REQ-20260721-000000-0000f3", store_mod.READY)
    held_a = cgc_spool.acquire_conversation_lease("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")     # a busy UNRELATED thread
    assert held_a is not None
    try:
        def cdp(kind, **kw):
            if kind == "wait":
                with open(kw["out"], "w") as f:
                    f.write("the answer on B")
            return {"code": 0, "out": kw.get("out"), "stderr": ""}

        final = backend.process_round(s, s.get_round("REQ-20260721-000000-0000f3"), cdp,
                                      daemon_instance_id="d1", validate=_OK_GATE)
    finally:
        held_a.close()
    assert final == store_mod.COMPLETED_VERIFIED, "conv-B proceeds while conv-A is held"


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
    s.create_round("REQ-20260721-000000-00000r", "retrieve", out_path="/tmp/r.txt", parent_rid="REQ-20260721-000000-src001",
                   spec_json=json.dumps({"conversation": "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "parent_rid": "REQ-20260721-000000-src001"}))
    s.set_state("REQ-20260721-000000-00000r", store_mod.READY)
    r = s.get_round("REQ-20260721-000000-00000r")
    calls = []
    seen = {}

    def cdp(kind, **kw):
        calls.append(kind)
        seen[kind] = kw
        with open(kw["out"], "w") as f:
            f.write("the retrieved answer")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.COMPLETED_VERIFIED
    assert calls == ["wait"], "retrieve must only wait — it must never submit/send"
    assert seen["wait"]["rid"] == "REQ-20260721-000000-src001", \
        "the wait must be pinned to the SOURCE round's rid, never the retrieve round's own"


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
    _complete_a_consult(store_mod, s, rid="REQ-20260721-000000-000AAA", conv="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    _complete_a_consult(store_mod, s, rid="REQ-20260721-000000-000BBB", conv="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")  # globally latest
    assert s.latest_conversation() == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    a = types.SimpleNamespace(rid="REQ-20260721-000000-000fp1", kind="followup",
                              parent="REQ-20260721-000000-000AAA", project_url="https://chatgpt.com/",
                              model="Pro", conversation="auto", poll=1, timeout=1)
    assert backend.enqueue_round(a, "continuing A specifically", str(tmp / "f.txt")) == 0
    r = s.get_round("REQ-20260721-000000-000fp1")
    assert json.loads(r["spec_json"])["conversation"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "parent pins causally, not latest"
    assert r["thread_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


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
             request_key=None, logical_sha=None, model="Pro", out=None, poll=1, timeout=5, quiet=False)
    d.update(over)
    return argparse.Namespace(**d)


def _lsha(body):
    """A stand-in for prep's placeholder-rendered logical-sha: hash the content that distinguishes
    one logical request from another. Same body -> same identity; different body -> different."""
    import hashlib
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class TestRequestKeyIdempotency:
    def _enqueue(self, backend, tmp_path, rid, key, body="review https://github.com/acme/w"):
        pf = tmp_path / f"{rid}.md"
        # the rendered prompt embeds its rid (sentinel instructions) — reproduce that
        pf.write_text(f"{body}\nwrap in BEGIN_RESPONSE:{rid}\n", encoding="utf-8")
        prompt = pf.read_text()
        return backend.enqueue_round(
            _enq_ns(rid, str(pf), request_key=key, logical_sha=_lsha(body)), prompt,
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
        self._complete(store_mod, s, "REQ-20260707-120000-0ad001", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        conv, why = s.latest_conversation_strict()
        assert conv == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa" and why is None

    def test_inflight_consult_makes_auto_ambiguous(self, env):
        store_mod, backend, s, tmp_path = env
        self._complete(store_mod, s, "REQ-20260707-120000-0ae001", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        s.create_round("REQ-20260707-120000-0ae002", "submit", prompt="p")
        s.set_state("REQ-20260707-120000-0ae002", store_mod.READY)
        s.begin_send("REQ-20260707-120000-0ae002", "p", "h" * 64, daemon_instance_id="d")
        conv, why = s.latest_conversation_strict()
        assert conv is None and "in flight" in why

    def test_two_recent_completions_on_different_threads_are_ambiguous(self, env):
        store_mod, backend, s, tmp_path = env
        self._complete(store_mod, s, "REQ-20260707-120000-0af001", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self._complete(store_mod, s, "REQ-20260707-120000-0af002", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        conv, why = s.latest_conversation_strict()
        assert conv is None and "ambiguous" in why and "--parent" in why

    def test_old_second_thread_does_not_block_auto(self, env):
        store_mod, backend, s, tmp_path = env
        self._complete(store_mod, s, "REQ-20260707-120000-0ag001", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self._complete(store_mod, s, "REQ-20260707-120000-0ag002", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        # age the first completion far beyond the ambiguity window
        s.db.execute("UPDATE rounds SET updated_at='2020-01-01T00:00:00+00:00' "
                     "WHERE rid='REQ-20260707-120000-0ag001'")
        conv, why = s.latest_conversation_strict()
        assert conv == "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb" and why is None


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


class TestStats:
    def test_stats_reports_rates_and_latency(self, env, capsys):
        store_mod, backend, s, tmp_path = env
        for i, final in enumerate([store_mod.COMPLETED_VERIFIED, store_mod.COMPLETED_VERIFIED,
                                   store_mod.COMPLETED_UNVERIFIED]):
            rid = f"REQ-20260707-120000-0st{i:03d}"
            s.create_round(rid, "submit", prompt="p")
            s.set_state(rid, store_mod.READY)
            aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d")
            s.mark_accepted(aid, f"conv-{i}")
            s.mark_waiting(rid)
            s.finish(rid, final, result_text="a")
        assert backend.stats_report() == 0
        rep = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rep["completed"] == 3
        assert abs(rep["unverified_rate"] - 1 / 3) < 0.01
        assert rep["latency_s"]["n"] == 3


class TestRequestFingerprint:
    """The idempotency identity covers the LOGICAL request — every caller-side routing field —
    not just the prompt bytes. Same key + different routing must conflict, never silently return
    the old receipt (an answer for the wrong causal request is worse than an error)."""

    def _enq(self, backend, tmp_path, rid, key, **over):
        pf = tmp_path / f"{rid}.md"
        pf.write_text(f"review https://github.com/acme/w\nwrap in BEGIN_RESPONSE:{rid}\n",
                      encoding="utf-8")
        # fixed body -> a constant logical-sha, so only the ROUTING fields under test drive conflicts
        over.setdefault("logical_sha", _lsha("review https://github.com/acme/w"))
        return backend.enqueue_round(_enq_ns(rid, str(pf), request_key=key, **over),
                                     pf.read_text(), str(tmp_path / f"a_{rid}.txt"))

    @pytest.mark.parametrize("field", [
        {"model": "5.6-Thinking"},
        {"project_url": "https://chatgpt.com/g/other/project"},
        {"parent": "REQ-20260707-110000-aaaaaa"},
        {"conversation": "ffffffff-ffff-4fff-8fff-ffffffffffff"},
    ])
    def test_same_key_different_routing_field_conflicts(self, env, capsys, field):
        store_mod, backend, s, tmp_path = env
        if "parent" in field or "conversation" in field:
            # give the parent/conversation something to resolve to so enqueue reaches the
            # fingerprint comparison rather than failing resolution first
            s.create_round(field.get("parent") or "REQ-20260707-110000-aaaaaa", "submit",
                           thread_id="conv-old", prompt="p")
            s.db.execute("UPDATE threads SET conversation_id='conv-old' WHERE thread_id='conv-old'")
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0f0001", "kf1") == 0
        capsys.readouterr()
        over = dict(field)
        if "parent" in field or "conversation" in field:
            over["kind"] = "followup"
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0f0002", "kf1", **over) == 2
        assert "request_key_conflict" in capsys.readouterr().err

    def test_literal_rid_in_task_text_is_not_normalized_away(self, env, capsys):
        """A rid QUOTED in caller text (e.g. a followup referencing an earlier consult) is user
        data. With --logical-sha (placeholder render) the fingerprint never rewrites it; two fires
        whose only difference is that quoted rid must NOT collide as 'same content'."""
        store_mod, backend, s, tmp_path = env

        def enq(rid, quoted, logical):
            pf = tmp_path / f"{rid}.md"
            pf.write_text(f"re-check {quoted} please\nBEGIN_RESPONSE:{rid}\n", encoding="utf-8")
            return backend.enqueue_round(
                _enq_ns(rid, str(pf), request_key="kf2", logical_sha=logical),
                pf.read_text(), str(tmp_path / f"a_{rid}.txt"))

        # logical_sha models prep's placeholder render: it hashes the quoted rid VERBATIM
        assert enq("REQ-20260707-120000-0f1001", "REQ-20260707-000000-aaaaaa", "lsha-A") == 0
        capsys.readouterr()
        assert enq("REQ-20260707-120000-0f1002", "REQ-20260707-000000-bbbbbb", "lsha-B") == 2
        assert "request_key_conflict" in capsys.readouterr().err

    def test_concurrent_same_key_resolves_deterministically(self, env, capsys, monkeypatch):
        """Two enqueues race the same key: both see no prior row, one insert wins the unique index,
        the loser must return the WINNER's receipt (or a conflict) — never a raw constraint error."""
        store_mod, backend, s, tmp_path = env
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0f2001", "kf3") == 0
        rid1 = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["rid"]
        # simulate the race: the pre-check misses the row that is already there
        real = store_mod.Store.round_by_request_key
        calls = {"n": 0}

        def racy(self, key):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(self, key)

        monkeypatch.setattr(store_mod.Store, "round_by_request_key", racy)
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0f2002", "kf3") == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["rid"] == rid1 and out.get("idempotent_repeat") is True
        assert calls["n"] >= 2, "the IntegrityError path must re-resolve the winner"


class TestStoreIdentityFence:
    def test_enqueue_refuses_a_daemon_on_a_different_store(self, env, capsys, monkeypatch):
        """A live daemon whose heartbeat names a different db/uuid (old code, or a relocated-away
        DB — the recorded live split-brain) must refuse the enqueue in seconds, with the fix."""
        store_mod, backend, s, tmp_path = env
        import cgc_spool
        monkeypatch.setattr(cgc_spool, "daemon_identity",
                            lambda: {"pid": os.getpid(), "ts": 9e12,
                                     "db_path": "/somewhere/else.db", "store_uuid": "not-mine"})
        pf = tmp_path / "p.md"
        pf.write_text("review https://github.com/acme/w\n", encoding="utf-8")
        rc = backend.enqueue_round(_enq_ns("REQ-20260707-120000-0f3001", str(pf)),
                                   pf.read_text(), str(tmp_path / "a.txt"))
        assert rc == 2
        err = capsys.readouterr().err
        assert "store_mismatch" in err and "kickstart" in err

    def test_enqueue_accepts_a_matching_daemon(self, env, capsys, monkeypatch):
        store_mod, backend, s, tmp_path = env
        import cgc_spool
        monkeypatch.setattr(cgc_spool, "daemon_identity",
                            lambda: {"pid": os.getpid(), "ts": 9e12,
                                     "db_path": os.path.abspath(store_mod.db_path()),
                                     "store_uuid": s.store_uuid()})
        pf = tmp_path / "p.md"
        pf.write_text("review https://github.com/acme/w\n", encoding="utf-8")
        rc = backend.enqueue_round(_enq_ns("REQ-20260707-120000-0f3002", str(pf)),
                                   pf.read_text(), str(tmp_path / "a.txt"))
        assert rc == 0


class TestEnvelopeContract:
    def test_await_emits_an_envelope_even_when_the_store_cannot_open(self, env, capsys, monkeypatch):
        """'Every await exit has an envelope' includes the exits BEFORE a round outcome: a store
        that cannot open (SchemaTooNew / corrupt file) must still terminate with one JSON object on
        stdout, not a bare traceback."""
        store_mod, backend, s, tmp_path = env

        def boom(*a, **kw):
            raise store_mod.SchemaTooNew("store schema is v9")

        monkeypatch.setattr(backend.store_mod, "Store", boom)
        a = types.SimpleNamespace(rid="REQ-20260707-120000-0f4001",
                                  out=str(tmp_path / "a.txt"), timeout=5, poll=1)
        assert backend.await_round(a) == 1
        out = capsys.readouterr().out.strip().splitlines()[-1]
        env_obj = json.loads(out)
        assert env_obj["schema"] == 1 and env_obj["state"] == "error"
        assert "SchemaTooNew" in env_obj["error"]

    def test_retryable_always_names_the_retry(self, env, capsys):
        """Envelope invariant: retryable=true must carry next_command or human_action — a verdict
        with no move is not actionable."""
        store_mod, backend, s, tmp_path = env
        backend._emit("REQ-20260707-120000-0f5001", "failed", retryable=True)
        out = json.loads(capsys.readouterr().out.strip())
        assert out["retryable"] is True
        assert out["human_action"] or out["next_command"]


class TestStrictFollowupEdges:
    def _complete(self, store_mod, s, rid, conv):
        s.create_round(rid, "submit", thread_id=conv, prompt="p")
        s.db.execute("UPDATE threads SET conversation_id=? WHERE thread_id=?", (conv, conv))
        s.set_state(rid, store_mod.READY)
        s.begin_send(rid, "p", store_mod.sha256("p"), daemon_instance_id="d")
        s.mark_accepted(s.get_round(rid)["current_attempt_id"], conv)
        s.mark_waiting(rid)
        s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="x")

    def test_queued_round_makes_bare_auto_ambiguous(self, env):
        """A consult WAITING TO START is as much a competing target as one mid-send — the
        in-flight refusal set must include queued/ready."""
        store_mod, backend, s, tmp_path = env
        self._complete(store_mod, s, "REQ-20260707-120000-0f6001", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        s.create_round("REQ-20260707-120000-0f6002", "submit", prompt="p")  # queued, not started
        conv, why = s.latest_conversation_strict()
        assert conv is None and "in flight" in why

    def test_busy_thread_cannot_hide_a_near_simultaneous_other_thread(self, env):
        """Two recent rounds on thread A must not mask that thread B ALSO completed moments ago —
        the ambiguity comparison is per-THREAD (max completion per distinct conversation)."""
        store_mod, backend, s, tmp_path = env
        self._complete(store_mod, s, "REQ-20260707-120000-0f7001", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        self._complete(store_mod, s, "REQ-20260707-120000-0f7002", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        self._complete(store_mod, s, "REQ-20260707-120000-0f7003", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        conv, why = s.latest_conversation_strict()
        assert conv is None and "ambiguous" in why


class TestRetrieveRidSemantics:
    def test_retrieve_pins_the_source_rid_not_its_own(self, env):
        """A retrieve round's own rid is fresh by construction and means nothing to the page — it
        must pin the SOURCE round's rid (its --parent at enqueue, carried in spec/parent_rid) as
        the wait rid: never its own fresh rid, and never 'auto' (which silently adopts whatever the
        conversation's LATEST turn happens to be — the causal-substitution bug this fixes)."""
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0f8001"
        source_rid = "REQ-20260707-115000-0f7000"
        s.create_round(rid, "retrieve", thread_id="conv-r", prompt=None, parent_rid=source_rid,
                       spec_json=json.dumps({"conversation": "conv-r", "parent_rid": source_rid}))
        s.db.execute("UPDATE threads SET conversation_id='conv-r' WHERE thread_id='conv-r'")
        s.set_state(rid, store_mod.READY)
        seen = {}

        def cdp(kind, **kw):
            seen[kind] = kw
            with open(kw["out"], "w") as f:
                f.write("the earlier round's answer")
            return {"code": 0, "out": kw["out"], "stderr": ""}

        final = backend.process_round(s, s.get_round(rid), cdp,
                                      daemon_instance_id="d1", validate=lambda p: (True, "ok"))
        assert final == store_mod.COMPLETED_VERIFIED
        assert seen["wait"]["rid"] == source_rid, \
            "retrieve must pin the SOURCE round's rid, never its own fresh rid or 'auto'"

    def test_retrieve_without_a_source_rid_fails_closed(self, env):
        """Belt-and-suspenders: even if a retrieve round somehow reaches process_round with no
        source_rid (spec/parent_rid both empty — enqueue_round refuses this earlier), the worker
        itself must still refuse rather than falling back to an unpinned/auto wait."""
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0f8002"
        s.create_round(rid, "retrieve", thread_id="conv-nosrc",
                       spec_json=json.dumps({"conversation": "conv-nosrc"}))
        s.db.execute("UPDATE threads SET conversation_id='conv-nosrc' WHERE thread_id='conv-nosrc'")
        s.set_state(rid, store_mod.READY)
        final = backend.process_round(s, s.get_round(rid), lambda *a, **k: pytest.fail("must not wait"),
                                      daemon_instance_id="d1", validate=lambda p: (True, "ok"))
        assert final == store_mod.FAILED
        assert "source_rid" in s.get_round(rid)["error_code"]

    def test_retrieve_never_adopts_a_later_same_thread_answer(self, env):
        """R1 (source) then R2 are both sent to the same conversation before/during recovery. A
        retrieve created for R1 must never complete with R2's answer. The fake CDP layer here
        stands in for cdp_consult's rid-scoped turn matching (cdp_consult._locate_source_turn is
        unit-tested directly, CDP-free, in TestSourceTurnMatching below): it reports the same
        refusal a real superseded/absent turn-match would — no usable answer, a CGC_ERROR line
        naming the mismatch. process_round must turn that into a plain FAILED, never a success
        carrying R2's content."""
        store_mod, backend, s, tmp_path = env
        source_rid = "REQ-20260707-120000-0aaa01"    # R1 — the uncertain round being recovered
        later_rid = "REQ-20260707-120500-0bbb01"     # R2 — sent to the same conversation afterward
        retrieve_rid = "REQ-20260707-121000-0ccc01"
        s.create_round(retrieve_rid, "retrieve", thread_id="conv-adv", parent_rid=source_rid,
                       spec_json=json.dumps({"conversation": "conv-adv", "parent_rid": source_rid}))
        s.db.execute("UPDATE threads SET conversation_id='conv-adv' WHERE thread_id='conv-adv'")
        s.set_state(retrieve_rid, store_mod.READY)

        def cdp(kind, **kw):
            assert kind == "wait"
            assert kw["rid"] == source_rid, "must ask cdp_consult to verify R1, never R2 or 'auto'"
            # No answer written to --out — cdp_consult refused to attribute one, exactly like a
            # live rid_superseded/rid_absent exit.
            return {"code": 5, "out": kw["out"],
                    "stderr": f"CGC_ERROR rid_superseded: source_rid={source_rid} is not the "
                             f"conversation's latest turn observed_rid={later_rid} and no "
                             "sentinel-wrapped answer for it was found — refusing to attribute "
                             "the latest message to it."}

        final = backend.process_round(s, s.get_round(retrieve_rid), cdp,
                                      daemon_instance_id="d1", validate=lambda p: (True, "ok"))
        assert final == store_mod.FAILED, "never a success carrying a later round's answer"
        r = s.get_round(retrieve_rid)
        assert not r["result_text"], "R2's answer must never be persisted as this retrieve's result"
        assert "rid_superseded" in r["error_code"]
        assert f"observed_rid={later_rid}" in r["error_code"]

    def test_retrieve_returns_the_source_answer_when_attributable(self, env):
        """The counterpart to the refusal above: when cdp_consult CAN attribute an answer to R1
        (its own sentinel-wrapped extract, regardless of R2 also existing on the thread), the
        retrieve still completes normally with R1's answer."""
        store_mod, backend, s, tmp_path = env
        source_rid = "REQ-20260707-120000-0aaa02"
        retrieve_rid = "REQ-20260707-121000-0ccc02"
        s.create_round(retrieve_rid, "retrieve", thread_id="conv-ok", parent_rid=source_rid,
                       spec_json=json.dumps({"conversation": "conv-ok", "parent_rid": source_rid}))
        s.db.execute("UPDATE threads SET conversation_id='conv-ok' WHERE thread_id='conv-ok'")
        s.set_state(retrieve_rid, store_mod.READY)

        def cdp(kind, **kw):
            assert kw["rid"] == source_rid
            with open(kw["out"], "w") as f:
                f.write("R1's own sentinel-wrapped answer")
            return {"code": 0, "out": kw["out"], "stderr": ""}

        final = backend.process_round(s, s.get_round(retrieve_rid), cdp,
                                      daemon_instance_id="d1", validate=lambda p: (True, "ok"))
        assert final == store_mod.COMPLETED_VERIFIED
        assert s.get_round(retrieve_rid)["result_text"] == "R1's own sentinel-wrapped answer"

    def test_failed_retrieve_envelope_carries_source_and_observed_rid(self, env, capsys):
        """Envelope transparency: awaiting a failed retrieve must surface WHICH round it was
        recovering (source_rid) and WHAT the CDP layer found instead (observed_rid) — not just
        that it failed."""
        store_mod, backend, s, tmp_path = env
        source_rid = "REQ-20260707-120000-0ddd01"
        later_rid = "REQ-20260707-120500-0eee01"
        retrieve_rid = "REQ-20260707-121000-0fff01"
        s.create_round(retrieve_rid, "retrieve", thread_id="conv-env", parent_rid=source_rid,
                       spec_json=json.dumps({"conversation": "conv-env", "parent_rid": source_rid}))
        s.db.execute("UPDATE threads SET conversation_id='conv-env' WHERE thread_id='conv-env'")
        s.set_state(retrieve_rid, store_mod.READY)

        def cdp(kind, **kw):
            return {"code": 5, "out": kw["out"],
                    "stderr": f"CGC_ERROR rid_superseded: source_rid={source_rid} "
                             f"observed_rid={later_rid}"}

        final = backend.process_round(s, s.get_round(retrieve_rid), cdp,
                                      daemon_instance_id="d1", validate=lambda p: (True, "ok"))
        assert final == store_mod.FAILED
        capsys.readouterr()
        code = backend.await_round(types.SimpleNamespace(
            rid=retrieve_rid, out=str(tmp_path / "r.txt"), poll=0.01, timeout=2))
        assert code == 1
        env_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert env_json["source_rid"] == source_rid
        assert env_json["observed_rid"] == later_rid


class TestSourceTurnMatching:
    """cdp_consult._locate_source_turn is the CDP-free core of retrieve's turn verification: given
    plain textContent strings (what a real DOM query would return) it decides whether the source
    round's own turn is present and still the conversation's latest. Per the constraint that
    cdp_consult.py drives a live browser, this is the one piece of its retrieve logic that is
    unit-testable without CDP — no mock browser harness, just the pure function.

    Fixture turns now carry a COMPLETE bare-line BEGIN+END pair (matching what the prompt
    templates actually render — see TestTurnCanonicalRid), since [S3]'s fix requires a turn's own
    rid to come from a paired sentinel, never a lone/substring BEGIN occurrence."""

    @staticmethod
    def _cdp(tmp_path):
        return _load("cdp_consult", tmp_path / "control.db")

    def test_source_turn_is_last_when_nothing_sent_after(self, tmp_path):
        cdp = self._cdp(tmp_path)
        turns = ["review https://github.com/x\n"
                 "BEGIN_RESPONSE:REQ-20260707-120000-0aaa01\nthe answer\nEND_RESPONSE:REQ-20260707-120000-0aaa01\n"]
        loc = cdp._locate_source_turn(turns, "REQ-20260707-120000-0aaa01")
        assert loc == {"present": True, "is_last": True,
                       "observed_rid": "REQ-20260707-120000-0aaa01", "turn_index": 0}

    def test_source_turn_not_last_when_a_later_turn_was_sent(self, tmp_path):
        """The exact shape of the audit's race: R1's turn exists, but R2 was sent afterward —
        is_last must be False, so the caller refuses unwrapped salvage."""
        cdp = self._cdp(tmp_path)
        turns = [
            "review https://github.com/x\n"
            "BEGIN_RESPONSE:REQ-20260707-120000-0aaa01\nthe answer\nEND_RESPONSE:REQ-20260707-120000-0aaa01\n",
            "one more thing\n"
            "BEGIN_RESPONSE:REQ-20260707-120500-0bbb01\nthe ask\nEND_RESPONSE:REQ-20260707-120500-0bbb01\n",
        ]
        loc = cdp._locate_source_turn(turns, "REQ-20260707-120000-0aaa01")
        assert loc["present"] is True
        assert loc["is_last"] is False
        assert loc["observed_rid"] == "REQ-20260707-120500-0bbb01"
        assert loc["turn_index"] == 0

    def test_source_turn_absent_reports_observed_latest(self, tmp_path):
        cdp = self._cdp(tmp_path)
        turns = ["one more thing\n"
                 "BEGIN_RESPONSE:REQ-20260707-120500-0bbb01\nthe ask\nEND_RESPONSE:REQ-20260707-120500-0bbb01\n"]
        loc = cdp._locate_source_turn(turns, "REQ-20260707-120000-0aaa01")
        assert loc == {"present": False, "is_last": False,
                       "observed_rid": "REQ-20260707-120500-0bbb01", "turn_index": None}

    def test_no_turns_at_all(self, tmp_path):
        cdp = self._cdp(tmp_path)
        assert cdp._locate_source_turn([], "REQ-20260707-120000-0aaa01") == \
            {"present": False, "is_last": False, "observed_rid": None, "turn_index": None}

    def test_substring_quote_of_an_old_rid_never_steals_its_identity(self, tmp_path):
        """[S3] audit fixture: R1 is being recovered because no valid sentinel-wrapped R1 answer is
        available. A later R2 user turn quotes/discusses the literal text 'BEGIN_RESPONSE:R1'
        before R2's OWN canonical footer — in prose, in a code fence (textContent-flattened), and
        as a fully quoted BEGIN+END R1 pair. None of these may cause R2's turn to be mistaken for
        R1's own turn: _locate_source_turn(..., R1) must keep the genuine R1 turn at index 0,
        is_last=False, and observed_rid must be R2 (R2's own canonical rid), never R1."""
        cdp = self._cdp(tmp_path)
        r1 = "REQ-20260707-120000-0aaa01"
        r2 = "REQ-20260707-120500-0bbb01"
        r1_turn = f"review https://github.com/x\nBEGIN_RESPONSE:{r1}\nthe R1 answer\nEND_RESPONSE:{r1}\n"

        # (a) quoted in prose
        r2_prose = (
            f"Earlier you answered starting with BEGIN_RESPONSE:{r1} but I think that's wrong.\n"
            f"Here is my real follow-up question.\n"
            f"BEGIN_RESPONSE:{r2}\nmy real ask\nEND_RESPONSE:{r2}\n"
        )
        loc = cdp._locate_source_turn([r1_turn, r2_prose], r1)
        assert loc["present"] is True and loc["is_last"] is False and loc["turn_index"] == 0
        assert loc["observed_rid"] == r2

        # (b) quoted inside a code fence (DOM textContent flattens the fence markers to plain lines)
        r2_fence = (
            "Here's what you sent me, for reference:\n"
            "```\n"
            f"BEGIN_RESPONSE:{r1}\n"
            "```\n"
            f"BEGIN_RESPONSE:{r2}\nmy real ask\nEND_RESPONSE:{r2}\n"
        )
        loc = cdp._locate_source_turn([r1_turn, r2_fence], r1)
        assert loc["present"] is True and loc["is_last"] is False and loc["turn_index"] == 0
        assert loc["observed_rid"] == r2

        # (c) a FULLY quoted BEGIN+END R1 pair, followed by R2's own canonical footer
        r2_full_pair = (
            f"You previously wrote:\nBEGIN_RESPONSE:{r1}\nthe R1 answer\nEND_RESPONSE:{r1}\n"
            "That's the context. Now, my real follow-up:\n"
            f"BEGIN_RESPONSE:{r2}\nmy real ask\nEND_RESPONSE:{r2}\n"
        )
        loc = cdp._locate_source_turn([r1_turn, r2_full_pair], r1)
        assert loc["present"] is True and loc["is_last"] is False and loc["turn_index"] == 0
        assert loc["observed_rid"] == r2


class TestTurnCanonicalRid:
    """cdp_consult._turn_canonical_rid — the single canonical parser for a user turn's OWN request
    rid (SHARED CONTRACT #3, [S3] fix). Pure function, unit-tested without CDP."""

    @staticmethod
    def _cdp(tmp_path):
        return _load("cdp_consult", tmp_path / "control.db")

    def test_returns_r2_despite_r1_quoted_in_prose(self, tmp_path):
        cdp = self._cdp(tmp_path)
        r1, r2 = "REQ-20260707-120000-0aaa01", "REQ-20260707-120500-0bbb01"
        text = (f"Earlier you answered starting with BEGIN_RESPONSE:{r1} but I think that's wrong.\n"
                f"Here is my real follow-up question.\n"
                f"BEGIN_RESPONSE:{r2}\nmy real ask\nEND_RESPONSE:{r2}\n")
        assert cdp._turn_canonical_rid(text) == r2

    def test_returns_r2_despite_r1_quoted_in_code_fence(self, tmp_path):
        cdp = self._cdp(tmp_path)
        r1, r2 = "REQ-20260707-120000-0aaa01", "REQ-20260707-120500-0bbb01"
        text = ("Here's what you sent me, for reference:\n"
                "```\n"
                f"BEGIN_RESPONSE:{r1}\n"
                "```\n"
                f"BEGIN_RESPONSE:{r2}\nmy real ask\nEND_RESPONSE:{r2}\n")
        assert cdp._turn_canonical_rid(text) == r2

    def test_returns_r2_despite_full_r1_pair_quoted_first(self, tmp_path):
        cdp = self._cdp(tmp_path)
        r1, r2 = "REQ-20260707-120000-0aaa01", "REQ-20260707-120500-0bbb01"
        text = (f"You previously wrote:\nBEGIN_RESPONSE:{r1}\nthe R1 answer\nEND_RESPONSE:{r1}\n"
                "That's the context. Now, my real follow-up:\n"
                f"BEGIN_RESPONSE:{r2}\nmy real ask\nEND_RESPONSE:{r2}\n")
        assert cdp._turn_canonical_rid(text) == r2

    def test_lone_begin_with_no_matching_end_yields_none(self, tmp_path):
        cdp = self._cdp(tmp_path)
        r1 = "REQ-20260707-120000-0aaa01"
        assert cdp._turn_canonical_rid(f"just discussing BEGIN_RESPONSE:{r1} with no end\n") is None

    def test_no_sentinel_text_at_all_yields_none(self, tmp_path):
        cdp = self._cdp(tmp_path)
        assert cdp._turn_canonical_rid("plain unrelated question, no sentinels\n") is None
        assert cdp._turn_canonical_rid("") is None
        assert cdp._turn_canonical_rid(None) is None

    def test_simple_single_pair_matches(self, tmp_path):
        cdp = self._cdp(tmp_path)
        rid = "REQ-20260707-120000-0aaa01"
        text = f"review https://github.com/x\nBEGIN_RESPONSE:{rid}\nthe answer\nEND_RESPONSE:{rid}\n"
        assert cdp._turn_canonical_rid(text) == rid

    def test_mismatched_begin_and_end_rid_does_not_pair(self, tmp_path):
        """A BEGIN for rid A followed by an END for a DIFFERENT rid B must not be treated as a
        pair for either — only a same-rid BEGIN/END pair counts."""
        cdp = self._cdp(tmp_path)
        a, b = "REQ-20260707-120000-0aaa01", "REQ-20260707-120500-0bbb01"
        text = f"BEGIN_RESPONSE:{a}\nsome text\nEND_RESPONSE:{b}\n"
        assert cdp._turn_canonical_rid(text) is None


class TestKeyReleaseRequiresNoSendProof:
    """Audit S3: a request-key releases to a retry ONLY when the store DURABLY proves the prior
    round never sent. Fingerprint is compared FIRST (a different logical request always conflicts,
    even after a failure). GENERIC FAILED does not release: possibly_accepted->failed and
    waiting->failed are legal reconciles that do NOT prove no-send, so their key stays owned. Only
    GATE_REJECTED, a FAILED that never entered `sending`, or an explicit operator not-sent proof
    releases — and the release+successor is one atomic key transfer."""

    BODY = "review https://github.com/acme/w"

    def _enq(self, backend, tmp_path, rid, key, body=None):
        body = self.BODY if body is None else body
        pf = tmp_path / f"{rid}.md"
        pf.write_text(f"{body}\nBEGIN_RESPONSE:{rid}\n", encoding="utf-8")
        return backend.enqueue_round(
            _enq_ns(rid, str(pf), request_key=key, logical_sha=_lsha(body)),
            pf.read_text(), str(tmp_path / f"a_{rid}.txt"))

    @staticmethod
    def _drive_possibly_accepted(store_mod, s, rid):
        """queued -> ready -> sending (an attempt row now exists) -> possibly_accepted: the round has
        crossed the send fence, so the click MAY have reached ChatGPT."""
        s.set_state(rid, store_mod.READY)
        aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d")
        s.mark_possibly_accepted(aid, "unknown send — uncertain")

    # ---- release IS allowed (durable no-send proof) --------------------------
    def test_gate_rejected_prior_releases_the_key(self, env, capsys):
        store_mod, backend, s, tmp_path = env
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g0001", "kg1") == 0
        s.set_state("REQ-20260707-120000-0g0001", store_mod.READY)
        s.gate_reject("REQ-20260707-120000-0g0001", "refused: private repo")  # pre-send by construction
        capsys.readouterr()
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g0002", "kg1") == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["rid"] == "REQ-20260707-120000-0g0002" and not out.get("idempotent_repeat")
        assert s.get_round("REQ-20260707-120000-0g0001")["request_key"] is None
        assert s.get_round("REQ-20260707-120000-0g0002")["request_key"] == "kg1"

    def test_failed_never_entered_sending_releases_the_key(self, env, capsys):
        """A prior cancelled while QUEUED never called begin_send, so there is no attempt row — the
        store durably proves nothing sent. Its key transfers to the retry (which requeues fresh)."""
        store_mod, backend, s, tmp_path = env
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g1001", "kg2") == 0
        capsys.readouterr()
        assert backend.cancel_round("REQ-20260707-120000-0g1001") == 0  # FAILED, never sent
        assert not s.has_send_attempt("REQ-20260707-120000-0g1001")
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g1002", "kg2") == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["rid"] == "REQ-20260707-120000-0g1002" and not out.get("idempotent_repeat")
        assert s.get_round("REQ-20260707-120000-0g1001")["request_key"] is None
        assert s.get_round("REQ-20260707-120000-0g1002")["request_key"] == "kg2"

    def test_operator_not_sent_proof_releases_a_post_send_failure(self, env, capsys):
        """The recorded incident: a send WebSocket-timed out post-click -> possibly_accepted; the
        operator proved via browser inspection it never sent and reconciled it. That explicit
        not-sent proof — and only it — lets the key move even though an attempt row exists."""
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0g2001"
        assert self._enq(backend, tmp_path, rid, "kg3") == 0
        self._drive_possibly_accepted(store_mod, s, rid)
        s.record_not_sent_proof(rid, "browser inspection: no such message on the thread")
        r = s.get_round(rid)
        assert r["state"] == store_mod.FAILED and r["send_disposition"] == store_mod.NOT_SENT_PROVEN
        capsys.readouterr()
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g2002", "kg3") == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["rid"] == "REQ-20260707-120000-0g2002" and not out.get("idempotent_repeat")
        assert s.get_round(rid)["request_key"] is None

    def test_preclick_exit_releases_the_key_without_operator_reconcile(self, env, capsys):
        """Driven end-to-end through process_round (not a manual record_not_sent_proof call, unlike
        the sibling test above): cdp_consult's EXIT_NOT_SENT_PRECLICK (6) is itself a durable
        not-sent proof, so the round reaches FAILED+NOT_SENT_PROVEN — and its request-key releases to
        a retry — with no operator step in between."""
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0g7001"
        assert self._enq(backend, tmp_path, rid, "kg9") == 0
        s.set_state(rid, store_mod.READY)

        def cdp(kind, **kw):
            return {"code": 6, "conversation": "",
                    "stderr": "CGC_ERROR not_sent_preclick: TimeoutError during paste: timed out"}

        final = backend.process_round(s, s.get_round(rid), cdp, daemon_instance_id="d1",
                                      validate=_OK_GATE)
        assert final == store_mod.FAILED
        r = s.get_round(rid)
        assert r["state"] == store_mod.FAILED and r["send_disposition"] == store_mod.NOT_SENT_PROVEN
        capsys.readouterr()
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g7002", "kg9") == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["rid"] == "REQ-20260707-120000-0g7002" and not out.get("idempotent_repeat")
        assert s.get_round(rid)["request_key"] is None

    # ---- release is REFUSED (the send may have happened) ---------------------
    def test_possibly_accepted_then_failed_keeps_the_key(self, env, capsys):
        """possibly_accepted -> failed is a legal reconcile but NOT proof of no-send: FAILED alone is
        insufficient. The key stays owned; the retry is refused as possibly-already-sent."""
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0g3001"
        assert self._enq(backend, tmp_path, rid, "kg4") == 0
        self._drive_possibly_accepted(store_mod, s, rid)
        s.set_state(rid, store_mod.FAILED, expect=store_mod.POSSIBLY_ACCEPTED,
                    error_code="operator reconcile without proof")
        capsys.readouterr()
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g3002", "kg4") == 2
        assert "request_key_locked" in capsys.readouterr().err
        assert s.get_round(rid)["request_key"] == "kg4", "key retained — the send may have landed"
        assert s.get_round("REQ-20260707-120000-0g3002") is None

    def test_waiting_then_failed_keeps_the_key(self, env, capsys):
        """waiting -> failed (a post-send wait failure reconciled) is likewise not no-send proof."""
        store_mod, backend, s, tmp_path = env
        rid = "REQ-20260707-120000-0g4001"
        assert self._enq(backend, tmp_path, rid, "kg5") == 0
        s.set_state(rid, store_mod.READY)
        aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d")
        s.mark_accepted(aid, "conv-w")
        s.mark_waiting(rid)
        s.finish(rid, store_mod.FAILED, error_code="wait failed post-send")
        capsys.readouterr()
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g4002", "kg5") == 2
        assert "request_key_locked" in capsys.readouterr().err
        assert s.get_round(rid)["request_key"] == "kg5"

    def test_live_or_completed_prior_still_owns_the_key(self, env, capsys):
        store_mod, backend, s, tmp_path = env
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g5001", "kg6") == 0
        capsys.readouterr()
        # queued (live) prior: idempotent receipt, no new round
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g5002", "kg6") == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["rid"] == "REQ-20260707-120000-0g5001" and out["idempotent_repeat"] is True

    # ---- fingerprint FIRST, regardless of terminal state ---------------------
    def test_different_fingerprint_after_failure_conflicts_not_releases(self, env, capsys):
        """The core S3 point: a DIFFERENT logical request under the same key must CONFLICT even when
        the prior FAILED releasably — the fingerprint is compared before any release decision, so a
        failure can never let a different request seize the key."""
        store_mod, backend, s, tmp_path = env
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g6001", "kg7") == 0
        assert backend.cancel_round("REQ-20260707-120000-0g6001") == 0  # FAILED, never sent -> releasable
        capsys.readouterr()
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g6002", "kg7",
                         body="review https://github.com/acme/DIFFERENT") == 2
        assert "request_key_conflict" in capsys.readouterr().err
        assert s.get_round("REQ-20260707-120000-0g6001")["request_key"] == "kg7", "not released"
        assert s.get_round("REQ-20260707-120000-0g6002") is None

    # ---- atomic transfer under a concurrent retry race -----------------------
    def test_concurrent_release_yields_exactly_one_successor(self, env, capsys, monkeypatch):
        """Two retries race a released key. The transfer clears the old key and inserts the successor
        in ONE guarded transaction, so exactly one successor owns the key; the loser (its pre-check
        saw the stale failed prior) re-resolves to the winner's receipt, never a second successor."""
        store_mod, backend, s, tmp_path = env
        prior_rid = "REQ-20260707-120000-0g7001"
        assert self._enq(backend, tmp_path, prior_rid, "kg8") == 0
        assert backend.cancel_round(prior_rid) == 0                # FAILED, never sent -> releasable
        stale = s.round_by_request_key("kg8")                       # both racers start from this view
        capsys.readouterr()
        # retry A wins: transfers the key to its fresh successor
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g7002", "kg8") == 0
        assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["rid"] == \
            "REQ-20260707-120000-0g7002"
        # retry B raced: its pre-check still returns the STALE failed prior, not A's successor
        real = store_mod.Store.round_by_request_key
        calls = {"n": 0}

        def racy(self, key):
            calls["n"] += 1
            return stale if calls["n"] == 1 else real(self, key)

        monkeypatch.setattr(store_mod.Store, "round_by_request_key", racy)
        assert self._enq(backend, tmp_path, "REQ-20260707-120000-0g7003", "kg8") == 0
        out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert out["rid"] == "REQ-20260707-120000-0g7002" and out["idempotent_repeat"] is True
        assert s.get_round("REQ-20260707-120000-0g7003") is None, "the loser created no round"
        assert calls["n"] >= 2, "the raced transfer must re-resolve to the winner"

    # ---- route preservation across a same-fingerprint retry ------------------
    def test_bare_auto_followup_retry_inherits_the_original_conversation(self, env, capsys):
        """Req 5: a bare-auto followup's fingerprint excludes the resolved thread, so two fires share
        it. When the first FAILED pre-send and a DIFFERENT consult has since become the 'latest
        completed', the retry must INHERIT the original resolved conversation, never re-resolve to
        the newer thread — two same-fingerprint requests must not diverge to different conversations."""
        store_mod, backend, s, tmp_path = env
        _complete_a_consult(store_mod, s, rid="REQ-20260707-118000-0h0001", conv="conv-first")
        body = "continuing; the next question"
        a1 = _enq_ns("REQ-20260707-120000-0h0002", str(tmp_path / "fu1.md"), kind="followup",
                     conversation="auto", request_key="kh1", logical_sha=_lsha(body))
        assert backend.enqueue_round(a1, body, str(tmp_path / "fu1.txt")) == 0
        assert json.loads(s.get_round("REQ-20260707-120000-0h0002")["spec_json"])["conversation"] \
            == "conv-first"
        assert backend.cancel_round("REQ-20260707-120000-0h0002") == 0   # FAILED pre-send -> releasable
        # a different consult completes and, once the first is aged back, becomes the unambiguous latest
        _complete_a_consult(store_mod, s, rid="REQ-20260707-119000-0h0003", conv="conv-second")
        s.db.execute("UPDATE rounds SET updated_at='2020-01-01T00:00:00+00:00' "
                     "WHERE rid='REQ-20260707-118000-0h0001'")
        assert s.latest_conversation_strict()[0] == "conv-second", "re-resolution would pick conv-second"
        capsys.readouterr()
        a2 = _enq_ns("REQ-20260707-120000-0h0004", str(tmp_path / "fu2.md"), kind="followup",
                     conversation="auto", request_key="kh1", logical_sha=_lsha(body))
        assert backend.enqueue_round(a2, body, str(tmp_path / "fu2.txt")) == 0
        r2 = s.get_round("REQ-20260707-120000-0h0004")
        assert json.loads(r2["spec_json"])["conversation"] == "conv-first", \
            "the retry inherited the prior's resolved thread, not the newer 'latest'"
        assert r2["thread_id"] == "conv-first"
        assert s.get_round("REQ-20260707-120000-0h0002")["request_key"] is None

    # ---- S2: low-level --request-key demands --logical-sha -------------------
    def test_request_key_without_logical_sha_is_refused(self, env, capsys):
        store_mod, backend, s, tmp_path = env
        pf = tmp_path / "p.md"
        pf.write_text("review https://github.com/acme/w\nBEGIN_RESPONSE:x\n", encoding="utf-8")
        rc = backend.enqueue_round(
            _enq_ns("REQ-20260707-120000-0h9001", str(pf), request_key="kx", logical_sha=None),
            pf.read_text(), str(tmp_path / "a.txt"))
        assert rc == 2
        assert "request_key_needs_logical_sha" in capsys.readouterr().err
        assert s.get_round("REQ-20260707-120000-0h9001") is None


# ---- Fix S3-B: canonical conversation identity (fail-closed) -----------------

def test_enqueue_rejects_rid_shaped_conversation_and_accepts_canonical(env, capsys):
    """An explicit --conversation must be a bare canonical conversation id. A rid-shaped alias is
    rejected AT ENQUEUE (never stored, never locked) with the guidance to use --parent; a canonical id
    is accepted and stored as-is."""
    store_mod, backend, s, tmp = env
    rid_shape = "REQ-20260721-000000-00ab01"
    a = types.SimpleNamespace(rid="REQ-20260721-000000-00rj01", kind="followup",
                              project_url="https://chatgpt.com/", model="Pro",
                              conversation=rid_shape, poll=1, timeout=1)
    assert backend.enqueue_round(a, "continuing this consult", str(tmp / "r.txt")) == 2
    assert "conversation_not_canonical" in capsys.readouterr().err
    assert s.get_round("REQ-20260721-000000-00rj01") is None, "the rejected round is never created"

    canon = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    b = types.SimpleNamespace(rid="REQ-20260721-000000-00rj02", kind="followup",
                              project_url="https://chatgpt.com/", model="Pro",
                              conversation=canon, poll=1, timeout=1)
    assert backend.enqueue_round(b, "continuing this consult", str(tmp / "r2.txt")) == 0
    assert json.loads(s.get_round("REQ-20260721-000000-00rj02")["spec_json"])["conversation"] == canon


def test_alias_and_parent_target_the_same_conversation_lock(env):
    """The audit's contention recipe: a follow-up pinned by canonical --conversation C and a follow-up
    pinned by --parent R0 (seeded R0->C) resolve to the SAME conversation and therefore the SAME lease
    file — the two handles for one thread can never lock different files and both drive its composer."""
    import cgc_spool
    store_mod, backend, s, tmp = env
    conv = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    _complete_a_consult(store_mod, s, rid="REQ-20260721-000000-00R000", conv=conv)  # R0 -> C
    a = types.SimpleNamespace(rid="REQ-20260721-000000-00e001", kind="followup",
                              project_url="https://chatgpt.com/", model="Pro",
                              conversation=conv, poll=1, timeout=1)             # explicit canonical C
    b = types.SimpleNamespace(rid="REQ-20260721-000000-00e002", kind="followup",
                              parent="REQ-20260721-000000-00R000", project_url="https://chatgpt.com/",
                              model="Pro", conversation="auto", poll=1, timeout=1)   # via --parent R0
    assert backend.enqueue_round(a, "continuing this consult", str(tmp / "a.txt")) == 0
    assert backend.enqueue_round(b, "continuing this consult", str(tmp / "b.txt")) == 0
    ca = json.loads(s.get_round("REQ-20260721-000000-00e001")["spec_json"])["conversation"]
    cb = json.loads(s.get_round("REQ-20260721-000000-00e002")["spec_json"])["conversation"]
    assert ca == cb == conv, "alias and --parent resolve to the one conversation"
    assert cgc_spool._conversation_lease_path(ca) == cgc_spool._conversation_lease_path(cb), \
        "one conversation, one lease file"
    if cgc_spool.fcntl is not None:  # and it is REAL lock contention, not just an equal path string
        held = cgc_spool.acquire_conversation_lease(ca)
        assert held is not None
        assert cgc_spool.acquire_conversation_lease(cb) is None, "same lock — the second acquire refuses"
        held.close()


def test_worker_fails_presend_on_noncanonical_conversation_without_locking_or_sending(env):
    """Worker boundary: a pre-release queued row carrying a rid-shaped conversation fails PRE-SEND
    (terminal FAILED) — no conversation lease taken, no CDP call, no attempt begun."""
    import cgc_spool
    store_mod, backend, s, tmp = env
    rid_shape = "REQ-20260721-000000-00ab01"
    s.create_round("REQ-20260721-000000-00w001", "followup", out_path=str(tmp / "w.txt"),
                   prompt="continuing this consult", spec_json=json.dumps({"conversation": rid_shape}))
    s.set_state("REQ-20260721-000000-00w001", store_mod.READY)
    final = backend.process_round(
        s, s.get_round("REQ-20260721-000000-00w001"),
        lambda *a, **k: pytest.fail("no browser call on a non-canonical conversation"),
        daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.FAILED
    rr = s.get_round("REQ-20260721-000000-00w001")
    assert rr["current_attempt_id"] is None, "no send attempt begun — pre-send by construction"
    assert "not a canonical id" in (rr["error_code"] or "")
    # no lease was taken for the alias: acquire_conversation_lease is the only opener of that file and
    # it returned before reaching it, so the lock file was never even created.
    assert not os.path.exists(cgc_spool._conversation_lease_path(rid_shape)), "no lock file was created"


# ---- an uncertain send must stay ADDRESSABLE ---------------------------------------------------
# The field failure this closes: submit clicked, the rid echo was unreadable, and the round went
# possibly_accepted with NO conversation. Recovery is addressed BY conversation, so there was
# nothing to retrieve from — while the answer was generating in a tab a minute from being swept.
# The human had to copy 30KB out of the browser by hand.

def test_unconfirmed_send_with_a_conversation_is_retrievable_not_a_dead_end(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)

    def cdp(kind, **kw):
        # unknown_send on a REUSED tab: the driver reports where the tab is, which is an address,
        # not a confirmation.
        return {"code": 3, "conversation": "conv-live",
                "stderr": "CGC_ERROR unknown_send: rid never echoed"}

    final = backend.process_round(s, r, cdp, daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.POSSIBLY_ACCEPTED, "an unconfirmed send is never laundered into accepted"
    rec = s.recover()
    assert r["rid"] in rec["uncertain"], "still uncertain — the send is not proven"
    assert r["rid"] not in rec["dispatchable"], "and never re-sendable"
    assert s.conversation_of(r["rid"]) == "conv-live"
    assert r["rid"] in rec["retrievable"], "the recorded address is what makes recovery automatic"


def test_unconfirmed_send_without_a_conversation_is_still_not_retrievable(env):
    """No address recorded → no auto-retrieve to attempt. It must fall to a human, not to a resend."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    final = backend.process_round(
        s, r, lambda *a, **k: {"code": 3, "conversation": "", "stderr": "unknown_send"},
        daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.POSSIBLY_ACCEPTED
    rec = s.recover()
    assert r["rid"] in rec["uncertain"] and r["rid"] not in rec["retrievable"]
    assert r["rid"] not in rec["dispatchable"]


def test_proven_not_sent_retry_does_not_inherit_a_thread(env):
    """A re-queued round must carry no thread: a conversation pinned from a send that never happened
    would follow it into its next attempt and mis-address the answer."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    final = backend.process_round(
        s, r, lambda *a, **k: {"code": 3, "conversation": "conv-stale",
                               "stderr": "CGC_ERROR model_not_selectable: wanted 'Pro'"},
        daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.QUEUED
    assert s.conversation_of(r["rid"]) is None


def test_link_conversation_never_moves_state_and_refuses_terminal(env):
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    rid = r["rid"]
    s.link_conversation(rid, "conv-x")
    assert s.get_round(rid)["state"] == store_mod.READY, "an address is not a state change"
    assert s.conversation_of(rid) == "conv-x"
    aid = s.begin_send(rid, r["rendered_prompt"] or "p", store_mod.sha256("p"),
                       daemon_instance_id="d1")
    s.mark_send_not_sent(aid, "proven pre-click")          # -> FAILED (terminal)
    with pytest.raises(store_mod.IllegalTransition):
        s.link_conversation(rid, "conv-y")


def test_a_verified_retrieve_closes_the_round_it_recovered(env):
    """The recovery loop must terminate. A retrieve that matched END_RESPONSE:<source> proved both
    that the source's send landed and what it answered — so the source stops being uncertain."""
    store_mod, backend, s, tmp = env
    src = _ready_round(store_mod, s)
    aid = s.begin_send(src["rid"], "p", store_mod.sha256("p"), daemon_instance_id="d1")
    s.mark_possibly_accepted(aid, "unknown send")

    s.create_round("REQ-20260721-000000-00re01", "retrieve", out_path=str(tmp / "r.txt"),
                   spec_json=json.dumps({"conversation": "conv-live", "parent_rid": src["rid"]}))
    s.set_state("REQ-20260721-000000-00re01", store_mod.READY)

    def cdp(kind, **kw):
        assert kind == "wait", "a retrieve NEVER sends"
        assert kw["rid"] == src["rid"], "it must verify the SOURCE round's sentinel"
        with open(kw["out"], "w") as f:
            f.write("the recovered answer")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    final = backend.process_round(s, s.get_round("REQ-20260721-000000-00re01"), cdp,
                                  daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.COMPLETED_VERIFIED
    rr = s.get_round(src["rid"])
    assert rr["state"] == store_mod.COMPLETED_VERIFIED
    assert rr["result_text"] == "the recovered answer"
    assert src["rid"] not in s.recover()["uncertain"], "no longer unfinished business"


def test_an_unverified_salvage_does_not_close_the_round_it_recovered(env):
    """An unwrapped answer cannot prove which turn produced it, so it may not settle an uncertain
    send — that is the one judgement a human still owns."""
    store_mod, backend, s, tmp = env
    src = _ready_round(store_mod, s)
    aid = s.begin_send(src["rid"], "p", store_mod.sha256("p"), daemon_instance_id="d1")
    s.mark_possibly_accepted(aid, "unknown send")
    s.create_round("REQ-20260721-000000-00re02", "retrieve", out_path=str(tmp / "r2.txt"),
                   spec_json=json.dumps({"conversation": "conv-live", "parent_rid": src["rid"]}))
    s.set_state("REQ-20260721-000000-00re02", store_mod.READY)

    def cdp(kind, **kw):
        with open(kw["out"], "w") as f:
            f.write("salvaged text")
        with open(kw["out"] + ".raw", "w") as f:   # the sidecar marks it unverified
            f.write("x")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    backend.process_round(s, s.get_round("REQ-20260721-000000-00re02"), cdp,
                          daemon_instance_id="d1", validate=_OK_GATE)
    assert s.get_round(src["rid"])["state"] == store_mod.POSSIBLY_ACCEPTED


def test_a_retrieve_does_not_terminalise_a_round_a_live_worker_owns(env, monkeypatch):
    """The retrieve holds only its OWN rid lease, so the round it recovers is unfenced: the daemon's
    one-shot auto-retrieve can be mid-wait on it. Closing it underneath that worker turns the
    worker's own finish into an IllegalTransition traceback."""
    store_mod, backend, s, tmp = env
    src = _ready_round(store_mod, s)
    aid = s.begin_send(src["rid"], "p", store_mod.sha256("p"), daemon_instance_id="d1")
    s.mark_possibly_accepted(aid, "unknown send")
    monkeypatch.setattr(backend, "_no_live_worker", lambda rid: False)   # someone owns it

    s.create_round("REQ-20260721-000000-00re03", "retrieve", out_path=str(tmp / "r3.txt"),
                   spec_json=json.dumps({"conversation": "conv-live", "parent_rid": src["rid"]}))
    s.set_state("REQ-20260721-000000-00re03", store_mod.READY)

    def cdp(kind, **kw):
        with open(kw["out"], "w") as f:
            f.write("the recovered answer")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    assert backend.process_round(s, s.get_round("REQ-20260721-000000-00re03"), cdp,
                                 daemon_instance_id="d1",
                                 validate=_OK_GATE) == store_mod.COMPLETED_VERIFIED
    assert s.get_round(src["rid"])["state"] == store_mod.POSSIBLY_ACCEPTED, "left to its owner"


def test_adopt_refuses_a_round_that_is_being_waited_on(env):
    """WAITING is a live worker's state — the store's own last fence behind the lease probe."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    s.set_state(r["rid"], store_mod.WAITING)
    assert s.adopt_retrieved_answer(r["rid"], "text") is False
    assert s.get_round(r["rid"])["state"] == store_mod.WAITING


def test_a_recovered_round_stays_continuable(env):
    """Recovering the answer but not the thread is half a recovery: without its conversation the
    round refuses `--parent <rid>` and drops out of `latest_conversation`, so the follow-up silently
    degrades into a fresh consult that throws the thread's context away. (Observed in the field: a
    re-review follow-up was refused `followup_parent_unresolved` and re-fired as a new submit.)"""
    store_mod, backend, s, tmp = env
    src = _ready_round(store_mod, s)
    aid = s.begin_send(src["rid"], "p", store_mod.sha256("p"), daemon_instance_id="d1")
    s.mark_possibly_accepted(aid, "unknown send")
    s.create_round("REQ-20260721-000000-00re04", "retrieve", out_path=str(tmp / "r4.txt"),
                   spec_json=json.dumps({"conversation": "conv-live", "parent_rid": src["rid"]}))
    s.set_state("REQ-20260721-000000-00re04", store_mod.READY)

    def cdp(kind, **kw):
        with open(kw["out"], "w") as f:
            f.write("recovered")
        return {"code": 0, "out": kw["out"], "stderr": ""}

    backend.process_round(s, s.get_round("REQ-20260721-000000-00re04"), cdp,
                          daemon_instance_id="d1", validate=_OK_GATE)
    assert s.conversation_of(src["rid"]) == "conv-live", "the thread the retrieve proved it lives in"
    assert s.latest_conversation() == "conv-live"


def test_a_round_created_on_a_known_thread_is_addressable(env):
    """`thread_id` IS the conversation id at every call site. Storing it without the conversation
    made a thread nobody could address — the round completed and was still not continuable."""
    store_mod, backend, s, tmp = env
    s.create_round("REQ-20260721-000000-00th01", "followup", out_path=str(tmp / "t.txt"),
                   prompt="continuing this consult", thread_id="conv-abc")
    assert s.conversation_of("REQ-20260721-000000-00th01") == "conv-abc"


def test_re_enqueuing_an_existing_rid_says_what_to_do_about_it(env, capsys):
    """A rid is minted per render, so re-enqueuing one is always a repeat of a request the store
    already owns — and a bare 'already exists' leaves a retry loop with nothing to act on (observed:
    the same command failed identically every time while the round it collided with had already
    answered). The refusal must name the state and the one command that follows from it."""
    import argparse
    import cgc_spool
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    s.set_state(r["rid"], store_mod.WAITING)
    s.finish(r["rid"], store_mod.COMPLETED_VERIFIED, result_text="done")

    pf = tmp / "p.md"
    pf.write_text("continuing this consult\nhttps://github.com/o/r/pull/1\n")
    a = argparse.Namespace(rid=r["rid"], prompt_file=str(pf), kind="submit", out=str(tmp / "o.txt"),
                           conversation="auto", parent=None, model="Pro", request_key=None,
                           logical_sha=None, project_url=None, poll=20, timeout=60, quiet=True)
    assert cgc_spool.cmd_enqueue(a) == 2
    err = capsys.readouterr().err
    assert "already_enqueued" in err
    assert "read its answer" in err and "--followup --parent" in err, \
        "a completed collision must point at the answer and at how to continue the thread"


def test_a_followup_that_provably_did_not_send_is_requeued_not_uncertain(env):
    """The two branches must read the same evidence the same way. `model_not_selectable` and the
    rest of _NOT_SENT_RETRY are emitted FAIL-CLOSED before the click, so the follow-up provably did
    not send. Filing that as UNCERTAIN is the costliest misfiling there is: it blocks the free
    automatic retry, burns a full auto-retrieve and then a manual one hunting an answer nobody
    asked for, and leaves a human with only a resend left to try — under exactly the uncertainty
    the invariant exists to prevent. (Observed: a Pro-tier drop cost an hour this way.)"""
    store_mod, backend, s, tmp = env
    s.create_round("REQ-20260721-000000-00fu01", "followup", out_path=str(tmp / "f.txt"),
                   prompt="continuing this consult",
                   spec_json=json.dumps({"conversation": "6a684b38-bef4-83ea-83d4-134bf9610e05"}))
    s.set_state("REQ-20260721-000000-00fu01", store_mod.READY)

    def cdp(kind, **kw):
        return {"code": 2, "stderr": "CGC_ERROR model_not_selectable: thread offers 'None', not 'Pro'"}

    final = backend.process_round(s, s.get_round("REQ-20260721-000000-00fu01"), cdp,
                                  daemon_instance_id="d1", validate=_OK_GATE)
    assert final == store_mod.QUEUED, "proven not-sent → retryable, never uncertain"
    rec = s.recover()
    assert "REQ-20260721-000000-00fu01" in rec["dispatchable"]
    assert "REQ-20260721-000000-00fu01" not in rec["uncertain"]


def test_a_failed_wait_does_not_overwrite_why_the_round_became_uncertain(env):
    """This fallback is also where the one-shot auto-retrieve of an ALREADY-uncertain round lands.
    Stamping 'accepted but wait produced no answer' there asserted a confirmation that never
    happened and erased the real disposition a human needs to judge a resend."""
    store_mod, backend, s, tmp = env
    r = _ready_round(store_mod, s)
    rid = r["rid"]
    aid = s.begin_send(rid, "p", store_mod.sha256("p"), daemon_instance_id="d1")
    s.mark_possibly_accepted(aid, "model_not_selectable — provably not sent")
    s.link_conversation(rid, "conv-live")
    assert s.claim_auto_retrieve(rid)          # possibly_accepted -> waiting, as the daemon does

    backend._wait_phase(s, rid, "conv-live", {}, lambda *a, **k: {"code": 4, "out": str(tmp / "x"),
                                                                  "stderr": "nothing"})
    err = s.get_round(rid)["error_code"]
    assert "accepted" not in err, "it was never accepted — the message must not claim it was"
    assert "model_not_selectable" in err, "the original disposition must survive"
