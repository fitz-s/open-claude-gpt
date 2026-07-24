#!/usr/bin/env python3
# Created: 2026-06-10
# Last reused or audited: 2026-07-01
# Authority basis: open-claude-gpt skill v2 — prep renders the GPT-5.6 outcome-first
#   PROMPT_TEMPLATE (title + steerable role + end-to-end depth mandate) and, with
#   --followup, the FOLLOWUP_TEMPLATE for a continuing thread; deliver builds
#   purpose-grouped GitHub refs. Used by both the CDP backend (primary) and the MCP
#   fallback. 2026-07-01: deliver hardened fail-closed on public-source provenance —
#   PUBLIC stamp requires gh-confirmed visibility=="public"; private/unknown visibility
#   refuses to emit refs unless --allow-nonpublic is passed; refs must always contain
#   >=1 real browsable URL (github.com/gist.github.com/raw.githubusercontent.com).
"""
Prep helper for the open-claude-gpt skill.

Generates a per-consult REQUEST_ID, renders the prompt file (sentinel contract +
"keep raw reference URLs inline"), and renders retrieval_window.js with the
request id + chunk sizes baked in. Prints a JSON state blob to stdout.

There is no local server / loopback in this design: the visible ChatGPT page
cannot open a connection to localhost (chatgpt.com serves a CSP that blocks
page->127.0.0.1), so completion is detected by agent-side scheduled polling and
the answer is read only through get_page_text + in-page DOM windowing.

Subcommands:
  prep   render prompt + retrieval window for one consult; print state JSON
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import secrets
import shlex
import subprocess
import sys
import os
import time


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

# Load persisted CGC_* settings from the user's config file (env still wins) — see
# cgc_config.py and `cgc set-project`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from cgc_config import load_config as _cgc_load_config
    _cgc_load_config()
except Exception:
    pass

# Scratch dir for prompt/refs/answer files — overridable via env (see docs/CONFIGURATION.md).
CGC_STATE_DIR = os.environ.get("CGC_STATE_DIR", "/tmp/cgc")


PROMPT_TEMPLATE = """# {title}

Role: {role}

# Goal
{task}

# Success criteria
- Open with the answer or recommendation and your confidence.
- Ground factual claims in the supplied sources or clearly named external sources.
- State the strongest alternative or counterargument you considered.
- For code/review tasks: cover correctness, security, migration/rollback, concurrency/ordering, and boundary cases.
- For plans/research/design/math tasks: use the task's requested structure and name the assumptions that would change the answer.
- Mark only claims that genuinely need a local runtime check as "verify locally: <check>".
{refs_block}{context_block}
# Constraints
- Where you lack the source for a claim, say so and mark it unknown rather than guessing; don't assert results you didn't verify.

# Output
{output_block}{output_body}

# Stop rules
- Answer in one shot from the evidence you have; if something material is missing, still give the verdict and name the one smallest fact that would change it.

# Final output format
Enclose your whole answer between these two lines, each on its OWN bare line (not inside a code block or \
quote). Always include BOTH wrapper lines, even on a short reply — an unwrapped reply can't be delivered. \
If space runs short, shorten the content, not the wrapper:
BEGIN_RESPONSE:{rid}
<full answer, references inline>
END_RESPONSE:{rid}"""


FOLLOWUP_TEMPLATE = """# {title}

Continuing this consult — Claude Code acted on your last answer locally; the results and the next ask are below.

# What I'm asking now
{task}
{refs_block}{context_block}
# How to answer
Where the local results contradict an earlier finding, revise it explicitly ("Revising <finding>: …") and \
say why; where they confirm it, say so and move on. If asked for a new plan or design, challenge whether the \
approach is right and name a superior alternative if one exists before detailing it. Commit to calls Claude Code \
can act on directly; reserve "verify locally: <check>" for the few claims that truly hinge on a runtime result \
you can't see (not a hedge on every point). Lead with a one-line verdict + confidence, then keep the finding shape:
`[SEVERITY] category — file:path:line — impact — concrete fix — verify locally: <check>`

