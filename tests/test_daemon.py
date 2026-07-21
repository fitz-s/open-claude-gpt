# Tests for the consult egress daemon (cgc_daemon.py): run_worker's gate-then-send flow and
# _finish_from_wait's exit-code -> terminal-status mapping.
#
# cgc_daemon.py does `import cgc_spool as spool` internally after inserting its own directory on
# sys.path. Both modules read env (SPOOL_DIR etc.) at import time, so we load cgc_spool FIRST
# under the test's tmp_path env, register it in sys.modules under the name "cgc_spool", then load
# cgc_daemon by path so its `import cgc_spool as spool` resolves to that same already-configured
# instance instead of re-importing (and re-reading env) on its own.
#
# No network, no real Chrome/cdp_consult: cgc_spool.validate_prompt and cgc_daemon._run are always
# monkeypatched.
import importlib.util
import json
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, filename))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CGC_SPOOL_DIR", str(tmp_path / "state" / "spool"))
    monkeypatch.delenv("CGC_GATE_ALLOW_GIST", raising=False)

    spool = _load_module("cgc_spool", "cgc_spool.py")
    sys.modules["cgc_spool"] = spool  # so cgc_daemon's `import cgc_spool as spool` reuses this instance

    d = _load_module("cgc_daemon", "cgc_daemon.py")
    d.spool.ensure_dirs()
    return d


def _rid(suffix="000001"):
    return f"REQ-20260707-120000-{suffix}"


def _seed_job(daemon, rid, prompt_text="please review https://github.com/acme/widgets", kind="submit", out=None):
    prompt_file = daemon.spool.SPOOL_DIR + f"/prompt_{rid}.txt"
    os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(prompt_text)
    job = {
        "rid": rid,
        "kind": kind,
        "prompt_file": prompt_file,
        "out": out or (daemon.spool.CGC_STATE_DIR + f"/answer_{rid}.txt"),
        "poll": 1,
        "timeout": 5,
        "conversation": "auto",
        "model": "Pro",
        "project_url": "https://chatgpt.com/",
    }
    daemon.spool.enqueue_job(job)
    return daemon.spool.claim(daemon.spool.pending_path(rid))


# ---- run_worker: gate refusal --------------------------------------------------

def test_run_worker_refused_by_gate_returns_2_sets_error_status_never_calls_run(daemon, monkeypatch):
    rid = _rid()
    processing_file = _seed_job(daemon, rid)

    monkeypatch.setattr(daemon.spool, "validate_prompt", lambda text: (False, "nope"))

    def _boom(cmd, timeout, rid=None, stdin_text=None):
        raise AssertionError("_run must not be called when the gate refuses")

    monkeypatch.setattr(daemon, "_run", _boom)

    code = daemon.run_worker(processing_file)

    assert code == 2
    st = daemon.spool.read_status(rid)
    assert st["state"] == "error"
    assert os.path.exists(daemon.spool.done_path(rid))
    assert not os.path.exists(processing_file)


# ---- run_worker: gate passes, submit + wait succeed ---------------------------

def test_run_worker_submit_kind_success_sets_done_status_and_conversation(daemon, monkeypatch):
    rid = _rid()
    processing_file = _seed_job(daemon, rid, kind="submit")

    monkeypatch.setattr(daemon.spool, "validate_prompt", lambda text: (True, "ok"))

    calls = []

    def _fake_run(cmd, timeout, rid=None, stdin_text=None):
        calls.append(cmd)
        if len(calls) == 1:
            # submit call
            return 0, json.dumps({"conversation_id": "conv123"}), ""
        # wait call
        return 0, "", ""

    monkeypatch.setattr(daemon, "_run", _fake_run)

    code = daemon.run_worker(processing_file)

    assert code == 0
    assert len(calls) == 2
    st = daemon.spool.read_status(rid)
    assert st["state"] == "done"
    assert st["conversation"] == "conv123"


# ---- _finish_from_wait: exit-code mapping --------------------------------------

