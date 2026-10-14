# The agent contract: every agent-path verb ENDS stdout with exactly one JSON envelope line, refusals
# and argument errors included; `code` comes from one table that covers every state and error name;
# `diagnose` answers a stuck round in one bounded line with a rule-chosen next_command.
import glob
import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)

import cgc_backend as backend  # noqa: E402
import cgc_store as store_mod  # noqa: E402

RID = "REQ-20261014-120000-a1b2c3"
CONV = "0123abcd-0000-4000-8000-0123456789ab"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path / "state"))
    return {**os.environ, "CGC_STATE_DIR": str(tmp_path / "state"),
            "CGC_SPOOL_DIR": str(tmp_path / "state" / "spool"), "PATH": "/usr/bin:/bin"}


def _run(env, script, *args):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, script), *args],
                       capture_output=True, text=True, env=env, cwd=ROOT, timeout=90)
    lines = r.stdout.strip().splitlines()
    assert lines, f"no stdout at all: {r.stderr[-400:]}"
    out = json.loads(lines[-1])
    missing = [k for k in backend.ENVELOPE_KEYS if k not in out]
    assert not missing, f"envelope lacks {missing}: {out}"
    assert out["code"] in backend.CODES, out
    assert out["detail"] is None or len(out["detail"]) <= 200
    assert sum(1 for ln in lines if ln.startswith('{"schema"')) == 1, "exactly one envelope line"
    return r.returncode, out


def _round(state, *, error=None, conv=None, unsent=False, sent=False, kind="submit", spec=None,
           prompt=f"CGC_RID:{RID}\nhi\nBEGIN_RESPONSE:{RID}\n<a>\nEND_RESPONSE:{RID}\n",
           result=None, out="/nonexistent/answer.txt", parent=None):
    with store_mod.Store() as s:
        s.create_round(RID, kind, thread_id=conv, prompt=prompt, out_path=str(out), parent_rid=parent,
                       spec_json=json.dumps(spec or {"model": "Pro", "model_family": "GPT-6"}))
        s.db.execute("UPDATE rounds SET state=?, error_code=?, send_disposition=?, result_text=? "
                     "WHERE rid=?",
                     (state, error, store_mod.NOT_SENT_PROVEN if unsent else None, result, RID))
        if sent:   # an attempt crossed the send fence
            s.db.execute("INSERT INTO attempts(attempt_id,rid,phase,started_at) VALUES('a1',?,?,?)",
                         (RID, "sending", "2026-10-14T12:00:00+00:00"))
        s.db.commit()


# ---- every verb: last stdout line is the envelope -------------------------------------------------

def test_fire_success_and_refusals_end_in_one_envelope(env):
    base = ["fire", "--no-code", "--title", "t", "--role", "r", "--request-key", "k1"]
    rc, ok = _run(env, "consult.py", *base, "--task", "q")
    assert rc == 0 and ok["code"] == "ok" and ok["rid"].startswith("REQ-")
    assert ok["next_command"].endswith(f"await --rid {ok['rid']}") and ok["await"]
    rc, again = _run(env, "consult.py", *base, "--task", "q")
    assert rc == 0 and again["rid"] == ok["rid"], "idempotent repeat returns the original receipt"
    rc, clash = _run(env, "consult.py", *base, "--task", "other")
    assert rc == 2 and clash["code"] == "invalid_request" and "request_key_conflict" in clash["detail"]
    rc, sched = _run(env, "consult.py", *base, "--task", "q2", "--use-by", "garbage")
    assert rc == 2 and "bad_schedule" in sched["detail"]
    rc, argp = _run(env, "consult.py", "fire", "--no-code")
    assert rc == 2 and argp["code"] == "invalid_request" and "--task" in argp["detail"]