# Final output format
Enclose your whole answer between these two lines, each on its OWN bare line (not inside a code block or \
quote). Always include BOTH wrapper lines, even on a short reply — an unwrapped reply can't be delivered. \
If space runs short, shorten the content, not the wrapper:
BEGIN_RESPONSE:{rid}
<full answer, references inline>
END_RESPONSE:{rid}"""


# The Output section body. DEFAULT = the standard findings contract. When the agent supplies
# its OWN output spec via --output-file AND passes --output-replace, the spec OWNS the output
# and this default is swapped for REPLACE_OUTPUT_CLOSE — so a custom findings shape/severity
# scale never collides with a second one appended by the template.
DEFAULT_OUTPUT_BODY = (
    "Open with the verdict and the reasoning behind it. For a review/code consult, "
    "then list the findings, one per entry in this shape:\n"
    "`[SEVERITY] category — file:path:line — impact (one sentence) — concrete fix — verify locally: <command/test>`\n"
    "(SEVERITY is one of BLOCKER / HIGH / MEDIUM / LOW / NIT). This findings list is for review/code consults; "
    "other deep work (research, analysis, design, a plan, math) follows the task's requested structure instead "
    "of the findings shape. Answer with conviction Claude Code can act on; append \"verify locally: <check>\" "
    "only on the few claims that genuinely need a runtime you can't see, not one per finding. Close with the "
    "sources you used (and any you couldn't read) and any load-bearing assumptions. Keep everything except a "
    "findings list in plain prose."
)
REPLACE_OUTPUT_CLOSE = (
    "Produce exactly the output structure above. Use ONE consistent severity scale and finding shape "
    "throughout — do not introduce a second. Commit to calls Claude Code can act on directly; reserve "
    "\"verify locally: <check>\" for the few claims that truly hinge on a runtime result you can't see, "
    "not a hedge on every point. Close with the sources you used (and any you couldn't read) "
    "and any load-bearing assumptions."
)


def _git(args, cwd):
    """Run a git command; return stripped stdout or None on failure."""
    try:
        r = subprocess.run(["git", "-C", cwd] + args, capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


# Whether this process may talk to github.com at all.
#
# `deliver` is run BY THE AGENT, and every `gh` call in it reaches out to github.com. That made the
# architecture's central claim — the agent touches only local files, the daemon does everything
# external — false in the one place nobody looked, and Claude Code's auto-mode classifier correctly
# denied the command. The fix is not to ask users to disable a safety classifier; it is to stop
# making the call. Nothing `gh` does here is required to BUILD the links: the slug, SHAs, merge-base
# and compare ranges all come from local git. `gh` only pre-verifies and enriches — and the one
# security-critical part of that, "is this repo really public?", is re-checked authoritatively by
# the daemon's egress gate before anything is sent. So the agent defers it instead of duplicating it.
_GH_ALLOWED = False


def _gh(args, cwd):
    if not _GH_ALLOWED:
        return None, "offline: deliver makes no network calls; the egress gate verifies provenance"
    try:
        r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=20, cwd=cwd)
    except Exception:
        return None, "gh_not_available"
    if r.returncode != 0:
        return None, (r.stderr.strip() or "gh_error")
    return r.stdout.strip(), None


def _prs_for_commit(slug, ref, cwd):
    """PRs whose head/branch contains this commit. Returns [{number,url,state}], OPEN first.
    A pushed commit almost always has an associated PR — that public link ALWAYS beats a gist
    (HARD RULE: link-first, gist-last). auto-detect's `gh pr view` only sees the local branch's
    OWN PR; this catches the commit's association even when the branch isn't checked out."""
    if not (slug and ref):
        return []
    j, _ = _gh(["api", f"repos/{slug}/commits/{ref}/pulls",
                "-H", "Accept: application/vnd.github+json",
                "--jq", ".[] | {number: .number, url: .html_url, state: .state}"], cwd)
    prs = []
    for line in (j or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            prs.append({"number": d.get("number"), "url": d.get("html_url") or d.get("url"),
                        "state": (d.get("state") or "").upper()})
        except Exception:
            pass
    prs.sort(key=lambda p: 0 if p["state"] == "OPEN" else 1)
    return prs


def _github_slug(remote_url):
    """git@github.com:owner/repo.git | https://github.com/owner/repo(.git) -> 'owner/repo' (github only)."""
    if not remote_url:
        return None
    u = remote_url.strip()
    m = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", u) or re.match(r"ssh://git@([^/]+)/(.+?)(?:\.git)?$", u)
    if m:
        host, path = m.group(1), m.group(2)
    else:
        m = re.match(r"https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?$", u)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    if "github.com" not in host:
        return None
    return path


def _has_browsable_url(groups):
    """True if at least one ref across all groups is a real browsable URL
    (github.com / gist.github.com / raw.githubusercontent.com). Guards against
    a refs file that reads as a source delivery but carries no actual link the
    external model could open."""
    for items in groups.values():
        for url, _purpose in items or []:
            if isinstance(url, str) and re.match(
                r"^https://(github\.com|gist\.github\.com|raw\.githubusercontent\.com)/", url):
                return True
    return False


