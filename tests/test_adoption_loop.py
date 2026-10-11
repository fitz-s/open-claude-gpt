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


# ---- item 4: feedback ----------------------------------------------------------------------------

def _fb(**kw):
    d = dict(rid=None, grade=None, reason=None, action=None, evidence=None, policy=None)
    d.update(kw)
    return argparse.Namespace(**d)


def _completed(rid="REQ-20260707-120000-0a0001", kind="submit", spec=None, parent=None,
               prompt="# Title of it\n\nbody", conv="conv-1"):
    with store_mod.Store() as s:
        s.create_round(rid, kind, thread_id=conv, prompt=prompt, parent_rid=parent,
                       spec_json=json.dumps(spec or {}))
        s.set_state(rid, store_mod.READY)
        aid = s.begin_send(rid, prompt, "h" * 64, daemon_instance_id="d")
        s.mark_accepted(aid, conv)
        s.mark_waiting(rid)
        s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="a")
    return rid


def _outcomes(rid):
    with store_mod.Store() as s:
        return [e for e in s.events(rid) if e["kind"] == "consult_outcome"]


def test_feedback_records_a_consult_outcome_and_the_latest_wins():
    rid = _completed()
    assert backend.record_feedback(_fb(rid=rid, grade=0, reason="unused", action="skipped it")) == 0
    assert backend.record_feedback(_fb(rid=rid, grade=2, reason="defect-caught", action="fixed",
                                       evidence="commit abc1234")) == 0
    evs = _outcomes(rid)
    assert len(evs) == 2, "history is kept; the latest event is the grade that stands"
    assert json.loads(evs[-1]["detail"]) == {"grade": 2, "reason": "defect-caught", "action": "fixed",
                                             "evidence": "commit abc1234"}
    w = _week()
    assert w["grades"]["by_trigger"]["none"] == {"2": 1}, "only the latest grade is counted"
    assert w["useful"]["outcomes"] == 1


@pytest.mark.parametrize("grade", [1, 2])
def test_a_positive_grade_requires_evidence(grade, capsys):
    rid = _completed()
    for ev in (None, "", "   "):
        assert backend.record_feedback(_fb(rid=rid, grade=grade, reason="useful", action="a",
                                           evidence=ev)) == 2
    assert "evidence_required" in capsys.readouterr().err and _outcomes(rid) == []


@pytest.mark.parametrize("grade", [-1, 0])
def test_a_non_positive_grade_needs_no_evidence(grade):
    rid = _completed()
    assert backend.record_feedback(_fb(rid=rid, grade=grade, reason="unused", action="a")) == 0


def test_feedback_refuses_unknown_and_incomplete(capsys):
    assert backend.record_feedback(_fb(rid="REQ-20260707-120000-ffffff", grade=0, reason="late",
                                       action="a")) == 2
    assert "no_such_round" in capsys.readouterr().err
    assert backend.record_feedback(_fb(rid="x", grade=0)) == 2
    assert "feedback_incomplete" in capsys.readouterr().err


def test_feedback_never_touches_updated_at():
    """Latency stats read rounds.updated_at; grading a day later must not change a round's latency."""
    rid = _completed()
    with store_mod.Store() as s:
        before = s.get_round(rid)["updated_at"]
    backend.record_feedback(_fb(rid=rid, grade=0, reason="late", action="a"))
    with store_mod.Store() as s:
        assert s.get_round(rid)["updated_at"] == before


def test_retrieve_feedback_resolves_to_the_source_round():
    src = _completed("REQ-20260707-120000-0a0002")
    ret = "REQ-20260707-120000-0a0003"
    with store_mod.Store() as s:
        s.create_round(ret, "retrieve", thread_id="conv-1", prompt=None, parent_rid=src)
    assert backend.record_feedback(_fb(rid=ret, grade=1, reason="useful", action="a",
                                       evidence="test_x")) == 0
    assert len(_outcomes(src)) == 1 and _outcomes(ret) == []
    assert json.loads(_outcomes(src)[0]["detail"])["via_retrieve"] == ret


def test_policy_update_is_recorded_with_the_current_policy_rev():
    assert backend.record_feedback(_fb(policy="  tightened the math trigger  ")) == 0
    with store_mod.Store() as s:
        ev = s.db.execute("SELECT detail FROM events WHERE kind='policy_update'").fetchone()
    d = json.loads(ev["detail"])
    assert d == {"policy_rev": consult.policy_rev(), "text": "tightened the math trigger"}
    assert backend.record_feedback(_fb(policy="x", rid="r")) == 2, "--policy takes no other flag"
    assert backend.record_feedback(_fb(policy="  ")) == 2


