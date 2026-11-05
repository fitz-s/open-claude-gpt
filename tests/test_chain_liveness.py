# Silent-outage chain: dead LaunchAgent interpreter -> every worker spawn fails ENOENT -> a round sits
# `ready` while status, await and doctor say healthy. Each test pins one link. No network/Chrome/launchd.
import datetime
import importlib.util
import os
import plistlib
import re
import sqlite3
import stat
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)

RID = "REQ-20261101-000000-0000a1"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, filename))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CGC_SPOOL_DIR", str(tmp_path / "state" / "spool"))
    sys.modules["cgc_spool"] = _load("cgc_spool", "cgc_spool.py")
    d = _load("cgc_daemon", "cgc_daemon.py")
    d.spool.ensure_dirs()
    clock = [1000.0]
    d.clock = clock
    monkeypatch.setattr(d, "time", types.SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0], sleep=lambda s: None))
    return d


def _round(d, state=None, rid=RID, kind="submit", **kw):
    with d.store_mod.Store() as s:
        s.create_round(rid, kind, prompt="p", **kw)
        if state:
            s.set_state(rid, state)


def _get(d, rid=RID):
    with d.store_mod.Store() as s:
        return s.get_round(rid), [e["kind"] for e in s.events(rid)]


def _decide(d, rid=RID):
    import cgc_backend
    with d.store_mod.Store() as s:
        return cgc_backend.decide(s, s.get_round(rid), daemon_up=True)


def _age(d, seconds, rid=RID):
    ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)).isoformat()
    with d.store_mod.Store() as s:
        s.db.execute("UPDATE rounds SET updated_at=? WHERE rid=?", (ts, rid))


# ---- 1. a spawn that can never succeed ends the round ----

def _enoent(*a, **k):
    raise FileNotFoundError(2, "No such file or directory", "/gone/python3")


def test_a_spawn_that_always_fails_ends_with_spawn_failed_not_a_silent_spin(daemon, monkeypatch):
    sm = daemon.store_mod
    _round(daemon, sm.READY)
    calls = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: calls.append(1) or _enoent())
    for _ in range(40):
        daemon._dispatch_store({}, 3, "d1")
        daemon.clock[0] += 70                 # past the longest backoff
    r, events = _get(daemon)
    assert r["state"] == sm.FAILED
    assert r["error_code"].startswith("spawn_failed:") and "/gone/python3" in r["error_code"]
    assert len(calls) == daemon._SPAWN_MAX, "capped attempts, not one per poll forever"
    assert "spawn_failed" in events
    assert not daemon._SPAWNS


def test_failed_spawns_back_off_instead_of_retrying_every_poll(daemon, monkeypatch):
    sm = daemon.store_mod
    _round(daemon, sm.READY)
    calls = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: calls.append(1) or _enoent())
    for _ in range(10):                       # no clock advance
        daemon._dispatch_store({}, 3, "d1")
    assert len(calls) == 1
    daemon.clock[0] += daemon._SPAWN_BACKOFF_S + 0.1
    daemon._dispatch_store({}, 3, "d1")
    assert len(calls) == 2
    assert _get(daemon)[0]["state"] == sm.READY


def test_a_successful_spawn_resets_the_failure_count(daemon, monkeypatch):
    sm = daemon.store_mod
    _round(daemon, sm.READY)
    outcome = [_enoent, _enoent, None]

    class _P:
        pid = 1

    def popen(*a, **k):
        f = outcome.pop(0)
        return f() if f else _P()
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    for _ in range(3):
        daemon._dispatch_store({}, 3, "d1")
        daemon.clock[0] += 70
    assert RID not in daemon._SPAWNS


def test_a_failed_resume_spawn_never_fails_a_round_that_already_sent(daemon, monkeypatch):
    sm = daemon.store_mod
    _round(daemon)
    with sm.Store() as s:
        s.set_state(RID, sm.READY)
        s.begin_send(RID, "p", sm.sha256("p"), daemon_instance_id="d1")
        aid = s.get_round(RID)["current_attempt_id"]
        s.mark_accepted(aid, "c" * 36)
    monkeypatch.setattr(daemon.subprocess, "Popen", _enoent)
    for _ in range(20):
        daemon._dispatch_store({}, 3, "d1")
        daemon.clock[0] += 70
    r, events = _get(daemon)
    assert r["state"] in (sm.ACCEPTED, sm.WAITING), "post-send: never failed"
    assert events.count("spawn_failed") == daemon._SPAWN_MAX


def test_decide_names_a_spawn_failure_for_the_awaiter(daemon, monkeypatch):
    sm = daemon.store_mod
    _round(daemon, sm.READY)
    monkeypatch.setattr(daemon.subprocess, "Popen", _enoent)
    for _ in range(daemon._SPAWN_MAX):
        daemon._dispatch_store({}, 3, "d1")
        daemon.clock[0] += 70
    d = _decide(daemon)
    assert d["code"] == "service_unavailable" and d["retryable"] is True
    assert "refire" in d["next_command"] and "interpreter" in d["human_action"]