def _render_groups(groups):
    """Render purpose-grouped refs into the markdown sections the prompt expects."""
    titles = [
        ("intent", "### Intent / discussion (read first)"),
        ("change_set", "### Change set (the diff under review)"),
        ("snapshot", "### Stable source snapshot (commit-pinned, for surrounding context)"),
        ("close_reading", "### Close-reading files (read these closely, then explore beyond them)"),
        ("discovery", "### Discovery (triage these to find what is relevant)"),
        ("fallback", "### Fallback (only if the above are inaccessible)"),
    ]
    out = []
    for key, title in titles:
        items = groups.get(key) or []
        if not items:
            continue
        out.append(title)
        for url, purpose in items:
            out.append(f"- {url}" + (f"  — {purpose}" if purpose else ""))
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def cmd_deliver(a: argparse.Namespace) -> int:
    global _GH_ALLOWED
    _GH_ALLOWED = bool(getattr(a, "verify", False))
    """Build purpose-grouped, ChatGPT-browsable GitHub refs. AGENT-DRIVEN.

    The AGENT decides what the task needs and passes it explicitly — different repo
    (--repo owner/repo), a specific PR (--pr N), a branch/tag/SHA (--ref main), a
    range (--compare A...B), files (--files), issues (--issues), the open-PR list
    (--pulls). deliver just renders those into grouped sections. ONLY when no explicit
    target is given does it auto-detect the current branch's PR / compare / gist as a
    convenience. Prefer LINKS over uploads — a PR/compare/tree/blob link carries the
    diff, intent, discussion, CI, and navigation that a single uploaded file cannot.
    """
    repo = a.repo_dir
    root = _git(["rev-parse", "--show-toplevel"], repo)
    out = {"mode": None, "slug": None, "groups": {}, "needs_gist": False,
           "refs_file": "", "note": "", "head_sha": None, "base_sha": None,
           "visibility": None, "public_ok": False}

    # slug: explicit --repo wins; else the local origin.
    slug = a.repo or (_github_slug(_git(["remote", "get-url", "origin"], root)) if root else None)
    if not slug:
        out.update(mode="manual", needs_gist=True,
                   note=("no repo slug — pass --repo owner/repo. A gist fallback needs a network "
                         "write, which this command deliberately cannot do; the gate refuses gist "
                         "links by default anyway (CGC_GATE_ALLOW_GIST)."))
        return out
    out["slug"] = slug
    base = "https://github.com/" + slug
    # Repo visibility — the consult sends a LINK to an external model, so the delivered payload
    # must make it unambiguous whether that link is world-readable. A public repo's link is not
    # exfiltration; a private repo's link IS (and ChatGPT can't even open it). Verifying + stamping
    # this into the refs keeps the payload from reading as "internal/private repo content".
    vj, _ = _gh(["api", f"repos/{slug}", "--jq", ".visibility"], repo if root else ".")
    visibility = vj.strip() if vj else None
    out["visibility"] = visibility
    files = list(a.files or [])
    issues = list(a.issues or [])
    groups = {k: [] for k in ("intent", "change_set", "snapshot", "close_reading",
                              "discovery", "fallback")}

    # local git facts (only meaningful when --repo matches the local origin / unset)
    head_sha = _git(["rev-parse", "HEAD"], root) if root else None
    out["head_sha"] = head_sha
    local_repo = root and (not a.repo)  # git facts apply only to the local repo

    explicit = any([a.repo, a.pr, a.ref, a.compare, a.pulls, a.files, a.issues])

    for iss in issues:
        n = str(iss).lstrip("#")
        groups["intent"].append((f"{base}/issues/{n}", "requirements / reproduction / discussion"))

    if explicit:
        # ---- agent-driven: build exactly what was asked ----
        out["mode"] = "explicit"
        pr_sha = None
        pr_files = []
        if a.pr:
            n = str(a.pr).lstrip("#")
            groups["intent"].insert(0, (f"{base}/pull/{n}", "PR description, discussion, CI/review context"))
            groups["change_set"].append((f"{base}/pull/{n}/files", "the changed-file diff — the primary review surface"))
            # The PR LINK above is the primary review surface (carries diff + intent + CI).
            # Only fetch per-file blob permalinks when --blobs is set (e.g. a huge PR whose
            # aggregate diff fails to render) — otherwise a blob dump just buries the link.
            if a.blobs:
                j, _ = _gh(["pr", "view", n, "-R", slug, "--json", "files,headRefOid"],
                           repo if root else ".")
                if j:
                    try:
                        d = json.loads(j)
                        pr_sha = d.get("headRefOid")
                        pr_files = [f.get("path") for f in (d.get("files") or []) if f.get("path")]
                    except Exception:
                        pass
                    if pr_sha:
                        out["head_sha"] = pr_sha
        ref = a.ref or pr_sha or (head_sha if local_repo else None)  # ref for tree/blob
        # The snapshot section is labelled "commit-pinned", but a bare branch/tag ref
        # (tree/main) MOVES — by the time ChatGPT opens it (or it's re-read days later) the
        # commit can differ from the one you consulted on. Resolve a non-sha ref to its
        # CURRENT commit SHA so every tree/blob link is immutable and "commit-pinned" is
        # truthful. A ref that already looks like a sha is kept as-is.
        ref_sha = ref
        if ref and not (7 <= len(ref) <= 40 and all(ch in "0123456789abcdef" for ch in ref.lower())):
            j, _ = _gh(["api", f"repos/{slug}/commits/{ref}", "--jq", ".sha"], repo if root else ".")
            if j:
                ref_sha = j
            else:
                out["note"] = ((out["note"] + " ") if out["note"] else "") + (
                    f"NOT PINNED: could not resolve '{ref}' to a commit SHA via gh — the snapshot link "
                    f"stays on the MOVING ref '{ref}'. Auth gh (gh auth login) or pass --ref <sha>.")
        # Currency guards — the pinned commit must reflect the code you actually want reviewed.
        if local_repo and head_sha and ref_sha:
            if ref_sha != head_sha:
                out["note"] = ((out["note"] + " ") if out["note"] else "") + (
                    f"STALE? pinned commit {ref_sha[:12]} != local HEAD {head_sha[:12]} — if you have "
                    f"newer local work, push it and --ref that sha, else the consult reviews older code.")
            elif not _git(["branch", "-r", "--contains", "HEAD"], root):
                out["note"] = ((out["note"] + " ") if out["note"] else "") + (
                    "HEAD NOT PUSHED: the pinned commit isn't on the remote yet — push the branch "
                    "first or the tree/blob links 404. A consult must run on PUSHED code.")
        if a.compare:
            groups["change_set"].append((f"{base}/compare/{a.compare}", "change set (compare range)"))
        # PR-over-everything: if no --pr was given but this commit HAS an associated PR, lead with
        # it. The PR link carries diff+intent+discussion+CI and is the least-exfiltration-looking
        # surface (a bare public github.com pointer). HARD RULE: a PR link always beats a gist/tree.
        if not a.pr and ref_sha:
            for pr in _prs_for_commit(slug, ref_sha, repo if root else "."):
                if pr.get("url") and pr.get("number"):
                    groups["intent"].insert(0, (pr["url"], f"PR #{pr['number']} ({pr['state'].lower()}) — description, discussion, CI/review context"))
                    groups["change_set"].insert(0, (f"{base}/pull/{pr['number']}/files", "the changed-file diff — primary review surface"))
                    out["associated_pr"] = pr["number"]
                    break
        if ref_sha:
            groups["snapshot"].append((f"{base}/tree/{ref_sha}", f"source snapshot at {ref_sha[:12] if len(ref_sha) >= 12 else ref_sha} — browse the tree for surrounding code"))
        # Per-file blobs: only what the agent EXPLICITLY asked (--files) or the PR's files
        # when --blobs was set. A review should lead with the PR/tree LINK, not a 60-blob
        # dump — so cap explicit --files and point overflow at the tree.
        blob_files = list(files) if files else (pr_files if a.blobs else [])
        if blob_files:
            if ref_sha:
                CAP = 25
                for f in blob_files[:CAP]:
                    groups["close_reading"].append((f"{base}/blob/{ref_sha}/{f}", "file for close reading"))
                if len(blob_files) > CAP:
                    overflow = f"{base}/pull/{str(a.pr).lstrip('#')}/files" if a.pr else f"{base}/tree/{ref_sha}"
                    groups["close_reading"].append(
                        (overflow, f"+{len(blob_files) - CAP} more files — open the rest from here at {ref_sha[:12]} as needed"))
            else:
                out["note"] = ("blob links need a ref — pass --ref <branch|tag|sha> (e.g. --ref main) "
                               "when using --files with a non-local repo and no --pr.")
        if a.pulls:
            groups["discovery"].append((f"{base}/pulls", "open-PR list — triage to find the relevant PR"))
        if not any(groups.values()) and not out["note"]:
            out["note"] = "nothing to render — pass --pr / --ref / --compare / --files / --pulls / --issues."
    else:
        # ---- convenience auto-detect for the LOCAL current branch ----
        pushed = bool(_git(["branch", "-r", "--contains", "HEAD"], root)) if head_sha else False
        base_sha = None
        defref = _git(["symbolic-ref", "refs/remotes/origin/HEAD"], root) if root else None
        if a.base:
            base_sha = _git(["rev-parse", a.base], root)
        elif defref:
            short = defref.split("/")[-1]
            base_sha = _git(["merge-base", short, "HEAD"], root)
        out["base_sha"] = base_sha
        pr_json, _ = _gh(["pr", "view", "--json", "url,number,state,headRefOid"], root) if root else (None, None)
        pr = None
        if pr_json:
            try:
                pr = json.loads(pr_json)
            except Exception:
                pr = None
        if pr and (pr.get("state") or "").upper() == "OPEN" and pr.get("url"):
            out["mode"] = "auto-pr"
            n = pr.get("number"); sha = pr.get("headRefOid") or head_sha
            groups["intent"].insert(0, (pr["url"], "PR description, discussion, CI/review context"))
            groups["change_set"].append((f"{base}/pull/{n}/files", "complete changed-file diff"))
            if sha:
                groups["snapshot"].append((f"{base}/tree/{sha}", "source snapshot at PR head"))
                for f in files:
                    groups["close_reading"].append((f"{base}/blob/{sha}/{f}", "high-risk changed file"))
        elif pushed and head_sha:
            out["mode"] = "auto-compare"
            if base_sha:
                groups["change_set"].append((f"{base}/compare/{base_sha}...{head_sha}", "full change set vs base"))
            else:
                groups["change_set"].append((f"{base}/commit/{head_sha}", "HEAD commit diff (base unknown)"))
            groups["snapshot"].append((f"{base}/tree/{head_sha}", "source snapshot at HEAD"))
            for f in files:
                groups["close_reading"].append((f"{base}/blob/{head_sha}/{f}", "key file"))
        else:
            # Last check before gisting: even if `gh pr view` saw no PR for the checked-out
            # branch, the HEAD commit may still be associated with a public PR. That link ALWAYS
            # beats a gist — resolve it and use auto-pr rather than falling to needs_gist.
            assoc = _prs_for_commit(slug, head_sha, root) if head_sha else []
            if assoc and assoc[0].get("url") and assoc[0].get("number"):
                pr = assoc[0]; n = pr["number"]
                out["mode"] = "auto-pr"; out["associated_pr"] = n
                groups["intent"].insert(0, (pr["url"], f"PR #{n} ({pr['state'].lower()}) — description, discussion, CI/review context"))
                groups["change_set"].append((f"{base}/pull/{n}/files", "complete changed-file diff"))
                if head_sha:
                    groups["snapshot"].append((f"{base}/tree/{head_sha}", "source snapshot at HEAD"))
                    for f in files:
                        groups["close_reading"].append((f"{base}/blob/{head_sha}/{f}", "high-risk changed file"))
            else:
                out.update(mode="local", needs_gist=True,
                           note="HEAD not pushed AND no associated PR — push the branch then re-run, "
                                "pass explicit --pr/--ref/--compare, or deliver local state via gist")
                return out

    out["groups"] = {k: v for k, v in groups.items() if v}
    if not any(groups.values()):
        return out

    # Fail-closed browsable-URL guarantee: a refs file that carries no actual
    # github.com / gist.github.com / raw.githubusercontent.com link is not a source
    # delivery at all — refuse rather than emit a file that reads as one.
    if not _has_browsable_url(groups):
        out.update(needs_gist=True)
        out["note"] = ((out["note"] + " ") if out["note"] else "") + (
            "NO BROWSABLE URL: none of the rendered refs are a github.com / gist.github.com / "
            "raw.githubusercontent.com link — refusing to emit a refs file. Push the code and pass "
            "--ref/--pr, or `gh gist create` and use the gist link.")
        return out

    body = _render_groups(groups)
    if not _GH_ALLOWED:
        # Provenance is DEFERRED, not skipped. The daemon's gate re-checks every repo slug in the
        # rendered prompt with `gh` and fails closed, so a private repo still cannot be sent — the
        # check simply happens at the egress point, where it is authoritative, instead of here,
        # where it is advisory and costs the agent an external call it is not allowed to make.
        out["public_ok"] = None
        out["visibility"] = "deferred"
        body = ("> Source visibility: TO BE VERIFIED AT THE EGRESS GATE — these links are not sent "
                "until the consult daemon confirms every repo is public.\n\n") + body
        # Unique per call, not per second: two concurrent deliveries at the same second wrote the
        # same path, so one consult could read the OTHER's repository references and review the
        # wrong source without anything looking wrong.
        pathlib.Path(CGC_STATE_DIR).mkdir(parents=True, exist_ok=True)
        refs_file = str(pathlib.Path(CGC_STATE_DIR) /
                        f"refs_{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.md")
        pathlib.Path(refs_file).write_text(body, encoding="utf-8")
        out["refs_file"] = refs_file
        return out
    is_public = visibility == "public"
    # Fail-closed: only a CONFIRMED public repo gets the PUBLIC stamp. Anything else —
    # PRIVATE, or visibility UNKNOWN because `gh` is missing/unauthenticated/errored —
    # must NOT emit a refs file that reads as a public delivery. The caller can override
    # with --allow-nonpublic, which stamps the file as explicitly non-public instead.
    if is_public:
        out["public_ok"] = True
        # Stamp the payload so it's self-evidently a public-link delivery, not private exfiltration.
        body = "> Source visibility: PUBLIC — world-readable github.com links.\n\n" + body
    elif a.allow_nonpublic:
        out["public_ok"] = False
        reason = f"{visibility} repo" if visibility else "visibility UNKNOWN (gh missing/unauthenticated/errored)"
        out["note"] = ((out["note"] + " ") if out["note"] else "") + (
            f"NON-PUBLIC delivery explicitly allowed via --allow-nonpublic ({reason}). These links may "
            f"not be world-readable — verify the consult destination can actually open them.")
        body = (f"> Source visibility: NON-PUBLIC (explicitly allowed) — {reason}; these links are NOT "
                f"confirmed world-readable.\n\n") + body
    else:
        # Refuse: do not write a refs file that could pass for a public delivery.
        out["needs_gist"] = True
        if visibility:
            out["note"] = ((out["note"] + " ") if out["note"] else "") + (
                f"PRIVATE repo ({visibility}): ChatGPT cannot open these links and delivering private "
                f"repo content externally is exfiltration — refusing to emit a refs file. Push the "
                f"reviewed commit to a public repo, deliver a redacted gist, or pass --allow-nonpublic "
                f"to explicitly override.")
        else:
            out["note"] = ((out["note"] + " ") if out["note"] else "") + (
                "VISIBILITY UNKNOWN: could not confirm this repo is public (gh missing, unauthenticated, "
                "or errored) — refusing to emit a refs file that would read as a public delivery. "
                "Auth gh (gh auth login) and re-run, or pass --allow-nonpublic to explicitly override.")
        return out

    pathlib.Path(CGC_STATE_DIR).mkdir(parents=True, exist_ok=True)
    refs_file = os.path.join(CGC_STATE_DIR, f"refs_{time.strftime('%Y%m%d-%H%M%S')}.md")
    pathlib.Path(refs_file).write_text(body, encoding="utf-8")
    out["refs_file"] = refs_file
    return out


