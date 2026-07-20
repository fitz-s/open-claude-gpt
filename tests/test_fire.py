# Tests for `consult.py fire` — the fused deliver -> prep -> enqueue path.
#
# The command shipped with no coverage at all, which a review of the fused commit called out
# alongside two release-stopping defects. Both are pinned here.
import argparse
import importlib.util
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSULT = os.path.join(ROOT, "skill", "scripts", "consult.py")


def _load():
    spec = importlib.util.spec_from_file_location("consult", CONSULT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ns(mod, tmp, **over):
    ns = argparse.Namespace(
        repo_dir=ROOT, repo="acme/widgets", pr=None, ref="a" * 40, compare=None,
        pulls=False, blobs=False, files=None, issues=None, base=None,
        verify=False, allow_nonpublic=False,
        backend="cdp", window_template=None, task="probe", title="t", role=None,
        followup=False, no_code=False, context_file=None, refs_file=None,
        output_file=None, output_replace=False, target_chars=32000, hard_chars=40000,
        expect_minutes=25, model="Pro", out=None,
        project_url="https://chatgpt.com/", conversation=None)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_followup_without_a_conversation_is_refused_before_any_side_effect(tmp_path, monkeypatch, capsys):
    """fire inherits --followup from prep's arguments. Enqueuing it as kind='submit' with
    conversation='auto' would open a FRESH conversation and silently lose everything the thread
    knew — no error, wrong answer, which is the worst shape a bug can take."""
    mod = _load()
    monkeypatch.setattr(mod, "CGC_STATE_DIR", str(tmp_path))
    called = []
    monkeypatch.setattr(mod, "cmd_deliver", lambda a: called.append("deliver"))
    assert mod.cmd_fire(_ns(mod, tmp_path, followup=True, no_code=True)) == 2
    assert called == [], "must refuse before doing anything"
    assert "followup_needs_conversation" in capsys.readouterr().err


def test_followup_with_a_conversation_enqueues_as_a_followup(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "CGC_STATE_DIR", str(tmp_path))
    seen = {}
    sys.path.insert(0, os.path.join(ROOT, "skill", "scripts"))
    import cgc_spool as spool
    monkeypatch.setattr(spool, "cmd_enqueue", lambda a: seen.update(vars(a)) or 0)
    r = mod.cmd_fire(_ns(mod, tmp_path, followup=True, no_code=True, conversation="conv-9"))
    assert isinstance(r, dict)
    assert seen["kind"] == "followup" and seen["conversation"] == "conv-9"


def test_a_plain_fire_enqueues_a_submit(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "CGC_STATE_DIR", str(tmp_path))
    seen = {}
    sys.path.insert(0, os.path.join(ROOT, "skill", "scripts"))
    import cgc_spool as spool
    monkeypatch.setattr(spool, "cmd_enqueue", lambda a: seen.update(vars(a)) or 0)
    r = mod.cmd_fire(_ns(mod, tmp_path, no_code=True))
    assert seen["kind"] == "submit" and r["rid"].startswith("REQ-")


def test_concurrent_deliveries_do_not_share_a_refs_path(tmp_path, monkeypatch):
    """Filenames used one-second resolution, so two fires in the same second wrote the same path and
    one consult could review the OTHER's repository references."""
    mod = _load()
    monkeypatch.setattr(mod, "CGC_STATE_DIR", str(tmp_path))
    paths = {mod.cmd_deliver(_ns(mod, tmp_path))["refs_file"] for _ in range(8)}
    assert len(paths) == 8, "refs paths must be unique per call, not per second"


def test_fire_runs_end_to_end_from_the_command_line(tmp_path):
    """A CLI smoke test: the parser must actually accept the fused argument set."""
    env = {**os.environ, "CGC_STATE_DIR": str(tmp_path),
           "CGC_SPOOL_DIR": str(tmp_path / "spool"), "PATH": "/usr/bin:/bin"}
    r = subprocess.run(
        [sys.executable, CONSULT, "fire", "--repo", "acme/widgets", "--ref", "b" * 40,
         "--title", "t", "--role", "You are a reviewer.", "--task", "probe"],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=90)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["rid"].startswith("REQ-") and out["out"] and out["await"]
