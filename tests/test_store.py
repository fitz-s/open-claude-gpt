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
    assert s.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(m.SCHEMA_VERSION)
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


def test_retrievable_bucket_only_conversation_bearing_possibly_accepted(store):
    """A possibly_accepted round is auto-retrievable (read-only, once) ONLY if it has a resolvable
    conversation — with an address the rid-sentinel wait can recover it safely; without one there is
    nothing to attach to and it must stay a human's job. And never after its one bounded attempt."""
    m, s = store
    # possibly_accepted WITH a conversation → retrievable (and still uncertain).
    s.create_round("P", "submit"); s.set_state("P", m.READY)
    p_aid = s.begin_send("P", "b", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(p_aid, "conv-p"); s.mark_waiting("P")
    s.set_state("P", m.POSSIBLY_ACCEPTED, error_code="wait died")
    # possibly_accepted with NO conversation → uncertain only, never retrievable.
    s.create_round("N", "submit"); s.set_state("N", m.READY)
    n_aid = s.begin_send("N", "b", "h" * 64, daemon_instance_id="d1")
    s.mark_possibly_accepted(n_aid, "submit gave no conversation")

    rec = s.recover()
    assert set(rec["uncertain"]) == {"P", "N"}
    assert rec["retrievable"] == ["P"], "only the conversation-bearing one is auto-retrievable"

    # Claiming the one-shot auto-retrieve is ATOMIC: it records the marker AND moves P to waiting in
    # one transaction (so a crash can't leave the marker without the transition, stranding recovery).
    assert s.claim_auto_retrieve("P") is True
    assert s.get_round("P")["state"] == m.WAITING
    assert "P" not in s.recover()["retrievable"], "one bounded attempt only"
    assert s.claim_auto_retrieve("P") is False, "second claim loses — already consumed"


def test_terminals_are_immutable(store):
    """A completed/blocked/failed round is FINAL. A second (racing) callback must not overwrite its
    result — the diff-review's worker-overlap 'answer replacement' path, which a same-state write
    would otherwise slip past the transition table."""
    m, s = store
    s.create_round("T", "submit"); s.set_state("T", m.READY)
    aid = s.begin_send("T", "b", "h" * 64, daemon_instance_id="d1")
    s.mark_accepted(aid, "c"); s.mark_waiting("T")
    s.finish("T", m.COMPLETED_VERIFIED, result_text="the real answer")
    with pytest.raises(m.IllegalTransition):
        s.finish("T", m.COMPLETED_VERIFIED, result_text="OVERWRITE")   # same-state overwrite
    with pytest.raises(m.IllegalTransition):
        s.set_state("T", m.FAILED)                                     # different terminal
    assert s.get_round("T")["result_text"] == "the real answer", "terminal result is immutable"


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


class TestLegacyRelocation:
    """The one-time /tmp→durable relocation of the store DB."""

    def _mk_legacy(self, tmp_path, monkeypatch):
        import importlib, cgc_store
        importlib.reload(cgc_store)
        state = tmp_path / "state"
        state.mkdir()
        monkeypatch.setenv("CGC_STATE_DIR", str(state))
        monkeypatch.setenv("CGC_DATA_DIR", str(tmp_path / "data"))
        monkeypatch.delenv("CGC_STORE_DB", raising=False)
        legacy = state / "control.db"
        s = cgc_store.Store(str(legacy))
        s.create_round("REQ-RELOC-1", "submit", prompt="p")
        s.close()
        return cgc_store, legacy

    def test_relocates_when_default_path_used(self, tmp_path, monkeypatch):
        cgc_store, legacy = self._mk_legacy(tmp_path, monkeypatch)
        s = cgc_store.Store()          # no explicit path → default → triggers relocation
        assert s.path == str(tmp_path / "data" / "control.db")
        assert s.get_round("REQ-RELOC-1") is not None          # data survived the move
        s.close()
        assert not legacy.exists()                             # never a second live authority
        assert (tmp_path / "state" / "control.db.migrated").exists()

    def test_no_relocation_with_explicit_override(self, tmp_path, monkeypatch):
        cgc_store, legacy = self._mk_legacy(tmp_path, monkeypatch)
        override = tmp_path / "elsewhere.db"
        monkeypatch.setenv("CGC_STORE_DB", str(override))
        s = cgc_store.Store()
        s.close()
        assert legacy.exists()                                 # untouched


class TestSchemaMigrations:
    """Ordered transactional migrations: v1 → v2 with a pre-migration backup; newer-schema refusal."""

    _V1_DDL = """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE threads (thread_id TEXT PRIMARY KEY, conversation_id TEXT, model TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE rounds (rid TEXT PRIMARY KEY, thread_id TEXT REFERENCES threads(thread_id),
      kind TEXT NOT NULL, source_mode TEXT, spec_json TEXT, rendered_prompt TEXT,
      prompt_sha256 TEXT, state TEXT NOT NULL, result_text TEXT, error_code TEXT,
      completion_confidence TEXT, current_attempt_id TEXT, out_path TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      schema_version INTEGER NOT NULL DEFAULT 1);
    CREATE INDEX idx_rounds_state ON rounds(state);
    CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, rid TEXT NOT NULL REFERENCES rounds(rid),
      daemon_instance_id TEXT, browser_epoch INTEGER, phase TEXT NOT NULL,
      started_at TEXT NOT NULL, last_progress_at TEXT, remote_evidence_json TEXT);
    CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, rid TEXT, attempt_id TEXT,
      kind TEXT NOT NULL, detail TEXT, at TEXT NOT NULL);
    """

    def _make_v1(self, path):
        import sqlite3 as _sq
        db = _sq.connect(path)
        db.executescript(self._V1_DDL)
        db.execute("INSERT INTO meta(key,value) VALUES('schema_version','1')")
        db.execute("INSERT INTO rounds(rid,kind,state,created_at,updated_at) "
                   "VALUES('REQ-MIG-1','submit','queued','t','t')")
        db.commit()
        db.close()

    def test_v1_db_migrates_to_current_with_backup(self, tmp_path):
        m = _load(tmp_path / "unused.db")
        p = str(tmp_path / "old.db")
        self._make_v1(p)
        s = m.Store(p)
        assert s.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == \
            str(m.SCHEMA_VERSION)
        # migrated columns exist and old data survived
        r = s.get_round("REQ-MIG-1")
        assert r is not None and "request_key" in r and "parent_rid" in r
        s.close()
        import os as _os
        assert _os.path.exists(p + ".v1.bak"), "a pre-migration backup must exist"

    def test_request_key_unique_after_migration(self, tmp_path):
        m = _load(tmp_path / "unused.db")
        p = str(tmp_path / "old2.db")
        self._make_v1(p)
        s = m.Store(p)
        s.db.execute("UPDATE rounds SET request_key='k1' WHERE rid='REQ-MIG-1'")
        s.create_round("REQ-MIG-2", "submit", prompt="p")
        import sqlite3 as _sq
        with pytest.raises(_sq.IntegrityError):
            s.db.execute("UPDATE rounds SET request_key='k1' WHERE rid='REQ-MIG-2'")
        s.close()

    def test_newer_schema_is_refused(self, tmp_path):
        p = str(tmp_path / "future.db")
        self._make_v1(p)
        import sqlite3 as _sq
        db = _sq.connect(p)
        db.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
        db.commit(); db.close()
        m = _load(tmp_path / "unused.db")
        with pytest.raises(m.SchemaTooNew):
            m.Store(p)