# ---- 1b. a held ready-orphan is parked, not re-claimed per poll ----

def test_a_held_ready_orphan_is_parked_with_a_not_before(daemon, monkeypatch):
    sm = daemon.store_mod
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: pytest.fail("a held round must not spawn"))
    _round(daemon, sm.READY)
    with sm.Store() as s:
        s.mark_broken("composer", phase="probe:paste", cause="selector drift")
    for _ in range(5):
        daemon._dispatch_store({}, 3, "d1")
    r, events = _get(daemon)
    assert r["state"] == sm.QUEUED and r["not_before"]
    assert events.count("claimed") == 0 and sum(1 for e in events if e == "state") == 2   # ready, queued


# ---- 1c. a failed spawn must not drain the queue; transient errnos never close a round ----

def test_a_failed_spawn_ends_the_claim_loop_instead_of_claiming_the_whole_queue(daemon, monkeypatch):
    sm = daemon.store_mod
    rids = [f"REQ-20261101-000000-0000b{i}" for i in range(8)]
    for rid in rids:
        _round(daemon, rid=rid)
    monkeypatch.setattr(daemon.subprocess, "Popen", _enoent)
    daemon._dispatch_store({}, 3, "d1")
    with sm.Store() as s:
        states = [s.get_round(r)["state"] for r in rids]
    assert states.count(sm.READY) == 1 and states.count(sm.QUEUED) == 7, states


def test_every_round_of_a_permanent_failure_still_ends_but_one_at_a_time(daemon, monkeypatch):
    sm = daemon.store_mod
    rids = [f"REQ-20261101-000000-0000c{i}" for i in range(4)]
    for rid in rids:
        _round(daemon, rid=rid)
    monkeypatch.setattr(daemon.subprocess, "Popen", _enoent)
    for _ in range(60):
        daemon._dispatch_store({}, 3, "d1")
        daemon.clock[0] += 70
    with sm.Store() as s:
        got = [(s.get_round(r)["state"], s.get_round(r)["error_code"] or "") for r in rids]
    assert all(st == sm.FAILED and ec.startswith("spawn_failed:") for st, ec in got), got


@pytest.mark.parametrize("err,closes", [
    (2, True), (13, True), (8, True), (21, True),            # ENOENT EACCES ENOEXEC EISDIR
    (24, False), (11, False), (12, False),                   # EMFILE EAGAIN ENOMEM
])
def test_only_errnos_that_name_the_executable_can_close_a_round(daemon, monkeypatch, err, closes):
    sm = daemon.store_mod
    _round(daemon, sm.READY)

    def boom(*a, **k):
        raise OSError(err, os.strerror(err))
    monkeypatch.setattr(daemon.subprocess, "Popen", boom)
    for _ in range(daemon._SPAWN_MAX + 3):
        daemon._dispatch_store({}, 3, "d1")
        daemon.clock[0] += 70
    state = _get(daemon)[0]["state"]
    assert (state == sm.FAILED) is closes, state


# ---- 2. interpreter self-test + identity ----

def test_a_daemon_whose_interpreter_is_missing_exits_nonzero_before_the_heartbeat(daemon, monkeypatch, capsys):
    monkeypatch.setattr(daemon.sys, "executable", "/gone/python3")
    beats = []
    monkeypatch.setattr(daemon.spool, "heartbeat_write", lambda *a, **k: beats.append(a))
    assert daemon.run_loop(2.0, 3, once=True) == daemon.EXIT_NO_INTERPRETER != 0
    assert not beats
    assert "FATAL" in capsys.readouterr().err


def test_a_non_executable_interpreter_is_a_fault_too(daemon, tmp_path, monkeypatch):
    f = tmp_path / "python3"
    f.write_text("#!/bin/sh\n")
    monkeypatch.setattr(daemon.sys, "executable", str(f))
    assert daemon._interpreter_fault()
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    assert daemon._interpreter_fault() is None


def test_the_heartbeat_identity_names_the_interpreter(daemon, monkeypatch):
    beats = []
    monkeypatch.setattr(daemon.spool, "heartbeat_write", lambda pid=None, identity=None: beats.append(identity))
    monkeypatch.setattr(daemon, "_tend_probe", lambda p, t: (None, t))
    monkeypatch.setattr(daemon, "_claimable", lambda: False)
    monkeypatch.setattr(daemon, "_maintenance_pass", lambda children, **k: (0, 0, False, False, 0))
    assert daemon.run_loop(0.0, 3, once=True) == 0
    assert any(b and b.get("executable") == sys.executable for b in beats)


