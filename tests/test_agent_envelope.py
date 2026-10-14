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
           prompt=f"CGC_RID:{RID}\nhi\nBEGIN_RESPONSE:{RID}\n<a>\nEND_RESPONSE:{RID}\n"):
    with store_mod.Store() as s:
        s.create_round(RID, kind, thread_id=conv, prompt=prompt, out_path="/nonexistent/answer.txt",
                       spec_json=json.dumps(spec or {"model": "Pro", "model_family": "GPT-6"}))
        s.db.execute("UPDATE rounds SET state=?, error_code=?, send_disposition=? WHERE rid=?",
                     (state, error, store_mod.NOT_SENT_PROVEN if unsent else None, RID))
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

@pytest.mark.parametrize("state,error,code,want", [
    (store_mod.FAILED, "CGC_ERROR turn_failed: ChatGPT marked it", "remote_failed", "diagnose"),
    (store_mod.FAILED, "CGC_ERROR rid_absent: never found", "source_unresolved", "diagnose"),
    (store_mod.GATE_REJECTED, "refused: secret", "policy_denied", "diagnose"),
    (store_mod.POSSIBLY_ACCEPTED, "unknown_send", "send_uncertain", "enqueue --kind retrieve"),
])
def test_await_failure_envelope_names_a_move(env, state, error, code, want):
    _round(state, error=error, conv=CONV)
    _, out = _run(env, "cgc_spool.py", "await", "--rid", RID, "--timeout", "5")
    assert out["code"] == code and want in out["next_command"], out
    assert out["error"] == error, "the legacy field is kept"


# ---- diagnose -------------------------------------------------------------------------------------

def _diag(env):
    rc, out = _run(env, "cgc_spool.py", "diagnose", "--rid", RID)
    raw = subprocess.run([sys.executable, os.path.join(SCRIPTS, "cgc_spool.py"), "diagnose", "--rid",
                          RID], capture_output=True, text=True, env=env).stdout
    assert len(raw.strip().encode()) <= backend.DIAGNOSE_MAX, len(raw)
    for k in ("phases", "attempts", "conversation", "answer_on_disk", "last_marker", "ui", "bundle"):
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
    (dict(state=store_mod.FAILED, error="CGC_ERROR selector_drift: sent, unreadable"),
     "ui_contract_broken", "cgc_probe.py --status"),
    (dict(state=store_mod.FAILED, error="CGC_ERROR timeout_no_answer: stalled", sent=True),
     "observation_deadline", None),
    (dict(state=store_mod.FAILED, error="CGC_ERROR turn_failed: failed twice", sent=True),
     "remote_failed", None),
    (dict(state=store_mod.COMPLETED_VERIFIED), "ok", None),
])
def test_diagnose_next_command_per_state(env, setup, code, want):
    _round(setup.pop("state"), **setup)
    out = _diag(env)
    assert out["code"] == code, out
    assert (out["next_command"] is None) if want is None else (want in out["next_command"]), out


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
