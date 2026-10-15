# Differential test of the egress gate's URL law against a real WHATWG URL parser (node).
#
# The oracle is independent of the gate's design: it shares none of its helpers (no tokenizer, no cap, no
# trimming), only the verdict. For every accepted prompt it builds the strings a URL parser could be
# handed and asks node what each one means:
#   * every `http(s):` occurrence in the TAB/CR/LF-stripped text of each chain of whitespace-delimited
#     runs joined only by TAB/CR/LF (a space ends a chain), so a scheme or host split by breaks is found;
#   * from each such start, the text cut at EVERY later piece boundary (every prefix join) and at the end
#     of the chain (the maximal join), however many breaks there are: unbounded, no cap to copy;
#   * each of those with trailing sentence punctuation stripped, and not; and, for a single final
#     period, both with it (the repo `widgets.` is legal) and without it (a sentence end). The oracle
#     assumes nothing about which one the writer meant: a reading that lands on a repo must land on one
#     the gate verified.
# A reading that lands on a GitHub host and names a repo GitHub can hold must name a repo the gate
# verified. The gate and the oracle are written independently, so a hole one of them has is found by the
# other. A prompt the gate REFUSED is never a bypass, so a gate that refuses what it cannot judge passes.
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
         "http://github.com/acme/widgets", "https://example.com/x", "ht", "https", "https://github.com/acme/widgets/https:"]
SEPS = ["", "\n", "\r\n", "\t", " ", "\n\n", "\n ", "\n\t", " \n", "\r", "\n\r\n", "\t\n", "\n\n\n\n", "\r\n\r\n"]
TAILS = ["priv/repo", "-priv/x", "/priv/repo", "/../priv/repo", "../../priv/repo", "//github.com/priv/repo",
         "hub.com/priv/repo", "server", "2", "-", "# Notes", "- item", "Then explain.", "(x)", "%2e%2e/priv",
         "\\priv\\repo", ".git", "_x", "extra words here", "https://github.com/other/repo", "/pull/9", "main/x",
         "gadgets", "'/../priv/repo", "<x>", "*", "tps://github.com/priv/repo", "%2E./priv", "Caf%C3%A9"]
PUNCT = ["", ".", ",", ")", ">", "'", "`", '"']
OWNER, REPO = re.compile(r"[A-Za-z0-9-]+"), re.compile(r"[A-Za-z0-9._-]+")
GH = ("github.com", "raw.githubusercontent.com", "api.github.com", "codeload.github.com", "www.github.com")
NON_REPO_OWNERS = ("orgs", "sponsors", "settings", "features", "about", "topics", "trending", "marketplace")


