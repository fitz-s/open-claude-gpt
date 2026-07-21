# Tests for the SQLite control-plane store (skill/scripts/cgc_store.py), phase 1.
# The store is standalone (not yet wired into the daemon); these tests pin the state machine and,
# above all, the anti-duplicate invariant: a round that may have reached ChatGPT is NEVER classified
# as dispatchable.
import importlib.util
import os
import stat

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load(db_path):
    os.environ["CGC_STORE_DB"] = str(db_path)
    spec = importlib.util.spec_from_file_location("cgc_store", os.path.join(SCRIPTS, "cgc_store.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def store(tmp_path):
    m = _load(tmp_path / "control.db")
    s = m.Store(str(tmp_path / "control.db"))
    yield m, s
    s.close()


def test_schema_and_file_mode(store, tmp_path):
    m, s = store
    # WAL journal + a schema_version row
    assert s.db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert s.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    mode = stat.S_IMODE(os.stat(tmp_path / "control.db").st_mode)
    assert mode == 0o600, oct(mode)


def test_create_and_get_round(store):
    m, s = store
    s.create_round("REQ-1", "submit", out_path="/tmp/a.txt")
    r = s.get_round("REQ-1")
    assert r["state"] == m.QUEUED and r["kind"] == "submit" and r["out_path"] == "/tmp/a.txt"
    assert s.get_round("nope") is None


def test_accept_persists_conversation_for_a_threadless_submit(store):
    """A fresh submit has no thread at enqueue; when the conversation first arrives at accept, a
    thread must be created and linked so the id is never lost (followup/retrieve depend on it).
    Regression: live cutover verification found the id was silently dropped."""
    m, s = store
    s.create_round("REQ-1", "submit")  # no thread_id
    s.set_state("REQ-1", m.READY)
    aid = s.begin_send("REQ-1", "b", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid, conversation_id="conv-xyz")
    r = s.get_round("REQ-1")
    assert r["thread_id"] == "conv-xyz"
    th = s.db.execute("SELECT conversation_id FROM threads WHERE thread_id=?", ("conv-xyz",)).fetchone()
    assert th["conversation_id"] == "conv-xyz"


def test_happy_path_transitions(store):
    m, s = store
    s.create_round("REQ-1", "submit")
    s.set_state("REQ-1", m.READY)
    aid = s.begin_send("REQ-1", "the exact bytes", "deadbeef" * 8, daemon_instance_id="d1", browser_epoch=3)
    assert isinstance(aid, str) and aid
    r = s.get_round("REQ-1")
    assert r["state"] == m.SENDING and r["rendered_prompt"] == "the exact bytes"
    assert r["current_attempt_id"] == aid
    s.mark_accepted(aid, conversation_id="conv-xyz")
    assert s.get_round("REQ-1")["state"] == m.ACCEPTED
    s.mark_waiting("REQ-1")
    s.finish("REQ-1", m.COMPLETED_VERIFIED, result_text="the answer")
    r = s.get_round("REQ-1")
    assert r["state"] == m.COMPLETED_VERIFIED
    assert r["result_text"] == "the answer" and r["completion_confidence"] == "verified"


@pytest.mark.parametrize("frm,to", [
    ("queued", "waiting"),          # can't skip straight to waiting
    ("queued", "sending"),          # must go through ready
    ("sending", "failed"),          # once sending, failure is NOT knowable → no sending->failed
    ("completed_verified", "waiting"),  # terminal has no exit
    ("gate_rejected", "ready"),
])
def test_illegal_transitions_raise(store, frm, to):
    m, s = store
    s.create_round("REQ-1", "submit")
    # drive to `frm` via legal steps
    path = {
        "queued": [],
        "sending": [("set", m.READY), ("send",)],
        "completed_verified": [("set", m.READY), ("send",), ("acc",), ("wait",), ("fin", m.COMPLETED_VERIFIED)],
        "gate_rejected": [("gate",)],
    }[frm]
    for step in path:
        if step[0] == "set":
            s.set_state("REQ-1", step[1])
        elif step[0] == "send":
            s._aid = s.begin_send("REQ-1", "b", "h" * 64, daemon_instance_id="d1")
        elif step[0] == "acc":
            s.mark_accepted(s._aid)
        elif step[0] == "wait":
            s.mark_waiting("REQ-1")
        elif step[0] == "fin":
            s.finish("REQ-1", step[1], result_text="x")
        elif step[0] == "gate":
            s.gate_reject("REQ-1", "not public")
    with pytest.raises(m.IllegalTransition):
        s.set_state("REQ-1", to)


def test_cas_expect_guard(store):
    m, s = store
    s.create_round("REQ-1", "submit")
    with pytest.raises(m.IllegalTransition):
        s.set_state("REQ-1", m.READY, expect=m.SENDING)  # current is queued, not sending
    s.set_state("REQ-1", m.READY, expect=m.QUEUED)       # correct expectation succeeds
    assert s.get_round("REQ-1")["state"] == m.READY


def test_begin_send_requires_ready(store):
    m, s = store
    s.create_round("REQ-1", "submit")  # still queued
    with pytest.raises(m.IllegalTransition):
        s.begin_send("REQ-1", "b", "h" * 64, daemon_instance_id="d1")


def test_possibly_accepted_from_sending_and_never_dispatchable(store):
    m, s = store
    s.create_round("REQ-1", "submit")
    s.set_state("REQ-1", m.READY)
    aid = s.begin_send("REQ-1", "b", "h" * 64, daemon_instance_id="d1")
    s.mark_possibly_accepted(aid, "unknown_send: RID never observed")
    assert s.get_round("REQ-1")["state"] == m.POSSIBLY_ACCEPTED
    rec = s.recover()
    assert "REQ-1" in rec["uncertain"]
    assert "REQ-1" not in rec["dispatchable"], "possibly_accepted MUST NEVER be dispatchable"


def test_recover_classifies_all_buckets(store):
    m, s = store
    # queued -> dispatchable
    s.create_round("Q", "submit")
    # ready -> dispatchable
    s.create_round("R", "submit"); s.set_state("R", m.READY)
    # accepted -> reattach
    s.create_round("A", "submit"); s.set_state("A", m.READY)
    a_aid = s.begin_send("A", "b", "h" * 64, daemon_instance_id="d1"); s.mark_accepted(a_aid, "conv-a")
    # waiting -> reattach
    s.create_round("W", "submit"); s.set_state("W", m.READY)
    w_aid = s.begin_send("W", "b", "h" * 64, daemon_instance_id="d1"); s.mark_accepted(w_aid, "conv-w")
    s.mark_waiting("W")
    # sending -> uncertain
    s.create_round("S", "submit"); s.set_state("S", m.READY)
    s.begin_send("S", "b", "h" * 64, daemon_instance_id="d1")
    # terminal -> not in any bucket
    s.create_round("D", "submit"); s.set_state("D", m.READY)
    d_aid = s.begin_send("D", "b", "h" * 64, daemon_instance_id="d1"); s.mark_accepted(d_aid, "c")
    s.mark_waiting("D"); s.finish("D", m.COMPLETED_VERIFIED, result_text="x")

    rec = s.recover()
    assert set(rec["dispatchable"]) == {"Q", "R"}
    assert set(rec["reattach"]) == {"A", "W"}
    assert set(rec["uncertain"]) == {"S"}
    for bucket in rec.values():
        assert "D" not in bucket, "terminal rounds are not recovered"


def test_promote_sending_to_uncertain_is_idempotent(store):
    m, s = store
    s.create_round("S", "submit"); s.set_state("S", m.READY)
    s.begin_send("S", "b", "h" * 64, daemon_instance_id="d1")
    moved = s.promote_sending_to_uncertain()
    assert moved == ["S"] and s.get_round("S")["state"] == m.POSSIBLY_ACCEPTED
    assert s.promote_sending_to_uncertain() == [], "no sending rounds left → idempotent"
    # and it is now uncertain, never dispatchable
    assert "S" in s.recover()["uncertain"] and "S" not in s.recover()["dispatchable"]


def test_finish_result_is_one_transaction(store):
    m, s = store
    s.create_round("R", "submit"); s.set_state("R", m.READY)
    aid = s.begin_send("R", "b", "h" * 64, daemon_instance_id="d1"); s.mark_accepted(aid, "c")
    s.mark_waiting("R")
    s.finish("R", m.COMPLETED_UNVERIFIED, result_text="salvaged text")
    r = s.get_round("R")
    # status and result committed together — never a done-state with a missing answer
    assert r["state"] == m.COMPLETED_UNVERIFIED and r["result_text"] == "salvaged text"
    assert r["completion_confidence"] == "unverified"


def test_concurrent_claim_gives_round_to_one(store, tmp_path):
    m, s = store
    s.create_round("ONLY", "submit")
    s2 = m.Store(str(tmp_path / "control.db"))
    try:
        first = s.claim_ready("d1")
        second = s2.claim_ready("d2")
        assert first is not None and first["rid"] == "ONLY" and first["state"] == m.READY
        assert second is None, "a second claimer must not re-claim an already-claimed round"
    finally:
        s2.close()


def test_events_are_appended(store):
    m, s = store
    s.create_round("R", "submit"); s.set_state("R", m.READY)
    s.begin_send("R", "b", "h" * 64, daemon_instance_id="d1")
    kinds = [e["kind"] for e in s.events("R")]
    assert "created" in kinds and "state" in kinds and "begin_send" in kinds
