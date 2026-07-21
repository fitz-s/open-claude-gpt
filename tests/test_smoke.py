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


def test_skill_frontmatter_valid_yaml():
    # a broken SKILL.md frontmatter means Claude Code may not load the skill at all
    try:
        import yaml
    except ImportError:
        return  # optional dep; CI installs it
    txt = open(os.path.join(ROOT, "skill", "SKILL.md"), encoding="utf-8").read()
    assert txt.startswith("---"), "SKILL.md has no frontmatter"
    fm = txt.split("---", 2)[1]
    d = yaml.safe_load(fm)
    assert isinstance(d, dict) and d.get("name") and d.get("description"), d


def test_doctor_json_shape():
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "cgc_doctor.py"), "--json"],
        capture_output=True, text=True, env={**os.environ, "NO_COLOR": "1"})
    import json
    d = json.loads(r.stdout)
    assert "ok" in d and isinstance(d["checks"], list) and d["checks"]


# ---- SHARED CONTRACT #1: line-anchored sentinel parser fixtures -------------
# The parser (skill/scripts/cdp_consult.py:_sentinel_parse) must fire `done` ONLY on a bare
# standalone sentinel line (trimmed value exactly equal to "BEGIN_RESPONSE:<rid>" / "END_RESPONSE:
# <rid>") — a line that merely CONTAINS the sentinel text (quoted in prose, or inside a fenced
# code block) must NOT match. The JS mirror (_sentinel_js) implements the identical rule; these
# tests exercise the importable Python helper directly.

_RID = "REQ-20260701-120000-abcdef"


def _cdp():
    return _load("cdp_consult.py")


def test_sentinel_parse_quoted_inline_then_real_wrapper():
    # (1) body quotes both BEGIN_RESPONSE/END_RESPONSE inline (in prose) before the real final
    # bare wrapper — the inline quoting must NOT be mistaken for the real markers.
    m = _cdp()
    text = (
        "some text\n"
        f"mentions BEGIN_RESPONSE:{_RID} inline in prose, and END_RESPONSE:{_RID} too, not real\n"
        f"BEGIN_RESPONSE:{_RID}\n"
        "real body line 1\n"
        "real body line 2\n"
        f"END_RESPONSE:{_RID}\n"
    )
    done, body = m._sentinel_parse(text, _RID)
    assert done is True
    assert body == "real body line 1\nreal body line 2"


def test_sentinel_parse_end_inside_fenced_code_block():
    # (2) END token inside a fenced code block (not a bare standalone line) must not satisfy done.
    m = _cdp()
    text = (
        f"BEGIN_RESPONSE:{_RID}\n"
        "here is some content\n"
        "```\n"
        f"END_RESPONSE:{_RID} is the marker\n"
        "```\n"
        "more content\n"
    )
    done, body = m._sentinel_parse(text, _RID)
    assert done is False
    assert body == ""


def test_sentinel_parse_missing_end():
    # (3) missing END entirely.
    m = _cdp()
    text = f"BEGIN_RESPONSE:{_RID}\nsome content, never terminated\n"
    done, body = m._sentinel_parse(text, _RID)
    assert done is False
    assert body == ""


def test_sentinel_parse_two_assistant_messages():
    # (4) v3: END must be the LAST non-blank line. Trailing content after a bare END means that END
    # is not the terminator (still streaming, or an injected early END) → NOT done. Accepting the
    # FIRST END (v2) was exactly the truncation vector a browsed public repo could exploit.
    m = _cdp()
    text = (
        "irrelevant text before\n"
        f"BEGIN_RESPONSE:{_RID}\n"
        "first body\n"
        f"END_RESPONSE:{_RID}\n"
        "trailing junk not part of any block\n"
    )
    done, body = m._sentinel_parse(text, _RID)
    assert done is False
    assert body == ""


def test_sentinel_parse_valid_final_bare_wrapper():
    # (5) a valid final bare wrapper, no surrounding noise.
    m = _cdp()
    text = f"BEGIN_RESPONSE:{_RID}\nhello world\nEND_RESPONSE:{_RID}"
    done, body = m._sentinel_parse(text, _RID)
    assert done is True
    assert body == "hello world"


def test_sentinel_parse_trailing_whitespace_on_wrapper_lines():
    # (6) trailing/leading whitespace on the wrapper lines should still match after trim().
    m = _cdp()
    text = f"BEGIN_RESPONSE:{_RID}   \nhello world\n   END_RESPONSE:{_RID}  \n"
    done, body = m._sentinel_parse(text, _RID)
    assert done is True
    assert body == "hello world"


def test_sentinel_parse_crlf_normalized():
    # \r\n must normalize to \n before line-splitting, per the shared contract.
    m = _cdp()
    text = f"BEGIN_RESPONSE:{_RID}\r\nhello\r\nEND_RESPONSE:{_RID}\r\n"
    done, body = m._sentinel_parse(text, _RID)
    assert done is True
    assert body == "hello"


def test_sentinel_parse_v2_bare_end_inside_fence_then_real_end():
    # v2 fence-awareness (a): a fenced code block contains a BARE END_RESPONSE:<rid> line (no
    # surrounding prose on that line) — it must NOT satisfy done, because it sits inside a ```
    # fence. Only the REAL bare wrapper OUTSIDE the fence, appearing later, must be matched.
    m = _cdp()
    text = (
        f"BEGIN_RESPONSE:{_RID}\n"
        "here is the real body, part 1\n"
        "```\n"
        f"END_RESPONSE:{_RID}\n"  # bare, but fenced — must be ignored
        "```\n"
        "here is the real body, part 2\n"
        f"END_RESPONSE:{_RID}\n"  # the real, unfenced terminator
    )
    done, body = m._sentinel_parse(text, _RID)
    assert done is True
    assert body == (
        "here is the real body, part 1\n"
        "```\n"
        f"END_RESPONSE:{_RID}\n"
        "```\n"
        "here is the real body, part 2"
    )


def test_sentinel_parse_v2_bare_begin_inside_fence_then_real_begin():
    # v2 fence-awareness (b): symmetric case — a fenced code block contains a BARE
    # BEGIN_RESPONSE:<rid> line before the real one. The fenced bare BEGIN must be ignored; the
    # real BEGIN outside any fence (appearing after) is the one that starts the block.
    m = _cdp()
    text = (
        "leading prose\n"
        "```\n"
        f"BEGIN_RESPONSE:{_RID}\n"  # bare, but fenced — must be ignored
        "```\n"
        f"BEGIN_RESPONSE:{_RID}\n"  # the real, unfenced start
        "real body\n"
        f"END_RESPONSE:{_RID}\n"
    )
    done, body = m._sentinel_parse(text, _RID)
    assert done is True
    assert body == "real body"


def test_code_url_re_requires_real_url():
    # PROVENANCE gate: the code-link check must require an actual github/gist URL, not merely
    # the substring "github" (the old spoofable check).
    m = _cdp()
    assert m._CODE_URL_RE.search("please read https://github.com/acme/widgets/tree/abc123")
    assert m._CODE_URL_RE.search("see https://gist.github.com/user/deadbeef")
    assert m._CODE_URL_RE.search("raw: https://raw.githubusercontent.com/acme/widgets/main/x.py")
    assert not m._CODE_URL_RE.search("this mentions github but has no link")
    assert not m._CODE_URL_RE.search("github.com/acme/widgets")  # no scheme


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
