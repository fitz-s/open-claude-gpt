# Tests for the validating egress gate + store-backed CLI (cgc_spool.py).
#
# cgc_spool.SPOOL_DIR is computed at import time from CGC_SPOOL_DIR (or CGC_STATE_DIR/spool), so
# every test sets env BEFORE importing and loads a FRESH copy of the module (mirrors
# tests/test_config.py / tests/test_state.py's importlib.util spec-loading pattern). Network is
# never touched: cgc_spool._repo_is_public (which shells `gh`) is monkeypatched to a stub. The
# round lifecycle itself lives in the SQLite store (test_store / test_store_backend); here the CLI
# is exercised end-to-end against a per-test store DB (pinned by conftest)._
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
    # Every test stubs these out by default so no test can accidentally shell out to `gh`.
    monkeypatch.setattr(m, "_repo_is_public", lambda slug: (True, "stub public"))
    monkeypatch.setattr(m, "_gh_exists", lambda path: (True, "exists"))
    return m


def _rid(suffix="000001"):
    return f"REQ-20260707-120000-{suffix}"


# ---- heartbeat / daemon_alive ------------------------------------------------

def test_heartbeat_write_then_daemon_alive_is_true(spool):
    spool.heartbeat_write(pid=os.getpid())
    assert spool.daemon_alive() is True


def test_stale_heartbeat_daemon_alive_is_false(spool):
    spool._atomic_write(spool.daemon_path(), {"pid": os.getpid(), "ts": time.time() - 1000})
    assert spool.daemon_alive() is False


def test_dead_pid_heartbeat_daemon_alive_is_false(spool):
    spool._atomic_write(spool.daemon_path(), {"pid": 999999, "ts": time.time()})
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


# ---- cmd_enqueue / cmd_await (CLI over the store) ----------------------------
#
# cmd_enqueue creates a store round; cmd_await polls it. Both import cgc_backend lazily, which
# imports cgc_store; the conftest fixtures pin CGC_STORE_DB into this test's tmp dir.

def _store(spool):
    sys.path.insert(0, SCRIPTS)
    import cgc_store
    return cgc_store


def _enqueue_args(rid, prompt_file, **overrides):
    defaults = dict(
        rid=rid,
        prompt_file=prompt_file,
        kind="submit",
        project_url="https://chatgpt.com/",
        conversation="auto",
        parent=None,
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
    code = spool.cmd_enqueue(_enqueue_args(rid, str(prompt_file)))
    assert code == 2
    sm = _store(spool)
    with sm.Store() as s:
        assert s.get_round(rid) is None, "a locally-refused prompt must never reach the store"


def test_cmd_enqueue_public_link_exits_0_and_queues(spool, tmp_path):
    rid = _rid()
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("please review https://github.com/acme/widgets")
    code = spool.cmd_enqueue(_enqueue_args(rid, str(prompt_file)))
    assert code == 0
    sm = _store(spool)
    with sm.Store() as s:
        r = s.get_round(rid)
    assert r is not None and r["state"] == "queued"
    assert r["rendered_prompt"] == "please review https://github.com/acme/widgets"


def test_cmd_enqueue_same_rid_same_bytes_is_idempotent(spool, tmp_path, capsys):
    """The canonical invocation is `enqueue && await`, so refusing an exact repeat short-circuited
    the chain that actually delivers the answer — the retry failed identically every time while the
    round sat COMPLETED in the store (observed in the field). Same rid + same bytes is the same
    request: nothing new is queued, and the caller proceeds to await."""
    rid = _rid("dd0001")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("please review https://github.com/acme/widgets")
    assert spool.cmd_enqueue(_enqueue_args(rid, str(prompt_file))) == 0
    assert spool.cmd_enqueue(_enqueue_args(rid, str(prompt_file))) == 0
    err = capsys.readouterr()
    assert "CGC_IDEMPOTENT" in err.err
    assert '"queued": false' in err.out, "a receipt claiming it queued something would be a lie"


def test_cmd_enqueue_same_rid_different_bytes_refuses(spool, tmp_path, capsys):
    """A different prompt under the same rid is a DIFFERENT request — answering it from the first
    round's thread would return an answer to a question nobody asked."""
    rid = _rid("dd0002")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("please review https://github.com/acme/widgets")
    assert spool.cmd_enqueue(_enqueue_args(rid, str(prompt_file))) == 0
    prompt_file.write_text("please review https://github.com/acme/widgets — and also this")
    assert spool.cmd_enqueue(_enqueue_args(rid, str(prompt_file))) == 2
    assert "already_enqueued" in capsys.readouterr().err


# ---- cmd_await (CLI over the store) ------------------------------------------

def _await_args(rid, out, timeout=1, poll=0.05):
    return argparse.Namespace(rid=rid, out=out, poll=poll, timeout=timeout)


def _terminal_round(spool, rid, state, **fields):
    sm = _store(spool)
    with sm.Store() as s:
        s.create_round(rid, "submit", prompt="p https://github.com/acme/widgets")
        s.set_state(rid, sm.READY)
        if state == sm.READY:
            return
        aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="t")
        if state == sm.BLOCKED:
            s.set_state(rid, sm.BLOCKED, expect=sm.SENDING, **fields)
            return
        s.mark_accepted(aid, "conv-t")
        s.mark_waiting(rid)
        if state in (sm.COMPLETED_VERIFIED, sm.COMPLETED_UNVERIFIED):
            s.finish(rid, state, **fields)
        elif state == sm.FAILED:
            s.set_state(rid, sm.FAILED, **fields)