@pytest.mark.parametrize("args,rc,code", [
    (["await"], 2, "invalid_request"),
    (["await", "--rid", RID, "--timeout", "1"], 1, "not_found"),
    (["status", "--rid", RID], 0, "not_found"),
    (["status"], 0, "service_unavailable"),
    (["cancel", "--rid", RID], 2, "not_found"),
    (["refire", "--rid", RID], 2, "not_found"),
    (["reconcile", "--rid", RID], 2, "invalid_request"),
    (["reconcile", "--rid", RID, "--not-sent", "--evidence", "looked"], 2, "not_found"),
    (["feedback", "--rid", RID], 2, "invalid_request"),
    (["feedback", "--rid", RID, "--grade", "0", "--reason", "unused", "--action", "x"], 2, "not_found"),
    (["diagnose", "--rid", RID], 1, "not_found"),
    (["bogus-verb"], 2, "invalid_request"),
])
def test_spool_verbs_end_in_one_envelope_on_failure(env, args, rc, code):
    got_rc, out = _run(env, "cgc_spool.py", *args)
    assert (got_rc, out["code"]) == (rc, code), out


def test_spool_verbs_success_envelopes(env):
    _round(store_mod.QUEUED)
    rc, st = _run(env, "cgc_spool.py", "status", "--rid", RID)
    assert rc == 0 and st["rid"] == RID and st["state"] == "queued"
    rc, c = _run(env, "cgc_spool.py", "cancel", "--rid", RID)
    assert rc == 0 and c["code"] == "ok" and c["state"] == "failed"
    rc, c2 = _run(env, "cgc_spool.py", "cancel", "--rid", RID)
    assert rc == 0 and c2["code"] == "ok", "idempotent"
    rc, rf = _run(env, "cgc_spool.py", "refire", "--rid", RID)
    assert rc == 0 and rf["code"] == "ok" and rf["rid"] != RID and "await" in rf["next_command"]
    rc, fb = _run(env, "cgc_spool.py", "feedback", "--rid", RID, "--grade", "0", "--reason",
                  "unused", "--action", "nothing")
    assert rc == 0 and fb["code"] == "ok" and fb["recorded"] == "consult_outcome"
    rc, pol = _run(env, "cgc_spool.py", "feedback", "--policy", "changed one trigger")
    assert rc == 0 and pol["recorded"] == "policy_update"


def test_cancel_and_refire_refusals(env):
    _round(store_mod.POSSIBLY_ACCEPTED, error="unknown_send", conv=CONV)
    rc, c = _run(env, "cgc_spool.py", "cancel", "--rid", RID)
    assert rc == 2 and c["code"] == "invalid_request" and "not_cancellable" in c["detail"]
    rc, rf = _run(env, "cgc_spool.py", "refire", "--rid", RID)
    assert rc == 2 and rf["code"] == "send_uncertain" and "diagnose" in rf["next_command"]
    rc, rec = _run(env, "cgc_spool.py", "reconcile", "--rid", RID, "--not-sent", "--evidence", "x")
    assert rc == 0 and rec["code"] == "ok" and rec["state"] == "failed"


# ---- await: non-success envelopes point at diagnose -----------------------------------------------

@pytest.mark.parametrize("state,error,sent,code,want", [
    (store_mod.FAILED, "CGC_ERROR turn_failed: ChatGPT marked it", True, "remote_failed", "diagnose"),
    (store_mod.FAILED, "CGC_ERROR rid_absent: never found", True, "source_unresolved",
     "enqueue --kind retrieve"),
    (store_mod.FAILED, "CGC_ERROR model_not_selectable: x", False, "ui_contract_broken", "refire"),
    (store_mod.GATE_REJECTED, "refused: secret", False, "policy_denied", "diagnose"),
    (store_mod.POSSIBLY_ACCEPTED, "unknown_send", True, "send_uncertain", "enqueue --kind retrieve"),
])
def test_await_failure_envelope_names_a_move(env, state, error, sent, code, want):
    _round(state, error=error, conv=CONV, sent=sent)
    _, out = _run(env, "cgc_spool.py", "await", "--rid", RID, "--timeout", "5")
    assert out["code"] == code and want in out["next_command"], out
    assert out["error"] == error, "the legacy field is kept"


