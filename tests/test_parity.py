#!/usr/bin/env python3
# Created: 2026-07-01
# Last audited: 2026-07-01
# Authority basis: open-claude-gpt skill v2 — SHARED CONTRACT #1 (fence-aware, line-anchored
#   sentinel parser) MUST stay behaviorally identical across its two parallel implementations:
#     1. _sentinel_parse   (Python, canonical)               skill/scripts/cdp_consult.py
#     2. _sentinel_js      (JS source string)                skill/scripts/cdp_consult.py
#   If one drifts from the canonical Python parser, a real consult can silently break (declared
#   complete when it isn't, or vice versa, or a differently-sliced body). This file runs the SAME
#   fixture corpus through both and asserts agreement. JS implementations are extracted from
#   the .py source AT TEST RUNTIME (never hardcoded) and executed under Node via subprocess.
"""
Cross-implementation parity tests for the fence-aware sentinel parser (SHARED CONTRACT #1).

Also includes a pure-unit table test for the /c/<id> conversation pathname matcher
(conversation_id() regex + _is_conv_url) since a spoofed query-string id must never match.

Run: python3 -m pytest tests/test_parity.py -q
If `node` is not on PATH, the JS-parity tests are skipped (with a clear reason) so the
suite still runs locally without Node — CI installs/has Node and must actually execute them.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
CDP_CONSULT_PY = os.path.join(SCRIPTS, "cdp_consult.py")

NODE = shutil.which("node")

_RID = "REQ-20260701-120000-abcdef"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_parity_{name}", os.path.join(SCRIPTS, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ============================================================================
# Fixture corpus — shared across all four implementations.
# Each fixture: (name, text, rid, expected_done, expected_body)
# Reuses/duplicates the sentinel fixtures already exercised individually in tests/test_smoke.py
# (against the Python canonical parser only), plus new adversarial ones for parity coverage.
# ============================================================================

def _fixtures():
    rid = _RID
    fx = []

    fx.append((
        "quoted_inline_then_real_wrapper",
        "some text\n"
        f"mentions BEGIN_RESPONSE:{rid} inline in prose, and END_RESPONSE:{rid} too, not real\n"
        f"BEGIN_RESPONSE:{rid}\n"
        "real body line 1\n"
        "real body line 2\n"
        f"END_RESPONSE:{rid}\n",
        rid, True, "real body line 1\nreal body line 2",
    ))

    fx.append((
        "end_inside_fenced_code_block",
        f"BEGIN_RESPONSE:{rid}\n"
        "here is some content\n"
        "```\n"
        f"END_RESPONSE:{rid} is the marker\n"
        "```\n"
        "more content\n",
        rid, False, "",
    ))

    fx.append((
        "missing_end",
        f"BEGIN_RESPONSE:{rid}\nsome content, never terminated\n",
        rid, False, "",
    ))

    fx.append((
        # v3: END must be the LAST non-blank line. Trailing content after a bare END means that END
        # is NOT the terminator (still streaming, or an early injected END) → NOT done. Under v2 this
        # accepted "first body" at the first END; that first-END acceptance is exactly the truncation
        # vector a browsed public repo could exploit, so it is now rejected.
        "trailing_content_after_end_is_rejected",
        "irrelevant text before\n"
        f"BEGIN_RESPONSE:{rid}\n"
        "first body\n"
        f"END_RESPONSE:{rid}\n"
        "trailing junk not part of any block\n",
        rid, False, "",
    ))

    fx.append((
        "valid_final_bare_wrapper",
        f"BEGIN_RESPONSE:{rid}\nhello world\nEND_RESPONSE:{rid}",
        rid, True, "hello world",
    ))

    fx.append((
        "trailing_whitespace_on_wrapper_lines",
        f"BEGIN_RESPONSE:{rid}   \nhello world\n   END_RESPONSE:{rid}  \n",
        rid, True, "hello world",
    ))

    fx.append((
        "crlf_normalized",
        f"BEGIN_RESPONSE:{rid}\r\nhello\r\nEND_RESPONSE:{rid}\r\n",
        rid, True, "hello",
    ))

    fx.append((
        "v2_bare_end_inside_fence_then_real_end",
        f"BEGIN_RESPONSE:{rid}\n"
        "here is the real body, part 1\n"
        "```\n"
        f"END_RESPONSE:{rid}\n"
        "```\n"
        "here is the real body, part 2\n"
        f"END_RESPONSE:{rid}\n",
        rid, True,
        "here is the real body, part 1\n```\nEND_RESPONSE:" + rid + "\n```\nhere is the real body, part 2",
    ))

    fx.append((
        "v2_bare_begin_inside_fence_then_real_begin",
        "leading prose\n"
        "```\n"
        f"BEGIN_RESPONSE:{rid}\n"
        "```\n"
        f"BEGIN_RESPONSE:{rid}\n"
        "real body\n"
        f"END_RESPONSE:{rid}\n",
        rid, True, "real body",
    ))

    fx.append((
        "empty_body",
        f"BEGIN_RESPONSE:{rid}\n\n\nEND_RESPONSE:{rid}\n",
        rid, False, "",
    ))

    fx.append((
        "no_sentinel_at_all",
        "just some ordinary assistant prose with no wrapper anywhere.\n",
        rid, False, "",
    ))

    fx.append((
        "whitespace_only_body_with_tabs",
        f"BEGIN_RESPONSE:{rid}\n   \t  \nEND_RESPONSE:{rid}\n",
        rid, False, "",
    ))

    # ---- new adversarial fixtures (per task step 2) --------------------------

    fx.append((
        "fenced_bare_end_before_real_end",
        # a fenced bare END appears BEFORE the real END — must be ignored, real END is the terminal
        # (last non-blank) line. (v3: no trailing content after the real END, or it would not be
        # the terminator; the trailing-content case is covered by its own fixture.)
        f"BEGIN_RESPONSE:{rid}\n"
        "part one\n"
        "~~~\n"
        f"END_RESPONSE:{rid}\n"
        "~~~\n"
        "part two, still going\n"
        f"END_RESPONSE:{rid}\n",
        rid, True, "part one\n~~~\nEND_RESPONSE:" + rid + "\n~~~\npart two, still going",
    ))

    # ---- v3 injection-defense fixtures: END must be the LAST non-blank line -------
    fx.append((
        "injected_early_bare_end_then_real_answer_and_terminal_end",
        # A browsed PUBLIC repo prompt-injects an early bare END to truncate the answer. The early
        # END is NOT the last non-blank line, so it is ignored; the REAL terminal END closes
        # extraction and the full answer (incl. the injected END as literal text) is captured — no
        # truncation. This is the whole point of the last-non-blank-line rule.
        f"BEGIN_RESPONSE:{rid}\n"
        "decoy sentence the injection wants us to stop at\n"
        f"END_RESPONSE:{rid}\n"
        "the REAL answer continues here\n"
        "and finishes here\n"
        f"END_RESPONSE:{rid}\n",
        rid, True,
        "decoy sentence the injection wants us to stop at\nEND_RESPONSE:" + rid
        + "\nthe REAL answer continues here\nand finishes here",
    ))

    fx.append((
        "injected_early_end_midstream_no_terminal_end_yet",
        # Same injection, but the real terminal END has not streamed yet. The early END is not the
        # last non-blank line, so done stays False and the waiter keeps polling — a mid-stream
        # injected END can NOT cause premature completion.
        f"BEGIN_RESPONSE:{rid}\n"
        "decoy\n"
        f"END_RESPONSE:{rid}\n"
        "answer is still streaming and not yet terminated\n",
        rid, False, "",
    ))

    fx.append((
        "terminal_end_followed_by_blank_lines_ok",
        # blank lines after the terminal END are fine — END is still the last NON-BLANK line.
        f"BEGIN_RESPONSE:{rid}\nhello\nEND_RESPONSE:{rid}\n\n   \n",
        rid, True, "hello",
    ))

    fx.append((
        "begin_inside_fence_then_real_begin_later_with_prose_between",
        "some assistant commentary\n"
        "```python\n"
        f"# BEGIN_RESPONSE:{rid}  -- not bare (has a leading '#'), also fenced\n"
        "```\n"
        "more prose in between\n"
        f"BEGIN_RESPONSE:{rid}\n"
        "the actual answer\n"
        f"END_RESPONSE:{rid}\n",
        rid, True, "the actual answer",
    ))

    fx.append((
        "complete_answer_while_stop_like_state_present",
        # proves stop-independence: the parser only looks at TEXT, so a "stop"-like string
        # embedded in the body must not affect done/body — completion never depends on a
        # UI stop-button state (which the parser doesn't even see; this fixture just proves
        # the text-only parse doesn't get confused by stop-adjacent text in the body).
        f"BEGIN_RESPONSE:{rid}\n"
        "the model is done; note: a 'Stop' control may still be visually present in the UI\n"
        f"END_RESPONSE:{rid}\n",
        rid, True,
        "the model is done; note: a 'Stop' control may still be visually present in the UI",
    ))

    fx.append((
        "sentinel_in_non_last_node_marker",
        # This fixture represents the text of the NON-LAST node that actually carries the
        # sentinel (multi-node harnesses feed this specific node's text to each impl).
        f"BEGIN_RESPONSE:{rid}\n"
        "answer lives in an earlier node, not the last one\n"
        f"END_RESPONSE:{rid}\n",
        rid, True, "answer lives in an earlier node, not the last one",
    ))

    return fx


FIXTURES = _fixtures()


# ============================================================================
# 1. Canonical Python reference
# ============================================================================

def _python_result(text, rid):
    m = _load("cdp_consult.py")
    done, body = m._sentinel_parse(text, rid)
    return {"done": bool(done), "body": body}


# ============================================================================
# 2. JS extraction helpers — pull the JS source AT TEST RUNTIME from the .py files,
#    the same way the production code builds it. Never hardcode a stale copy.
# ============================================================================

def _sentinel_js_source(rid, text_expr="__INPUT_TEXT__"):
    """Call the real _sentinel_js(rid, text_expr) builder from cdp_consult.py and return the
    JS expression string it produces. text_expr is a JS expression (a variable name here)."""
    m = _load("cdp_consult.py")
    return m._sentinel_js(rid, text_expr)


def _run_node(script):
    """Run `script` under `node -e`, return parsed JSON stdout. Raises on non-zero exit."""
    r = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"node exited {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}")
    return json.loads(r.stdout.strip())


def _sentinel_js_result(text, rid):
    """Run the canonical _sentinel_js(rid, text_expr) builder against `text` under Node."""
    js_expr = _sentinel_js_source(rid, "INPUT")
    script = (
        "var INPUT = " + json.dumps(text) + ";\n"
        "var res = " + js_expr + ";\n"
        "process.stdout.write(JSON.stringify(res));\n"
    )
    return _run_node(script)


# ============================================================================
# Parity assertions
# ============================================================================

def _require_node():
    if NODE is None:
        pytest.skip("node not found on PATH — JS parity tests skipped locally; CI has Node.")


@pytest.mark.parametrize("name,text,rid,expected_done,expected_body", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_python_canonical_matches_expected(name, text, rid, expected_done, expected_body):
    """Sanity: the canonical Python parser matches the fixture's own expectation."""
    result = _python_result(text, rid)
    assert result["done"] == expected_done, f"[{name}] python done mismatch: {result}"
    assert result["body"] == expected_body, f"[{name}] python body mismatch: {result}"


