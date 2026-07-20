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


def test_cmd_await_no_answer_and_error_both_exit_1(spool, tmp_path, monkeypatch):
    """One outcome, not two. "the daemon finished and produced nothing" and "the daemon errored"
    differ in cause but not in what the caller does next — read the log — so they are one code."""
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    for state in ("no_answer", "error"):
        rid = _rid()
        out = tmp_path / f"{state}.txt"
        spool.write_status(rid, state, out=str(out))
        assert spool.cmd_await(_await_args(rid, str(out))) == 1, state


def test_await_keeps_waiting_while_a_live_daemon_works_the_job(spool, tmp_path, monkeypatch):
    """The normal path for a GPT-5.6 Pro consult: still reasoning. Waiting longer is the waiter's
    job, so it must NOT return — this used to exit 5, turning every healthy round into something
    the caller had to notice and manually retry."""
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    out = tmp_path / "answer.txt"
    spool.write_status(rid, "processing", out=str(out))
    slept = []
    monkeypatch.setattr(spool.time, "sleep", lambda n: slept.append(n))
    # A deadline far out: it must still be looping (not returning) after many polls.
    a = _await_args(rid, str(out), timeout=0.5, poll=0.01)
    spool.cmd_await(a)
    assert len(slept) > 1, "the waiter must keep polling a healthy in-flight consult"


def test_await_declares_stuck_not_slow_when_the_deadline_passes(spool, tmp_path, monkeypatch):
    """Reaching STUCK_AFTER_S is not "the answer is late" — it is past any time the work explains,
    so the message must send the caller to the log instead of inviting another wait."""
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    out = tmp_path / "answer.txt"
    spool.write_status(rid, "processing", out=str(out))
    code = spool.cmd_await(_await_args(rid, str(out), timeout=0.05, poll=0.01))
    assert code == 1



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


def test_one_timeout_and_it_means_stuck_not_slow(spool):
    """There is exactly ONE deadline. A GPT-5.6 Pro round reasons ~25 min, so any deadline at or
    near that kills healthy consults; the deadline must sit far enough past the work that reaching
    it means malfunction, not slowness. Regression guard against re-introducing a second clock."""
    assert spool.STUCK_AFTER_S >= 3600
    assert not hasattr(spool, "CONSULT_TIMEOUT_S"), "a per-consult budget is not a timeout"
    assert not hasattr(spool, "AGENT_POLL_S"), "an observation window is not a timeout"



# The fixture stubs _repo_is_public so no test can shell out to `gh`; the two tests that
# exercise the real implementation restore it explicitly from a pristine module load.
_REAL_REPO_IS_PUBLIC = _load()._repo_is_public


# ---- gate: "could not verify" is not the same finding as "not public" --------

def test_gate_unverified_gh_is_not_reported_as_a_private_repo(spool, monkeypatch):
    """A gh that times out proves nothing about the repo. Reporting that as 'not confirmed PUBLIC'
    reads as a security verdict the repo never earned, and tells the caller to give up on a job a
    retry would have sent. Both refuse — only the reason differs, and the reason drives the action."""
    monkeypatch.setattr(spool, "_repo_is_public",
                        lambda slug: (False, "unverified: gh timed out after 25s"))
    ok, why = spool.validate_prompt("review https://github.com/acme/widgets")
    assert ok is False
    assert why.startswith("unverified:")
    assert "not confirmed PUBLIC" not in why
    assert "re-enqueue" in why.lower()


def test_gate_confirmed_nonpublic_still_reads_as_a_refusal(spool, monkeypatch):
    """The terminal case must keep its old wording — it is a real security verdict."""
    monkeypatch.setattr(spool, "_repo_is_public", lambda slug: (False, "visibility=private"))
    ok, why = spool.validate_prompt("review https://github.com/acme/widgets")
    assert ok is False and why.startswith("refused:") and "not confirmed PUBLIC" in why


def test_repo_is_public_retries_then_reports_unverified_on_timeout(spool, monkeypatch):
    """gh reads its token from the OS keyring, which can block far longer in a launchd daemon than
    in a shell. One 20s attempt cost a real consult its whole budget, so: retry, then say so."""
    calls = []

    def _timeout(*args, **kw):
        calls.append(1)
        raise __import__("subprocess").TimeoutExpired(cmd="gh", timeout=25)

    monkeypatch.setattr(spool, "_repo_is_public", _REAL_REPO_IS_PUBLIC)
    monkeypatch.setattr(spool.subprocess, "run", _timeout)
    ok, detail = spool._repo_is_public("acme/widgets")
    assert ok is False
    assert len(calls) == spool._GH_ATTEMPTS
    assert detail.startswith("unverified:") and "timed out" in detail


def test_repo_is_public_404_is_terminal_not_retried(spool, monkeypatch):
    """A 404 IS gh's answer (this token cannot see the repo) — retrying can only waste the budget."""
    calls = []

    class _R:
        returncode = 1
        stdout = ""
        stderr = "gh: Not Found (HTTP 404)"

    def _run(*args, **kw):
        calls.append(1)
        return _R()

    monkeypatch.setattr(spool, "_repo_is_public", _REAL_REPO_IS_PUBLIC)
    monkeypatch.setattr(spool.subprocess, "run", _run)
    ok, detail = spool._repo_is_public("acme/ghost")
    assert ok is False and len(calls) == 1 and not detail.startswith("unverified:")