def test_cmd_await_verified_answer_exits_0_and_materializes(spool, tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    out = tmp_path / "answer.txt"
    _terminal_round(spool, rid, _store(spool).COMPLETED_VERIFIED, result_text="the answer")
    code = spool.cmd_await(_await_args(rid, str(out)))
    assert code == 0
    assert out.read_text() == "the answer", "the store's result must be materialized to --out"


def test_cmd_await_blocked_exits_3(spool, tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    _terminal_round(spool, rid, _store(spool).BLOCKED, error_code="login_needed")
    assert spool.cmd_await(_await_args(rid, str(tmp_path / "a.txt"))) == 3


def test_cmd_await_failed_exits_1(spool, tmp_path, monkeypatch):
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    _terminal_round(spool, rid, _store(spool).FAILED, error_code="wait failed")
    assert spool.cmd_await(_await_args(rid, str(tmp_path / "a.txt"))) == 1


def test_await_keeps_waiting_while_a_live_daemon_works_the_job(spool, tmp_path, monkeypatch):
    """The normal path for a GPT-5.6 Pro consult: still reasoning. Waiting longer is the waiter's
    job, so it must NOT return early — that would turn every healthy round into something the
    caller had to notice and manually retry."""
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    sm = _store(spool)
    with sm.Store() as s:
        s.create_round(rid, "submit", prompt="p")   # queued, being worked
    import cgc_backend
    slept = []
    monkeypatch.setattr(cgc_backend.time, "sleep", lambda n: slept.append(n))
    spool.cmd_await(_await_args(rid, str(tmp_path / "a.txt"), timeout=0.5, poll=0.01))
    assert len(slept) > 1, "the waiter must keep polling a healthy in-flight consult"


def test_await_declares_stuck_not_slow_when_the_deadline_passes(spool, tmp_path, monkeypatch):
    """Reaching STUCK_AFTER_S is not "the answer is late" — it is past any time the work explains,
    so the message must send the caller to the log instead of inviting another wait."""
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid()
    sm = _store(spool)
    with sm.Store() as s:
        s.create_round(rid, "submit", prompt="p")
    assert spool.cmd_await(_await_args(rid, str(tmp_path / "a.txt"), timeout=0.05, poll=0.01)) == 1



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

def test_await_reports_a_dead_daemon_as_broken_not_as_still_running(spool, tmp_path, monkeypatch):
    """A dead daemon is the one case where "still running" would be a lie: nothing is working the
    round, so waiting can never end. That must surface as broken-with-next-step promptly — not as
    the full STUCK window of silent polling."""
    rid = _rid("aa0002")
    sm = _store(spool)
    with sm.Store() as s:
        s.create_round(rid, "submit", prompt="p")   # queued, nobody working it
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


# One id, one worker: in the store this is structural, not fenced by pids — a duplicate rid is a
# primary-key refusal at enqueue (test_cmd_enqueue_same_rid_twice_refuses above), atomic claim_ready
# gives a round to exactly one worker (test_daemon), and terminal states are immutable so a
# superseded worker cannot overwrite an outcome (test_store).


# ---- dead-reference detection (the offline-deliver blind spot) ----------------
#
# deliver is offline by design, so a typo'd PR number produces syntactically perfect dead links and
# the failure surfaces 25 minutes later at ChatGPT. The gate has gh anyway — it must catch this in
# seconds instead.

def test_gate_refuses_a_nonexistent_pr(spool, monkeypatch):
    monkeypatch.setattr(spool, "_gh_exists",
                        lambda path: (False, "404") if "/pulls/" in path else (True, "exists"))
    ok, why = spool.validate_prompt(
        "review https://github.com/acme/widgets/pull/99999 and https://github.com/acme/widgets")
    assert ok is False and "does not exist" in why and "re-fire" in why.lower()


def test_gate_refuses_a_nonexistent_ref(spool, monkeypatch):
    monkeypatch.setattr(spool, "_gh_exists",
                        lambda path: (False, "404") if "/commits/" in path else (True, "exists"))
    ok, why = spool.validate_prompt("review https://github.com/acme/widgets/tree/deadbeef00")
    assert ok is False and "does not exist" in why


def test_gate_existence_check_unverified_is_retryable_not_a_verdict(spool, monkeypatch):
    monkeypatch.setattr(spool, "_gh_exists", lambda path: (None, "unverified: gh timed out"))
    ok, why = spool.validate_prompt("review https://github.com/acme/widgets/pull/12")
    assert ok is False and why.startswith("unverified:")


def test_gate_passes_when_all_refs_exist(spool, monkeypatch):
    monkeypatch.setattr(spool, "_gh_exists", lambda path: (True, "exists"))
    ok, why = spool.validate_prompt(
        "review https://github.com/acme/widgets/pull/12 and "
        "https://github.com/acme/widgets/tree/abc1234/src")
    assert ok, why


def test_gate_existence_only_checks_admitted_repos(spool, monkeypatch):
    """The visibility pass is the admission control; existence checks must not become a way to probe
    arbitrary repos gh can see."""
    probed = []
    monkeypatch.setattr(spool, "_gh_exists", lambda path: probed.append(path) or (True, "exists"))
    spool.validate_prompt("review https://github.com/acme/widgets/pull/12")
    assert all("acme/widgets" in p for p in probed)


def test_cmd_await_uncertain_exits_3_a_human_must_act_not_1_broken(spool, tmp_path, monkeypatch):
    """The documented contract is 0 answer / 3 a human must act / 1 BROKEN. An uncertain send is the
    definition of "a human must act" — nothing is broken, the send may well have landed. Reporting
    it as broken made every one of these read as a tool failure in the caller's log ("failed with
    exit code 1") instead of as the one action item it is."""
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    rid = _rid("uu0001")
    sm = _store(spool)
    with sm.Store() as s:
        s.create_round(rid, "submit", prompt="p https://github.com/acme/widgets")
        s.set_state(rid, sm.READY)
        aid = s.begin_send(rid, "p", "h" * 64, daemon_instance_id="t")
        s.mark_possibly_accepted(aid, "unknown send")
    assert spool.cmd_await(_await_args(rid, str(tmp_path / "u.txt"))) == 3


def test_enqueue_reads_the_rid_from_the_prompt_when_none_is_passed(spool, tmp_path):
    """The rid is a property of the RENDERED PROMPT — prep writes BEGIN_RESPONSE:<rid> into the text
    and the waiter accepts an answer only if that exact sentinel comes back. Hand-typing the format
    is a step that only ever produces bad_rid (observed: an agent burned a round on it, then reached
    for `date` to synthesise one)."""
    import argparse
    rid = _rid("bb0001")
    pf = tmp_path / "p.md"
    pf.write_text(f"review https://github.com/acme/widgets\n\nBEGIN_RESPONSE:{rid}\n...\nEND_RESPONSE:{rid}\n")
    a = _enqueue_args(rid, str(pf))
    a.rid = None
    assert spool.cmd_enqueue(a) == 0
    sm = _store(spool)
    with sm.Store() as s:
        assert s.get_round(rid) is not None


def test_enqueue_refuses_a_rid_that_disagrees_with_the_prompt(spool, tmp_path, capsys):
    """A mismatch guarantees an answer that can never verify: the waiter checks the PROMPT's own
    sentinel, not the rid the caller happened to type."""
    pf = tmp_path / "p.md"
    pf.write_text(f"review https://github.com/acme/widgets\n\nBEGIN_RESPONSE:{_rid('bb0002')}\n")
    assert spool.cmd_enqueue(_enqueue_args(_rid("bb0003"), str(pf))) == 2
    assert "rid_prompt_mismatch" in capsys.readouterr().err


# ---- a retrieve mints its own rid --------------------------------------------

def test_retrieve_without_rid_mints_one_and_returns_it(spool, capsys):
    """A retrieve has no prompt to read a rid back from — and no caller-meaningful rid at all: what
    identifies the recovery is --parent, the round being recovered. So enqueue mints it and the
    receipt RETURNS it. Hand-writing the format was the last path to `bad_rid`, and it burned a
    live recovery in the field."""
    parent = _rid("00pa01")
    a = _enqueue_args(None, None, kind="retrieve", conversation="8f2a1c00-0000-4000-8000-000000000001",
                      parent=parent)
    assert spool.cmd_enqueue(a) == 0
    minted = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["rid"]
    assert spool._RID_RE.match(minted), f"minted rid must be canonical, got {minted!r}"
    sm = _store(spool)
    with sm.Store() as s:
        r = s.get_round(minted)
    assert r is not None and r["kind"] == "retrieve" and r["parent_rid"] == parent


def test_retrieve_still_refuses_a_malformed_hand_written_rid(spool):
    """Minting is a default, not a laundering step: a rid that IS passed still has to be canonical,
    because everything downstream (leases, the sentinel, the store key) fullmatches the shape."""
    a = _enqueue_args("not-a-rid", None, kind="retrieve",
                      conversation="8f2a1c00-0000-4000-8000-000000000002", parent=_rid("00pa02"))
    assert spool.cmd_enqueue(a) == 2


def test_new_rid_is_accepted_by_the_validator_that_guards_every_round(spool):
    """One definition of the rid shape: the minter and the regex live together, so they cannot
    drift into a format that mints rounds the store refuses."""
    assert spool._RID_RE.match(spool.new_rid())
