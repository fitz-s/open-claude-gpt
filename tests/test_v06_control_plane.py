# Control-plane safety regressions from the v0.6 independent review: conversation ordering across
# READY, admission of READY orphans, recovery that cannot send, a wait budget no restart or continue
# can extend, tab-sweep ownership, private materialization temps, and migration under parallel open.
# Real SQLite throughout; the CDP layer and the browser endpoints are fakes.
import importlib.util
import io
import json
import os
import sqlite3
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)
CONV = "6a684b38-bef4-83ea-83d4-134bf9610e05"


def _load(name, filename=None):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, (filename or name) + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def store(tmp_path):
    m = _load("cgc_store")
    s = m.Store(str(tmp_path / "control.db"))
    yield m, s
    s.close()


def _followup(s, sm, rid, state):
    s.create_round(rid, "followup", thread_id=CONV, prompt="continuing this consult")
    if state != sm.QUEUED:
        s.set_state(rid, sm.READY)
    if state == sm.POSSIBLY_ACCEPTED:
        aid = s.begin_send(rid, "p", sm.sha256("p"), daemon_instance_id="t")
        s.mark_possibly_accepted(aid, "unknown send")


# ---- 1. a READY follow-up reserves its conversation ---------------------------

def test_ready_followup_reserves_conversation(store):
    """Two queued follow-ups for one thread, claimed back to back: the first is READY (its worker has
    not reached begin_send yet). The composer lease would only serialize their clicks, so the second
    would send while the first is being answered. It must not be claimable."""
    sm, s = store
    _followup(s, sm, "REQ-20261013-000000-00a001", sm.QUEUED)
    _followup(s, sm, "REQ-20261013-000000-00a002", sm.QUEUED)
    first = s.claim_ready("d1")
    assert first["rid"] == "REQ-20261013-000000-00a001"
    assert s.claim_ready("d1") is None, "a READY follow-up already holds the thread"
    s.finish("REQ-20261013-000000-00a001", sm.FAILED, error_code="x")
    assert s.claim_ready("d1")["rid"] == "REQ-20261013-000000-00a002"


def test_an_uncertain_send_holds_its_thread_until_its_auto_retrieve_has_run(store):
    """possibly_accepted may be generating in the thread right now — a follow-up sent beside it is the
    same overlap. The hold ends once the one-shot auto-retrieve has run, so it is bounded."""
    sm, s = store
    _followup(s, sm, "REQ-20261013-000000-00a003", sm.POSSIBLY_ACCEPTED)
    _followup(s, sm, "REQ-20261013-000000-00a004", sm.QUEUED)
    assert s.claim_ready("d1") is None
    assert s.claim_auto_retrieve("REQ-20261013-000000-00a003")
    # the retrieve found nothing: back to possibly_accepted, marker kept — no longer a holder
    s.set_state("REQ-20261013-000000-00a003", sm.POSSIBLY_ACCEPTED)
    assert s.claim_ready("d1")["rid"] == "REQ-20261013-000000-00a004"


# ---- 7. parallel open of a legacy DB runs each migration once ------------------