@pytest.mark.parametrize("code,expected_state,expected_return", [
    (0, "done", 0),
    (3, "blocker", 3),
    (4, "no_answer", 4),
    (7, "error", 2),
])
def test_finish_from_wait_maps_exit_code_to_terminal_status(daemon, code, expected_state, expected_return):
    rid = _rid()
    daemon.spool.enqueue_job({"rid": rid})
    daemon.spool.claim(daemon.spool.pending_path(rid))

    result = daemon._finish_from_wait(rid, code, "/tmp/out.txt", "some stderr", "conv1")

    assert result == expected_return
    st = daemon.spool.read_status(rid)
    assert st["state"] == expected_state


# ---- orphan recovery: a claimed job with no worker must not vanish ----

def _job(daemon, rid, age_s, **extra):
    """Put a job in processing/ as if a previous daemon had claimed it `age_s` ago."""
    import os
    import time
    path = daemon.spool.processing_path(rid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = {"rid": rid, "kind": "submit", "out": f"/tmp/{rid}.txt"}
    body.update(extra)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f)
    ts = time.time() - age_s
    os.utime(path, (ts, ts))
    return path


def test_a_dead_worker_is_detected_immediately_not_after_an_hour(daemon):
    """Orphanhood is a fact, not a timer. The dispatcher records the worker pid, so a dead worker is
    known at once — the old age rule had to outwait any possible worker, which once the deadline
    became a single 60-minute number meant a consult sat dead for 62 minutes before anything looked."""
    rid = "REQ-20260707-120000-0000a1"
    _job(daemon, rid, age_s=5)                       # young: the age rule would have skipped it
    daemon.spool.write_status(rid, "processing", worker_pid=2 ** 22)  # certainly not running
    assert daemon._recover_orphans() == 1
    assert not os.path.exists(daemon.spool.processing_path(rid))


def test_a_live_worker_is_never_touched(daemon):
    """The guarantee that matters: never recover a job someone is still working, or the same consult
    is sent twice and the quota billed twice."""
    rid = "REQ-20260707-120000-0000a2"
    _job(daemon, rid, age_s=daemon.spool.STUCK_AFTER_S + 9999)  # ancient — age rule would requeue
    daemon.spool.write_status(rid, "processing", worker_pid=os.getpid())  # but its worker is alive
    assert daemon._recover_orphans() == 0
    assert os.path.exists(daemon.spool.processing_path(rid))


def test_orphan_with_a_live_conversation_is_retrieved_not_resent(daemon):
    """The waiter dying does not stop ChatGPT. Re-sending would open a SECOND conversation, redo the
    round and bill it twice; attaching reads the answer that is already being written."""
    rid = "REQ-20260707-120000-0000a3"
    _job(daemon, rid, age_s=5)
    daemon.spool.write_status(rid, "processing", worker_pid=2 ** 22, conversation="conv-abc")
    assert daemon._recover_orphans() == 1
    queued = daemon.spool._read_json(daemon.spool.pending_path(rid))
    assert queued["kind"] == "retrieve" and queued["conversation"] == "conv-abc"


def test_orphan_without_a_conversation_is_resent(daemon):
    """Nothing to attach to — it never got that far — so re-sending is the only recovery."""
    rid = "REQ-20260707-120000-0000a4"
    _job(daemon, rid, age_s=5)
    daemon.spool.write_status(rid, "processing", worker_pid=2 ** 22)
    assert daemon._recover_orphans() == 1
    assert daemon.spool._read_json(daemon.spool.pending_path(rid))["kind"] == "submit"


def test_a_pre_pid_job_still_falls_back_to_the_age_rule(daemon):
    """Jobs claimed by an older daemon carry no pid; they must still be recoverable."""
    rid = "REQ-20260707-120000-0000a5"
    _job(daemon, rid, age_s=daemon.spool.STUCK_AFTER_S + 600)
    assert daemon._recover_orphans() == 1


# ---- kind=retrieve: recovery must stay on the daemon path --------------------