# ---- 3. decide() / status do not call a stalled round healthy ----

def test_a_stale_ready_round_with_no_attempt_is_not_reported_ok(daemon):
    sm = daemon.store_mod
    _round(daemon, sm.READY)
    assert _decide(daemon)["code"] == "ok"   # fresh
    _age(daemon, 40 * 60)
    d = _decide(daemon)
    assert d["code"] == "service_unavailable" and d["retryable"] is True
    assert "diagnose" in d["next_command"] and "40 min" in d["human_action"]


def test_a_stale_queued_round_is_stalled_while_others_run_unless_every_slot_is_taken(daemon):
    sm = daemon.store_mod
    _round(daemon)                                            # queued
    _age(daemon, 20 * 60)
    for i in range(2):                                        # two of the three slots busy
        _round(daemon, sm.READY, rid=f"REQ-20261101-000000-0000d{i}")
    assert _decide(daemon)["code"] == "service_unavailable"
    _round(daemon, sm.READY, rid="REQ-20261101-000000-0000d9")     # now the house is full: a wait
    assert _decide(daemon)["code"] == "ok"


def test_a_stale_queued_followup_behind_a_live_round_of_its_thread_is_a_wait(daemon):
    sm = daemon.store_mod
    conv = "c" * 36
    _round(daemon, rid="REQ-20261101-000000-0000e1", kind="followup", thread_id=conv)
    _round(daemon, sm.READY, rid="REQ-20261101-000000-0000e2", kind="followup", thread_id=conv)
    _age(daemon, 20 * 60, rid="REQ-20261101-000000-0000e1")
    assert _decide(daemon, "REQ-20261101-000000-0000e1")["code"] == "ok"


def test_a_stale_queued_retrieve_behind_a_live_round_of_its_thread_is_still_stalled(daemon):
    # claim_ready's fence is follow-up only, so a live sibling never excuses an undispatched retrieve
    sm = daemon.store_mod
    conv = "f" * 36
    _round(daemon, rid="REQ-20261101-000000-0000f1", kind="retrieve", thread_id=conv)
    _round(daemon, sm.READY, rid="REQ-20261101-000000-0000f2", kind="followup", thread_id=conv)
    _age(daemon, 20 * 60, rid="REQ-20261101-000000-0000f1")
    assert _decide(daemon, "REQ-20261101-000000-0000f1")["code"] == "service_unavailable"


def test_a_ready_round_whose_worker_holds_its_lease_is_progressing_not_stalled(daemon):
    sm = daemon.store_mod
    doc = _load("cgc_doctor_cl3", "cgc_doctor.py")
    _round(daemon, sm.READY)
    _age(daemon, 40 * 60)
    lease = daemon.spool.acquire_rid_lease(RID)               # a worker mid-gate
    assert lease is not None
    try:
        assert _decide(daemon)["code"] == "ok"
        r = doc.Report()
        doc.check_oldest_open_round(r)
        assert r.rows[0][0] == "pass", r.rows
    finally:
        lease.close()
    assert _decide(daemon)["code"] == "service_unavailable"   # worker gone: stalled
    r = doc.Report()
    doc.check_oldest_open_round(r)
    assert r.rows[0][0] == "fail"


def test_a_round_that_has_sent_is_never_a_stalled_presend(daemon):
    sm = daemon.store_mod
    _round(daemon, sm.READY)
    with sm.Store() as s:
        s.begin_send(RID, "p", sm.sha256("p"), daemon_instance_id="d1")
        s.mark_accepted(s.get_round(RID)["current_attempt_id"], "c" * 36)
    _age(daemon, 3600)
    assert _decide(daemon)["code"] == "ok"


def test_status_exposes_age_in_state(daemon, capsys):
    import cgc_backend
    _round(daemon, daemon.store_mod.READY)
    _age(daemon, 125)
    cgc_backend.store_status(True, RID)
    out = capsys.readouterr().out
    m = re.search(r'"age_in_state": (\d+)', out)
    assert m and 120 <= int(m.group(1)) <= 140, out


# ---- 4. doctor ----

def _plist(path, argv0):
    with open(path, "wb") as f:
        plistlib.dump({"Label": "x", "ProgramArguments": [argv0, "d.py"]}, f)


def test_doctor_fails_a_plist_whose_interpreter_is_gone(tmp_path):
    doc = _load("cgc_doctor_cl", "cgc_doctor.py")
    p = tmp_path / "a.plist"
    _plist(p, "/gone/miniconda3/bin/python3")
    r = doc.Report()
    doc.check_launch_interpreter(r, str(p))
    (level, name, detail, fix), = r.rows
    assert level == "fail" and "/gone/miniconda3/bin/python3" in detail and not r.can_send
    _plist(p, sys.executable)
    r = doc.Report()
    doc.check_launch_interpreter(r, str(p))
    assert r.rows[0][0] == "pass" and sys.executable in r.rows[0][2]
    r = doc.Report()
    doc.check_launch_interpreter(r, str(tmp_path / "absent.plist"))
    assert r.rows == [], "no plist is not an error"