def test_parallel_open_runs_each_migration_once(tmp_path):
    """The daemon and a CLI opening an un-migrated DB together: both read the old version outside any
    lock. The loser must re-read it under the migration lock and skip — not re-run an ALTER TABLE that
    then fails with 'duplicate column', and not back up the DB the winner already migrated over the
    original control.db.v4.bak (the backup is write-once and must hold the v4 state)."""
    m = _load("cgc_store")
    p = str(tmp_path / "legacy.db")
    # The v4 shape: today's DDL minus the three columns migration 5 adds.
    head, tail = m._DDL.split("  -- Schema 5.", 1)
    v4 = head.rstrip().rstrip(",") + "\n);" + tail.split(");", 1)[1]
    db = sqlite3.connect(p)
    db.execute("PRAGMA journal_mode=WAL")   # as every Store-written DB already is
    db.executescript(v4)
    db.execute("INSERT INTO meta(key,value) VALUES('schema_version','4')")
    db.commit()
    db.close()

    # Both openers read v4 before either migrates: the first SELECT of the version waits on a barrier.
    gate = threading.Barrier(2, timeout=10)
    real = m.Store._migrate

    def racing(self):
        self.db.set_trace_callback(
            lambda sql: gate.wait() if "SELECT value FROM meta WHERE key='schema_version'" in sql
            and not getattr(self, "_raced", None) and not setattr(self, "_raced", 1) else None)
        try:
            real(self)
        finally:
            self.db.set_trace_callback(None)

    # The worst order for the backup: a second _backup runs only after the first opener has finished
    # migrating (bounded, so a fix that never reaches a second _backup does not stall the test).
    real_backup, backups, first_done = m.Store._backup, [], threading.Event()

    def backup(self, version):
        backups.append(version)
        if len(backups) > 1:
            first_done.wait(10)
        real_backup(self, version)

    m.Store._migrate = racing
    m.Store._backup = backup
    errors = []

    def open_one():
        try:
            m.Store(p).close()
        except Exception as e:      # noqa: BLE001 — the assertion reports it
            errors.append(e)
        finally:
            first_done.set()

    try:
        ts = [threading.Thread(target=open_one) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
    finally:
        m.Store._migrate = real
        m.Store._backup = real_backup
    assert errors == [], errors
    s = m.Store(p)
    assert s.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(m.SCHEMA_VERSION)
    s.close()
    bak = sqlite3.connect(p + ".v4.bak")
    assert bak.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4", \
        "the pre-migration backup was overwritten with a migrated copy"
    bak.close()


def test_a_backup_is_write_once(store, tmp_path):
    sm, s = store
    v = sm.SCHEMA_VERSION
    s._backup(v)
    s.db.execute("UPDATE meta SET value='changed' WHERE key='store_uuid'")
    s._backup(v)
    bak = sqlite3.connect(str(tmp_path / f"control.db.v{v}.bak"))
    assert bak.execute("SELECT value FROM meta WHERE key='store_uuid'").fetchone()[0] != "changed"
    bak.close()


def _make_v4(m, p):
    head, tail = m._DDL.split("  -- Schema 5.", 1)
    db = sqlite3.connect(p)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(head.rstrip().rstrip(",") + "\n);" + tail.split(");", 1)[1])
    db.execute("INSERT INTO meta(key,value) VALUES('schema_version','4')")
    db.commit()
    db.close()


@pytest.mark.parametrize("bad", [b"", b"SQLite format 3\x00" + b"\x00" * 80])
def test_a_crash_left_backup_is_rebuilt_not_trusted(tmp_path, bad):
    """A crash mid-copy leaves a 0-byte or partial .v4.bak. Trusting it by existence would migrate the
    DB with its original backup lost for good. It is renamed aside and rebuilt from the v4 DB first."""
    m = _load("cgc_store")
    p = str(tmp_path / "legacy.db")
    _make_v4(m, p)
    with open(p + ".v4.bak", "wb") as f:
        f.write(bad)
    s = m.Store(p)
    assert s.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(m.SCHEMA_VERSION)
    s.close()
    assert m._valid_backup(p + ".v4.bak", 4), "a valid v4 backup must be in place"
    aside = [n for n in os.listdir(tmp_path) if ".v4.bak.corrupt-" in n]
    assert len(aside) == 1 and open(tmp_path / aside[0], "rb").read() == bad, "kept aside, not deleted"
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


def test_a_failed_backup_refuses_to_migrate(tmp_path, monkeypatch):
    """No verified backup, no migration: the DB stays v4 and nothing half-written takes the final name."""
    m = _load("cgc_store")
    p = str(tmp_path / "legacy.db")
    _make_v4(m, p)
    monkeypatch.setattr(m, "_valid_backup", lambda path, v: False)
    with pytest.raises(sqlite3.DatabaseError):
        m.Store(p)
    assert not os.path.exists(p + ".v4.bak")
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]
    db = sqlite3.connect(p)
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    db.close()


# ---- 2. a READY orphan passes the same admission a fresh claim does ------------

@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CGC_SPOOL_DIR", str(tmp_path / "state" / "spool"))
    spool = _load("cgc_spool")
    d = _load("cgc_daemon")
    d.spool.ensure_dirs()
    spawned = []

    class _P:
        pid = 4321
    monkeypatch.setattr(d.subprocess, "Popen", lambda argv, **k: spawned.append(argv[-2:]) or _P())
    d.spawned = spawned
    return d


