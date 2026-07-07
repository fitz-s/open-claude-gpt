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

    def _boom(cmd, timeout):
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

    def _fake_run(cmd, timeout):
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
