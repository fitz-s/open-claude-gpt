# Tests for the usage feedback loop: intent metadata and scheduling on `fire`, use-by expiry at
# dispatch, local grading (`feedback`), the rolling-week report, and the SessionStart snapshot.
# Everything runs against the suite's isolated store (tests/conftest.py pins CGC_STORE_DB); nothing
# here may touch the user's real control.db.
import argparse
import datetime
import importlib.util
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


consult = _load("consult")
import cgc_backend as backend  # noqa: E402
import cgc_store as store_mod  # noqa: E402


def _fire_ns(tmp, **over):
    ns = argparse.Namespace(
        repo_dir=ROOT, repo=None, pr=None, ref=None, compare=None, pulls=False, blobs=False,
        files=None, issues=None, base=None, verify=False, allow_nonpublic=False, backend="cdp",
        window_template=None, task="probe", title="t", role=None, followup=False, no_code=True,
        context_file=None, refs_file=None, output_file=None, output_replace=False,
        target_chars=32000, hard_chars=40000, expect_minutes=25, model="Pro", out=None,
        project_url="https://chatgpt.com/", conversation=None, mention=[])
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture
def fire(tmp_path, monkeypatch):
    """Run cmd_fire up to (and through) the real enqueue into the isolated store."""
    monkeypatch.setattr(consult, "CGC_STATE_DIR", str(tmp_path))
    import cgc_spool
    monkeypatch.setattr(cgc_spool, "daemon_alive", lambda: True)
    monkeypatch.setattr(cgc_spool, "daemon_identity", lambda: None)

    def go(**over):
        return consult.cmd_fire(_fire_ns(tmp_path, **over))
    return go


def _spec(rid):
    with store_mod.Store() as s:
        r = s.get_round(rid)
    return r, json.loads(r["spec_json"])


# ---- item 2: intent metadata ---------------------------------------------------------------------

def test_fire_stores_intent_and_policy_rev_in_the_spec(fire):
    out = fire(origin="agent", trigger="design-fork", decision="pick the queue shape",
               baseline="use a table", check="a benchmark")
    assert isinstance(out, dict)
    _r, spec = _spec(out["rid"])
    meta = spec["consult_meta"]
    assert meta["origin"] == "agent" and meta["trigger"] == "design-fork"
    assert meta["decision"] == "pick the queue shape" and meta["baseline"] == "use a table"
    assert meta["check"] == "a benchmark"
    assert len(meta["policy_rev"]) == 12 and int(meta["policy_rev"], 16) >= 0


def test_fire_without_intent_still_records_policy_rev_only(fire):
    out = fire()
    _r, spec = _spec(out["rid"])
    assert list(spec["consult_meta"]) == ["policy_rev"]


def test_intent_is_never_rendered_into_the_prompt(fire):
    out = fire(decision="DECISION-MARKER", baseline="BASELINE-MARKER", check="CHECK-MARKER",
               trigger="math", origin="user")
    r, _ = _spec(out["rid"])
    for needle in ("DECISION-MARKER", "BASELINE-MARKER", "CHECK-MARKER", "design-fork"):
        assert needle not in r["rendered_prompt"]


def test_policy_rev_tracks_the_policy_files(tmp_path, monkeypatch):
    base = consult.policy_rev()
    assert base == consult.policy_rev(), "stable across calls"
    monkeypatch.setattr(consult, "PROMPT_TEMPLATE", consult.PROMPT_TEMPLATE + " x")
    assert consult.policy_rev() != base, "editing a prompt template changes the revision"


def _fp(**meta):
    ns = argparse.Namespace(kind="submit", logical_sha="x", project_url="u", model="Pro",
                            model_family="GPT-6", parent=None, conversation=None, mentions=[])
    if meta is not None:
        ns.consult_meta = meta
    return backend._request_fingerprint(ns, "p")


def test_intent_joins_the_fingerprint_only_when_set():
    plain = _fp()
    assert _fp(policy_rev="abc", use_by="2030-01-01T00:00:00+00:00") == plain, \
        "policy_rev and the schedule are not part of a request's identity"
    assert _fp(decision="d") != plain
    assert _fp(decision="d") != _fp(decision="e")
    legacy = argparse.Namespace(kind="submit", logical_sha="x", project_url="u", model="Pro",
                                model_family="GPT-6", parent=None, conversation=None, mentions=[])
    assert backend._request_fingerprint(legacy, "p") == plain, "no meta attr == empty meta"


# ---- item 3: scheduling --------------------------------------------------------------------------