@pytest.mark.parametrize("hold", ["probe", "broken"])
def test_probe_hold_applies_to_ready_orphans(daemon, hold):
    """Nothing was sent for a READY round, so a probe holding sends (sends=False) or a capability it
    needs recorded broken must stop it exactly as it stops a fresh claim: back to `queued`, not
    dispatched. A ready retrieve sends nothing and still runs."""
    sm = daemon.store_mod
    with sm.Store() as s:
        s.create_round("REQ-20261013-000000-00b001", "submit", prompt="p")
        s.set_state("REQ-20261013-000000-00b001", sm.READY)
        s.create_round("REQ-20261013-000000-00b002", "retrieve", thread_id=CONV,
                       parent_rid="REQ-20261013-000000-00b001")
        s.set_state("REQ-20261013-000000-00b002", sm.READY)
        if hold == "broken":
            s.mark_broken("composer", phase="probe:paste", cause="test")
    daemon._dispatch_store({}, concurrency=3, daemon_instance_id="d1", sends=hold != "probe")
    assert ["--worker-store", "REQ-20261013-000000-00b001"] not in daemon.spawned
    assert ["--worker-store", "REQ-20261013-000000-00b002"] in daemon.spawned
    with sm.Store() as s:
        assert s.get_round("REQ-20261013-000000-00b001")["state"] == sm.QUEUED
    # once the hold clears it is claimed and dispatched like any queued round
    if hold == "broken":
        with sm.Store() as s:
            s.db.execute("DELETE FROM meta WHERE key LIKE 'ui.%'")
    daemon._dispatch_store({}, concurrency=3, daemon_instance_id="d1", sends=True)
    assert ["--worker-store", "REQ-20261013-000000-00b001"] in daemon.spawned


# ---- 5. the tab sweep closes only tabs positively known to be consult tabs -----

OWNED = "6a630a18-cbcc-83ea-a985-2fcd42a0a173"
HUMAN = "6a111111-2222-83ea-a985-2fcd42a0a173"


def test_sweep_preserves_foreign_and_unknown_targets(daemon, monkeypatch):
    """The browser is the human's too: an idle controller proves no tab is disposable. Closed: only a
    chatgpt.com tab on a conversation the store recorded, or on the landing a fresh consult opens.
    Kept: foreign sites, the human's own ChatGPT chats, look-alike hosts, unreadable URLs."""
    closed = []

    class FakeWS:
        def send(self, m): closed.append(json.loads(m)["params"]["targetId"])
        def close(self): pass

    urls = ["https://chatgpt.com/",                                  # T0, the spare
            "https://mail.google.com/mail/u/0/",                     # T1 foreign
            f"https://chatgpt.com/c/{HUMAN}",                        # T2 the human's own chat
            f"https://chatgpt.com/c/{OWNED}",                        # T3 ours -> closed
            "https://chatgpt.com/",                                  # T4 consult landing -> closed
            f"https://chatgpt.com.evil.example/c/{OWNED}",           # T5 look-alike host
            "",                                                      # T6 unreadable
            "chrome://newtab/"]                                      # T7 browser page

    def urlopen(url, *a, **k):
        if url.endswith("/json/version"):
            return io.BytesIO(json.dumps({"webSocketDebuggerUrl": "ws://x"}).encode())
        return io.BytesIO(json.dumps([{"type": "page", "id": f"T{i}", "url": u}
                                      for i, u in enumerate(urls)]).encode())

    monkeypatch.setattr(daemon.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(daemon.websocket, "create_connection", lambda *a, **k: FakeWS())
    monkeypatch.setenv("CGC_PROJECT_URL", "https://chatgpt.com/")
    with daemon.store_mod.Store() as s:
        s.create_round("REQ-20261013-000000-00c001", "followup", thread_id=OWNED)
    assert daemon._sweep_tabs(set(), []) == 2
    assert closed == ["T3", "T4"]


# ---- 6. concurrent materializations never share a temp -------------------------

def test_concurrent_result_materialization(tmp_path, monkeypatch):
    """The producer and an `await` can write the same answer file at once. Interleave them at the worst
    point — B runs whole while A is between its write and its rename. With one shared `<out>.tmp`, B
    renames A's temp away and A's rename fails (or publishes B's truncation); each writer must own its
    temp, so both complete, the file holds one whole answer, it stays 0600, and no temp is left."""
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path))
    backend = _load("cgc_backend")
    out = str(tmp_path / "answers" / "a.txt")
    real = os.replace
    nested = []

    def replace(src, dst):
        if not nested:
            nested.append(1)
            backend._materialize(out, "B" * 5000)
        real(src, dst)

    monkeypatch.setattr(backend.os, "replace", replace)
    backend._materialize(out, "A" * 5000)
    with open(out) as f:
        assert f.read() == "A" * 5000, "the last rename wins with its own whole content"
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    assert os.listdir(tmp_path / "answers") == ["a.txt"], "no temp left behind"