def test_retrieve_job_sends_nothing_and_skips_the_gate(daemon, monkeypatch, tmp_path):
    """Retrieval reads an existing conversation. There is no outbound payload, so there is nothing
    for the gate to validate — and validate_prompt must not even be reached (it would need a prompt
    file that a retrieve job deliberately does not have)."""
    called = []
    monkeypatch.setattr(daemon.spool, "validate_prompt",
                        lambda t: called.append(t) or (True, "should not run"))
    seen = {}

    def _fake_run(cmd, timeout, rid=None, stdin_text=None):
        seen["cmd"] = cmd
        return 0, "", ""

    monkeypatch.setattr(daemon, "_run", _fake_run)
    rid = "REQ-20260707-120000-00re01"
    out = str(tmp_path / "a.txt")
    path = daemon.spool.processing_path(rid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rid": rid, "kind": "retrieve", "conversation": "conv-123", "out": out}, f)

    assert daemon.run_worker(path) == 0
    assert called == [], "the gate must not run for a job that sends nothing"
    assert "wait" in seen["cmd"], "retrieve must run wait, never submit/followup"
    assert "submit" not in seen["cmd"] and "followup" not in seen["cmd"]
    assert daemon.spool.read_status(rid)["state"] == "done"


def test_retrieve_job_carrying_a_prompt_is_refused(daemon, monkeypatch, tmp_path):
    """The gate is skipped only because a retrieve job structurally cannot carry content. Enforce
    that, or 'retrieve' becomes a way to send unvalidated text."""
    def _boom(cmd, timeout, rid=None, stdin_text=None):
        raise AssertionError("must not run anything")

    monkeypatch.setattr(daemon, "_run", _boom)
    rid = "REQ-20260707-120000-00re02"
    path = daemon.spool.processing_path(rid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rid": rid, "kind": "retrieve", "conversation": "c1",
                   "prompt_file": str(tmp_path / "p.md"), "out": str(tmp_path / "a.txt")}, f)

    assert daemon.run_worker(path) == 2
    assert "must carry no prompt" in daemon.spool.read_status(rid)["msg"]


def test_retrieve_job_without_a_conversation_is_refused(daemon, monkeypatch, tmp_path):
    def _boom(cmd, timeout, rid=None, stdin_text=None):
        raise AssertionError("must not run anything")

    monkeypatch.setattr(daemon, "_run", _boom)
    rid = "REQ-20260707-120000-00re03"
    path = daemon.spool.processing_path(rid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rid": rid, "kind": "retrieve", "conversation": "auto",
                   "out": str(tmp_path / "a.txt")}, f)

    assert daemon.run_worker(path) == 2


def test_the_gate_validated_bytes_are_what_get_sent(daemon, monkeypatch, tmp_path):
    """The gate is the justification for this whole egress design, so it must cover the bytes that
    actually leave. Passing the CDP child a PATHNAME let it reopen a file any same-user process
    could rewrite after validation — the prompt that passed the public-repo and secret checks and
    the prompt that reached ChatGPT were not provably the same object."""
    calls = []

    def _fake_run(cmd, timeout, rid=None, stdin_text=None):
        calls.append((cmd, stdin_text))
        return 0, json.dumps({"conversation_id": "c1"}), ""

    monkeypatch.setattr(daemon, "_run", _fake_run)
    monkeypatch.setattr(daemon.spool, "validate_prompt", lambda t: (True, "ok"))
    rid = "REQ-20260707-120000-00b001"
    pf = tmp_path / "prompt.md"
    pf.write_text("VALIDATED BYTES https://github.com/acme/widgets", encoding="utf-8")
    path = daemon.spool.processing_path(rid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rid": rid, "kind": "submit", "prompt_file": str(pf),
                   "out": str(tmp_path / "a.txt")}, f)

    daemon.run_worker(path)
    send_cmd, send_stdin = calls[0]          # the submit call, not the later wait
    assert send_stdin == "VALIDATED BYTES https://github.com/acme/widgets"
    assert send_cmd[send_cmd.index("--prompt-file") + 1] == "-", \
        "the child must be handed bytes on stdin, never a path it can reopen"
    assert str(pf) not in send_cmd, "the mutable prompt path must not reach the sender"