@pytest.mark.parametrize("v,secs", [("+90m", 5400), ("+2h", 7200), ("+1d", 86400), ("+1h30m", 5400)])
def test_when_parses_durations(v, secs):
    t = consult._when(v)
    delta = (t - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    assert abs(delta - secs) < 5 and t.utcoffset() == datetime.timedelta(0)


def test_when_parses_iso_and_refuses_junk():
    assert consult._when("2031-02-03T04:05:06Z").isoformat() == "2031-02-03T04:05:06+00:00"
    assert consult._when("2031-02-03T04:05:06+02:00").isoformat() == "2031-02-03T02:05:06+00:00"
    for bad in ("tomorrow", "+2", "2h", "+h", ""):
        with pytest.raises(ValueError):
            consult._when(bad)


def test_not_before_writes_the_round_and_claim_ready_honours_it(fire):
    out = fire(not_before="+2h")
    r, _ = _spec(out["rid"])
    assert r["not_before"] and r["state"] == store_mod.QUEUED
    with store_mod.Store() as s:
        assert s.claim_ready("d") is None, "a future not_before keeps the round in the backlog"
        s.db.execute("UPDATE rounds SET not_before=? WHERE rid=?",
                     ((datetime.datetime.now(datetime.timezone.utc)
                       - datetime.timedelta(seconds=1)).isoformat(), out["rid"]))
        assert s.claim_ready("d")["rid"] == out["rid"], "due once the instant passes"


def test_a_bad_schedule_queues_nothing(fire, capsys):
    assert fire(not_before="whenever") == 2
    assert fire(use_by="+1h", not_before="+2h") == 2, "use-by before not-before can never send"
    assert fire(use_by="2001-01-01T00:00:00Z") == 2, "a use-by in the past can never send"
    err = capsys.readouterr().err
    assert err.count("bad_schedule") == 3
    with store_mod.Store() as s:
        assert s.db.execute("SELECT count(*) FROM rounds").fetchone()[0] == 0


def _ready(fire, **over):
    out = fire(**over)
    with store_mod.Store() as s:
        s.db.execute("UPDATE rounds SET not_before=NULL WHERE rid=?", (out["rid"],))
        r = s.claim_ready("d")
    assert r["rid"] == out["rid"]
    return r


def test_a_round_past_use_by_never_sends_and_is_not_charged(fire):
    r = _ready(fire, use_by="+1h")
    with store_mod.Store() as s:
        # the deadline passes while the round waits in the backlog
        spec = json.loads(r["spec_json"])
        spec["consult_meta"]["use_by"] = "2001-01-01T00:00:00+00:00"
        s.db.execute("UPDATE rounds SET spec_json=? WHERE rid=?", (json.dumps(spec), r["rid"]))
        r = s.get_round(r["rid"])
        sent = []
        final = backend.process_round(s, r, lambda *a, **k: sent.append(a) or {"code": 0},
                                      daemon_instance_id="d", validate=lambda p: (True, "ok"))
        row = s.get_round(r["rid"])
        assert final == store_mod.FAILED and sent == [], "an expired round must not reach the driver"
        assert row["error_code"] == "expired: use-by 2001-01-01T00:00:00+00:00 passed before send"
        assert row["send_disposition"] == store_mod.NOT_SENT_PROVEN
        assert store_mod.spent_messages(s.db) == [], "never charged"
        assert backend._release_eligible(s, row), "its request-key is released"


def test_a_round_before_use_by_sends_normally(fire):
    r = _ready(fire, use_by="+1h")
    with store_mod.Store() as s:
        calls = []

        def cdp(kind, **kw):
            calls.append(kind)
            return {"code": 6, "stderr": ""}
        backend.process_round(s, r, cdp, daemon_instance_id="d", validate=lambda p: (True, "ok"))
        assert calls == ["submit"]


def test_refire_replays_intent_but_not_the_deadline(fire, monkeypatch):
    r = _ready(fire, use_by="+1h", trigger="math", decision="d1")
    with store_mod.Store() as s:
        s.set_state(r["rid"], store_mod.FAILED, expect=store_mod.READY,
                    send_disposition=store_mod.NOT_SENT_PROVEN, error_code="expired")
    import cgc_spool
    monkeypatch.setattr(cgc_spool, "daemon_alive", lambda: True)
    assert backend.refire_round(r["rid"]) == 0
    with store_mod.Store() as s:
        new = s.db.execute("SELECT rid FROM rounds WHERE rid<>?", (r["rid"],)).fetchone()["rid"]
    _r2, spec = _spec(new)
    assert spec["consult_meta"]["trigger"] == "math" and "use_by" not in spec["consult_meta"]
    assert spec["consult_meta"]["policy_rev"]


def test_fire_cli_accepts_the_new_flags(tmp_path):
    """A CLI smoke: the real parser takes the intent and schedule flags and queues the round."""
    import subprocess
    env = {**os.environ, "CGC_STATE_DIR": str(tmp_path / "s"), "CGC_SPOOL_DIR": str(tmp_path / "sp"),
           "CGC_DATA_DIR": str(tmp_path / "d"), "CGC_STORE_DB": str(tmp_path / "d" / "control.db"),
           "PATH": "/usr/bin:/bin"}
    cli = [sys.executable, os.path.join(SCRIPTS, "consult.py"), "fire", "--no-code", "--task", "probe"]
    r = subprocess.run(
        cli + ["--origin", "agent", "--trigger", "math", "--decision", "d", "--baseline", "b",
               "--check", "c", "--not-before", "+3h", "--use-by", "+1d"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr
    rid = json.loads(r.stdout)["rid"]
    db = sqlite3.connect(str(tmp_path / "d" / "control.db"))
    nb, spec = db.execute("SELECT not_before, spec_json FROM rounds WHERE rid=?", (rid,)).fetchone()
    meta = json.loads(spec)["consult_meta"]
    assert nb and meta["use_by"] and meta["trigger"] == "math" and meta["check"] == "c"
    bad = subprocess.run(cli + ["--trigger", "nonsense"], capture_output=True, text=True, env=env,
                         cwd=ROOT, timeout=60)
    assert bad.returncode == 2 and "invalid choice" in bad.stderr