# ---- 3 / 4. recovery cannot send; no restart or continue extends the budget ----

RID = "REQ-20261013-120000-00d001"
FAILED_WAIT = {"code": 4, "stderr": "CGC_ERROR turn_failed: ChatGPT marked it 'Thinking failed'\n"}


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """A round the send worker already sent and saw accepted: waiting on CONV."""
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path))
    sm = _load("cgc_store")
    spool = _load("cgc_spool")
    backend = _load("cgc_backend")
    monkeypatch.setattr(spool, "acquire_conversation_lease", lambda conv: object())
    s = sm.Store(str(tmp_path / "control.db"))
    s.create_round(RID, "submit", prompt="p", spec_json=json.dumps({"model": "Pro", "timeout": 5400}))
    s.set_state(RID, sm.READY)
    aid = s.begin_send(RID, "p", sm.sha256("p"), daemon_instance_id="d1")
    s.mark_accepted(aid, CONV)
    s.mark_waiting(RID)
    s.aid = aid
    yield sm, backend, s
    s.close()


def _cdp(*steps):
    calls, it = [], iter(steps)

    def cdp(kind, **kw):
        calls.append((kind, kw))
        step = next(it)
        if kind == "wait" and step.get("answer"):
            with open(kw["out"], "w") as f:
                f.write(step["answer"])
            return {"code": 0, "out": kw["out"], "stderr": ""}
        return dict(step)
    cdp.calls = calls
    return cdp


def _backdate(s, seconds):
    import datetime as dt
    t = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat()
    s.db.execute("UPDATE attempts SET started_at=? WHERE attempt_id=?", (t, s.aid))


@pytest.mark.parametrize("state", ["waiting", "accepted", "possibly_accepted"])
def test_recovery_has_no_mutation_capability(sent, state):
    """A reattach after a worker death, and the one-shot auto-retrieve of an uncertain round, both find
    ChatGPT's failed turn. The continue is a new paid message under the original rid — recovery has no
    authority to spend it. Only `wait` is ever called; the round ends FAILED, telling the agent to
    re-fire with a new key."""
    sm, backend, s = sent
    if state == "accepted":
        s.db.execute("UPDATE rounds SET state='accepted' WHERE rid=?", (RID,))
    elif state == "possibly_accepted":
        s.set_state(RID, sm.POSSIBLY_ACCEPTED, error_code="wait died")
    cdp = _cdp(FAILED_WAIT)
    final = backend.resume_round(s, s.get_round(RID), cdp)
    assert [k for k, _ in cdp.calls] == ["wait"], "recovery sent something"
    assert final == sm.FAILED
    err = s.get_round(RID)["error_code"]
    assert "no automatic continue went out" in err and "NEW --request-key" in err
    assert s.auto_continue_state(RID) is None, "the continue was never even claimed"


def test_restart_and_continue_cannot_extend_deadline(sent):
    """The budget belongs to the send (begin_send + timeout), not to each wait. A resume 5000s into a
    5400s budget watches only what is left, instead of a fresh 5400s."""
    sm, backend, s = sent
    _backdate(s, 5000)
    cdp = _cdp({"answer": "late answer"})
    assert backend.resume_round(s, s.get_round(RID), cdp) == sm.COMPLETED_VERIFIED
    assert 0 < cdp.calls[0][1]["timeout"] <= 400


def test_a_continue_rewait_gets_only_the_remainder(sent):
    """The old re-wait floor, max(600, left), watched past the deadline."""
    sm, backend, s = sent
    _backdate(s, 5300)
    cdp = _cdp(FAILED_WAIT, {"code": 0}, {"answer": "a"})
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro", "timeout": 5400}, cdp, may_continue=True)
    assert final == sm.COMPLETED_VERIFIED
    waits = [kw["timeout"] for k, kw in cdp.calls if k == "wait"]
    assert len(waits) == 2 and all(0 < w <= 100 for w in waits), waits