@pytest.mark.parametrize("name,text,rid,expected_done,expected_body", FIXTURES, ids=[f[0] for f in FIXTURES])
def test_sentinel_js_parity_with_python(name, text, rid, expected_done, expected_body):
    _require_node()
    py = _python_result(text, rid)
    js = _sentinel_js_result(text, rid)
    assert js["done"] == py["done"], (
        f"[{name}] PARITY FAILURE: _sentinel_js diverges from canonical _sentinel_parse on "
        f"`done` — python={py} js(_sentinel_js)={js}")
    assert js["body"] == py["body"], (
        f"[{name}] PARITY FAILURE: _sentinel_js diverges from canonical _sentinel_parse on "
        f"`body` — python={py} js(_sentinel_js)={js}")


def test_parity_matrix_size():
    """Documents the coverage: N fixtures x 2 implementations (python canonical + _sentinel_js)."""
    assert len(FIXTURES) == 19


# ============================================================================
# 5. Pure-unit table test: /c/<id> conversation pathname matcher
#    - conversation_id(): regex (?:^|/)c/([0-9a-f-]+)(?:/|$) SEARCHED in location.pathname
#    - _is_conv_url(): same segment regex over urlparse(u).path
#    /c/<id> may sit at the ROOT (/c/<id>) or inside a PROJECT (/g/g-p-<pid>/c/<id>).
#    Both operate on PATH ONLY — a spoofed query string (?x=/c/def) must never match.
# ============================================================================

