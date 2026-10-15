# Differential test of the egress gate's URL law against a real WHATWG URL parser (node).
#
# The oracle: if validate_prompt accepts a prompt, then every reading a URL parser could make of it (each
# `http(s):` start, alone and joined with the following runs of text across TAB/CR/LF, which a parser
# drops) that lands on a GitHub host must land on a repo the gate verified, or on no repo GitHub can
# hold. A URL parser drops EVERY TAB, CR and LF, so a blank line joins exactly like a single break and
# the oracle makes no distinction. The gate and the oracle are written independently, so a hole one of
# them has is found by the other.
#
# Skipped without node (CI has it), like the sentinel-parser parity tests.
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH: the WHATWG oracle needs it")

HEADS = ["https://github.com/acme/widgets", "https://github.com/acme/widgets/", "https://github.com/acme/widgets/pull/12",
         "https://github.com/acme/widgets/tree/main", "https://github.com/acme/widgets/blob/main/a.py",
         "https://github.com/acme", "https://github.com/", "https://github.com", "https://github", "https://git",
         "https:", "https://raw.githubusercontent.com/acme/widgets/main/x", "https://api.github.com/repos/acme/widgets",
         "https://github.com/ac", "https://github.com/acme/wid", "https://github.com/acme/widgets/tree/",
         "http://github.com/acme/widgets", "https://example.com/x"]
SEPS = ["", "\n", "\r\n", "\t", " ", "\n\n", "\n ", "\n\t", " \n", "\r", "\n\r\n", "\t\n", "\n\n\n\n", "\r\n\r\n"]
TAILS = ["priv/repo", "-priv/x", "/priv/repo", "/../priv/repo", "../../priv/repo", "//github.com/priv/repo",
         "hub.com/priv/repo", "server", "2", "-", "# Notes", "- item", "Then explain.", "(x)", "%2e%2e/priv",
         "\\priv\\repo", ".git", "_x", "extra words here", "https://github.com/other/repo", "/pull/9", "main/x",
         "gadgets", "'/../priv/repo", "<x>", "*"]
PUNCT = ["", ".", ",", ")", ">", "'", "`", '"']
OWNER, REPO = re.compile(r"[A-Za-z0-9-]+"), re.compile(r"[A-Za-z0-9._-]+")
GH = ("github.com", "raw.githubusercontent.com", "api.github.com", "codeload.github.com", "www.github.com")


def _spool():
    spec = importlib.util.spec_from_file_location("cgc_spool_diff", os.path.join(SCRIPTS, "cgc_spool.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _build(rng):
    parts = [rng.choice(["review ", "see ", "", "- ", "(", "<", "`", '"', "'"]), rng.choice(HEADS)]
    for _ in range(rng.choice([1, 1, 2, 3])):
        parts += [rng.choice(PUNCT) if rng.random() < .3 else "", rng.choice(SEPS), rng.choice(TAILS)]
    parts.append(rng.choice(["", "\nfin", " fin"]))
    return "".join(parts)


def _readings(sp, text):
    """Every string a URL parser could be handed for a link: it alone, and joined with the next runs of
    text across gaps made only of TAB/CR/LF (any number of them: node drops them all)."""
    runs = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    out = []
    for i, (a, b) in enumerate(runs):
        for m in re.finditer(r"https?:", text[a:b], re.I):
            base = text[a + m.start():b]
            out.append(sp._trim(base))
            end, acc = b, base
            for j in range(i + 1, min(i + 4, len(runs))):
                na, nb = runs[j]
                if not re.fullmatch(r"[\t\r\n]+", text[end:na]) or re.match(r"https?:", text[na:nb], re.I):
                    break
                acc += text[na:nb]
                out.append(sp._trim(acc))
                end = nb
    return out


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_no_accepted_prompt_reaches_a_repo_the_gate_did_not_verify(seed, monkeypatch, tmp_path):
    monkeypatch.setenv("CGC_CONFIG", str(tmp_path / "absent"))
    sp = _spool()
    verified = set()
    monkeypatch.setattr(sp, "_repo_is_public", lambda slug: (verified.add(slug.lower()) or (True, "stub")))
    monkeypatch.setattr(sp, "_gh_exists", lambda p: (True, "exists"))
    rng = random.Random(seed)
    texts = [_build(rng) for _ in range(1200)]
    verdicts = []
    for t in texts:
        verified.clear()
        ok, _why = sp.validate_prompt(t)
        verdicts.append((ok, set(verified)))
    assert any(ok for ok, _ in verdicts) and not all(ok for ok, _ in verdicts), "the corpus must exercise both verdicts"
    assert any(re.search(r"\n\s*\n", t) and ok for t, (ok, _) in zip(texts, verdicts)), \
        "the corpus must include accepted prompts with a blank line, or the blank-line rule is untested"

    urls = sorted({r for t, (ok, _) in zip(texts, verdicts) if ok for r in _readings(sp, t)})
    js = ("const c=JSON.parse(require('fs').readFileSync(0,'utf8'));const o={};for(const u of c){try{const p=new URL(u);"
          "o[u]=[p.protocol,p.hostname,p.pathname]}catch(e){o[u]=null}}console.log(JSON.stringify(o))")
    res = json.loads(subprocess.run([NODE, "-e", js], input=json.dumps(urls), capture_output=True,
                                    text=True, timeout=120, check=True).stdout)
    bypasses = []
    for t, (ok, ver) in zip(texts, verdicts):
        if not ok:
            continue
        for r in _readings(sp, t):
            got = res.get(r)
            if not got or got[1].lower() not in GH:
                continue
            segs = [x for x in got[2].split("/") if x]
            if got[1].lower() == "api.github.com":
                pair = segs[1:3] if len(segs) >= 3 and segs[0] == "repos" else None
            else:
                pair = segs[:2] if len(segs) >= 2 else None
            if (not pair or not OWNER.fullmatch(pair[0]) or not REPO.fullmatch(pair[1])
                    or pair[0].lower() in sp._NON_REPO_OWNERS):
                continue    # names no repo GitHub can hold
            if "/".join(pair).lower() not in ver:
                bypasses.append((t, r, got[1:], sorted(ver)))
                break
    assert not bypasses, bypasses[:3]