def test_browser_repair_actually_triggers_on_an_attach_failure(daemon, monkeypatch, tmp_path):
    """The detection must read the job LOG, not the returned stderr. stderr is redirected into that
    log, so the returned string is only its last 240 chars — and the token sits at the START of a
    long message, so an `in se` check silently never fired and the repair never ran."""
    calls = []

    def _fake_run(cmd, timeout, rid=None, stdin_text=None, env_extra=None):
        calls.append((cmd, env_extra))
        if cmd[0] == "bash":                       # the launcher restart
            return 0, "", ""
        if len([c for c in calls if c[0][0] != "bash"]) == 1:
            # first submit: write the real failure into the log, return only a tail like _run does
            with open(daemon.spool.log_path(rid), "a", encoding="utf-8") as f:
                f.write("CGC_ERROR cdp_attach_failed: " + "x" * 500 + "\n")
            return 1, "", "x" * 240
        return 0, json.dumps({"conversation_id": "c1"}), ""

    monkeypatch.setattr(daemon, "_run", _fake_run)
    monkeypatch.setattr(daemon.spool, "validate_prompt", lambda t: (True, "ok"))
    rid = "REQ-20260707-120000-00c001"
    pf = tmp_path / "p.md"
    pf.write_text("https://github.com/acme/widgets", encoding="utf-8")
    path = daemon.spool.processing_path(rid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rid": rid, "kind": "submit", "prompt_file": str(pf),
                   "out": str(tmp_path / "a.txt")}, f)

    daemon.run_worker(path)
    restarts = [c for c in calls if c[0][0] == "bash"]
    assert len(restarts) == 1, "a browser that cannot open a tab must be restarted, not re-tried"
    assert restarts[0][1]["CGC_RESTART"] == "1"


def test_chrome_is_not_restarted_while_another_consult_is_live(daemon, monkeypatch, tmp_path):
    """The restart is global — it takes every tab with it. Doing it for one job while another is
    sending or waiting is how a healthy consult gets reported as broken; observed exactly that."""
    other = "REQ-20260707-120000-00d001"
    path = daemon.spool.processing_path(other)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rid": other, "kind": "submit"}, f)
    daemon.spool.write_status(other, "processing", worker_pid=os.getpid())  # alive

    ran = []
    monkeypatch.setattr(daemon, "_run", lambda *a, **k: ran.append(a) or (0, "", ""))
    assert daemon._restart_chrome("REQ-20260707-120000-00d002") is False
    assert ran == [], "must not have launched anything"


def test_chrome_is_restarted_when_nothing_else_is_live(daemon, monkeypatch):
    ran = []

    def _fake(cmd, timeout, rid=None, stdin_text=None, env_extra=None):
        ran.append((cmd, env_extra))
        return 0, "", ""

    monkeypatch.setattr(daemon, "_run", _fake)
    assert daemon._restart_chrome("REQ-20260707-120000-00d003") is True
    assert ran[0][1]["CGC_RESTART"] == "1"


def test_tab_sweep_never_runs_while_a_job_is_live(daemon, monkeypatch):
    """A tab the sweep can see must be unowned. The only cheap way to guarantee that is to sweep
    only when nothing is running — otherwise it would close the tab a live send is using."""
    other = "REQ-20260707-120000-00e001"
    path = daemon.spool.processing_path(other)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rid": other, "kind": "submit"}, f)
    daemon.spool.write_status(other, "processing", worker_pid=os.getpid())

    touched = []
    monkeypatch.setattr(daemon.urllib.request, "urlopen",
                        lambda *a, **k: touched.append(a) or (_ for _ in ()).throw(AssertionError()))
    assert daemon._sweep_tabs() == 0
    assert touched == [], "must not even look at the browser while a job is live"


def test_tab_sweep_is_a_noop_with_one_tab(daemon, monkeypatch):
    """The browser is left with a tab on purpose — a profile with zero windows is a worse state."""
    import io

    class _R:
        def __init__(self, payload): self._p = json.dumps(payload).encode()
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(daemon.urllib.request, "urlopen",
                        lambda *a, **k: io.BytesIO(json.dumps([{"type": "page", "id": "A"}]).encode()))
    assert daemon._sweep_tabs() == 0


