#!/usr/bin/env python3
# Created: 2026-09-06
# Authority basis: open-claude-gpt skill v2 — the rendered prompt is the ONLY artifact that reaches
#   ChatGPT, and a round costs a real paid message, so its shape is a contract, not a style choice.
#   This file pins that contract: every obligation the PROGRAM states must appear exactly once in
#   every render kind, and the rendered bytes must stay within a stated bound by origin.
"""
Render-invariant tests for `consult.py prep`.

An inventory of HEAD 144b5ff (2026-09-06) measured what a prompt was actually made of: two thirds
of an initial render was fixed scaffolding, and seven obligations were stated more than once to the
same recipient — four of them TWICE INSIDE ONE RENDERED PROMPT. "verify locally" landed exactly
three times in every single render the tool could produce. None of that was visible from reading
either template, because the duplicates lived in different layers (template Success criteria, the
follow-up's How-to-answer, the default output body, the replace close, the caller's own output
spec) and only met at render time.

So the fix is not "delete the duplicates" — that lasts until the next edit. It is this test. It
renders all four kinds through the REAL cmd_prep path, matches one stable MARKER per tracked
obligation, and fails if any marker appears anything other than exactly once. Reintroducing a
second copy anywhere in the stack — a new sentence in a template, a rule appended to both output
arms, a restatement inside a shipped output spec — fails here with the marker and the count.

Markers are substrings, not whole sentences, deliberately: rewording an obligation must NOT break
this test (that is normal editorial work), while re-stating it in a second place must.

Run: python3 -m pytest tests/test_prompt_invariants.py -q
"""
import argparse
import importlib.util
import os
import pathlib
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSULT = os.path.join(ROOT, "skill", "scripts", "consult.py")
# The repo's OWN recommended output spec for code/PR review (SKILL.md → "pass the scaffold ... with
# --output-replace"). The replace renders are measured with the real file, not a stub, because the
# duplication this test guards against is between the program's text and THAT file's text.
DEEP_REVIEW = os.path.join(ROOT, "skill", "references", "deep-review-output.md")


