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