def _spool():
    spec = importlib.util.spec_from_file_location("cgc_spool_diff", os.path.join(SCRIPTS, "cgc_spool.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _spelled(prefix, rest, sep="\n"):
    """`prefix` then every character of `rest` on its own line: a name spelled across breaks."""
    return prefix + sep.join([""] + list(rest))


def _build(rng):
    parts = [rng.choice(["review ", "see ", "", "- ", "(", "<", "`", '"', "'"]), rng.choice(HEADS)]
    for _ in range(rng.choice([1, 1, 2, 3])):
        parts += [rng.choice(PUNCT) if rng.random() < .3 else "", rng.choice(SEPS), rng.choice(TAILS)]
    parts.append(rng.choice(["", "\nfin", " fin"]))
    return "".join(parts)


def _long(rng):
    """Prompts with MORE breaks than any sane cap, or a host/scheme spelled across breaks."""
    n = rng.choice([20, 70, 150])
    sep = rng.choice(["\n", "\r\n", "\n\n", "\n \n"[:1]])
    kind = rng.choice(["host", "repo", "scheme", "tail"])
    if kind == "host":
        return "review https://github.com/cli/cli/ \n\n<" + _spelled("https://git", "hub.com/private/repo", sep) + "> "
    if kind == "repo":
        return "review " + _spelled("https://github.com/acme/widgets", "x" * n, sep)
    if kind == "scheme":
        return "review " + _spelled("ht", "tps://github.com/priv/repo", sep)
    return "review https://github.com/acme/widgets/pull/1\n" + "\n".join(["words"] * n)


def _readings(text):
    """Every string a URL parser could be handed for a link in `text`. Independent of the gate."""
    runs = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
    chains, cur = [], []
    for a, b in runs:
        if cur and re.fullmatch(r"[\t\r\n]+", text[cur[-1][1]:a]):
            cur.append((a, b))
        else:
            if cur:
                chains.append(cur)
            cur = [(a, b)]
    if cur:
        chains.append(cur)
    out = set()
    for chain in chains:
        pieces = [text[a:b] for a, b in chain]
        joined = "".join(pieces)
        ends, pos = [], 0
        for p in pieces:
            pos += len(p)
            ends.append(pos)
        for m in re.finditer(r"https?:", joined, re.I):
            for e in [x for x in ends if x > m.start()]:
                raw = joined[m.start():e]
                for cand in (raw, re.sub(r"[,;:!?)\]}'\"*>|`]+$", "", raw)):
                    # Both readings of a final period: it belongs to the name, or it ends a sentence.
                    out.add(cand)
                    if cand.endswith(".") and not cand.endswith(".."):
                        out.add(cand[:-1])
    return out


def _lands_on(res, r):
    got = res.get(r)
    if not got or got[1].lower() not in GH:
        return None
    segs = [x for x in got[2].split("/") if x]
    if got[1].lower() == "api.github.com":
        pair = segs[1:3] if len(segs) >= 3 and segs[0] == "repos" else None
    else:
        pair = segs[:2] if len(segs) >= 2 else None
    if (not pair or not OWNER.fullmatch(pair[0]) or not REPO.fullmatch(pair[1])
            or pair[0].lower() in NON_REPO_OWNERS):
        return None     # names no repo GitHub can hold
    return "/".join(pair).lower()


def _whatwg(urls):
    js = ("const c=JSON.parse(require('fs').readFileSync(0,'utf8'));const o={};for(const u of c){try{const p=new URL(u);"
          "o[u]=[p.protocol,p.hostname,p.pathname]}catch(e){o[u]=null}}console.log(JSON.stringify(o))")
    return json.loads(subprocess.run([NODE, "-e", js], input=json.dumps(sorted(urls)), capture_output=True,
                                     text=True, timeout=180, check=True).stdout)


def _run(sp, texts, monkeypatch):
    verified = set()

    def vis(slug):
        verified.add(slug.lower())      # every repo the gate ASKED about; an unasked one is the bypass
        if slug.endswith("."):          # a name ending in a period: no such repo, so the period was punctuation
            return False, sp._INVISIBLE + " to your gh token (404 - private or nonexistent)"
        return True, "visibility=public"
    monkeypatch.setattr(sp, "_repo_is_public", vis)
    monkeypatch.setattr(sp, "_gh_exists", lambda p: (True, "exists"))
    verdicts = []
    for t in texts:
        verified.clear()
        ok, why = sp.validate_prompt(t)
        verdicts.append((ok, set(verified), why))
    allr = set()
    for t, (ok, _v, _w) in zip(texts, verdicts):
        if ok:
            allr |= _readings(t)
    res = _whatwg(allr)
    bypasses = []
    for t, (ok, ver, _why) in zip(texts, verdicts):
        if not ok:
            continue
        for r in sorted(_readings(t)):
            slug = _lands_on(res, r)
            if slug and slug not in ver:
                bypasses.append((t[:160], r[:160], slug, sorted(ver)))
                break
    return verdicts, bypasses


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_no_accepted_prompt_reaches_a_repo_the_gate_did_not_verify(seed, monkeypatch, tmp_path):
    monkeypatch.setenv("CGC_CONFIG", str(tmp_path / "absent"))
    sp = _spool()
    rng = random.Random(seed)
    texts = [_build(rng) for _ in range(1200)]
    verdicts, bypasses = _run(sp, texts, monkeypatch)
    assert any(ok for ok, _, _ in verdicts) and not all(ok for ok, _, _ in verdicts), "the corpus must exercise both verdicts"
    assert any(re.search(r"\n\s*\n", t) and ok for t, (ok, _, _) in zip(texts, verdicts)), \
        "the corpus must include accepted prompts with a blank line, or the blank-line rule is untested"
    assert not bypasses, bypasses[:3]


@pytest.mark.parametrize("seed", [21, 22])
def test_chains_longer_than_any_cap_and_names_spelled_across_breaks_are_never_judged_on_a_prefix(seed, monkeypatch, tmp_path):
    monkeypatch.setenv("CGC_CONFIG", str(tmp_path / "absent"))
    sp = _spool()
    rng = random.Random(seed)
    texts = [_long(rng) for _ in range(60)] + [_build(rng) for _ in range(300)]
    verdicts, bypasses = _run(sp, texts, monkeypatch)
    assert not bypasses, bypasses[:3]
    # the oracle really did see the long spelled-out forms the generator makes
    spelled = [t for t in texts[:60] if t.count("\n") > 64 or "hub.com" in t.replace("\n", "")]
    assert spelled, "the corpus must contain a chain with more breaks than the gate's cap"


def test_the_lead_repro_host_spelled_across_twenty_breaks_is_refused(monkeypatch, tmp_path):
    """A host spelled one character per line, behind a clean cli/cli link: the maximal join lands on a
    repo the gate never verified, so it must refuse rather than yield a verdict on a prefix."""
    monkeypatch.setenv("CGC_CONFIG", str(tmp_path / "absent"))
    sp = _spool()
    spelled = _spelled("https://git", "hub.com/private/repo")
    text = "review https://github.com/cli/cli/ \n\n<" + spelled + "> "
    verdicts, bypasses = _run(sp, [text], monkeypatch)
    assert not verdicts[0][0] and not bypasses
    # the oracle itself sees the maximal join and where a parser lands (it is not vacuous)
    stripped = re.sub(r"[\t\r\n]", "", spelled)       # the oracle keys readings by their break-free text
    assert stripped in _readings(text)
    assert _lands_on(_whatwg(_readings(text)), stripped) == "private/repo"