def cmd_prep(a: argparse.Namespace) -> int:
    if a.target_chars < 2000 or a.hard_chars < a.target_chars:
        raise SystemExit("CGC_ERROR bad_chunk_sizes: need target-chars>=2000 and hard-chars>=target-chars")
    if a.expect_minutes < 1 or a.expect_minutes > 60:
        raise SystemExit("CGC_ERROR bad_expect_minutes: need 1<=expect-minutes<=60")
    rid = "REQ-" + time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)

    # Wake plan. Each wake = one full main-context reload (ScheduleWakeup sleep
    # >5 min always misses the prompt cache), so token cost ~= wake_count *
    # context_size. The poll_js round-trip itself is trivial. Being LATE is free
    # (idle wall-time, no tokens); waking EARLY on an unfinished answer burns a
    # whole reload for nothing. So bias LONG: first wake lands at ~85% of the
    # expected latency, then back off in coarse 6-min steps. Target <=4 wakes.
    first_wake = max(120, int(a.expect_minutes * 60 * 0.85))
    repoll = 360
    # Cover ~expect+18 min before giving up; clamp wake count to [3, 8].
    span = a.expect_minutes * 60 + 18 * 60 - first_wake
    max_polls = int(min(8, max(3, 2 + (span // repoll))))

    # Per-consult scratch lives in $CGC_STATE_DIR (default /tmp/cgc) so it is easy to
    # clean (`rm -rf`) and does not flat-litter /tmp. Answers go wherever `wait --out` points.
    scratch = pathlib.Path(CGC_STATE_DIR)
    # 0700: prompts are public by construction, but this directory also holds refs, embedded local
    # context and (from the waiter) answers, under a world-traversable /tmp.
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        scratch.chmod(0o700)
    except OSError:
        pass

    # The DOM-windowing script is ONLY used by the MCP fallback (Backend B). On the
    # default CDP backend it is dead weight — skip it (was creating a junk file/consult).
    window_js_file = ""
    if a.backend == "mcp":
        if not a.window_template:
            raise SystemExit("CGC_ERROR need_window_template: --backend mcp requires --window-template")
        window_template = pathlib.Path(a.window_template).read_text(encoding="utf-8")
        cfg = {"requestId": rid, "targetChars": a.target_chars, "hardChars": a.hard_chars}
        rendered = window_template.replace("__CGC_CONFIG__", json.dumps(cfg, ensure_ascii=False))
        if "__CGC_CONFIG__" in rendered:
            raise SystemExit("CGC_ERROR window_render_failed")
        window_js_file = str(scratch / f"window_{rid}.js")
        pathlib.Path(window_js_file).write_text(rendered, encoding="utf-8")

    prompt_file = ""
    # The rid-independent prompt identity: the SAME render with a literal <RID> placeholder. It
    # feeds the request-key fingerprint, so a re-fire (new rid, identical logical request) matches
    # without normalizing user text — a rid quoted in task text is never touched.
    logical_sha = None
    if a.task is not None:
        # Title (before Role) + Role frame the consult and are STEERING levers: a sharp
        # title + a persona matched to the job (security auditor / distributed-systems
        # reviewer / API designer / …) primes a deeper answer than the generic default.
        title = a.title or ("Follow-up round" if a.followup else "Engineering consult")
        role = a.role or "You are a senior expert in the domain this task concerns."

        # Read sources ONCE (stripped → empty/blank file counts as NO source).
        refs = pathlib.Path(a.refs_file).read_text(encoding="utf-8").strip() if a.refs_file else ""
        ctx = pathlib.Path(a.context_file).read_text(encoding="utf-8").strip() if a.context_file else ""
        # A code/architecture consult about a repo MUST carry the actual code as a LINK (--refs-file
        # from `deliver`: a GitHub PR/tree/blob/compare, or a gist for unpushed code). A --context-file
        # of ABSTRACTED PROSE is NOT the code — ChatGPT can't read the repo from a description and
        # answers BLIND ("no file access, can't cite file:line"). So --refs-file is REQUIRED for a
        # non-followup consult; --context-file is only a supplement. This is THE recurring failure.
        if not a.followup and not refs and not a.no_code:
            raise SystemExit(
                "CGC_ERROR no_code_source: no actual code link attached (no --refs-file)."
                + (" A --context-file was given, but ABSTRACTED PROSE IS NOT THE CODE — ChatGPT can't "
                   "read your repo from a description and will answer BLIND (the recurring 'no file "
                   "access / can't cite file:line' failure)." if ctx else "")
                + " Refusing to render — there is NO override. FIX: run\n"
                "    consult.py deliver --repo <owner/repo> --ref <sha>     # whole-repo /tree link — for a broad design consult, NO need to pick files\n"
                "    consult.py deliver --repo <owner/repo> --pr <N>        # or a specific PR / --compare / --files; for unpushed code it makes a gist\n"
                "then pass the printed refs_file to prep as --refs-file <file>. --context-file is "
                "SUPPLEMENTARY only (failure bundles, perf numbers, external design positions) — it is "
                "NOT the code. Every code/architecture consult MUST ship the code link, including a "
                "design/strategy consult about an existing repo. If you have a design doc but no repo, "
                "`gh gist create <doc>` and pass the gist link as --refs-file.")

        context_block = ""
        if ctx:
            label = "Local results since the last round" if a.followup else "Context"
            context_block = f"\n## {label}\n{ctx}\n"
        # The source-handling instruction lives WITH the sources, so when no refs are
        # attached the prompt makes no dangling reference to a source section.
        refs_block = ""
        if a.no_code and not refs:
            # Declares the consult carries no code, for the model AND for the daemon's gate, which
            # keys its rule-1 exemption off this sentence (see cgc_spool.validate_prompt).
            refs_block = ("\n# Source\n"
                          "This consult references no code — it is a self-contained question. "
                          "Reason from first principles and name any public sources you rely on.\n")
        elif refs:
            if a.followup:
                refs_block = (
                    "\n# New or changed source since the last round\n"
                    "These are new or updated since your previous answer (e.g. the fix Claude Code "
                    "applied). Open them, read what changed, and navigate out to anything they now "
                    "affect before responding.\n"
                    f"{refs}\n")
            else:
                refs_block = (
                    "\n# Source — entry points (explore freely from here)\n"
                    "These links are starting points, not a closed list. Open them, then navigate the repo "
                    "yourself — follow imports, callers, tests, config, and related files wherever the "
                    "question leads, and pull in whatever you need to ground the verdict. Treat the actual "
                    "code as ground truth over any inline summary. If a link won't load, try another route "
                    "and name anything you still cannot read.\n"
                    f"{refs}\n")
        if a.followup:
            prompt = FOLLOWUP_TEMPLATE.format(title=title, task=a.task, refs_block=refs_block,
                                              context_block=context_block, rid=rid)
            logical_sha = _sha256(FOLLOWUP_TEMPLATE.format(
                title=title, task=a.task, refs_block=refs_block,
                context_block=context_block, rid="<RID>"))
        else:
            output_block = ""
            output_body = DEFAULT_OUTPUT_BODY
            if a.output_file:
                output_block = pathlib.Path(a.output_file).read_text(encoding="utf-8").strip() + "\n\n"
                if a.output_replace:
                    # The spec owns the whole Output section — drop the default findings contract so
                    # a custom shape/severity scale can't collide with a second one.
                    output_body = REPLACE_OUTPUT_CLOSE
            elif a.output_replace:
                sys.stderr.write("CGC_WARN output_replace_ignored: --output-replace has no effect "
                                 "without --output-file.\n")
            prompt = PROMPT_TEMPLATE.format(title=title, role=role, task=a.task,
                                            refs_block=refs_block, context_block=context_block,
                                            output_block=output_block, output_body=output_body, rid=rid)
            logical_sha = _sha256(PROMPT_TEMPLATE.format(
                title=title, role=role, task=a.task, refs_block=refs_block,
                context_block=context_block, output_block=output_block,
                output_body=output_body, rid="<RID>"))
        # Provenance is declared once, concisely, by deliver's "> Source visibility: PUBLIC"
        # stamp inside the refs block — no need to also prepend a verbose scope banner.
        prompt_file = str(scratch / f"prompt_{rid}.md")
        pathlib.Path(prompt_file).write_text(prompt, encoding="utf-8")

    end = f"END_RESPONSE:{rid}"
    # Ready-to-paste JS snippets with the request id baked in (no manual <id> fill-in).
    # Both return METADATA ONLY (booleans / counts) — never page content or URLs.
    begin = f"BEGIN_RESPONSE:{rid}"
    # done is line-ANCHORED and FENCE-AWARE (SHARED CONTRACT #1, v2 — mirrors
    # _sentinel_parse / _sentinel_js in cdp_consult.py exactly): scan lines tracking
    # in_fence (a trimmed line starting with ``` or ~~~ toggles fence state and is
    # itself never a sentinel); bi = first non-fenced line === BEGIN_RESPONSE:<rid>;
    # ei = first non-fenced line AFTER bi === END_RESPONSE:<rid>. A bare sentinel line
    # ECHOED INSIDE a fenced code block is ignored — only bare sentinel lines outside
    # any fence satisfy completion. Reads textContent (not innerText, which can be
    # stale/empty on a backgrounded tab — see cdp_consult.py's textContent-vs-innerText
    # note) and scans assistant nodes NEWEST-to-OLDEST, returning the first node whose
    # parse is a complete non-empty wrapped answer — not just the last DOM node, since
    # the sentinel-bearing turn need not be the last node in every layout. `done` does
    # NOT depend on the stop button being gone (parity with the canonical _detect_js:
    # the model writes END_RESPONSE only as its final line, so its presence alone means
    # complete+extractable); `generating` still reports stop-button presence as
    # telemetry only, never as a completion gate.
    poll_js = (
        "(function(){"
        "var BG=" + json.dumps(begin) + ",EN=" + json.dumps(end) + ";"
        # Single-return by design (a body-parity probe patches this one return). bi = first bare
        # unfenced BEGIN. END must be the LAST non-blank line, bare and unfenced (anti-injection: an
        # early bare END is ignored while real answer text still follows). Mirrors _sentinel_parse.
        "function parse(t){"
        "t=(t||'').replace(/\\r\\n/g,'\\n');var L=t.split('\\n');"
        "var inFence=false,bi=-1;"
        "for(var i=0;i<L.length;i++){"
        "var ln=L[i].trim();"
        "if(ln.slice(0,3)==='```'||ln.slice(0,3)==='~~~'){inFence=!inFence;continue;}"
        "if(!inFence&&ln===BG){bi=i;break;}"
        "}"
        "var last=-1;"
        "if(bi>=0){for(var c=L.length-1;c>bi;c--){if(L[c].trim()!==''){last=c;break;}}}"
        "var endok=false;"
        "if(bi>=0&&last>bi&&L[last].trim()===EN){"
        "var f2=false;"
        "for(var b=bi+1;b<last;b++){var lb=L[b].trim();"
        "if(lb.slice(0,3)==='```'||lb.slice(0,3)==='~~~')f2=!f2;}"
        "endok=!f2;}"
        "var body=endok?L.slice(bi+1,last).join('\\n').trim():'';"
        "return {done:(endok&&body.length>0),len:t.length};"
        "}"
        # Either turn markup: older ChatGPT builds tag the message node
        # data-message-author-role="assistant", the current build tags the turn
        # data-turn="assistant". Matching only the old one selected nothing, so `done` could
        # never fire. Mirrors _SEL_A in cdp_consult.py — the parsers must agree.
        "var a=document.querySelectorAll('[data-message-author-role=\"assistant\"],"
        "[data-turn=\"assistant\"]');"
        "var res={done:false,len:0};"
        "for(var k=a.length-1;k>=0;k--){"
        "var r=parse(a[k].textContent);"
        "if(r.done){res=r;break;}"
        "if(k===a.length-1)res=r;"
        "}"
        "var stop=!!document.querySelector('[data-testid=\"stop-button\"],button[aria-label*=\"Stop\"],button[aria-label*=\"停止\"]');"
        "var blocker=null;"
        "if(document.querySelector('input[type=\"password\"]')||/\\/auth|login/i.test(location.pathname))blocker='login';"
        "else if(document.querySelector('iframe[src*=\"captcha\" i],iframe[title*=\"captcha\" i],[id*=\"challenge\"]'))blocker='captcha';"
        "else if(/rate limit|too many requests|usage limit/i.test((document.body.innerText||'').slice(0,4000)))blocker='rate_limit';"
        "var done=(res.done&&a.length>0);"
        "return JSON.stringify({generating:stop,done:done,blocker:blocker,assistantCount:a.length,len:res.len});})()"
    )
    preflight_js = (
        "(function(){"
        "var composer=!!document.querySelector('#prompt-textarea,[contenteditable=\"true\"],textarea');"
        "var loginLike=!!document.querySelector('input[type=\"password\"]')||/log[- ]?in|auth/i.test(location.pathname);"
        "var captchaLike=!!document.querySelector('iframe[src*=\"captcha\" i],iframe[title*=\"captcha\" i],[id*=\"challenge\"]');"
        "var isChatGPT=/(^|\\.)chatgpt\\.com$/.test(location.hostname);"
        "return JSON.stringify({isChatGPT:isChatGPT,composer:composer,loginLike:loginLike,captchaLike:captchaLike});})()"
    )
    state = {
        "request_id": rid,
        "prompt_file": prompt_file,
        "logical_sha": logical_sha,
    }
    # poll_js / preflight_js / the window script / the ScheduleWakeup wake plan exist ONLY for the
    # MCP fallback (Backend B), which pastes them into javascript_tool and paces its own wakes. On
    # the default CDP+daemon path nothing reads them — cdp_consult.py rebuilds the sentinels from
    # the rid — so printing them dumped ~2.5k chars of dead JS straight into the agent's context on
    # every single consult. The caller that needs them asks for them.
    if a.backend == "mcp":
        state.update({
            "window_js_file": window_js_file,
            "target_chars": a.target_chars,
            "hard_chars": a.hard_chars,
            "sentinel_begin": f"BEGIN_RESPONSE:{rid}",
            "sentinel_end": end,
            "poll_js": poll_js,
            "preflight_js": preflight_js,
            "expect_minutes": a.expect_minutes,
            "first_wake_seconds": first_wake,
            "repoll_seconds": repoll,
            "max_polls": max_polls,
        })
    if a.backend != "mcp" and prompt_file:
        spool_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cgc_spool.py")
        argv = ["python3", spool_py, "enqueue", "--rid", rid, "--prompt-file", prompt_file]
        sys.stderr.write(
            "CGC_NEXT enqueue it (LOCAL write; the user's daemon validates + sends):\n"
            f"  {' '.join(shlex.quote(x) for x in argv)}\n")
    return state



def cmd_fire(a: argparse.Namespace) -> int:
    """deliver -> prep -> enqueue, in one process.

    The agent makes no decision between those three stages: deliver's refs_file feeds prep, prep's
    rid and prompt_file feed enqueue, and nothing in between is a judgement call. Splitting them
    across three Bash calls made the model copy implementation paths from one JSON blob to the next
    — pure cost, plus a chance to relay the wrong rid. The stages stay available on their own for
    debugging and for anyone who wants to edit the refs or the prompt in between."""
    import cgc_spool as spool

    # A follow-up continues a thread — but it no longer needs a hand-tracked conversation id. A bare
    # `--followup` (or `--conversation auto`/`last`) defaults to the LAST completed consult's thread,
    # which enqueue resolves from the store while the intent is fresh. This is the whole point of
    # follow-up ergonomics: the store remembers which thread you last got an answer from, so the
    # cheap path (continue the thread ChatGPT already has context for) is also the zero-effort path.
    # It stays FAIL-CLOSED: if nothing resolves, enqueue refuses — it never opens a fresh conversation
    # and silently loses the thread's context. Pass an explicit --conversation to target a specific
    # older thread (e.g. when other consults ran in between).
    if a.followup and not a.conversation:
        a.conversation = "auto"

    if not a.no_code:
        d = cmd_deliver(a)
        if not isinstance(d, dict) or not d.get("refs_file"):
            note = (d or {}).get("note") or "deliver produced no refs"
            sys.stderr.write(f"CGC_ERROR fire_no_refs: {note}\n")
            return 2
        a.refs_file = d["refs_file"]
    else:
        a.refs_file = None

    st = cmd_prep(a)
    if not isinstance(st, dict) or not st.get("prompt_file"):
        return 2
    rid, prompt_file = st["request_id"], st["prompt_file"]

    eq = argparse.Namespace(
        rid=rid, prompt_file=prompt_file,
        kind="followup" if a.followup else "submit",
        project_url=a.project_url, conversation=(a.conversation or "auto"),
        parent=getattr(a, "parent", None), model=a.model,
        request_key=getattr(a, "request_key", None),
        logical_sha=st.get("logical_sha"),
        out=a.out, poll=spool.POLL_S, timeout=spool.STUCK_AFTER_S, quiet=True)
    if spool.cmd_enqueue(eq) != 0:
        return 2
    out = os.path.abspath(a.out or os.path.join(spool.CGC_STATE_DIR, f"answer_{rid}.txt"))
    spool_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cgc_spool.py")
    await_argv = ["python3", spool_py, "await", "--rid", rid, "--out", out]
    # argv is the canonical receipt; the string is only a copyable display (shell-quoted so a path
    # with a space or a shell metacharacter can't break or change the parsed command).
    return {"rid": rid, "out": out, "await_argv": await_argv,
            "await": " ".join(shlex.quote(x) for x in await_argv)}


def _add_prep_args(pp):
    pp.add_argument("--backend", choices=["cdp", "mcp"], default="cdp",
                    help="cdp (default): skip the DOM-window script (unused on CDP). "
                         "mcp: also render retrieval_window.js for the MCP fallback.")
    pp.add_argument("--window-template", help="path to retrieval_window.js (only needed for --backend mcp)")
    pp.add_argument("--task", required=True, help="the question / what ChatGPT should plan or review")
    pp.add_argument("--title", help="headline shown ABOVE the role (frames the consult; a sharp, "
                                    "specific one primes depth). Default 'Engineering consult'.")
    pp.add_argument("--role", help="the expert persona for this consult — match it to the job, any "
                                   "domain (e.g. 'You are a distributed-systems security auditor', "
                                   "'You are a research analyst', 'You are a staff API designer'). "
                                   "Default: a senior expert in the task's domain.")
    pp.add_argument("--followup", action="store_true",
                    help="render a FOLLOW-UP prompt for an existing conversation (continues the same "
                         "thread; pair with `cdp_consult.py followup --conversation <id>`). Use "
                         "--context-file for the local results you're feeding back, --refs-file for "
                         "any new/changed source. Skips role/criteria/output (set in round 1).")
    pp.add_argument("--no-code", action="store_true",
                    help="this consult is not about code, so no --refs-file is required: a maths "
                         "proof, a research or writing question, a self-contained analysis. ONLY "
                         "for questions with no code subject at all — anything about a repo, a "
                         "diff, or a design in code MUST still ship the link (that rule exists "
                         "because prose about code gets a blind answer, and --no-code is not an "
                         "escape from it).")
    pp.add_argument("--context-file", help="optional context markdown to embed inline (on --followup, "
                                           "this is the local-results bundle fed back to the thread)")
    pp.add_argument("--refs-file", help="optional refs markdown (from `deliver`) — GitHub/PR "
                                        "links ChatGPT is told to open with its browser")
    pp.add_argument("--output-file", help="optional scenario-specific output-requirements markdown. "
                                          "By default PREPENDED above the standard findings contract; "
                                          "pass --output-replace if your spec defines its own findings shape.")
    pp.add_argument("--output-replace", action="store_true",
                    help="with --output-file: REPLACE the default findings contract with your spec entirely "
                         "(use when your spec defines its own findings shape/severity scale, so the two "
                         "don't conflict). Keeps only a short universal close. No effect without --output-file.")
    pp.add_argument("--target-chars", type=int, default=32000)
    pp.add_argument("--hard-chars", type=int, default=40000)
    pp.add_argument("--expect-minutes", type=int, default=25,
                    help="expected Pro latency; first wake lands at ~85%% of it. Default 25 — a "
                         "GPT-5.6 Pro consult reasons for a long time (multi-angle, proof-style). "
                         "Raise it further (40+) for a genuinely huge review to cut wake count.")
    return pp


def _add_deliver_args(pd):
    pd.add_argument("--repo-dir", default=".", help="path inside the local git repo (for auto-detect)")
    pd.add_argument("--repo", help="explicit target repo 'owner/repo' (different from the local origin)")
    pd.add_argument("--pr", help="explicit PR number → /pull/N + /pull/N/files")
    pd.add_argument("--ref", help="branch/tag/SHA for tree+blob links, e.g. 'main' or a commit SHA")
    pd.add_argument("--compare", help="explicit compare range 'BASE...HEAD' (refs or SHAs)")
    pd.add_argument("--pulls", action="store_true", help="include the open-PR list (/pulls) for triage")
    pd.add_argument("--blobs", action="store_true",
                    help="with --pr: ALSO emit per-file blob permalinks (only for a huge PR whose "
                         "aggregate diff won't render; default is just the PR link, which is enough)")
    pd.add_argument("--files", nargs="*", help="repo-relative paths for close-reading blob links (need --ref for a non-local repo)")
    pd.add_argument("--issues", nargs="*", help="issue numbers to add to intent refs")
    pd.add_argument("--base", help="base ref for AUTO compare (default: merge-base with origin HEAD)")
    pd.add_argument("--verify", action="store_true",
                    help="check repo visibility and enrich via `gh` (NETWORK). Off by default: the "
                         "agent must make no external call, and the daemon's egress gate re-checks "
                         "provenance authoritatively before anything is sent. Use it when driving "
                         "deliver by hand.")
    pd.add_argument("--allow-nonpublic", action="store_true",
                    help="explicitly allow delivering refs when the repo is PRIVATE or visibility "
                         "could not be confirmed (gh missing/unauthenticated/errored). Without this, "
                         "deliver REFUSES to write a refs file in those cases (fail-closed) — it will "
                         "only ever stamp PUBLIC when gh confirms visibility=='public'. With this flag "
                         "set, the refs file is written with a 'NON-PUBLIC (explicitly allowed)' stamp "
                         "instead of the PUBLIC stamp.")
    return pd


def main() -> int:
    p = argparse.ArgumentParser(prog="consult.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    # `fire` is the normal path: one call instead of three, because the agent makes no decision
    # between deliver, prep and enqueue. The individual verbs stay for debugging and for editing
    # the refs or the prompt in between.
    pf = sub.add_parser("fire", help="deliver + prep + enqueue in ONE call (the normal path)")
    _add_deliver_args(pf)
    _add_prep_args(pf)
    pf.add_argument("--model", default=os.environ.get("CGC_MODEL", "Pro"))
    pf.add_argument("--out", help="answer file (default $CGC_STATE_DIR/answer_<rid>.txt)")
    pf.add_argument("--project-url", default=os.environ.get("CGC_PROJECT_URL", "https://chatgpt.com/"))
    pf.add_argument("--conversation",
                    help="with --followup, the thread to continue; omit (or 'last'/'auto') to "
                         "continue the LAST completed consult automatically — no id to track. Pass an "
                         "explicit /c/<id> only to target a specific older thread.")
    pf.add_argument("--parent",
                    help="with --followup, the rid of the consult being continued — resolves to ITS "
                         "conversation (causal, unambiguous when other consults ran in between). "
                         "Prefer this over bare --followup whenever you ran interleaved consults.")
    pf.add_argument("--request-key",
                    help="caller-chosen logical-request id: re-firing the SAME key with the same "
                         "content returns the original receipt instead of queuing a duplicate; the "
                         "same key with different content is refused. Use it whenever a retry after "
                         "lost output must not double-send.")
    pf.set_defaults(fn=cmd_fire)

    pp = _add_prep_args(sub.add_parser("prep"))
    pp.set_defaults(fn=cmd_prep)
    pd = _add_deliver_args(sub.add_parser(
        "deliver", help="build purpose-grouped GitHub refs (agent-driven)"))
    pd.set_defaults(fn=cmd_deliver)

    a = p.parse_args()
    r = a.fn(a)
    if isinstance(r, dict):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    return r


if __name__ == "__main__":
    raise SystemExit(main())