# ---- diagnose -------------------------------------------------------------------------------------

def _diag(env, full=True):
    rc, out = _run(env, "cgc_spool.py", "diagnose", "--rid", RID)
    raw = subprocess.run([sys.executable, os.path.join(SCRIPTS, "cgc_spool.py"), "diagnose", "--rid",
                          RID], capture_output=True, text=True, env=env).stdout
    assert len(raw.strip().encode()) <= backend.DIAGNOSE_MAX, len(raw)
    for k in ("phases", "attempts", "conversation", "answer_on_disk", "last_marker", "ui", "bundle") if full else ():
        assert k in out
    return out


@pytest.mark.parametrize("setup,code,want", [
    (dict(state=store_mod.POSSIBLY_ACCEPTED, error="unknown_send", conv=CONV), "send_uncertain",
     f"enqueue --kind retrieve --conversation {CONV} --parent {RID}"),
    (dict(state=store_mod.POSSIBLY_ACCEPTED, error="unknown_send"), "send_uncertain",
     f"find-conversation --rid {RID}"),
    (dict(state=store_mod.BLOCKED, error="login_needed", unsent=True), "human_required",
     f"refire --rid {RID}"),
    (dict(state=store_mod.BLOCKED, error="captcha", conv=CONV), "human_required",
     "enqueue --kind retrieve"),
    (dict(state=store_mod.FAILED, error="CGC_ERROR selector_drift: sent, unreadable", sent=True,
          conv=CONV), "ui_contract_broken", "enqueue --kind retrieve"),
    (dict(state=store_mod.BLOCKED, error="model_not_selectable — still failing after 3", unsent=True),
     "ui_contract_broken", f"refire --rid {RID}"),
    # crossed the send fence, no not-sent proof, not a provider-declared failure: retrieve, never re-send
    (dict(state=store_mod.FAILED, error="CGC_ERROR timeout_no_answer: stalled", sent=True),
     "observation_deadline", f"find-conversation --rid {RID}"),
    (dict(state=store_mod.FAILED, error="CGC_ERROR rid_absent: gone", sent=True, conv=CONV),
     "source_unresolved", "enqueue --kind retrieve"),
    (dict(state=store_mod.FAILED, error="CGC_ERROR turn_failed: failed twice", sent=True),
     "remote_failed", None),
    (dict(state=store_mod.COMPLETED_VERIFIED), "result_unverified", f"await --rid {RID}"),  # empty
    # crossed the fence, generic error, no proof: retrieve — never "a NEW --request-key"
    (dict(state=store_mod.FAILED, error="transport_error: connection lost after submit", sent=True,
          conv=CONV), "send_uncertain", "enqueue --kind retrieve"),
    # blocked after an attempt, no proof, no conversation: find it, never "nothing was sent"
    (dict(state=store_mod.BLOCKED, error="captcha", sent=True), "human_required",
     f"find-conversation --rid {RID}"),
])
def test_diagnose_next_command_per_state(env, setup, code, want):
    _round(setup.pop("state"), **setup)
    out = _diag(env)
    assert out["code"] == code, out
    assert (out["next_command"] is None) if want is None else (want in out["next_command"]), out


def test_never_refire_or_new_key_for_a_round_that_may_have_sent(env):
    _round(store_mod.FAILED, error="CGC_ERROR timeout_no_answer: may still answer", sent=True)
    out = _diag(env)
    assert "refire" not in out["next_command"] and "request-key" not in (out["human_action"] or "")


def test_mismatched_attribution_is_not_ok_in_diagnose(env, tmp_path):
    _round(store_mod.COMPLETED_VERIFIED, result="answer", out=tmp_path / "a.txt")
    with store_mod.Store() as s:
        s.db.execute("UPDATE rounds SET attribution=?, model_slug='gpt-4o' WHERE rid=?",
                     (store_mod.ATTR_MISMATCHED, RID))
        s.db.commit()
    out = _diag(env)
    assert out["code"] == "human_required" and "gpt-4o" in out["human_action"]