# ---- await: a daemon that dies mid-consult must terminate the wait -----------

def test_await_gives_up_when_daemon_dies_mid_processing(spool, tmp_path, monkeypatch):
    """The liveness guard used to switch OFF once the job reached `processing`. A daemon that died
    after pickup therefore left every later await burning its full window and returning 5 = 'still
    running, re-run me' — an unbounded loop over a job nobody was working."""
    rid = _rid("aa0001")
    spool.write_status(rid, "processing", out=str(tmp_path / "a.txt"))
    monkeypatch.setattr(spool, "daemon_alive", lambda: False)
    monkeypatch.setattr(spool, "DAEMON_GRACE_S", 0)
    code = spool.cmd_await(_await_args(rid, str(tmp_path / "a.txt"), timeout=3))
    assert code == 1


def test_await_reports_a_dead_daemon_as_broken_not_as_still_running(spool, tmp_path, monkeypatch):
    """A daemon that died mid-consult is the one case where "still running" would be a lie: nothing
    is working the job, so waiting can never end. That must surface as broken."""
    rid = _rid("aa0002")
    spool.write_status(rid, "processing", out=str(tmp_path / "b.txt"))
    monkeypatch.setattr(spool, "daemon_alive", lambda: False)
    monkeypatch.setattr(spool, "DAEMON_GRACE_S", 0)
    assert spool.cmd_await(_await_args(rid, str(tmp_path / "b.txt"), timeout=3)) == 1

# ---- private repos: a user-declared allowlist, never a blanket off switch ----
#
# A ChatGPT GitHub connector can read repos the account has access to, private included, so
# "ChatGPT cannot open a private link" is no longer true once one is configured. That kills the
# CAPABILITY argument for public-only but not the SAFETY one: this gate is what mitigates a
# prompt-injected agent exfiltrating private data. A boolean would let an injected agent name any
# private repo the connector reaches; naming them keeps the decision with the human.

_PRIV = "review https://github.com/acme/secret-thing/tree/main"


def test_private_repo_is_still_refused_by_default(spool, monkeypatch):
    monkeypatch.delenv("CGC_GATE_PRIVATE_REPOS", raising=False)
    monkeypatch.setattr(spool, "_repo_is_public", lambda s: (False, "visibility=private"))
    ok, why = spool.validate_prompt(_PRIV)
    assert ok is False and "not confirmed PUBLIC" in why


def test_an_allowlisted_private_repo_passes(spool, monkeypatch):
    monkeypatch.setenv("CGC_GATE_PRIVATE_REPOS", "acme/secret-thing")
    monkeypatch.setattr(spool, "_repo_is_public", lambda s: (False, "visibility=private"))
    ok, why = spool.validate_prompt(_PRIV)
    assert ok, why


def test_allowlisting_one_repo_does_not_allow_another(spool, monkeypatch):
    """The whole point of naming them: an injected agent cannot reach a repo the human did not."""
    monkeypatch.setenv("CGC_GATE_PRIVATE_REPOS", "acme/something-else")
    monkeypatch.setattr(spool, "_repo_is_public", lambda s: (False, "visibility=private"))
    ok, why = spool.validate_prompt(_PRIV)
    assert ok is False and "not confirmed PUBLIC" in why


def test_allowlist_is_case_insensitive(spool, monkeypatch):
    monkeypatch.setenv("CGC_GATE_PRIVATE_REPOS", "ACME/Secret-Thing")
    monkeypatch.setattr(spool, "_repo_is_public", lambda s: (False, "visibility=private"))
    assert spool.validate_prompt(_PRIV)[0]


def test_star_restores_the_boolean_behaviour(spool, monkeypatch):
    monkeypatch.setenv("CGC_GATE_PRIVATE_REPOS", "*")
    monkeypatch.setattr(spool, "_repo_is_public", lambda s: (False, "visibility=private"))
    assert spool.validate_prompt(_PRIV)[0]


def test_allowlisted_but_unresolvable_repo_is_still_refused(spool, monkeypatch):
    """An allowlist entry says 'this repo of mine may go', not 'skip the check'. If gh cannot even
    confirm the repo exists, nothing is known and nothing is sent."""
    monkeypatch.setenv("CGC_GATE_PRIVATE_REPOS", "acme/secret-thing")
    monkeypatch.setattr(spool, "_repo_is_public", lambda s: (False, "unverified: gh timed out"))
    ok, why = spool.validate_prompt(_PRIV)
    assert ok is False and why.startswith("unverified:")


def test_allowlist_never_exempts_secrets(spool, monkeypatch):
    """Allowing a repo says nothing about credentials in the prose around it."""
    monkeypatch.setenv("CGC_GATE_PRIVATE_REPOS", "*")
    monkeypatch.setattr(spool, "_repo_is_public", lambda s: (False, "visibility=private"))
    ok, why = spool.validate_prompt(_PRIV + "\nAKIAABCDEFGHIJKLMNOP\n")
    assert ok is False and "AWS access key id" in why