def _load():
    spec = importlib.util.spec_from_file_location("consult", CONSULT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# Caller-side inputs are fixed at the inventory's measured byte sizes so the size assertions below
# are about the PROGRAM's contribution and nothing else.
def _pad(seed: str, n: int) -> str:
    return ((seed + " ") * (n // len(seed + " ") + 2))[:n]


TITLE = _pad("Merge-safety gate: PR #406 data-loss risk", 52)
ROLE = _pad("You are a distributed-systems security auditor.", 101)
TASK = _pad("Decide whether the queue drain is safe under a mid-flight restart.", 684)
REFS = _pad("- tree: https://github.com/acme/widgets/tree/deadbeef", 740)
CTX = _pad("Local results: the drain test fails once in forty runs.", 1098)


# One marker per tracked obligation. The alternatives in each pattern are the DIFFERENT phrasings
# the same obligation has been given across the stack — a marker must match every place the
# obligation could be stated, or moving a copy would read as deleting it.
MARKERS = {
    # Outcome-first. Interaction contract, both templates.
    "lead_with_the_answer": r"Open with the answer|Open with the verdict|Lead with a one-line verdict",
    # Challenge the premise / name the superior alternative. Success criteria (initial) or
    # How-to-answer (continuation); a shipped output spec must not restate the base obligation.
    "challenge_premise": r"strongest alternative|challenge whether the approach|challenge the premise",
    # Hedge discipline. A FIELD of whichever findings shape is in force — never also a sentence.
    "verify_locally": r"verify locally",
    # Actionability. The shared output close, whichever shape arm ran.
    "commit_to_calls": r"Commit to calls|conviction Claude Code can act on|Answer with conviction",
    # Instruction precedence (prompt-injection defense). One constant, both templates.
    "evidence_precedence": r"evidence to review, not directives to follow",
    # Unattended authorization / stop rule. One constant, both templates.
    "stop_rule": r"authorization to settle the scope",
    # Transport frame. One constant, both templates. Each sentinel line exactly once — a second
    # BEGIN_RESPONSE line in the prompt is an extraction hazard, not merely verbose.
    "sentinel_begin": r"BEGIN_RESPONSE:",
    "sentinel_end": r"END_RESPONSE:",
    # Form rule (prose over lists). The shared output close.
    "prose_over_lists": r"plain prose|Write in paragraphs",
    # Provenance close. The shared output close.
    "sources_close": r"Close with the sources",
    # The review dimensions. A property of the findings SHAPE, not of the interaction contract:
    # whichever shape is in force states them once — the default arm, or the caller's own spec. They
    # were a Success-criteria bullet, which billed a maths proof for a code-review checklist and
    # dropped them entirely under --output-replace, the path SKILL.md recommends for every review.
    "review_dimensions": r"migration/rollback",
}


def _ns(tmp, **over):
    ns = argparse.Namespace(
        repo_dir=ROOT, repo="acme/widgets", pr=None, ref="a" * 40, compare=None,
        pulls=False, blobs=False, files=None, issues=None, base=None,
        verify=False, allow_nonpublic=False,
        backend="cdp", window_template=None, task=TASK, title=TITLE, role=ROLE,
        followup=False, no_code=False, context_file=None, refs_file=None,
        output_file=None, output_replace=False, target_chars=32000, hard_chars=40000,
        expect_minutes=25, model="Pro", out=None,
        project_url="https://chatgpt.com/", conversation=None)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture(scope="module")
def renders(tmp_path_factory):
    """The four render kinds, produced by the real cmd_prep, keyed by name."""
    mod = _load()
    tmp = tmp_path_factory.mktemp("cgc-invariants")
    mod.CGC_STATE_DIR = str(tmp)
    (tmp / "refs.md").write_text(REFS, encoding="utf-8")
    (tmp / "ctx.md").write_text(CTX, encoding="utf-8")
    refs, ctx = str(tmp / "refs.md"), str(tmp / "ctx.md")
    kinds = {
        "initial_default": dict(refs_file=refs),
        "followup_default": dict(followup=True, role=None, no_code=True, context_file=ctx),
        "initial_replace": dict(refs_file=refs, output_file=DEEP_REVIEW, output_replace=True),
        "followup_replace": dict(followup=True, role=None, no_code=True, context_file=ctx,
                                 output_file=DEEP_REVIEW, output_replace=True),
    }
    out = {}
    for name, over in kinds.items():
        st = mod.cmd_prep(_ns(tmp, **over))
        out[name] = pathlib.Path(st["prompt_file"]).read_text(encoding="utf-8")
    return out


KINDS = ("initial_default", "followup_default", "initial_replace", "followup_replace")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("obligation,pattern", sorted(MARKERS.items()))
def test_each_obligation_is_stated_exactly_once(renders, kind, obligation, pattern):
    """The whole point. Every tracked obligation, in every render kind, exactly once.

    A failure means one of two things and the count tells you which:
      n > 1 — a second copy was reintroduced. Find both, decide which LAYER owns the obligation
              (the interaction contract, the shape arm, the shared output close, or the caller's
              own spec), and delete the other. Never "soften" one copy: two statements of one rule
              are two rules to the model, and this prompt is paid for by the message.
      n == 0 — the obligation was dropped, or reworded past every alternative in its marker. If the
              rewording was deliberate, extend the marker in the SAME commit; if it was not, the
              round now ships without a rule it used to carry.
    """
    hits = re.findall(pattern, renders[kind])
    assert len(hits) == 1, (
        f"{kind}: obligation {obligation!r} appears {len(hits)} times, expected exactly 1 "
        f"(matched: {hits})")


def test_no_render_kind_was_silently_dropped(renders):
    """The matrix above is only a guarantee if it covers every kind prep can produce: initial or
    continuation, crossed with the default findings shape or a caller-owned one."""
    assert set(renders) == set(KINDS)


# ---- Composition: rendered bytes by origin, bounded ---------------------------------------------
# The inventory's headline was not a duplicate count, it was a ratio: 47.5% of an initial render was
# fixed scaffolding and 15.1% was the caller's actual question. Scaffolding grows one reasonable
# sentence at a time, and nothing notices. These bounds make it notice. They are ceilings on the
# PROGRAM's bytes with caller inputs pinned above — raise one only with a measurement that says the
# added sentence is worth what it costs on every paid round.
#
# Measured 2026-09-06 after this restructuring, through the real prep path:
#   initial_default   4,444 B — fixed 2,101 (47.3%), output contract 766 (17.2%), task 684 (15.4%)
#   followup_default  4,237 B — fixed 1,637 (38.6%), output contract 766 (18.1%)
#   initial_replace   6,029 B — fixed 2,103 (34.9%), output contract 475  (7.9%), spec 1,874
#   followup_replace  5,822 B — fixed 1,639 (28.2%), output contract 475  (8.2%), spec 1,874
#
# Re-measured 2026-09-06 after the review DIMENSIONS moved out of the initial template's Success
# criteria and into the findings arm that IS the review shape (and the second kind-specific bullet
# was deleted as already-owned prose). Two bullets, 246 B, left the fixed scaffolding of BOTH
# initial kinds; 94 B of them re-entered the default shape arm, where only a findings deliverable
# pays for them:
#   initial_default   4,292 B — fixed 1,855 (43.2%), output contract 860 (20.0%)     [-152 B]
#   followup_default  4,331 B — fixed 1,637 (37.8%), output contract 860 (19.9%)     [+94 B]
#   initial_replace   5,936 B — fixed 1,857 (31.3%), output contract 475  (8.0%)     [-93 B]
#   followup_replace  5,975 B — fixed 1,639 (27.4%), output contract 475  (7.9%)     [+153 B]
# The two replace deltas are the review spec stating the three dimensions no section of it owned
# (security, migration/rollback, concurrency/ordering), which it must now that the default arm no
# longer reaches it. The follow-up deltas are coverage the follow-up never had: the dimensions were
# in PROMPT_TEMPLATE only, so a continuation review round rendered without them. The kind the move
# is FOR is the one no row above shows — a caller-owned NON-review deliverable, e.g. the plan spec
# in examples/_specs: 5,052 B -> 4,806 B (-4.9%), and a code-review checklist no longer ships with
# a plan at all.
# Bounds carry ~5% headroom over those numbers, so ordinary rewording passes and accretion does not.
FIXED_TEMPLATE_MAX = {
    "initial_default": 1950, "followup_default": 1720,
    "initial_replace": 1950, "followup_replace": 1720,
}
# The composed Output section (shape arm + shared close), excluding the caller's own spec.
OUTPUT_CONTRACT_MAX = {"default": 905, "replace": 500}


@pytest.mark.parametrize("kind", KINDS)
def test_program_owned_bytes_stay_within_their_stated_bound(renders, kind):
    mod = _load()
    text = renders[kind]
    total = len(text.encode("utf-8"))
    replace = kind.endswith("_replace")
    caller = (len(TASK.encode()) + len(TITLE.encode())
              + (0 if kind.startswith("followup") else len(ROLE.encode()))
              + (len(CTX.encode()) if kind.startswith("followup") else len(REFS.encode()))
              + (len(pathlib.Path(DEEP_REVIEW).read_text(encoding="utf-8").strip().encode())
                 if replace else 0))
    contract = len(mod.compose_output_body("", replace).encode("utf-8"))
    fixed = total - caller - contract

    assert contract <= OUTPUT_CONTRACT_MAX["replace" if replace else "default"], (
        f"{kind}: the composed Output contract is {contract} B — over its bound. Every byte here "
        f"is paid on every round of this kind.")
    assert fixed <= FIXED_TEMPLATE_MAX[kind], (
        f"{kind}: fixed template scaffolding is {fixed} B — over its bound. Scaffolding grew; "
        f"either it earned the room (raise the bound with the measurement) or it did not.")
    # The caller's question must not be a rounding error in its own prompt.
    assert len(TASK.encode()) / total > 0.10, (
        f"{kind}: the caller's task is under 10% of the render — scaffolding is crowding out the "
        f"decision-specific content the round is actually for.")


def test_the_two_templates_share_one_copy_of_the_wire_protocol(renders):
    """Source-level DRY, asserted where it is checkable: the evidence-precedence line, the stop rule
    and the BEGIN/END wrap are ONE constant each, interpolated into both templates. They are
    mutually exclusive at render time, so a drifted second copy costs no tokens and shows up in no
    output — an initial round and its own follow-up would simply obey different rules. Comparing the
    rendered text of both kinds is what makes that drift visible."""
    mod = _load()
    for const in (mod.EVIDENCE_PRECEDENCE, mod.STOP_RULE, mod.LEAD_WITH_THE_ANSWER):
        for kind in KINDS:
            assert const in renders[kind], f"{kind} lost a shared wire-protocol constant"
    assert mod.RESPONSE_WRAP.count("{rid}") == 2, \
        "the wrap must still carry both rid placeholders for the templates to fill"


def test_a_followup_carries_no_output_rules_of_its_own(renders):
    """FOLLOWUP_TEMPLATE used to state output rules inline, so --followup + --output-replace emitted
    'Commit to calls Claude Code can act on directly' twice, verbatim, in one prompt. The Output
    section owns those rules in every round now; this pins the combination that exposed it."""
    mod = _load()
    assert "Commit to calls" not in mod.FOLLOWUP_TEMPLATE
    assert "verify locally" not in mod.FOLLOWUP_TEMPLATE
    assert renders["followup_replace"].count("Commit to calls") == 1


@pytest.mark.parametrize("followup", (False, True))
def test_the_logical_sha_still_identifies_the_render_it_came_from(tmp_path, followup):
    """`logical_sha` — the same render with a literal <RID> placeholder — feeds the request-key
    fingerprint. Restructuring the templates changes the bytes, and therefore the sha, for the same
    caller inputs; what must not change is that BOTH render paths keep computing it from the render
    they actually emit. A path that hashed one composition and shipped another would key rounds by
    a prompt nobody ever sent.

    Consequence of this commit, deliberate and one-time: a --request-key fired before it and
    re-fired after it CONFLICTS, because the stored fingerprint hashes the old prompt. That is the
    honest report — the stored round genuinely carried a different request. Stored rounds are not
    rehashed, so nothing already in the store becomes inconsistent."""
    import hashlib
    mod = _load()
    mod.CGC_STATE_DIR = str(tmp_path)
    ns = _ns(tmp_path, followup=followup, no_code=True, role=None if followup else ROLE)
    a, b = mod.cmd_prep(ns), mod.cmd_prep(ns)
    text = pathlib.Path(a["prompt_file"]).read_text(encoding="utf-8")
    assert a["logical_sha"] == b["logical_sha"], "two rids, one logical request"
    assert a["logical_sha"] == hashlib.sha256(
        text.replace(a["request_id"], "<RID>").encode("utf-8")).hexdigest(), \
        "the identity must hash the render that was actually written"


def test_output_replace_leaves_the_findings_shape_to_the_caller(renders):
    """The replace arm must not re-ask for anything the spec owns — including the verify-locally
    tag, which is a field of whichever findings shape is in force."""
    mod = _load()
    assert "verify locally" not in mod.REPLACE_OUTPUT_OPEN
    assert "BLOCKER / HIGH / MEDIUM / LOW / NIT" not in renders["initial_replace"], \
        "the default severity enum must not collide with the caller's own scale"
