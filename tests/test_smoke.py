#!/usr/bin/env python3
# Offline smoke tests — no browser, no network. Run: python3 -m pytest tests/ -q
# (also runnable plain: python3 tests/test_smoke.py)
import ast
import glob
import importlib.util
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")


def _load(name, env=None):
    """Import a script module in isolation, optionally with env overrides."""
    old = dict(os.environ)
    try:
        if env:
            os.environ.update({k: str(v) for k, v in env.items()})
        spec = importlib.util.spec_from_file_location(f"_{name}", os.path.join(SCRIPTS, name))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_all_scripts_parse():
    for f in glob.glob(os.path.join(SCRIPTS, "*.py")):
        ast.parse(open(f, encoding="utf-8").read())


def test_shell_syntax():
    files = [os.path.join(ROOT, p) for p in ("install.sh", "uninstall.sh", "bin/cgc")]
    files += glob.glob(os.path.join(SCRIPTS, "*.sh"))
    for f in files:
        assert subprocess.run(["bash", "-n", f]).returncode == 0, f


def test_config_true_defaults():
    for k in ("CGC_PORT", "CGC_STATE_DIR", "CGC_MODEL", "CGC_AUTO_MODEL", "CGC_PROJECT_URL"):
        os.environ.pop(k, None)
    m = _load("cdp_consult.py")
    assert m.CGC_PORT == 9333
    assert m.CGC_AUTO_MODEL is True
    assert m.CGC_MODEL == "Pro"
    assert m.STATE_PATH == "/tmp/cgc/active.json"


def test_auto_model_toggle_off():
    m = _load("cdp_consult.py", env={"CGC_AUTO_MODEL": "0", "CGC_MODEL": "High"})
    assert m.CGC_AUTO_MODEL is False
    assert m.CGC_MODEL == "skip"          # off overrides the tier


def test_auto_model_toggle_variants():
    for val in ("false", "no", "off", "0"):
        m = _load("cdp_consult.py", env={"CGC_AUTO_MODEL": val})
        assert m.CGC_MODEL == "skip", val
    for val in ("1", "true", "on", "yes"):
        m = _load("cdp_consult.py", env={"CGC_AUTO_MODEL": val, "CGC_MODEL": "Pro"})
        assert m.CGC_MODEL == "Pro", val


def test_state_dir_override():
    m = _load("cdp_consult.py", env={"CGC_STATE_DIR": "/tmp/cgc-xyz"})
    assert m.STATE_PATH == "/tmp/cgc-xyz/active.json"
    c = _load("consult.py", env={"CGC_STATE_DIR": "/tmp/cgc-xyz"})
    assert c.CGC_STATE_DIR == "/tmp/cgc-xyz"


def test_deliver_refuses_no_code_source():
    # prep with no refs must fail closed (CGC_ERROR no_code_source)
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "consult.py"), "prep",
         "--title", "x", "--task", "y"],
        capture_output=True, text=True)
    assert r.returncode != 0
    assert "no_code_source" in (r.stdout + r.stderr)


def test_doctor_json_shape():
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "cgc_doctor.py"), "--json"],
        capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"})
    import json
    d = json.loads(r.stdout)
    assert "ok" in d and isinstance(d["checks"], list) and d["checks"]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{'PASS' if not fails else 'FAIL'} — {fails} failure(s)")
    sys.exit(1 if fails else 0)