def test_a_resume_past_the_deadline_ends_uncertain_without_watching(sent):
    """At the deadline the episode ends uncertain (retrieve, don't resend) — never 'not sent'."""
    sm, backend, s = sent
    _backdate(s, 6000)
    cdp = _cdp()
    assert backend.resume_round(s, s.get_round(RID), cdp) == sm.POSSIBLY_ACCEPTED
    assert cdp.calls == []
    assert "observation deadline passed" in s.get_round(RID)["error_code"]


def test_the_one_shot_auto_retrieve_gets_one_short_look_past_the_deadline(sent):
    """A wait that ran out its whole budget leaves the round uncertain, and the answer may have finished
    a minute later. The one-shot auto-retrieve exists for exactly that, so it gets ONE idle-bounded look
    of RETRIEVE_IDLE_EXIT_S — once, by its marker — never a fresh budget. A plain reattach gets none."""
    sm, backend, s = sent
    _backdate(s, 6000)
    s.set_state(RID, sm.POSSIBLY_ACCEPTED, error_code="wait produced no answer (exit 4)")
    cdp = _cdp({"answer": "finished just after the waiter gave up"})
    assert backend.resume_round(s, s.get_round(RID), cdp) == sm.COMPLETED_VERIFIED
    (_, kw), = cdp.calls
    assert kw["timeout"] == backend.RETRIEVE_IDLE_EXIT_S and kw["idle_exit"] == backend.RETRIEVE_IDLE_EXIT_S
    assert not s.claim_auto_retrieve(RID), "spent — there is no second look"


def test_no_continue_is_sent_once_the_budget_is_spent(sent, monkeypatch):
    """The first wait used the whole budget and then saw the failed turn: a continue there is a paid
    message with no time left to read its answer. Not sent; the round ends FAILED."""
    sm, backend, s = sent
    _backdate(s, 5390)
    clock = [backend.time.time()]
    monkeypatch.setattr(backend.time, "time", lambda: clock[0])

    def cdp(kind, **kw):
        cdp.calls.append(kind)
        clock[0] += 60                  # the wait ran to its end
        return dict(FAILED_WAIT)
    cdp.calls = []
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro", "timeout": 5400}, cdp,
                               may_continue=True) == sm.FAILED
    assert cdp.calls == ["wait"]


# ---- review round 2 -------------------------------------------------------------

def test_the_driver_never_turns_a_spent_budget_into_a_fresh_one(daemon, monkeypatch):
    """The real _make_run_cdp argv: timeout=0 is a spent budget and must reach the waiter as 0, not as
    the 5400 s default `0 or STUCK_AFTER_S` made of it. Only a missing timeout gets the default."""
    argvs = []
    monkeypatch.setattr(daemon, "_run", lambda cmd, *a, **k: argvs.append(cmd) or (0, "{}", ""))
    run = daemon._make_run_cdp()
    for t in (0, 1, None):
        run("wait", rid=RID, conversation=CONV, out="/x", timeout=t)
    got = [cmd[cmd.index("--timeout") + 1] for cmd in argvs]
    assert got == ["0", "1", str(daemon.spool.STUCK_AFTER_S)]


def test_the_remainder_checked_is_the_remainder_passed(sent, monkeypatch):
    """One clock read per wait. With the clock advancing between two reads, a 1 s check used to pass
    a 0 s argv (which the driver then read as 'no budget')."""
    sm, backend, s = sent
    base = backend.time.time()
    s.db.execute("UPDATE attempts SET started_at=? WHERE attempt_id=?",
                 (__import__("datetime").datetime.fromtimestamp(
                     base - 5400 + 1.5, __import__("datetime").timezone.utc).isoformat(), s.aid))
    ticks = iter([base, base + 1, base + 2, base + 3])
    monkeypatch.setattr(backend.time, "time", lambda: next(ticks))
    cdp = _cdp({"answer": "a"})
    backend._wait_phase(s, RID, CONV, {"model": "Pro", "timeout": 5400}, cdp, may_continue=True)
    assert cdp.calls[0][1]["timeout"] == 1