def test_completed_answer_file_is_rebuilt_from_the_store(env, tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("a stale, different answer")
    _round(store_mod.COMPLETED_VERIFIED, result="verified text", out=a,
           error="unknown_send")   # a stale error_code from before the recovery
    for verb in ("diagnose", "status", "await"):
        _, out = _run(env, "cgc_spool.py", verb, "--rid", RID, *(["--timeout", "2"] if verb == "await" else []))
        assert out["code"] == "ok" and out["answer_path"] == str(a), (verb, out)
        assert out["detail"] is None, "a completed round is classified from its result, not error_code"
    assert a.read_text() == "verified text"


def test_a_leftover_file_is_never_reported_for_a_round_that_has_not_completed(env, tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("a previous round's answer")
    _round(store_mod.QUEUED, out=a)
    out = _diag(env)
    assert out["answer_path"] is None and out["answer_on_disk"] is False


def test_await_diagnose_and_status_agree(env):
    _round(store_mod.BLOCKED, error="captcha", sent=True)
    _, aw = _run(env, "cgc_spool.py", "await", "--rid", RID, "--timeout", "2")
    _, dg = _run(env, "cgc_spool.py", "diagnose", "--rid", RID)
    _, st = _run(env, "cgc_spool.py", "status", "--rid", RID)
    for k in ("code", "next_command", "human_action", "retryable"):
        assert aw[k] == dg[k] == st[k], k
    assert aw["retryable"] is False and "refire" not in aw["next_command"]


def test_await_on_a_blocked_retrieve_recovers_the_source(env):
    src = "REQ-20261014-110000-ffffff"
    with store_mod.Store() as s:
        s.create_round(src, "submit", thread_id=CONV, prompt="CGC_RID:y\n", spec_json="{}")
    _round(store_mod.BLOCKED, error="captcha", kind="retrieve", conv=CONV, prompt=None, parent=src)
    _, out = _run(env, "cgc_spool.py", "await", "--rid", RID, "--timeout", "2")
    assert f"--parent {src}" in out["next_command"] and out["source_rid"] == src


def test_fire_crash_after_commit_keeps_the_rid(env, monkeypatch, capsys):
    """A crash after the round is durably queued must still hand back its rid, with await next."""
    import consult

    def crash(r):
        raise RuntimeError("post-commit failure")
    monkeypatch.setattr(consult, "_fire_envelope", crash)
    monkeypatch.setattr(sys, "argv", ["consult.py", "fire", "--no-code", "--title", "t", "--role",
                                      "r", "--task", "q", "--request-key", "kc"])
    assert consult.main() == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["rid"] and out["code"] == "ok" and f"await --rid {out['rid']}" in out["next_command"]
    with store_mod.Store() as s:
        assert s.get_round(out["rid"])["state"] == "queued"


def test_diagnose_bound_holds_for_a_wide_unicode_path(env, tmp_path):
    deep = tmp_path / ("界" * 60) / ("界" * 60) / ("界" * 60)
    deep.mkdir(parents=True)
    _round(store_mod.COMPLETED_VERIFIED, result="x", out=deep / "answer.txt")
    out = _diag(env, full=False)
    assert out["code"] == "ok" and out["answer_path"]


def test_completed_retrieve_with_missing_file_is_rematerialized(env, tmp_path):
    a = tmp_path / "r.txt"
    _round(store_mod.COMPLETED_VERIFIED, result="verified text", out=a, kind="retrieve", conv=CONV)
    _, out = _run(env, "cgc_spool.py", "diagnose", "--rid", RID)
    assert out["answer_path"] == str(a) and a.read_text() == "verified text"


def test_blocked_retrieve_recovers_its_source_not_itself(env):
    src = "REQ-20261014-110000-ffffff"
    with store_mod.Store() as s:
        s.create_round(src, "submit", thread_id=CONV, prompt="CGC_RID:y\n", spec_json="{}")
    _round(store_mod.BLOCKED, error="captcha", kind="retrieve", conv=CONV, prompt=None)
    with store_mod.Store() as s:
        s.db.execute("UPDATE rounds SET parent_rid=? WHERE rid=?", (src, RID))
        s.db.commit()
    out = _diag(env)
    assert f"--parent {src}" in out["next_command"] and f"--parent {RID}" not in out["next_command"]


def test_commands_are_shell_quoted(monkeypatch):
    import shlex
    monkeypatch.setattr(backend, "__file__", "/opt/Name With Spaces/skill/scripts/cgc_backend.py")
    argv = shlex.split(backend.diagnose_cmd(RID))
    assert argv[1] == "/opt/Name With Spaces/skill/scripts/cgc_spool.py" and argv[-1] == RID


def test_help_and_interrupt_still_end_in_one_envelope(env):
    rc, out = _run(env, "cgc_spool.py", "cancel", "--help")
    assert rc == 0 and out["code"] == "ok"
    _round(store_mod.QUEUED)
    import signal
    import time
    p = subprocess.Popen([sys.executable, os.path.join(SCRIPTS, "cgc_spool.py"), "await", "--rid",
                          RID, "--timeout", "30", "--poll", "10"], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
    time.sleep(1.5)
    p.send_signal(signal.SIGINT)
    so, _ = p.communicate(timeout=20)
    line = json.loads(so.strip().splitlines()[-1])
    assert line["state"] == "interrupted" and "diagnose" in line["next_command"]
    assert sum(1 for ln in so.splitlines() if ln.startswith('{"schema"')) == 1


def test_non_utf8_stdout_still_gets_one_envelope(env):
    _round(store_mod.FAILED, error="CGC_ERROR turn_failed: 界界界", sent=True)
    _, out = _run({**env, "PYTHONIOENCODING": "ascii"}, "cgc_spool.py", "await", "--rid", RID,
                  "--timeout", "2")
    assert out["code"] == "remote_failed" and out["detail"].endswith("界界界")


def test_interrupt_during_the_envelope_write_still_leaves_exactly_one_line(capsys):
    import io
    import signal
    import contextlib

    class SignalOnWrite(io.StringIO):
        def __init__(self):
            super().__init__()
            self.once = True

        def write(self, text):
            if self.once:
                self.once = False
                os.kill(os.getpid(), signal.SIGINT)
            return super().write(text)

        def flush(self):
            if self.once:
                self.once = False
                os.kill(os.getpid(), signal.SIGINT)

    for call in (lambda: backend.envelope(code="ok") and 0, lambda: 2):
        sink = SignalOnWrite()
        with contextlib.redirect_stdout(sink):
            backend.agent_cli(call)
        lines = [ln for ln in sink.getvalue().splitlines() if ln.startswith('{"schema"')]
        assert len(lines) == 1, sink.getvalue()


def test_emitted_flag_is_per_invocation(capsys):
    backend.envelope(code="ok")
    capsys.readouterr()
    assert backend.agent_cli(lambda: 2, lambda: RID) == 2
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["state"] == "refused"


def test_diagnose_gated_round_names_probe_and_bundle(env, tmp_path):
    _round(store_mod.QUEUED)
    with store_mod.Store() as s:
        s.mark_broken("composer", phase="paste", cause="composer node moved")
    b = tmp_path / "state" / "incidents" / "I-1"
    b.mkdir(parents=True)
    (b / "diagnosis.json").write_text(json.dumps({"rid": RID}))
    out = _diag(env)
    assert out["code"] == "ui_contract_broken" and "cgc_probe.py --status" in out["next_command"]
    assert out["ui"]["broken"] == {"composer": "composer node moved"}
    assert out["bundle"] == str(b / "diagnosis.json")


def test_diagnose_is_bounded_with_huge_fields(env, tmp_path):
    _round(store_mod.FAILED, error="CGC_ERROR selector_drift: " + "x" * 5000)
    log = tmp_path / "state" / "spool" / "logs" / f"{RID}.log"
    log.parent.mkdir(parents=True)
    log.write_text("noise\n" * 100 + "CGC_ERROR selector_drift: " + "y" * 4000 + "\nCGC_WAIT tail\n")
    out = _diag(env)
    assert out["last_marker"] is None or out["last_marker"].startswith("CGC_ERROR selector_drift")
    assert out["next_command"], "the decision survives the trim"


def test_diagnose_bound_holds_with_every_capability_broken_and_wide_text(env, tmp_path):
    _round(store_mod.QUEUED, spec={"model": "Pro", "model_family": "GPT-6", "mentions": ["App"]})
    with store_mod.Store() as s:
        for cap in ("composer", "mention", "reader"):
            s.mark_broken(cap, phase="p", cause="界" * 80)
        s.mark_broken("model", phase="p", cause="界" * 80, ref="model:Pro@GPT-6")
        s.db.execute("UPDATE rounds SET error_code=? WHERE rid=?", ("界" * 400, RID))
        s.db.commit()
    out = _diag(env, full=False)   # optional diagnostics may drop; the decision may not
    assert out["code"] == "ui_contract_broken" and out["next_command"] and "attempts" in out


def test_diagnose_reports_phases_and_attempts(env):
    with store_mod.Store() as s:
        s.create_round(RID, "submit", prompt="CGC_RID:x\n", spec_json="{}")
        s.claim_ready("d1")
        att = s.begin_send(RID, "CGC_RID:x\n", store_mod.sha256("CGC_RID:x\n"), daemon_instance_id="d1")
        s.mark_possibly_accepted(att, "unknown_send")
    out = _diag(env)
    assert {"created", "claimed", "begin_send", "finished"} <= set(out["phases"]), out["phases"]
    assert out["attempts"] == 1 and out["state"] == "possibly_accepted"


# ---- the taxonomy table ---------------------------------------------------------------------------

def test_every_state_has_a_code():
    missing = [st for st in store_mod._LEGAL if st not in backend.STATE_CODE]
    assert not missing, f"new state(s) without a code mapping: {missing}"
    assert set(backend.STATE_CODE.values()) <= set(backend.CODES)
    assert set(backend.ERROR_CODE.values()) <= set(backend.CODES)


def test_every_cgc_error_name_has_a_code():
    names = set()
    for f in glob.glob(os.path.join(SCRIPTS, "*.py")):
        names |= set(re.findall(r"CGC_(?:ERROR|BLOCKER) ([a-z][a-z_]+)[:\s\"\\]", open(f).read()))
    names -= {"line"}   # prose: "prints a CGC_ERROR line"
    missing = sorted(n for n in names if n not in backend.ERROR_CODE)
    assert not missing, f"CGC_ERROR name(s) without a code: {missing}"


@pytest.mark.parametrize("error,code", [
    ("submit did not confirm the send (exit 1)", "send_uncertain"),
    ("submit failed before the click (exit 6): boom", "service_unavailable"),
    ("wait produced no answer (exit 4) — retrieve, don't resend", "send_uncertain"),
    ("observation deadline passed — retrieve, don't resend", "observation_deadline"),
    ("expired: use-by 2026-01-01 passed before send", "observation_deadline"),
    ("unverified: could not check whether repo a/b is public", "service_unavailable"),
    ("refused: repo a/b is not confirmed PUBLIC", "policy_denied"),
    ("cancelled by caller before send", "ok"),
    ("model_not_selectable — still failing after 3 automatic retries", "ui_contract_broken"),
    ("mention_not_found: popup offered Foo", "human_required"),
    ("daemon died while sending — uncertain, not resent", "send_uncertain"),
    ("accepted but no conversation to reattach — retrieve manually", "send_uncertain"),
    ("retrieve could not confirm source_rid=R (exit 4)", "source_unresolved"),
    ("ChatGPT failed the turn; the automatic continue may have been sent", "send_uncertain"),
    ("reconciled not-sent (proven): looked", "service_unavailable"),
])
def test_stored_error_texts_map_to_their_class(error, code):
    assert backend.code_of(store_mod.FAILED, error) == code
