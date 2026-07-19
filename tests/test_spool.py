# Tests for the local spool + validating egress gate (cgc_spool.py).
#
# cgc_spool.SPOOL_DIR is computed at import time from CGC_SPOOL_DIR (or CGC_STATE_DIR/spool), so
# every test sets env BEFORE importing and loads a FRESH copy of the module (mirrors
# tests/test_config.py / tests/test_state.py's importlib.util spec-loading pattern). Network is
# never touched: cgc_spool._repo_is_public (which shells `gh`) is monkeypatched to a stub.
import argparse
import importlib.util
import json
import os
import sys
import time

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load():
    spec = importlib.util.spec_from_file_location("cgc_spool", os.path.join(SCRIPTS, "cgc_spool.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def spool(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CGC_SPOOL_DIR", str(tmp_path / "state" / "spool"))
    monkeypatch.delenv("CGC_GATE_ALLOW_GIST", raising=False)
    m = _load()
    # Every test stubs this out by default so no test can accidentally shell out to `gh`.
    monkeypatch.setattr(m, "_repo_is_public", lambda slug: (True, "stub public"))
    return m


def _rid(suffix="000001"):
    return f"REQ-20260707-120000-{suffix}"


# ---- enqueue_job / list_pending / claim -------------------------------------

def test_enqueue_job_writes_pending_file(spool):
    rid = _rid()
    path = spool.enqueue_job({"rid": rid, "kind": "submit"})
    assert path == spool.pending_path(rid)
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as f:
        job = json.load(f)
    assert job["rid"] == rid
    assert job["kind"] == "submit"


def test_enqueue_job_bad_rid_raises_value_error(spool):
    with pytest.raises(ValueError):
        spool.enqueue_job({"rid": "not-a-valid-rid"})


def test_list_pending_finds_enqueued_job(spool):
    rid = _rid()
    spool.enqueue_job({"rid": rid})
    assert spool.list_pending() == [spool.pending_path(rid)]


def test_claim_moves_pending_job_to_processing(spool):
    rid = _rid()
    pending = spool.enqueue_job({"rid": rid})
    result = spool.claim(pending)
    assert result == spool.processing_path(rid)
    assert os.path.exists(spool.processing_path(rid))
    assert not os.path.exists(pending)


def test_second_claim_of_same_pending_file_returns_none(spool):
    rid = _rid()
    pending = spool.enqueue_job({"rid": rid})
    first = spool.claim(pending)
    assert first is not None
    second = spool.claim(pending)
    assert second is None


# ---- write_status / read_status / finish_job --------------------------------

def test_write_status_then_read_status_roundtrip(spool):
    rid = _rid()
    spool.write_status(rid, "queued", out="/tmp/answer.txt")
    st = spool.read_status(rid)
    assert st["rid"] == rid
    assert st["state"] == "queued"
    assert st["out"] == "/tmp/answer.txt"


def test_finish_job_moves_processing_to_done_and_sets_terminal_status(spool):
    rid = _rid()
    pending = spool.enqueue_job({"rid": rid})
    spool.claim(pending)
    assert os.path.exists(spool.processing_path(rid))

    spool.finish_job(rid, state="done", exit=0, out="/tmp/answer.txt", conversation="conv1")

    assert not os.path.exists(spool.processing_path(rid))
    assert os.path.exists(spool.done_path(rid))
    st = spool.read_status(rid)
    assert st["state"] == "done"
    assert st["exit"] == 0
    assert st["conversation"] == "conv1"


# ---- heartbeat / daemon_alive ------------------------------------------------

def test_heartbeat_write_then_daemon_alive_is_true(spool):
    spool.heartbeat_write(pid=os.getpid())
    assert spool.daemon_alive() is True


def test_stale_heartbeat_daemon_alive_is_false(spool):
    spool._atomic_write(spool.DAEMON_PATH, {"pid": os.getpid(), "ts": time.time() - 1000})
    assert spool.daemon_alive() is False


def test_dead_pid_heartbeat_daemon_alive_is_false(spool):
    spool._atomic_write(spool.DAEMON_PATH, {"pid": 999999, "ts": time.time()})
    assert spool.daemon_alive() is False


# ---- validate_prompt: the gate ------------------------------------------------

def test_validate_prompt_public_github_link_ok(spool):
    ok, reason = spool.validate_prompt("please review https://github.com/acme/widgets")
    assert ok is True


def test_validate_prompt_private_repo_stub_refused(spool, monkeypatch):
    monkeypatch.setattr(spool, "_repo_is_public", lambda slug: (False, "stub private"))
    ok, reason = spool.validate_prompt("please review https://github.com/acme/widgets")
    assert ok is False


def test_validate_prompt_secret_refused_even_with_valid_public_link(spool):
    prompt = "see https://github.com/acme/widgets and use AKIAABCDEFGHIJKLMNOP to auth"
    ok, reason = spool.validate_prompt(prompt)
    assert ok is False
    assert "AWS access key id" in reason


def test_validate_prompt_no_link_and_not_followup_refused(spool):
    ok, reason = spool.validate_prompt("just some prose, no links here")
    assert ok is False


def test_validate_prompt_followup_marker_exempts_missing_link(spool):
    ok, reason = spool.validate_prompt("continuing this consult, here's more context")
    assert ok is True


def test_validate_prompt_followup_still_refused_if_secret_present(spool):
    prompt = "continuing this consult, ghp_" + "a" * 36
    ok, reason = spool.validate_prompt(prompt)
    assert ok is False
    assert "GitHub personal access token" in reason


def test_validate_prompt_gist_link_refused_by_default(spool):
    ok, reason = spool.validate_prompt("see https://gist.github.com/someuser/abc123")
    assert ok is False
    assert "gist" in reason


def test_validate_prompt_gist_link_allowed_when_env_set(spool, monkeypatch):
    monkeypatch.setenv("CGC_GATE_ALLOW_GIST", "1")
    ok, reason = spool.validate_prompt("see https://gist.github.com/someuser/abc123")
    assert ok is True


# ---- cmd_enqueue (CLI) --------------------------------------------------------

def _enqueue_args(rid, prompt_file, **overrides):
    defaults = dict(
        rid=rid,
        prompt_file=prompt_file,
        kind="submit",
        project_url="https://chatgpt.com/",
        conversation="auto",
        model="Pro",
        out=None,
        poll=20,
        timeout=870,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_cmd_enqueue_secret_in_prompt_exits_2_and_does_not_queue(spool, tmp_path):
    rid = _rid()
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("here is my key AKIAABCDEFGHIJKLMNOP for github.com/acme/widgets")
    args = _enqueue_args(rid, str(prompt_file))
    code = spool.cmd_enqueue(args)
    assert code == 2
    assert not os.path.exists(spool.pending_path(rid))


def test_cmd_enqueue_public_link_exits_0_and_queues(spool, tmp_path):
    rid = _rid()
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("please review https://github.com/acme/widgets")
    args = _enqueue_args(rid, str(prompt_file))
    code = spool.cmd_enqueue(args)
    assert code == 0
    assert os.path.exists(spool.pending_path(rid))
    st = spool.read_status(rid)
    assert st["state"] == "queued"


# ---- cmd_await (CLI state machine) -------------------------------------------

def _await_args(rid, out, timeout=1, poll=0.05):
    return argparse.Namespace(rid=rid, out=out, poll=poll, timeout=timeout)


def test_cmd_await_done_state_exits_0(spool, tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    out = tmp_path / "answer.txt"
    out.write_text("the answer")
    spool.write_status(rid, "done", exit=0, out=str(out))
    code = spool.cmd_await(_await_args(rid, str(out)))
    assert code == 0


def test_cmd_await_blocker_state_exits_3(spool, tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    out = tmp_path / "answer.txt"
    spool.write_status(rid, "blocker", exit=3, out=str(out))
    code = spool.cmd_await(_await_args(rid, str(out)))
    assert code == 3


def test_cmd_await_no_answer_state_exits_4(spool, tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    out = tmp_path / "answer.txt"
    spool.write_status(rid, "no_answer", exit=4, out=str(out))
    code = spool.cmd_await(_await_args(rid, str(out)))
    assert code == 4


def test_cmd_await_error_state_exits_2(spool, tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    out = tmp_path / "answer.txt"
    spool.write_status(rid, "error", exit=2, out=str(out))
    code = spool.cmd_await(_await_args(rid, str(out)))
    assert code == 2


def test_cmd_await_window_elapsed_on_live_job_exits_5_not_4(spool, tmp_path, monkeypatch):
    """The normal path for a GPT-5.6 Pro consult: the daemon is still reasoning when this await's
    window runs out. That MUST be 5 (still running -> await again), never 4 (no usable answer),
    because the two demand opposite actions."""
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    out = tmp_path / "answer.txt"
    spool.write_status(rid, "processing", out=str(out))
    code = spool.cmd_await(_await_args(rid, str(out)))
    assert code == 5


def test_cmd_await_window_elapsed_while_still_queued_exits_5(spool, tmp_path, monkeypatch):
    """Queued behind another consult with a live daemon is also 'still running', not a failure."""
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    out = tmp_path / "answer.txt"
    spool.write_status(rid, "queued", out=str(out))
    code = spool.cmd_await(_await_args(rid, str(out)))
    assert code == 5


_NO_CODE = ("# Prove it\nThis consult references no code — it is a self-contained question. "
            "Reason from first principles.\nProve the supermartingale maximal inequality.\n")


def test_gate_allows_declared_no_code_consult(spool, monkeypatch):
    """`prep --no-code` (maths/research/writing) has no repo to link, so rule 1 must not refuse it."""
    monkeypatch.setattr(spool, "_repo_is_public", lambda slug: (True, "public"))
    ok, why = spool.validate_prompt(_NO_CODE)
    assert ok, why


def test_gate_still_refuses_secrets_in_a_no_code_consult(spool):
    """The no-code exemption is scoped to rule 1 only — secrets are refused on every path."""
    ok, why = spool.validate_prompt(_NO_CODE + "\nAKIAABCDEFGHIJKLMNOP\n")
    assert not ok and "key" in why.lower()


def test_gate_still_refuses_private_repo_in_a_no_code_consult(spool, monkeypatch):
    """Nor does it exempt a private repo that the prompt happens to cite."""
    monkeypatch.setattr(spool, "_repo_is_public", lambda slug: (False, "private"))
    ok, why = spool.validate_prompt(_NO_CODE + "\nhttps://github.com/acme/secret\n")
    assert not ok and "not confirmed PUBLIC" in why


def test_gate_still_refuses_prose_without_the_no_code_declaration(spool):
    """A code consult that shipped prose instead of a link must still be refused."""
    ok, why = spool.validate_prompt("Please review the design of my repository, it is a big refactor.")
    assert not ok and "no public code link" in why


def test_agent_can_cover_the_consult_timeout_in_slices(spool):
    """The agent watches in AGENT_POLL_S slices because Claude Code kills its background tasks at
    900s; two slices must still cover a full CONSULT_TIMEOUT_S consult."""
    assert spool.AGENT_POLL_S <= 870, "must stay under the 900s background-task kill"
    assert 2 * spool.AGENT_POLL_S >= spool.CONSULT_TIMEOUT_S