_CONV_ID_RE = re.compile(r"(?:^|/)c/([0-9a-f-]+)(?:/|$)")


def _conversation_id_from_path(path):
    m = _CONV_ID_RE.search(path)
    return m.group(1) if m else ""


def _is_conv_url(url, match):
    import urllib.parse
    path = urllib.parse.urlparse(url or "").path
    return re.search(r"(?:^|/)c/" + re.escape(match) + r"(?:/|$)", path) is not None


CONV_PATH_CASES = [
    # (path, expected_id)
    ("/c/abc", "abc"),
    ("/c/abc/", "abc"),
    ("/c/abc/anything", "abc"),
    # PROJECT-scoped conversation: id lives under /g/g-p-<pid>/c/<id> — must still resolve.
    ("/g/g-p-deadbeef-claude-code/c/abc", "abc"),
    ("/g/g-p-deadbeef-claude-code/c/abc/", "abc"),
    # NOTE: a real location.pathname never contains a query string ('?...') — the query/hash
    # spoof case (?x=/c/def) is exercised against FULL urls in the _is_conv_url table below,
    # where urlparse strips the query before matching. So we don't feed a '?' path here.
    ("/g/g-p-deadbeef-project", ""),   # project ROOT, no /c/ segment → no conversation
    ("/", ""),
]


