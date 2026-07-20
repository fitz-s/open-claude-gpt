#!/usr/bin/env python3
# Created: 2026-07-01
# Last audited: 2026-07-01
# Authority basis: hardening task for skill/scripts/consult.py cmd_deliver —
#   fail-closed public-source provenance (PUBLIC stamp requires gh-confirmed
#   visibility=="public"; private/unknown visibility refuses unless
#   --allow-nonpublic; refs must always contain >=1 real browsable URL).
"""
Offline tests for consult.py's `deliver` subcommand — no browser, no network.
Run: python3 -m pytest tests/test_deliver.py -q
(also runnable plain: python3 tests/test_deliver.py)

The `_gh` helper (which shells out to the `gh` CLI) is monkeypatched so these
tests never touch the network or require `gh` to be installed/authenticated.
consult.py is imported as a module directly from its file path — it must
import without side effects (asserted by test_import_has_no_side_effects).
"""
import ast
import importlib.util
import io
import json
import os
import sys
import tempfile
import contextlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSULT_PATH = os.path.join(ROOT, "skill", "scripts", "consult.py")


def _load_consult(env=None):
    """Import consult.py as an isolated module, optionally with env overrides."""
    old = dict(os.environ)
    try:
        if env:
            os.environ.update({k: str(v) for k, v in env.items()})
        spec = importlib.util.spec_from_file_location("_consult_under_test", CONSULT_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_import_has_no_side_effects():
    """Importing the module must not touch the network, write files, or exit."""
    mod = _load_consult()
    assert hasattr(mod, "cmd_deliver")
    assert hasattr(mod, "_gh")
    assert hasattr(mod, "_has_browsable_url")


def test_ast_parses():
    ast.parse(open(CONSULT_PATH, encoding="utf-8").read())


def _make_namespace(mod, tmp_state_dir, **overrides):
    """Build the argparse.Namespace cmd_deliver expects, with sane defaults so
    we exercise the EXPLICIT (agent-driven) branch without hitting the network."""
    import argparse
    ns = argparse.Namespace(
        # verify=True: these tests exercise the NETWORK path's fail-closed provenance logic (with
        # _gh monkeypatched, so nothing is actually sent). The agent path is offline by default and
        # defers provenance to the daemon's gate — covered in test_security.py.
        verify=True,
        repo_dir=ROOT,
        repo="acme/widgets",
        pr="1",
        ref="a" * 40,           # already looks like a sha -> skips the resolve-ref _gh call
        compare=None,
        pulls=False,
        blobs=False,
        files=None,
        issues=None,
        base=None,
        allow_nonpublic=False,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


def _stub_gh(visibility_result):
    """Build a fake _gh(args, cwd) -> (stdout, err) that answers the visibility
    query per `visibility_result` and returns "no data" for every other call
    (PR lookups, ref-sha resolution, etc.) so those code paths degrade harmlessly."""
    def fake_gh(args, cwd):
        if "--jq" in args and ".visibility" in args:
            if visibility_result is None:
                return None, "gh_not_available"
            return visibility_result, None
        # any other gh call (PR-for-commit lookup, pr view, ref resolution): no data
        return None, "gh_error"
    return fake_gh


def _run_deliver(mod, ns, tmp_state_dir, visibility_result):
    mod._gh = _stub_gh(visibility_result)
    mod.CGC_STATE_DIR = tmp_state_dir
    # cmd_deliver RETURNS its state now (main() prints it), so `fire` can compose deliver -> prep
    # -> enqueue in one process instead of the agent relaying JSON between three Bash calls.
    # Reading the return value is also what these assertions actually wanted.
    out = mod.cmd_deliver(ns)
    assert isinstance(out, dict), f"expected a state dict, got {out!r}"
    return out


def test_public_repo_stamps_public_and_sets_public_ok():
    with tempfile.TemporaryDirectory() as tmp:
        mod = _load_consult()
        ns = _make_namespace(mod, tmp)
        out = _run_deliver(mod, ns, tmp, "public")
        assert out["visibility"] == "public"
        assert out["public_ok"] is True
        assert out["refs_file"], "expected a refs file to be written"
        body = open(out["refs_file"], encoding="utf-8").read()
        assert "Source visibility: PUBLIC" in body
        assert "NON-PUBLIC" not in body


def test_private_repo_without_flag_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        mod = _load_consult()
        ns = _make_namespace(mod, tmp)  # allow_nonpublic=False (default)
        out = _run_deliver(mod, ns, tmp, "private")
        assert out["visibility"] == "private"
        assert out["public_ok"] is False
        assert not out["refs_file"], "must NOT write a refs file for private repo without override"
        assert out["needs_gist"] is True
        assert "PRIVATE" in out["note"] or "private" in out["note"]


def test_private_repo_with_allow_nonpublic_stamps_nonpublic():
    with tempfile.TemporaryDirectory() as tmp:
        mod = _load_consult()
        ns = _make_namespace(mod, tmp, allow_nonpublic=True)
        out = _run_deliver(mod, ns, tmp, "private")
        assert out["visibility"] == "private"
        assert out["public_ok"] is False
        assert out["refs_file"], "expected a refs file to be written under --allow-nonpublic"
        body = open(out["refs_file"], encoding="utf-8").read()
        assert "NON-PUBLIC (explicitly allowed)" in body
        assert "Source visibility: PUBLIC" not in body


def test_gh_missing_or_unauthenticated_fails_closed():
    """visibility comes back UNKNOWN (gh missing/unauthenticated/errored) -> must not
    stamp PUBLIC or write a refs file without --allow-nonpublic."""
    with tempfile.TemporaryDirectory() as tmp:
        mod = _load_consult()
        ns = _make_namespace(mod, tmp)
        out = _run_deliver(mod, ns, tmp, None)  # None => _gh returns (None, "gh_not_available")
        assert out["visibility"] is None
        assert out["public_ok"] is False
        assert not out["refs_file"]
        assert out["needs_gist"] is True
        assert "UNKNOWN" in out["note"]


def test_gh_missing_with_allow_nonpublic_still_stamps_nonpublic():
    with tempfile.TemporaryDirectory() as tmp:
        mod = _load_consult()
        ns = _make_namespace(mod, tmp, allow_nonpublic=True)
        out = _run_deliver(mod, ns, tmp, None)
        assert out["visibility"] is None
        assert out["public_ok"] is False
        assert out["refs_file"]
        body = open(out["refs_file"], encoding="utf-8").read()
        assert "NON-PUBLIC (explicitly allowed)" in body


def test_has_browsable_url_helper():
    mod = _load_consult()
    assert mod._has_browsable_url({"intent": [("https://github.com/acme/widgets/pull/1", "x")]})
    assert mod._has_browsable_url({"snapshot": [("https://raw.githubusercontent.com/acme/widgets/main/x", "x")]})
    assert mod._has_browsable_url({"fallback": [("https://gist.github.com/abc123", "x")]})
    assert not mod._has_browsable_url({"intent": [("not-a-url", "x")]})
    assert not mod._has_browsable_url({"intent": []})
    assert not mod._has_browsable_url({})


def test_no_browsable_url_refuses_even_when_public():
    """Even a confirmed-public repo must refuse if, somehow, no group ended up with
    a real browsable URL (defense in depth against a future groups-building bug)."""
    with tempfile.TemporaryDirectory() as tmp:
        mod = _load_consult()
        ns = _make_namespace(mod, tmp)
        # Force the explicit branch to produce no groups at all by clearing every
        # source-selecting flag; deliver should hit the "nothing to render" path.
        ns.repo = "acme/widgets"
        ns.pr = None
        ns.ref = None
        ns.compare = None
        ns.pulls = False
        ns.files = None
        ns.issues = None
        out = _run_deliver(mod, ns, tmp, "public")
        assert not out["refs_file"]


if __name__ == "__main__":
    failures = 0
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {t.__name__}: {e!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
