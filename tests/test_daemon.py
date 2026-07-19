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

    def _boom(cmd, timeout, rid=None):
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

    def _fake_run(cmd, timeout, rid=None):
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


# ---- orphan requeue: a claimed job with no worker must not vanish ------------

def _job(daemon, rid, age_s):
    """Put a job in processing/ as if a previous daemon had claimed it `age_s` ago."""
    import os
    import time
    path = daemon.spool.processing_path(rid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rid": rid, "kind": "submit"}, f)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


def test_requeue_orphans_recovers_a_job_stranded_by_a_daemon_restart(daemon):
    """Workers are children of the daemon, and nothing ever re-scans processing/. So a crash or a
    launchd restart used to lose the consult silently while `await` still reported it running."""
    rid = "REQ-20260707-120000-0000a1"
    _job(daemon, rid, age_s=daemon.spool.CONSULT_TIMEOUT_S + 600)
    assert daemon._requeue_orphans() == 1
    assert os.path.exists(daemon.spool.pending_path(rid))
    assert not os.path.exists(daemon.spool.processing_path(rid))
    assert daemon.spool.read_status(rid)["state"] == "queued"


def test_requeue_orphans_leaves_a_job_that_could_still_have_a_live_worker(daemon):
    """A worker's hard ceiling is CONSULT_TIMEOUT_S + 40, so anything younger may still be in
    flight — requeuing it would send the same consult twice and bill the quota twice."""
    rid = "REQ-20260707-120000-0000a2"
    _job(daemon, rid, age_s=60)
    assert daemon._requeue_orphans() == 0
    assert os.path.exists(daemon.spool.processing_path(rid))


def test_run_writes_the_child_transcript_to_the_job_log(daemon, tmp_path):
    """The waiter's per-poll heartbeat is the only evidence of WHY a consult produced nothing; the
    daemon used to capture it and throw it away, keeping a 240-char tail."""
    rid = "REQ-20260707-120000-0000a3"
    code, so, se = daemon._run([sys.executable, "-c",
                                "import sys; print('OUT'); sys.stderr.write('CGC_WAIT alive: ac=0\\n')"],
                               30, rid)
    assert code == 0
    log = open(daemon.spool.log_path(rid), encoding="utf-8").read()
    assert "OUT" in log and "CGC_WAIT alive: ac=0" in log


def test_requeue_orphans_is_rescanned_not_only_run_at_startup(daemon):
    """A job becomes eligible only once it outlives a worker's ceiling, so a daemon that restarts
    EARLY in that job's life scans while it is still ineligible. If that were the only scan the
    consult would sit in processing/ forever — observed live: a restart 415s into a job whose
    window opens at 1620s."""
    import inspect
    src = inspect.getsource(daemon.run_loop)
    assert "_requeue_orphans()" in src, "the loop must rescan for orphans"
    body = src.split("while _running:", 1)
    assert len(body) == 2 and "_requeue_orphans()" in body[1], \
        "the orphan scan must sit INSIDE the loop, not only before it"


# ---- kind=retrieve: recovery must stay on the daemon path --------------------

def test_retrieve_job_sends_nothing_and_skips_the_gate(daemon, monkeypatch, tmp_path):
    """Retrieval reads an existing conversation. There is no outbound payload, so there is nothing
    for the gate to validate — and validate_prompt must not even be reached (it would need a prompt
    file that a retrieve job deliberately does not have)."""
    called = []
    monkeypatch.setattr(daemon.spool, "validate_prompt",
                        lambda t: called.append(t) or (True, "should not run"))
    seen = {}

    def _fake_run(cmd, timeout, rid=None):
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
    def _boom(cmd, timeout, rid=None):
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
    def _boom(cmd, timeout, rid=None):
        raise AssertionError("must not run anything")

    monkeypatch.setattr(daemon, "_run", _boom)
    rid = "REQ-20260707-120000-00re03"
    path = daemon.spool.processing_path(rid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rid": rid, "kind": "retrieve", "conversation": "auto",
                   "out": str(tmp_path / "a.txt")}, f)

    assert daemon.run_worker(path) == 2