@pytest.mark.parametrize("path,expected_id", CONV_PATH_CASES, ids=[c[0] for c in CONV_PATH_CASES])
def test_conversation_id_regex_table(path, expected_id):
    # location.pathname never contains '?' in a real browser; passing a path with '?' here
    # exercises the regex's own robustness (it still must not match beyond a real /c/<id>).
    assert _conversation_id_from_path(path) == expected_id


IS_CONV_URL_CASES = [
    # (url, match, expected)
    ("https://chatgpt.com/c/abc", "abc", True),
    ("https://chatgpt.com/c/abc/", "abc", True),
    ("https://chatgpt.com/c/abc/anything", "abc", True),
    # PROJECT-scoped conversation URL — the id is a path segment under /g/g-p-<pid>/c/<id>.
    ("https://chatgpt.com/g/g-p-deadbeef-claude-code/c/abc", "abc", True),
    ("https://chatgpt.com/g/g-p-deadbeef-claude-code/c/abc/", "abc", True),
    # a DIFFERENT project conversation must not match our id.
    ("https://chatgpt.com/g/g-p-deadbeef-claude-code/c/xyz", "abc", False),
    # SPOOF CASE: query string mentions a DIFFERENT id — must match the PATH's abc, not def,
    # and must NOT be fooled into matching "def" when matching against match="def".
    ("https://chatgpt.com/c/abc?x=/c/def", "abc", True),
    ("https://chatgpt.com/c/abc?x=/c/def", "def", False),
    ("https://chatgpt.com/g/g-p-deadbeef-slug/project", "abc", False),
    ("https://chatgpt.com/", "abc", False),
    # hash-based spoof: /c/<id> appearing only in the fragment must not count either.
    ("https://chatgpt.com/#/c/abc", "abc", False),
]


@pytest.mark.parametrize("url,match,expected", IS_CONV_URL_CASES,
                         ids=[f"{c[0]}::{c[1]}" for c in IS_CONV_URL_CASES])
def test_is_conv_url_table(url, match, expected):
    assert _is_conv_url(url, match) == expected


