# Tests for `consult.py fire` — the fused deliver -> prep -> enqueue path.
#
# The command shipped with no coverage at all, which a review of the fused commit called out
# alongside two release-stopping defects. Both are pinned here.
import argparse
import importlib.util
import json
import os
import pathlib
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


def test_followup_without_a_conversation_defaults_to_the_active_thread(tmp_path, monkeypatch):
    """A bare `--followup` no longer needs a hand-tracked id: fire defaults --conversation to 'auto'
    (the active thread) and enqueues kind='followup'. The fail-closed guarantee — never silently open
    a fresh conversation — moves to the backend enqueue, which resolves 'auto' to the last completed
    consult or refuses (store: see test_store_backend; spool: active.json / no_active_thread). fire's
    job is only to stop making the agent copy a conversation id around."""
    mod = _load()
    monkeypatch.setattr(mod, "CGC_STATE_DIR", str(tmp_path))
    seen = {}
    sys.path.insert(0, os.path.join(ROOT, "skill", "scripts"))
    import cgc_spool as spool
    monkeypatch.setattr(spool, "cmd_enqueue", lambda a: seen.update(vars(a)) or 0)
    r = mod.cmd_fire(_ns(mod, tmp_path, followup=True, no_code=True))
    assert isinstance(r, dict), "no longer refused up front"
    assert seen["kind"] == "followup"
    assert seen["conversation"] == "auto", "defaults to the active thread — no id to track"


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


# ---- the output contract must survive the initial/continuation split ---------------------------
# Until 2026-09-06 --output-file/--output-replace were resolved INSIDE prep's non-followup branch,
# so a follow-up silently got FOLLOWUP_TEMPLATE's hardcoded findings prose no matter what the
# caller asked for. Verified live: REQ-20260906-025243-c975c2 was fired with
# `--output-file skill/references/deep-review-output.md --output-replace` and both flags were
# dropped. Routing and CLI behaviour were covered here; contract propagation was not.

def _rendered(mod, tmp, **over):
    """Run prep and return the prompt it actually wrote — the only artifact that reaches ChatGPT."""
    st = mod.cmd_prep(_ns(mod, tmp, **over))
    return pathlib.Path(st["prompt_file"]).read_text(encoding="utf-8")


def test_initial_and_continuation_honour_the_same_output_spec(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "CGC_STATE_DIR", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("# Deliverable\nAn experiment design: hypothesis, procedure, kill criteria.\n")
    initial = _rendered(mod, tmp_path, no_code=True, output_file=str(spec))
    follow = _rendered(mod, tmp_path, no_code=True, followup=True, output_file=str(spec))
    for name, text in (("initial", initial), ("follow-up", follow)):
        assert "An experiment design: hypothesis, procedure, kill criteria." in text, \
            f"the {name} render dropped the supplied output specification"


def test_output_replace_drops_the_findings_contract_on_a_followup_too(tmp_path, monkeypatch):
    """--output-replace means the spec OWNS the Output section. A follow-up that still appended the
    default findings shape would hand the model two colliding contracts."""
    mod = _load()
    monkeypatch.setattr(mod, "CGC_STATE_DIR", str(tmp_path))
    spec = tmp_path / "spec.md"
    spec.write_text("# Deliverable\nA decision memo. No severity scale.\n")
    text = _rendered(mod, tmp_path, no_code=True, followup=True,
                     output_file=str(spec), output_replace=True)
    assert "A decision memo. No severity scale." in text
    assert "BLOCKER / HIGH / MEDIUM / LOW / NIT" not in text, \
        "the default findings contract must not collide with the caller's own shape"
    assert mod.REPLACE_OUTPUT_CLOSE.split(".")[0] in text, "the universal close still applies"


def test_a_followup_with_no_spec_still_gets_the_default_contract(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "CGC_STATE_DIR", str(tmp_path))
    text = _rendered(mod, tmp_path, no_code=True, followup=True)
    assert "BLOCKER / HIGH / MEDIUM / LOW / NIT" in text


def test_an_unchanged_source_sha_is_not_described_as_new_or_updated(tmp_path, monkeypatch):
    """prep is a pure renderer: it never sees the parent round's source ref, so it cannot know
    whether anything moved. REQ-20260906-025243-c975c2 linked the SAME commit SHA as its parent and
    was still told its sources were new and that Claude Code had applied a fix. Neither claim may
    appear on a follow-up render."""
    mod = _load()
    monkeypatch.setattr(mod, "CGC_STATE_DIR", str(tmp_path))
    same_sha = "c" * 40
    refs = tmp_path / "refs.md"
    refs.write_text(f"- https://github.com/acme/widgets/tree/{same_sha}\n")
    text = _rendered(mod, tmp_path, followup=True, refs_file=str(refs))
    assert same_sha in text, "the source still ships"
    for claim in ("new or updated", "New or changed source", "acted on your last answer"):
        assert claim not in text, f"renderer asserted {claim!r} it has no evidence for"