def test_feedback_cli_is_wired_in_cgc_spool_and_bin_cgc(tmp_path):
    import subprocess
    env = {**os.environ, "CGC_STATE_DIR": str(tmp_path / "s"), "CGC_DATA_DIR": str(tmp_path / "d"),
           "CGC_STORE_DB": str(tmp_path / "d" / "control.db"), "PATH": "/usr/bin:/bin"}
    spool = [sys.executable, os.path.join(SCRIPTS, "cgc_spool.py"), "feedback"]
    r = subprocess.run(spool + ["--rid", "REQ-20260707-120000-ffffff", "--grade", "3",
                                "--reason", "late", "--action", "a"],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 2 and "invalid choice" in r.stderr
    r = subprocess.run(spool + ["--policy", "p"], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and json.loads(r.stdout)["recorded"] == "policy_update"
    bin_cgc = open(os.path.join(ROOT, "bin", "cgc"), encoding="utf-8").read()
    assert "enqueue|await|cancel|stats|feedback)" in bin_cgc, "cgc must dispatch feedback"


# ---- item 7: --workspace as a declared source ----------------------------------------------------

@pytest.fixture
def spool(monkeypatch):
    import cgc_spool
    monkeypatch.setattr(cgc_spool, "_repo_is_public", lambda slug: (True, "public"))
    monkeypatch.setattr(cgc_spool, "_gh_exists", lambda path: (True, "exists"))
    return cgc_spool


def _ws_prompt(fire, **over):
    out = fire(no_code=False, workspace="my repo", mention=["WebCodex Demo"], **over)
    assert isinstance(out, dict), out
    return _spec(out["rid"])


def test_workspace_replaces_the_code_link_and_renders_a_short_source_block(fire):
    r, spec = _ws_prompt(fire)
    p = r["rendered_prompt"]
    assert "Source: local workspace `my repo`, reachable through the \"WebCodex Demo\" app's tools; " \
           "state the revision/as-of time you read." in p
    assert "references no code" not in p, "the round no longer falsely claims --no-code"
    assert spec["mentions"] == ["WebCodex Demo"]
    assert "github.com" not in p, "no deliver ran: a workspace-only round needs no pushed branch"


def test_workspace_requires_a_mention_and_refuses_no_code(fire, capsys):
    assert fire(no_code=False, workspace="my repo", mention=[]) == 2
    assert "workspace_needs_mention" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        consult.cmd_prep(_fire_ns(None, no_code=True, workspace="w", mention=["A"]))
    assert "workspace_conflict" in str(e.value)
    with store_mod.Store() as s:
        assert s.db.execute("SELECT count(*) FROM rounds").fetchone()[0] == 0


def test_the_source_rule_still_demands_something_without_a_workspace(fire):
    with pytest.raises(SystemExit) as e:
        consult.cmd_prep(_fire_ns(None, no_code=False, workspace=None))
    assert "no_code_source" in str(e.value)


def test_workspace_rides_beside_links_when_both_are_given(fire, monkeypatch):
    monkeypatch.setattr(consult, "_gh", lambda *a, **k: (None, "offline"))
    out = fire(no_code=False, workspace="my repo", mention=["WebCodex Demo"], repo="acme/widgets",
               ref="a" * 40)
    p = _spec(out["rid"])[0]["rendered_prompt"]
    assert "Source: local workspace `my repo`" in p and "https://github.com/acme/widgets/tree/" in p


def test_workspace_block_renders_on_a_followup_too(tmp_path, monkeypatch):
    monkeypatch.setattr(consult, "CGC_STATE_DIR", str(tmp_path))
    st = consult.cmd_prep(_fire_ns(None, no_code=False, workspace="my repo", mention=["A"],
                                   followup=True))
    assert "Source: local workspace `my repo`" in open(st["prompt_file"], encoding="utf-8").read()


def test_all_four_source_layers_agree_on_a_workspace_prompt(fire, spool):
    """prep renders it, the enqueue pre-check, the daemon gate and the driver's send-time backstop
    all accept it — and all refuse the same prose without the declaration."""
    cdp = _load("cdp_consult")
    r, _ = _ws_prompt(fire)
    p = r["rendered_prompt"]
    assert spool.validate_prompt(p)[0] and cdp._has_sendable_source(p)
    assert consult.WORKSPACE_SENTINEL.lower() == spool._WORKSPACE_SENTINEL == cdp._WORKSPACE_SENTINEL
    prose = "Please read my local workspace for the stall, it is on my machine."
    assert not spool.validate_prompt(prose)[0] and not cdp._has_sendable_source(prose)


def test_enqueue_precheck_requires_a_mention_for_a_workspace_prompt(fire, spool, tmp_path, capsys):
    r, _ = _ws_prompt(fire)
    pf = tmp_path / "p.md"
    pf.write_text(r["rendered_prompt"], encoding="utf-8")

    def ns(**over):
        d = dict(rid=None, prompt_file=str(pf), kind="submit", project_url="https://chatgpt.com/",
                 conversation="auto", parent=None, model="Pro", model_family="GPT-6",
                 request_key=None, logical_sha=None, out=None, poll=1, timeout=5, quiet=True)
        d.update(over)
        return argparse.Namespace(**d)
    assert spool.cmd_enqueue(ns()) == 2
    assert "workspace_needs_mention" in capsys.readouterr().err
    spool.cmd_enqueue(ns(mentions=["WebCodex Demo"]))
    err = capsys.readouterr().err
    assert "no_code_source" not in err and "workspace_needs_mention" not in err, err


def test_a_workspace_prompt_keeps_every_secret_and_url_gate(fire, spool, monkeypatch):
    cdp = _load("cdp_consult")
    r, _ = _ws_prompt(fire)
    base = r["rendered_prompt"]
    ok, why = spool.validate_prompt(base + "\nAKIAABCDEFGHIJKLMNOP\n")
    assert not ok and "key" in why.lower(), "secret shapes are still refused"
    monkeypatch.setattr(spool, "_repo_is_public", lambda slug: (False, "private"))
    ok, why = spool.validate_prompt(base + "\nhttps://github.com/acme/secret\n")
    assert not ok and "not confirmed PUBLIC" in why, "URL provenance is still enforced"
    ok, why = spool.validate_prompt(base + "\nhttps://gist.github.com/u/deadbeef\n")
    assert not ok and "gist" in why
    monkeypatch.setenv("CGC_GATE_ALLOW_GIST", "0")
    assert cdp._has_sendable_source(base), "the backstop only decides source; the egress gate decides safety"


@pytest.mark.parametrize("env,given,want", [
    ("", "Anything", "Anything"),
    ("My Repo, Data Lake", "my repo", "My Repo"),
    ("My Repo, Data Lake", "DATA", "Data Lake"),
    ("My Repo, My Other", "my", "my"),            # ambiguous prefix passes through
    ("My Repo", "elsewhere", "elsewhere"),         # unlisted passes through
])
def test_workspace_names_snap_like_apps(monkeypatch, env, given, want):
    monkeypatch.setenv("CGC_WORKSPACES", env)
    assert consult._workspace_name(given) == want


def test_workspace_name_refuses_what_would_break_the_backtick_span():
    for bad in ("", "a`b", "x" * 81, "a\x07b"):
        with pytest.raises(argparse.ArgumentTypeError):
            consult._workspace_name(bad)


def test_apps_lists_workspaces_next_to_apps(monkeypatch, capsys):
    monkeypatch.delenv("CGC_WORKSPACES", raising=False)
    assert consult.cmd_apps(None) == 0
    assert json.loads(capsys.readouterr().out)["workspaces"] == []
    monkeypatch.setenv("CGC_WORKSPACES", "My Repo, ,Data Lake")
    consult.cmd_apps(None)
    assert json.loads(capsys.readouterr().out)["workspaces"] == ["My Repo", "Data Lake"]


# ---- item 5: the week view -----------------------------------------------------------------------

def _week():
    with store_mod.Store() as s:
        return store_mod.week_view(s.db)


def _age(rid, days):
    """Backdate a round's events and timestamps so it falls outside the rolling window."""
    ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    with store_mod.Store() as s:
        s.db.execute("UPDATE events SET at=? WHERE rid=?", (ts, rid))
        s.db.execute("UPDATE attempts SET started_at=? WHERE rid=?", (ts, rid))
        s.db.execute("UPDATE rounds SET updated_at=?, created_at=? WHERE rid=?", (ts, ts, rid))


def test_week_budget_counts_only_the_rolling_window(monkeypatch):
    monkeypatch.setenv("CGC_WEEKLY_BUDGET", "10")
    _completed("REQ-20260707-120000-0b0001")
    old = _completed("REQ-20260707-120000-0b0002")
    _age(old, 8)
    assert _week()["budget"] == {"weekly": 10, "spent": 1, "available": 9}


def test_week_budget_unset_says_so(monkeypatch):
    monkeypatch.delenv("CGC_WEEKLY_BUDGET", raising=False)
    b = _week()["budget"]
    assert b["weekly"] is None and b["available"] is None and "budget unset" in b["note"]


def test_week_lists_ungraded_completed_rounds_with_titles():
    a = _completed("REQ-20260707-120000-0b0003", prompt="# The queue question\n\nx")
    g = _completed("REQ-20260707-120000-0b0004")
    backend.record_feedback(_fb(rid=g, grade=0, reason="unused", action="a"))
    assert [(u["rid"], u["title"]) for u in _week()["ungraded"]] == [(a, "The queue question")]


def test_week_backlog_and_expired(fire):
    waiting = fire(not_before="+2h", use_by="+1d", trigger="math")
    due = fire()
    with store_mod.Store() as s:
        s.db.execute("UPDATE rounds SET not_before=NULL WHERE rid=?", (due["rid"],))
    w = _week()
    assert [b["rid"] for b in w["backlog"]] == [waiting["rid"]], "only a FUTURE not_before is backlog"
    assert w["backlog"][0]["use_by"]
    with store_mod.Store() as s:
        s.db.execute("UPDATE rounds SET not_before=NULL WHERE rid=?", (waiting["rid"],))
        s.db.execute("UPDATE rounds SET state='failed' WHERE rid=?", (due["rid"],))
        row = s.claim_ready("d")
        spec = json.loads(row["spec_json"])
        spec["consult_meta"]["use_by"] = "2001-01-01T00:00:00+00:00"
        s.db.execute("UPDATE rounds SET spec_json=? WHERE rid=?", (json.dumps(spec), row["rid"]))
        backend.process_round(s, s.get_round(row["rid"]), lambda *a, **k: {"code": 0},
                              daemon_instance_id="d", validate=lambda p: (True, "ok"))
    w = _week()
    assert [e["rid"] for e in w["expired"]] == [waiting["rid"]] and w["backlog"] == []


def test_week_grades_by_trigger_and_mention_and_useful_rate(monkeypatch):
    monkeypatch.setenv("CGC_WEEKLY_BUDGET", "50")
    spec_m = {"mentions": ["WebCodex Demo"], "consult_meta": {"trigger": "workspace-evidence"}}
    spec_p = {"mentions": [], "consult_meta": {"trigger": "math"}}
    a = _completed("REQ-20260707-120000-0c1001", spec=spec_m)
    b = _completed("REQ-20260707-120000-0c1002", spec=spec_p)
    c = _completed("REQ-20260707-120000-0c1003", spec=spec_p)
    backend.record_feedback(_fb(rid=a, grade=2, reason="defect-caught", action="x", evidence="e"))
    backend.record_feedback(_fb(rid=b, grade=0, reason="unused", action="x"))
    backend.record_feedback(_fb(rid=c, grade=1, reason="useful", action="x", evidence="e"))
    w = _week()
    assert w["grades"]["by_trigger"] == {"workspace-evidence": {"2": 1}, "math": {"0": 1, "1": 1}}
    assert w["grades"]["by_mention"] == {"with_mention": {"2": 1}, "without_mention": {"0": 1, "1": 1}}
    assert w["useful"] == {"outcomes": 2, "per_spent_message": round(2 / 3, 3)}
    assert w["ungraded"] == []


def test_week_policy_update_flag():
    assert _week()["policy_update"] == {"happened": False}
    backend.record_feedback(_fb(policy="changed the math trigger"))
    pu = _week()["policy_update"]
    assert pu["happened"] and pu["text"] == "changed the math trigger"


def test_stats_week_prints_json_and_plain_stats_is_unchanged(capsys):
    _completed()
    assert backend.week_report() == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["budget"]["spent"] == 1 and "CGC_WEEK" in cap.err
    assert backend.stats_report() == 0
    rep = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert set(rep) == {"rounds", "by_state", "completed", "failed", "unverified_rate", "latency_s",
                        "messages"}