def test_conversation_id_method_matches_regex_used_in_source():
    """Cross-check: the regex embedded in this test file is byte-identical to the one CDP.
    conversation_id() actually uses in skill/scripts/cdp_consult.py — extracted at runtime,
    not hardcoded, so a change to the production regex fails this test instead of silently
    diverging from the table above."""
    src = open(CDP_CONSULT_PY, encoding="utf-8").read()
    m = re.search(r'm = re\.search\((r"(?:[^"\\]|\\.)*")\s*,\s*path\)', src)
    assert m, "conversation_id() regex not found in cdp_consult.py"
    prod_pattern = eval(m.group(1))  # noqa: S307 — trusted local source file, not external input
    assert prod_pattern == _CONV_ID_RE.pattern


def test_is_conv_url_logic_matches_source():
    """Cross-check: _is_conv_url's segment-match logic is present in the actual source, so a
    change to production logic is caught here rather than silently diverging from the table."""
    src = open(CDP_CONSULT_PY, encoding="utf-8").read()
    assert r'return re.search(r"(?:^|/)c/" + re.escape(match) + r"(?:/|$)", path) is not None' in src, (
        "_is_conv_url's segment-match expression changed shape — update this test's mirrored "
        "logic (_is_conv_url in this file) to match, after confirming the new behavior is intended."
    )


# ============================================================================
# SHARED CONTRACT: the source-legitimacy gate has two implementations —
#   1. cgc_spool.validate_prompt   rule 1 (the authoritative egress gate)
#   2. cdp_consult._has_sendable_source  (the send-time backstop, direct path + daemon child)
# Both must agree on whether a prompt carries a legitimate basis to send: a real code LINK, or a
# sentinel prep stamps to declare it needs none (follow-up / --no-code). They drifted once — the
# backstop honored only the follow-up sentinel — so every `prep --no-code` consult passed the spool
# gate and was refused at send with no_code_source. This test pins them together.
# ============================================================================

_GATE_MATRIX = [
    # (name, prompt, expect_sendable)
    ("bare_prose_no_link", "Please review the design of my repo, it is a big refactor.", False),
    ("no_code_sentinel",
     "# Prove it\nThis consult references no code — it is a self-contained question.\nProve it.\n",
     True),
    ("followup_sentinel",
     "Continuing this consult. Here are the local results since the last round.\n", True),
    ("real_code_link", "Review https://github.com/acme/widget/tree/abc123 for races.\n", True),
    ("workspace_source",
     "# Why does it stall\n\n# Workspace\nSource: local workspace `my repo`, reachable through the "
     "\"WebCodex Demo\" app's tools; state the revision/as-of time you read.\nFind it.\n", True),
    ("prose_mentioning_a_workspace_is_not_a_declaration",
     "Please read my local workspace for the stall, it is on my machine.\n", False),
]


@pytest.mark.parametrize("name,prompt,expect", _GATE_MATRIX, ids=[c[0] for c in _GATE_MATRIX])
def test_source_gate_parity(name, prompt, expect, monkeypatch):
    """cdp_consult's send-gate and cgc_spool's rule 1 must reach the identical send/refuse verdict.
    Every prompt in the matrix is secret-free and gist-free, so validate_prompt's other rules never
    fire; the link case monkeypatches the public-repo check so no network is touched."""
    cdp = _load("cdp_consult.py")
    spool = _load("cgc_spool.py")
    monkeypatch.setattr(spool, "_repo_is_public", lambda slug: (True, "public"))
    monkeypatch.setattr(spool, "_gh_exists", lambda path: (True, "exists"))

    cdp_sendable = cdp._has_sendable_source(prompt)
    spool_ok, spool_why = spool.validate_prompt(prompt)

    assert cdp_sendable == expect, f"{name}: cdp backstop verdict wrong"
    assert spool_ok == expect, f"{name}: spool gate verdict wrong ({spool_why})"
    assert cdp_sendable == spool_ok, (
        f"{name}: source gates DISAGREE — cdp={cdp_sendable} spool={spool_ok} ({spool_why}); "
        "the two implementations of the source-legitimacy contract have drifted")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