def test_doctor_reports_the_oldest_stalled_round(daemon):
    doc = _load("cgc_doctor_cl2", "cgc_doctor.py")
    sm = daemon.store_mod
    r = doc.Report()
    doc.check_oldest_open_round(r)
    assert r.rows == [], "no store yet"
    _round(daemon, sm.READY)
    r = doc.Report()
    doc.check_oldest_open_round(r)
    assert r.rows[0][0] == "pass"
    _age(daemon, 40 * 60)
    r = doc.Report()
    doc.check_oldest_open_round(r)
    level, _, detail, _ = r.rows[0]
    assert level == "fail" and RID in detail and "40 min" in detail and not r.can_send


# ---- 5. install-daemon ----

def test_install_daemon_refuses_an_interpreter_that_is_not_executable(sb, tmp_path):
    r = sb.cgc("install-daemon", CGC_PYTHON=str(tmp_path / "gone" / "python3"))
    assert r.returncode == 2 and "executable" in r.stderr
    assert not sb.plist.exists() and sb.calls() == []


def test_install_daemon_refuses_an_interpreter_inside_a_conda_tree(sb, tmp_path):
    py = tmp_path / "miniconda3" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    r = sb.cgc("install-daemon", CGC_PYTHON=str(py), CGC_ALLOW_UNSTABLE_PYTHON="0")
    assert r.returncode == 2 and "refusing" in r.stderr and not sb.plist.exists()


def test_install_daemon_writes_a_throttle_a_stable_interpreter_and_a_log_beside_the_state(sb):
    r = sb.cgc("install-daemon")
    assert r.returncode == 0, r.stdout + r.stderr
    with open(sb.plist, "rb") as f:
        pl = plistlib.load(f)
    assert pl["KeepAlive"] is True, "a singleton loser exits 0 and must be respawned"
    assert pl["ThrottleInterval"] >= 10
    py = pl["ProgramArguments"][0]
    assert os.path.isabs(py) and os.access(py, os.X_OK)
    assert pl["StandardOutPath"] == pl["StandardErrorPath"] == str(sb.state / "daemon.log")


def test_the_default_daemon_log_is_not_under_tmp():
    line = next(ln for ln in open(os.path.join(ROOT, "bin", "cgc")) if ln.startswith("DAEMON_LOG="))

    def log(**env):
        return subprocess.run(["bash", "-c", line + 'printf %s "$DAEMON_LOG"'], capture_output=True, text=True,
                              env={"HOME": "/h", "PATH": os.environ["PATH"], **env}).stdout
    assert log() == "/h/.local/state/cgc/daemon.log"
    assert log(CGC_STATE_DIR="/mine") == "/mine/daemon.log"


# ---- 6. the probe defers when the browser is down ----

def test_probe_defers_with_one_line_when_the_browser_is_unreachable(tmp_path, monkeypatch, capsys):
    sys.modules.pop("cgc_probe", None)
    probe = _load("cgc_probe_cl", "cgc_probe.py")
    monkeypatch.setattr(probe, "_browser_up", lambda: False)
    monkeypatch.setattr(probe, "_open", lambda url: pytest.fail("no tab may be opened with no browser"))
    monkeypatch.setattr(probe.cdp, "CGC_MODEL", "skip", raising=False)
    report = probe.run_probe(only="composer")
    assert report and all(v["verdict"] == "transient" and v["phase"] == "deferred" and not v["recorded"]
                          for v in report.values())
    with probe.store_mod.Store() as s:
        assert s.capabilities() == {} and s.broken_capabilities() == {}
    probe.print_states(report)
    err = capsys.readouterr().err
    assert err.count("CGC_PROBE deferred") == 1 and "new_tab_failed" not in err
    assert not re.search(r"CGC_PROBE composer:", err)


def test_a_deferred_probe_still_says_how_to_name_an_app(tmp_path, monkeypatch):
    sys.modules.pop("cgc_probe", None)
    probe = _load("cgc_probe_cl", "cgc_probe.py")
    monkeypatch.setenv("CGC_APPS", "")
    monkeypatch.setattr(probe, "_browser_up", lambda: False)
    monkeypatch.setattr(probe, "_open", lambda url: pytest.fail("no tab may be opened with no browser"))
    report = probe.run_probe()
    assert report["mention"]["verdict"] == "unestablished"
    assert all(v["phase"] == "deferred" for c, v in report.items() if c != "mention")