def test_a_legacy_thread_without_an_address_is_backfilled_and_retrievable(tmp_path):
    """Threads created before create_round stamped conversation_id have it NULL. An uncertain round in
    one had no address, so its one retrieve was never scheduled — and the thread fence held forever.
    Opening the store backfills the address it already implies (thread_id IS the conversation id),
    which schedules the retrieve; after it, the thread frees."""
    sm = _load("cgc_store")
    p = str(tmp_path / "control.db")
    s = sm.Store(p)
    _followup(s, sm, "REQ-20261013-000000-00e001", sm.POSSIBLY_ACCEPTED)
    _followup(s, sm, "REQ-20261013-000000-00e002", sm.QUEUED)
    s.db.execute("UPDATE threads SET conversation_id=NULL")          # the pre-5b363a3 shape
    s.close()
    s = sm.Store(p)
    assert s.conversation_of("REQ-20261013-000000-00e001") == CONV
    assert "REQ-20261013-000000-00e001" in s.recover()["retrievable"]
    assert s.claim_ready("d1") is None, "held while its retrieve is pending"
    assert s.claim_auto_retrieve("REQ-20261013-000000-00e001")
    s.set_state("REQ-20261013-000000-00e001", sm.POSSIBLY_ACCEPTED)
    assert s.claim_ready("d1")["rid"] == "REQ-20261013-000000-00e002"
    s.close()


def test_an_uncertain_round_with_no_recoverable_address_does_not_fence(store):
    """No canonical thread id and no conversation in any spec: no retrieve can ever be scheduled, so
    holding the thread for it would be forever. It does not hold."""
    sm, s = store
    tid = "legacy-thread-1"
    s.db.execute("INSERT INTO threads(thread_id,conversation_id,created_at,updated_at) "
                 "VALUES(?,NULL,'t','t')", (tid,))
    for rid, st in (("REQ-20261013-000000-00e003", sm.POSSIBLY_ACCEPTED),
                    ("REQ-20261013-000000-00e004", sm.QUEUED)):
        s.create_round(rid, "followup", prompt="p")
        s.db.execute("UPDATE rounds SET thread_id=? WHERE rid=?", (tid, rid))
        if st == sm.POSSIBLY_ACCEPTED:
            s.set_state(rid, sm.READY)
            s.mark_possibly_accepted(s.begin_send(rid, "p", sm.sha256("p"), daemon_instance_id="t"), "x")
    s._backfill_thread_addresses()
    assert s.conversation_of("REQ-20261013-000000-00e003") is None
    assert s.claim_ready("d1")["rid"] == "REQ-20261013-000000-00e004"


@pytest.mark.parametrize("where", ["validation", "lease"])
def test_a_continue_cannot_cross_the_observation_deadline(sent, monkeypatch, where):
    """The continue is a paid message whose answer must be watched inside the send's budget. If the
    egress gate or the conversation-lease wait runs past the deadline, nothing is sent."""
    sm, backend, s = sent
    import cgc_spool as sp
    _backdate(s, 5370)                                 # 30 s left — less than the 60 s lease wait
    now = [backend.time.time()]
    start = now[0]
    monkeypatch.setattr(backend.time, "time", lambda: now[0])
    monkeypatch.setattr(backend.time, "sleep", lambda d: now.__setitem__(0, now[0] + d))
    if where == "validation":
        real = sp.validate_prompt

        def slow(prompt):
            now[0] += 200
            return real(prompt)
        monkeypatch.setattr(sp, "validate_prompt", slow)
    else:
        monkeypatch.setattr(sp, "acquire_conversation_lease", lambda conv: None)  # always held
    cdp = _cdp(FAILED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro", "timeout": 5400}, cdp,
                               may_continue=True) == sm.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"], "nothing may be sent past the deadline"
    if where == "lease":
        assert now[0] - start <= 32, "the lease wait must end at the deadline, not after 60 s"


def test_the_continue_click_is_bounded_by_the_observation_deadline(sent):
    """The driver re-checks --not-after at the click; the continue passes the earlier of use-by and the
    observation deadline, so a slow attach/paste cannot click after the deadline."""
    sm, backend, s = sent
    _backdate(s, 5300)
    cdp = _cdp(FAILED_WAIT, {"code": 0}, {"answer": "a"})
    backend._wait_phase(s, RID, CONV, {"model": "Pro", "timeout": 5400}, cdp, may_continue=True)
    import datetime as dt
    na = dt.datetime.fromisoformat(cdp.calls[1][1]["not_after"]).timestamp()
    assert abs(na - s.observation_deadline(RID, 5400)) < 1