def test_restart_is_not_reported_successful_until_a_tab_actually_works(daemon, monkeypatch):
    """The launcher proves the port is up and the session is logged in. Neither is what broke: an
    unwell Chrome keeps serving existing tabs while new ones never answer Runtime.enable, so both
    those checks stay true while every consult dies. A retry 3s after a 'successful' restart failed
    with `No such target id`, because the browser was still settling."""
    monkeypatch.setattr(daemon, "_run", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(daemon.time, "sleep", lambda n: None)
    monkeypatch.setattr(daemon, "_other_live_jobs", lambda rid: [])
    monkeypatch.setattr(daemon, "_new_tab_healthy", lambda: False)
    assert daemon._restart_chrome("REQ-20260707-120000-00f001") is False, \
        "a browser that still cannot open a tab is not a successful restart"

    monkeypatch.setattr(daemon, "_new_tab_healthy", lambda: True)
    assert daemon._restart_chrome("REQ-20260707-120000-00f002") is True


def test_health_check_is_the_real_capability_not_a_ping(daemon):
    """It must create a tab and drive it, because 'the port answers' was already true when the
    browser was broken."""
    import inspect
    src = inspect.getsource(daemon._new_tab_healthy)
    assert "Target.createTarget" in src and "Runtime.enable" in src
    assert "Target.closeTarget" in src, "the probe must not leak the tab it opens"


def test_health_probe_keeps_the_last_tab(daemon):
    """A browser with zero pages has nothing for the login probe to evaluate on, so the gate
    degrades to 'login unverified' and the login check fails OPEN. Observed exactly that."""
    import inspect
    src = inspect.getsource(daemon._new_tab_healthy)
    assert "if others:" in src, "the probe must not close the only remaining tab"


def test_admission_and_restart_take_the_same_lock(daemon):
    """The restart guard was check-then-act: _other_live_jobs() is a lock-free snapshot, and the
    dispatcher can admit a worker between the scan and the restart — so a consult that started a
    moment later still had its tab torn away. That is how live consults were lost, and how the
    agents watching them then re-dispatched duplicates."""
    import inspect
    loop = inspect.getsource(daemon.run_loop)
    assert "lifecycle_lock(" in loop, "admission must be serialised"
    claim_at = loop.index("spool.claim(pf)")
    lock_at = loop.index("lifecycle_lock(")
    pid_at = loop.index("worker_pid=p.pid")
    assert lock_at < claim_at < pid_at, "the lock must span claim through publishing the pid"
    worker = inspect.getsource(daemon.run_worker)
    assert "lifecycle_lock(" in worker, "the restart must take the same lock"


def test_the_lock_fails_closed_not_open(daemon):
    """A lock whose body runs unlocked is not a lock. On timeout it must report NOT acquired, and
    BOTH callers must fail closed (skip + retry) rather than proceed unserialised — the old fail-open
    is exactly how a global restart tore the tab off a just-admitted worker (the reported bug)."""
    import inspect
    lock_src = inspect.getsource(daemon.spool.lifecycle_lock)
    assert "proceeding unserialised" not in lock_src, "the lock must not run its body unlocked"
    assert "self.acquired" in lock_src, "the lock must report acquisition so callers can fail closed"
    loop = inspect.getsource(daemon.run_loop)
    assert "lk.acquired" in loop, "admission must check acquisition and admit nothing while held"
    worker = inspect.getsource(daemon.run_worker)
    assert "lk.acquired" in worker, "restart must check acquisition and skip while held"


def test_lifecycle_lock_second_holder_fails_closed(daemon):
    """Functional proof (not source text): while one holder has the lock, a second acquisition
    reports acquired is False within its timeout, and becomes acquirable again after release."""
    LL = daemon.spool.lifecycle_lock
    with LL() as held:
        assert held.acquired, "first holder must acquire"
        with LL(timeout=0.3) as second:
            assert second.acquired is False, "second holder must fail closed while the lock is held"
    with LL(timeout=0.3) as after:
        assert after.acquired, "lock must be acquirable again once released"
