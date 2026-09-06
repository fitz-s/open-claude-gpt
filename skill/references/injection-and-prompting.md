<!-- Source: ChatGPT Pro consult REQ-20260612-082838, 2026-06-12. -->

> **AUTHORITY (read first).** This doc predates `consult.py`'s `deliver` (which now automates scenario detection + ref selection) and the adoption of the official GPT-6 Astra prompting guide (`references/gpt-6-astra-prompting-principles.md`). So:
> - The live `PROMPT_TEMPLATE` in `scripts/consult.py` is the **authoritative prompt** — it overrides anything here.
> - **Live, still-useful value:** §2 (URL-type catalog) and §9 (high-signal file selection). Use these.
> - **Superseded / historical:** §1 "Core Contract", §3 "Deliver-Step Decision Procedure", §10 "Master Template", §12 "Implementation Checklist" — these describe the manual, pre-`deliver` workflow and a second prompt skeleton that no longer matches the tooling. Don't compose prompts from them.
> - **§4 scenario templates** are pre-outcome-first *legacy* (process-heavy, ALWAYS/NEVER, heading-per-finding). Mine them only as raw material for an `--output-file` contract; the live template owns the actual prompt structure.

chatgpt-consult Information-Injection and Prompting Layer

This section defines how Claude Code should package repository context for ChatGPT consults. Claude Code remains the local executor. ChatGPT is a remote reviewer that can browse URLs, reason over code, and return advisory output that Claude Code verifies locally.

The design goal is to inject the smallest set of high-signal, browseable references that let ChatGPT answer the requested question without relying on malformed raw gists, missing local state, or memory.

1. Core Contract for Every Consult Prompt

Every consult prompt MUST include:

Markdown
You are an external consultant for a Claude Code session. Claude Code is the local executor: it reads/edits the repo and runs all tests locally. You do not run anything.

Do not claim to have run commands, opened local files, edited files, or executed tests. You MAY use your web browsing tool to open any GitHub / pull-request / raw URLs given below and read the actual code before answering. When URLs are provided, prefer reading them directly before forming conclusions. Keep every reference URL inline as a raw URL at the point it supports a claim.

Your job is to provide advisory analysis that Claude Code can verify locally.

## Task
{TASK}

## Source to review — open these with your browser tool before answering
{REFS}

## Local context from Claude Code
{CONTEXT}

## Output requirements
{OUTPUT_REQUIREMENTS}

Wrap your FINAL answer exactly between:
BEGIN_RESPONSE:{RID}
...
END_RESPONSE:{RID}

Do not ask ChatGPT to run tests, inspect the local filesystem, infer unstated local state, or modify code. Ask for review findings, reasoning, hypotheses, plans, patch sketches, and local verification steps.

2. Information-Injection Catalog
2.1 Repository root
What it is

The top-level GitHub repository page.

URL shape
https://github.com/<owner>/<repo>

Example placeholder:

https://github.com/OWNER/REPO
Right choice when

Use this as supporting context when the task involves broad repository orientation, project layout, README-level architecture, language/framework identification, or locating likely subsystems.

Repository root alone is rarely sufficient for code review. Pair it with PR, tree, blob, compare, or issue URLs.

Prompt phrasing
Markdown
Start by opening the repository root to orient yourself to project structure and documentation:
https://github.com/OWNER/REPO

Then use the more specific URLs below for the actual code review. Do not rely only on README-level information if code URLs are also provided.
Failure modes

Root pages do not expose enough source detail for deep review.

Default branch may not match the relevant branch.

Private repositories may be inaccessible to ChatGPT.

GitHub web UI may hide generated files or large files.

2.2 Whole open-PR list
What it is

The repository’s list of open pull requests.

URL shape
https://github.com/<owner>/<repo>/pulls

For filtered lists:

https://github.com/<owner>/<repo>/pulls?q=is%3Apr+is%3Aopen+author%3A<user>
https://github.com/<owner>/<repo>/pulls?q=is%3Apr+is%3Aopen+head%3A<branch>
https://github.com/<owner>/<repo>/pulls?q=is%3Apr+is%3Aopen+base%3A<base-branch>
Right choice when

Use /pulls when Claude Code does not know which PR is relevant, when the branch may have multiple candidate PRs, when asking ChatGPT to triage open work, or when the consult task is “which PR should we inspect?”

This is especially useful for high-level repository state questions:

“Find the relevant open PR for branch feature/foo.”

“Review the riskiest currently open PR.”

“Triage open PRs touching auth, payments, migrations, or public APIs.”

“Compare the current branch against open PRs and identify likely overlap.”

Prompt phrasing
Markdown
First open the open-PR list and enumerate candidate PRs relevant to this task:
https://github.com/OWNER/REPO/pulls

Triage the list by title, branch, author, labels, and visible metadata. Select the most relevant PR(s), then open each selected PR page and its `/files` tab before giving findings. If the list is inaccessible or does not show enough information, say that explicitly and proceed only from the other provided references.

For branch-specific discovery:

Markdown
Open the open-PR list and look for an open PR whose head branch appears to match `{BRANCH}`:
https://github.com/OWNER/REPO/pulls?q=is%3Apr+is%3Aopen+head%3A{BRANCH}

If found, use that PR and its files diff as the primary review source. If not found, fall back to the commit/tree/blob references below.
Failure modes

/pulls is a discovery page, not a stable source of complete code.

Sorting/filtering may hide relevant PRs.

Private repositories require ChatGPT session access.

If many PRs exist, ChatGPT may skim unless instructed to enumerate and select.

Search query encoding is fragile; include unfiltered /pulls as backup when uncertain.

2.3 Single pull request overview
What it is

The PR conversation/overview page. It contains title, description, commits, status checks, linked issues, review comments, and high-level intent.

URL shape
https://github.com/<owner>/<repo>/pull/<number>

Example:

https://github.com/OWNER/REPO/pull/123
Right choice when

Use this for almost every PR-related consult. It gives intent and review context that the raw diff cannot provide.

Always include this for:

Merge-gate review.

Design dispute on a PR.

Regression diagnosis after a PR.

Reviewing whether implementation matches stated intent.

Understanding reviewer concerns or CI status.

Pair it with /pull/N/files.

Prompt phrasing
Markdown
Open the PR overview first to understand the stated intent, linked issues, review discussion, and visible CI/review context:
https://github.com/OWNER/REPO/pull/123

Then open the files diff:
https://github.com/OWNER/REPO/pull/123/files

Use the PR description and discussion to judge whether the implementation actually satisfies the stated goal.
Failure modes

Conversation may be long; ChatGPT can miss buried review comments.

Some comments may be hidden, outdated, collapsed, or unavailable.

The overview does not show all changed code clearly.

PR may have changed since Claude Code prepared local context; include head SHA in context.

2.4 Pull request files diff
What it is

The GitHub web diff for all files changed in a PR.

URL shape
https://github.com/<owner>/<repo>/pull/<number>/files

Example:

https://github.com/OWNER/REPO/pull/123/files
Right choice when

Use this as the primary source for PR review. It is better than a gist for understanding changed files, comments, and file-level context.

Always include this when an open PR exists and the consult concerns changed code.

Prompt phrasing
Markdown
Open and read the PR files diff carefully:
https://github.com/OWNER/REPO/pull/123/files

Review file-by-file. Prioritize correctness, security, data loss, migration safety, concurrency, API compatibility, test coverage, and maintainability. For every serious finding, cite the exact file path and the closest visible line or diff hunk. If GitHub hides a large diff, say which file appears hidden and request/consult the blob or raw URL if provided.

For long diffs:

Markdown
The diff may be long. Do not summarize after only reading the first files. First build an inventory of changed files from:
https://github.com/OWNER/REPO/pull/123/files

Then inspect the highest-risk files in this order:
1. Public API or schema changes
2. Auth/security/payment/data-loss paths
3. Core business logic
4. Concurrency/caching/persistence
5. Tests
6. Low-risk mechanical changes

Return the changed-file inventory and identify any files you could not inspect.
Failure modes

GitHub may collapse or omit large generated diffs.

Renames can make context harder to follow.

Diff view may lack surrounding code needed for correctness.

Some comments/check annotations may not load.

For very large PRs, ChatGPT may need explicit triage instructions.

2.5 Commit page
What it is

A single commit diff and metadata page.

URL shape
https://github.com/<owner>/<repo>/commit/<sha>

Example:

https://github.com/OWNER/REPO/commit/0123456789abcdef0123456789abcdef01234567
Right choice when

Use commit pages when there is no PR, when reviewing a specific commit, when bisecting a suspected regression, or when HEAD is pushed but no PR exists.

Good for:

“Review this pushed commit before opening a PR.”

“This commit introduced a failure; inspect likely cause.”

“Compare intent in commit message to implementation.”

Prompt phrasing
Markdown
Open this commit page and review the diff and commit message:
https://github.com/OWNER/REPO/commit/{SHA}

Treat it as the primary change under review. If additional tree/blob URLs are provided, use them for surrounding context because the commit diff may not show enough unchanged code.
Failure modes

One commit may not represent the full branch.

Commit diff can omit context needed for multi-commit feature work.

Force-pushes do not affect commit-pinned URLs, but the commit may no longer be branch HEAD.

Private commits are inaccessible without GitHub access.

2.6 Tree at a ref
What it is

A repository file tree at a specific branch, tag, or commit SHA.

URL shape
https://github.com/<owner>/<repo>/tree/<ref>
https://github.com/<owner>/<repo>/tree/<sha>
https://github.com/<owner>/<repo>/tree/<sha>/<path>

Examples:

https://github.com/OWNER/REPO/tree/main
https://github.com/OWNER/REPO/tree/0123456789abcdef0123456789abcdef01234567
https://github.com/OWNER/REPO/tree/0123456789abcdef0123456789abcdef01234567/src/auth
Right choice when

Use a commit-pinned tree for subsystem review, architecture consults, broad investigation, and non-PR branch review when the branch is pushed.

A tree URL is useful for navigation, not sufficient by itself for line-level review. Pair it with key blob permalinks.

Prefer commit SHA refs over branch names for reproducibility.

Prompt phrasing
Markdown
Use this commit-pinned tree as the repository snapshot for navigation:
https://github.com/OWNER/REPO/tree/{SHA}

Focus on this subsystem path:
https://github.com/OWNER/REPO/tree/{SHA}/src/auth

When you need exact code, open the blob permalinks listed below rather than assuming from filenames.
Failure modes

Tree pages do not show full file contents.

Branch refs move; SHA refs are stable.

GitHub web navigation may make it hard to inspect many files.

Private repositories may be inaccessible.

Very large directories need a preselected file list from Claude Code.

2.7 Blob permalink
What it is

A stable GitHub source file URL pinned to a commit SHA.

URL shape
https://github.com/<owner>/<repo>/blob/<sha>/<path>

Example:

https://github.com/OWNER/REPO/blob/0123456789abcdef0123456789abcdef01234567/src/auth/session.ts
Right choice when

Use blob permalinks for close source reading. They are the preferred format for files ChatGPT must inspect carefully.

Always use commit-pinned blob URLs for:

Critical files in a subsystem review.

Files mentioned in a bug report or stack trace.

Files changed in a PR when the diff lacks enough context.

Public API definitions.

Tests that define expected behavior.

Migration/schema/config files.

Security-sensitive code.

Prompt phrasing
Markdown
Open these commit-pinned blob URLs for close reading. Treat them as the authoritative source snapshot:

- https://github.com/OWNER/REPO/blob/{SHA}/src/auth/session.ts
- https://github.com/OWNER/REPO/blob/{SHA}/src/auth/session.test.ts
- https://github.com/OWNER/REPO/blob/{SHA}/src/db/schema.ts

When making a finding, cite the relevant raw URL inline and include file path plus function/class/test name. If exact line numbers are visible, include them; otherwise cite the nearest symbol or diff hunk.
Failure modes

Blob pages can be slow or partially rendered for very large files.

Line anchors may shift if branch refs are used; use SHA permalinks.

Generated/minified files are poor review targets.

ChatGPT may not open every blob unless the prompt explicitly says which are primary.

2.8 Blob permalink with line anchors
What it is

A GitHub file URL pinned to a specific line or range.

URL shape
https://github.com/<owner>/<repo>/blob/<sha>/<path>#L<line>
https://github.com/<owner>/<repo>/blob/<sha>/<path>#L<start>-L<end>

Examples:

https://github.com/OWNER/REPO/blob/{SHA}/src/auth/session.ts#L42
https://github.com/OWNER/REPO/blob/{SHA}/src/auth/session.ts#L42-L88
Right choice when

Use line anchors when Claude Code already knows the suspicious location, stack trace line, failing assertion, or disputed implementation.

Prompt phrasing
Markdown
Start with the anchored region below, then inspect surrounding code in the same file as needed:
https://github.com/OWNER/REPO/blob/{SHA}/src/auth/session.ts#L42-L88

The anchored lines are suspected to be relevant to `{BUG_OR_DESIGN_QUESTION}`. Confirm or refute that hypothesis and identify any surrounding code that changes the conclusion.
Failure modes

Anchors may be wrong if not pinned to a SHA.

GitHub line numbers can be unavailable if rendering fails.

The bug may be caused by callers/callees outside the anchored range.

2.9 Compare view
What it is

A GitHub comparison between two refs, branches, tags, or SHAs.

URL shape
https://github.com/<owner>/<repo>/compare/<base>...<head>
https://github.com/<owner>/<repo>/compare/<base-sha>...<head-sha>

Examples:

https://github.com/OWNER/REPO/compare/main...feature/auth-refresh
https://github.com/OWNER/REPO/compare/{BASE_SHA}...{HEAD_SHA}
Right choice when

Use compare views when no PR exists but the branch is pushed, or when the consult is about the full delta between two refs.

Good for:

Pre-PR review.

Comparing a feature branch to main.

Reviewing release branch deltas.

Understanding divergence or regression range.

“What changed between known-good and known-bad?”

Prefer SHA-to-SHA compare for stable consults.

Prompt phrasing
Markdown
Open this compare view and treat it as the complete change set under review:
https://github.com/OWNER/REPO/compare/{BASE_SHA}...{HEAD_SHA}

First inventory changed files and commits. Then review the highest-risk code paths. Use the tree/blob URLs below for surrounding context where the compare diff is insufficient.
Failure modes

Compare direction matters. base...head means changes in head relative to base.

Branch names move; use SHA compare when possible.

Large diffs may be truncated.

Compare pages lack PR discussion and CI context.

Fork comparisons may require different URL shape:

https://github.com/<base-owner>/<base-repo>/compare/<base-ref>...<head-owner>:<head-ref>
2.10 Issue
What it is

A GitHub issue page with bug reports, requirements, reproduction steps, discussion, labels, and linked PRs.

URL shape
https://github.com/<owner>/<repo>/issues/<number>

Example:

https://github.com/OWNER/REPO/issues/456
Right choice when

Use issues when the task is requirement interpretation, bug diagnosis, acceptance-criteria review, design dispute, or “does this PR solve the reported problem?”

Always pair relevant issues with PR or code URLs.

Prompt phrasing
Markdown
Open the issue first to understand the reported problem, reproduction details, acceptance criteria, and discussion:
https://github.com/OWNER/REPO/issues/456

Then inspect the implementation references below. Judge whether the proposed code actually addresses the issue, and call out any missing edge cases from the issue discussion.
Failure modes

Issue discussion can be stale or contradicted by later comments.

Linked PRs may not be visible without manual inspection.

Issue labels are hints, not ground truth.

Private issues are inaccessible without GitHub access.

2.11 GitHub raw file URL
What it is

A direct raw text file from GitHub.

URL shapes
https://raw.githubusercontent.com/<owner>/<repo>/<sha>/<path>
https://github.com/<owner>/<repo>/raw/<sha>/<path>

Example:

https://raw.githubusercontent.com/OWNER/REPO/{SHA}/src/auth/session.ts
Right choice when

Use raw GitHub URLs only for plain text files that need exact content and are known to render cleanly. Prefer GitHub blob URLs for browseability, navigation, line anchors, and reviewer ergonomics.

Raw GitHub URLs are useful for:

Small config files.

Lockfile excerpts when exact text matters.

Generated artifacts where GitHub blob rendering is poor.

Machine-readable files that ChatGPT should quote or parse.

Prompt phrasing
Markdown
Use this raw GitHub URL only if the blob view is insufficient:
https://raw.githubusercontent.com/OWNER/REPO/{SHA}/src/config/example.yml

If the raw view appears malformed, collapsed, truncated, or hard to read, say so and rely on the blob URL instead:
https://github.com/OWNER/REPO/blob/{SHA}/src/config/example.yml
Failure modes

Raw text can lose visual context.

Some browsing tools render raw content poorly.

Raw URLs lack GitHub navigation and line anchors.

For gist raw URLs specifically, newlines may collapse; avoid gist raw as a primary source for close code review.

2.12 Secret gist
What it is

A GitHub gist containing local files or generated excerpts when the relevant code is not available in a pushed repository, PR, compare, or blob URL.

URL shapes

Gist page:

https://gist.github.com/<user>/<gist-id>

Gist raw:

https://gist.githubusercontent.com/<user>/<gist-id>/raw/<revision>/<filename>
Right choice when

Use gist only as a fallback when the relevant local state is not available on browseable GitHub URLs.

Good uses:

Unpushed local changes.

Private repository where ChatGPT lacks GitHub access.

Generated local artifacts not committed.

Small curated bundles of key files.

Test output, stack traces, logs, and local diffs.

Prefer the gist page over gist raw for readability when possible. Avoid relying on gist raw for code because newlines may collapse in browsing.

Prompt phrasing
Markdown
The relevant local state is not available as a pushed GitHub PR/tree/blob. Use this gist page as a fallback source:
https://gist.github.com/USER/GIST_ID

Important: prefer the gist page over raw gist URLs. Raw gist rendering may collapse newlines and make code appear malformed. If formatting looks broken, say so and avoid drawing line-sensitive conclusions from the raw rendering.

For local diff gist:

Markdown
This gist contains a local unpushed diff and selected files:
https://gist.github.com/USER/GIST_ID

Treat the diff as advisory input from Claude Code. Identify findings by file path and hunk/function name rather than relying on exact line numbers unless they are clearly visible.
Failure modes

Raw gist newlines may collapse.

Secret gists are not access-controlled; anyone with the URL may access them.

Gists can become stale relative to local working tree.

Gists lack PR discussion, review comments, CI, branch context, and repository navigation.

Large gists are hard to review comprehensively.

File ordering in gists may hide important context.

2.13 Inline snippet
What it is

Small code, logs, stack traces, diffs, or command output pasted directly into the prompt.

Right choice when

Use inline text only for small, high-signal material:

Error messages.

Stack traces.

Failing test names.

A single function or short class.

A short diff hunk.

Local command output summaries.

Constraints not visible in GitHub.

Keep inline snippets short enough that ChatGPT sees them directly in the prompt. Large inline pastes may be converted to attachments and become unreadable.

Prompt phrasing
Markdown
The following small inline snippet is authoritative local context from Claude Code. Use it together with the URLs above.

```text
{SNIPPET}

Do not assume this is the whole file unless explicitly stated. If the snippet conflicts with the GitHub URL, note the conflict and prefer the local snippet for current local state.


### Failure modes

- Large inline snippets may become attachments.
- Snippets omit surrounding context.
- Formatting may be damaged by Markdown fences if nested incorrectly.
- ChatGPT may overweight snippets unless told they are partial.

---

## 2.14 User-uploaded file

### What it is

A file uploaded into the ChatGPT session, such as a patch, archive, log, design doc, or generated report.

### Right choice when

Use uploaded files only when the ChatGPT integration can reliably attach files and the session can read them. This is a secondary fallback after GitHub PR/tree/blob URLs and curated inline snippets.

Useful uploads:

- Large logs.
- Test reports.
- Coverage reports.
- Architecture documents.
- Bundled local patches.
- Generated dependency reports.
- Large but structured JSON.

### Prompt phrasing

```md
A file has been uploaded with this prompt: `{FILENAME}`.

Before answering, inspect the uploaded file and state whether you were able to read it. Use it as supporting context together with the GitHub URLs. If you cannot read the file, say so explicitly and answer only from the visible URLs and inline context.
Failure modes

Uploaded files may not be readable by the ChatGPT browsing/extraction path.

Large uploads can be partially indexed or inaccessible.

Archives require extraction support, which may not exist in the web session.

File references are not URLs and are harder for Claude Code to audit after the consult.

Do not use uploads as the primary source when GitHub URLs are sufficient.

2.15 GitHub search URL
What it is

A GitHub repository search page for code, issues, or PRs.

URL shapes

Code search:

https://github.com/<owner>/<repo>/search?q=<query>&type=code

Issues/PR search:

https://github.com/<owner>/<repo>/issues?q=<query>
https://github.com/<owner>/<repo>/pulls?q=<query>

Examples:

https://github.com/OWNER/REPO/search?q=SessionManager&type=code
https://github.com/OWNER/REPO/issues?q=is%3Aissue+is%3Aopen+auth
https://github.com/OWNER/REPO/pulls?q=is%3Apr+is%3Aopen+auth
Right choice when

Use search URLs as discovery aids when the skill can identify symbols, failing test names, error strings, or subsystem terms but does not know all relevant files.

Prompt phrasing
Markdown
Use this GitHub search URL as a discovery aid for related files:
https://github.com/OWNER/REPO/search?q=SessionManager&type=code

Do not treat search results as complete. Use them to identify candidate files, then open the concrete blob URLs listed below or visible from the search results before making findings.
Failure modes

GitHub code search can omit results or require login.

Search result ranking may be poor.

Search pages are discovery, not authoritative source content.

Private repository search may fail.

2.16 Actions/checks URL
What it is

GitHub Actions runs, PR checks, or CI logs.

URL shapes

Repository Actions:

https://github.com/<owner>/<repo>/actions

Workflow run:

https://github.com/<owner>/<repo>/actions/runs/<run-id>

PR checks usually visible from:

https://github.com/<owner>/<repo>/pull/<number>
Right choice when

Use checks URLs when the consult is about failing CI, flaky tests, build failures, or whether a PR is merge-safe.

Prompt phrasing
Markdown
Open the PR overview and inspect visible check status:
https://github.com/OWNER/REPO/pull/123

If this workflow run is accessible, use it for failing job names and error context:
https://github.com/OWNER/REPO/actions/runs/RUN_ID

Do not claim to have rerun CI. Only reason from visible check/log information and the inline local output below.
Failure modes

Actions logs often require authentication.

Logs may expire or be truncated.

ChatGPT may not be able to expand individual failed steps.

Local Claude Code output is often more reliable; include concise failing output inline.

2.17 Release, tag, or branch URL
What it is

A stable or semi-stable release/tag/branch reference.

URL shapes

Release:

https://github.com/<owner>/<repo>/releases/tag/<tag>

Tag tree:

https://github.com/<owner>/<repo>/tree/<tag>

Branch tree:

https://github.com/<owner>/<repo>/tree/<branch>
Right choice when

Use this for release regression review, migration planning between versions, or API compatibility questions.

Prompt phrasing
Markdown
Use this release/tag as the old baseline:
https://github.com/OWNER/REPO/tree/v1.2.3

Use this commit-pinned tree as the proposed new state:
https://github.com/OWNER/REPO/tree/{SHA}

Focus on compatibility and migration risks between the two.
Failure modes

Branches move; tags are more stable, SHAs are most stable.

Release notes can omit implementation details.

Tags may not correspond to deployed code in all projects.

3. Deliver-Step Decision Procedure

The deliver step should choose the minimal sufficient reference set based on task type, repository accessibility, and whether local changes are pushed.

3.1 Delivery principles

Prefer existing GitHub context over generated gists.

Prefer high-level GitHub objects for intent and review context: PR, issue, compare.

Prefer commit-pinned blob URLs for close code reading.

Prefer SHA refs over branch refs for reproducibility.

Use gist only for unpushed local state, private-inaccessible state, or generated local artifacts.

Use inline text only for small snippets: failing output, stack traces, exact local constraints, short diffs.

Never send only /pulls for a deep review. /pulls is discovery; pair selected PR(s) with /pull/N and /pull/N/files.

Never send only a tree URL when exact code must be reviewed. Pair tree with key blobs.

Include enough task-specific orientation so ChatGPT knows what to open first and what to ignore.

3.2 Reference selection algorithm

Use this decision tree.

INPUTS:
- owner/repo
- current branch
- HEAD SHA
- base branch and base SHA, if known
- whether HEAD is pushed
- open PR(s) for current branch, if known
- issue URLs or numbers, if task mentions them
- changed files
- high-risk files
- failing tests/log snippets
- task scenario
- repository public/private/accessibility status, if known
Step 1: Classify the consult

Classify into one primary scenario:

PR_MERGE_GATE
DEEP_MODULE_REVIEW
PRE_REFACTOR_ARCHITECTURE
BROAD_INVESTIGATION_PLAN
DESIGN_DISPUTE
STUCK_BUG
MULTI_ROUND_LOOP
GENERAL_SECOND_OPINION

This classification controls the prompt template and source set.

Step 2: Determine GitHub accessibility
IF repository is known public:
    use GitHub URLs freely
ELSE IF repository is private but likely accessible to the logged-in ChatGPT browser session:
    still provide GitHub URLs, but include an explicit fallback note
ELSE:
    provide GitHub URLs for auditability if harmless, but prepare gist/inline fallback

Prompt note for private repos:

Markdown
Some URLs may require GitHub access. Try to open them. If they are inaccessible, say so explicitly and rely on the provided gist/inline context instead.
Step 3: If an open PR exists for the current branch

Inject:

Required:
- PR overview: https://github.com/<owner>/<repo>/pull/<N>
- PR files: https://github.com/<owner>/<repo>/pull/<N>/files

Strongly recommended:
- Issue(s) linked or mentioned by task: https://github.com/<owner>/<repo>/issues/<N>
- Commit-pinned head tree: https://github.com/<owner>/<repo>/tree/<HEAD_SHA>
- Commit-pinned blob URLs for the highest-risk changed files
- Inline failing test/log output if relevant

For merge gates:

Include both /pull/N and /pull/N/files.
Include top 5-20 high-risk changed blob URLs pinned to HEAD_SHA.
Include base/head SHA in context.
Include CI/test status summary from Claude Code if known.

For design disputes:

Include /pull/N, /pull/N/files, issue/design-doc URLs, and blob URLs for disputed files.

For stuck bugs:

Include /pull/N only if the bug is PR-related.
Always include stack trace/failing output inline and blob URLs for implicated files.
Step 4: If no open PR exists but HEAD is pushed

Inject:

Required:
- Compare: https://github.com/<owner>/<repo>/compare/<BASE_SHA>...<HEAD_SHA>
- Commit-pinned head tree: https://github.com/<owner>/<repo>/tree/<HEAD_SHA>

Recommended:
- Commit page(s) for relevant commits:
  https://github.com/<owner>/<repo>/commit/<SHA>
- Blob permalinks for key changed files:
  https://github.com/<owner>/<repo>/blob/<HEAD_SHA>/<path>
- Relevant issues:
  https://github.com/<owner>/<repo>/issues/<N>

If base SHA is unknown but base branch is known:

Use branch compare as weaker fallback:
https://github.com/<owner>/<repo>/compare/<base-branch>...<current-branch>

Also include HEAD SHA in context and warn that branch refs may move.

Prompt note:

Markdown
There is no open PR. Treat the compare view as the change set under review and the commit-pinned tree/blob URLs as the source snapshot.
Step 5: If local changes are unpushed

Inject:

Required:
- Gist page containing local diff and selected files:
  https://gist.github.com/<user>/<gist-id>

Also include if available:
- Repository root: https://github.com/<owner>/<repo>
- Base tree: https://github.com/<owner>/<repo>/tree/<BASE_SHA>
- Base blob URLs for unchanged surrounding files
- Inline summary of changed files
- Inline failing output or stack trace

Gist content should include:

- metadata.txt:
  - repo
  - base branch
  - base SHA
  - local branch
  - local HEAD SHA if available
  - dirty/untracked status summary
  - task scenario
- diff.patch:
  - git diff against base or HEAD
- selected files:
  - full contents of files changed or needed for context
- test-output.txt:
  - concise local failures, if relevant

Prompt note:

Markdown
The local state is unpushed, so GitHub may not show the current changes. Use the gist as the authoritative local delta, and use GitHub URLs only for baseline repository context.
Step 6: If repository is inaccessible from ChatGPT

Inject:

Required:
- Gist page with selected files/diffs
- Small inline task context
- Small inline stack traces/test output

Do not rely on private GitHub URLs as the only source.

Prompt note:

Markdown
The GitHub repository may be inaccessible from this ChatGPT session. Try the URLs if useful, but if access fails, use the gist/inline context and clearly state that the GitHub pages were inaccessible.
Step 7: Add scenario-specific context
For PR merge gate

Add:

- PR overview
- PR files
- linked issues
- head tree
- high-risk changed blobs
- local test summary
- known reviewer concerns
For deep module review

Add:

- head tree at subsystem path
- key module blob URLs
- public API entry points
- tests for subsystem
- README/design docs if present
- recent PR/compare only if reviewing changes
For pre-refactor architecture

Add:

- repo root
- tree at relevant subsystem paths
- key current implementation blobs
- issue/design doc if any
- constraints inline
- known pain points inline
For broad investigation

Add:

- repo root
- /pulls or filtered PR/issues search if investigation involves current work
- tree at HEAD
- search URLs for relevant symbols
- key blobs discovered by Claude Code
- inline symptoms/evidence
For design dispute

Add:

- PR/issue/design discussion URLs
- disputed files as blob URLs
- concise statement of positions A and B
- constraints and non-negotiables
For stuck bug

Add:

- stack trace inline
- failing test output inline
- reproduction steps inline
- implicated file blobs
- recent compare/PR/commit if regression suspected
For multi-round loop

Add:

- previous ChatGPT recommendation summary
- Claude Code actions taken
- test results after execution
- new errors/logs
- updated PR/compare/blob URLs
3.3 Minimal source sets by task
Open PR merge gate
Markdown
## Source to review
Primary PR context:
- https://github.com/OWNER/REPO/pull/123
- https://github.com/OWNER/REPO/pull/123/files

Stable source snapshot:
- https://github.com/OWNER/REPO/tree/{HEAD_SHA}

High-risk changed files:
- https://github.com/OWNER/REPO/blob/{HEAD_SHA}/path/to/risky-file-1.ts
- https://github.com/OWNER/REPO/blob/{HEAD_SHA}/path/to/risky-file-2.ts

Related requirements:
- https://github.com/OWNER/REPO/issues/456
Pushed branch, no PR
Markdown
## Source to review
Change set:
- https://github.com/OWNER/REPO/compare/{BASE_SHA}...{HEAD_SHA}

Stable source snapshot:
- https://github.com/OWNER/REPO/tree/{HEAD_SHA}

Key files:
- https://github.com/OWNER/REPO/blob/{HEAD_SHA}/path/to/file-1.ts
- https://github.com/OWNER/REPO/blob/{HEAD_SHA}/path/to/file-2.ts
Unpushed local work
Markdown
## Source to review
Local unpushed state:
- https://gist.github.com/USER/GIST_ID

Baseline repository context:
- https://github.com/OWNER/REPO/tree/{BASE_SHA}

Relevant baseline files:
- https://github.com/OWNER/REPO/blob/{BASE_SHA}/path/to/file-1.ts
Subsystem review
Markdown
## Source to review
Repository snapshot:
- https://github.com/OWNER/REPO/tree/{HEAD_SHA}

Subsystem tree:
- https://github.com/OWNER/REPO/tree/{HEAD_SHA}/src/subsystem

Entry points:
- https://github.com/OWNER/REPO/blob/{HEAD_SHA}/src/subsystem/index.ts
- https://github.com/OWNER/REPO/blob/{HEAD_SHA}/src/subsystem/service.ts

Tests:
- https://github.com/OWNER/REPO/blob/{HEAD_SHA}/src/subsystem/service.test.ts
Stuck bug
Markdown
## Source to review
Suspected implementation files:
- https://github.com/OWNER/REPO/blob/{HEAD_SHA}/src/foo/bar.ts#L120-L190
- https://github.com/OWNER/REPO/blob/{HEAD_SHA}/src/foo/bar.test.ts

Recent changes, if regression suspected:
- https://github.com/OWNER/REPO/compare/{KNOWN_GOOD_SHA}...{KNOWN_BAD_SHA}

Local failure output is pasted below in context.
4. Prompt Templates by Scenario

All templates below are intended to replace {TASK}, {REFS}, {CONTEXT}, and {OUTPUT_REQUIREMENTS} inside the core contract.

4.1 High-risk PR merge gate / adversarial review
Context to inject

Required:

- https://github.com/<owner>/<repo>/pull/<N>
- https://github.com/<owner>/<repo>/pull/<N>/files
- https://github.com/<owner>/<repo>/tree/<HEAD_SHA>
- linked issue/design docs, if any
- high-risk changed blob URLs pinned to HEAD_SHA
- local test/CI summary from Claude Code

Optional:

- specific reviewer concerns
- deployment constraints
- database migration notes
- API compatibility requirements
- security/data-loss concerns
Template
Markdown
## Task
Perform an adversarial merge-gate review of this PR. Assume we want to merge only if there are no credible BLOCKER or HIGH-severity issues. Focus on correctness, security, data loss, backwards compatibility, migration safety, concurrency/race conditions, performance cliffs, test adequacy, and maintainability risks that could cause production incidents.

## Source to review — open these with your browser tool before answering
Open the PR overview first:
https://github.com/OWNER/REPO/pull/PR_NUMBER

Then open and carefully review the full files diff:
https://github.com/OWNER/REPO/pull/PR_NUMBER/files

Use this commit-pinned tree as the stable source snapshot:
https://github.com/OWNER/REPO/tree/HEAD_SHA

Open these high-risk changed files for surrounding context:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/FILE1
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/FILE2

Related requirements/issues:
- https://github.com/OWNER/REPO/issues/ISSUE_NUMBER

## Local context from Claude Code
Base branch: BASE_BRANCH
Base SHA: BASE_SHA
Head branch: HEAD_BRANCH
Head SHA: HEAD_SHA

Changed files summary:
```text
CHANGED_FILES_SUMMARY

Local verification already performed by Claude Code:

TEST_OR_CI_SUMMARY

Known concerns:

KNOWN_CONCERNS_OR_NONE
Output requirements

Return an adversarial review with this structure:

Verdict

One of: BLOCK, MERGE WITH FIXES, MERGEABLE, INSUFFICIENT INFO.

Give a one-paragraph rationale.

Critical findings

List only BLOCKER and HIGH findings.

For each finding:

Severity: BLOCKER or HIGH

Category: correctness/security/data-loss/migration/API/concurrency/performance/test-gap/maintainability

Location: file path plus closest line/function/diff hunk

Evidence: include the raw GitHub URL inline, e.g. https://github.com/OWNER/REPO/pull/PR_NUMBER/files
 or https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH

Why it matters

Concrete fix direction

What Claude Code should verify locally

Medium and low findings

Include only actionable items. Avoid style-only comments unless they affect maintainability.

Test gaps

Identify missing tests or weak assertions that matter for merge safety.

Local verification checklist for Claude Code

Exact tests, type checks, migrations, manual scenarios, or targeted inspections Claude Code should run locally.

Do not claim you ran them.

Files inspected

List URLs you used.

List any important files or diffs you could not access or that appeared truncated.


---

## 4.2 Deep module/subsystem review

### Context to inject

Required:

```text
- repo root
- commit-pinned tree
- subsystem tree at SHA
- key entry-point blobs
- key implementation blobs
- tests for subsystem

Optional:

- recent incidents or bugs
- known pain points
- performance/security constraints
- relevant PRs/issues
Template
Markdown
## Task
Perform a deep design and implementation review of the `{SUBSYSTEM_NAME}` subsystem. This is not a PR diff review; evaluate the current architecture and code as a subsystem. Identify correctness risks, hidden coupling, weak invariants, missing tests, maintainability problems, and refactor opportunities.

## Source to review — open these with your browser tool before answering
Repository root for orientation:
https://github.com/OWNER/REPO

Stable repository snapshot:
https://github.com/OWNER/REPO/tree/HEAD_SHA

Subsystem tree:
https://github.com/OWNER/REPO/tree/HEAD_SHA/PATH/TO/SUBSYSTEM

Primary entry points:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/SUBSYSTEM/index.ts
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/SUBSYSTEM/service.ts

Core implementation files:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/SUBSYSTEM/file1.ts
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/SUBSYSTEM/file2.ts

Tests:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/SUBSYSTEM/file1.test.ts
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/SUBSYSTEM/integration.test.ts

Related issues/docs, if any:
- https://github.com/OWNER/REPO/issues/ISSUE_NUMBER

## Local context from Claude Code
Subsystem purpose:
```text
SUBSYSTEM_PURPOSE

Known pain points:

KNOWN_PAIN_POINTS

Constraints:

CONSTRAINTS
Output requirements

Return a subsystem review with this structure:

Executive summary

3-7 bullets on the subsystem’s current health and main risks.

Mental model

Explain the intended architecture, data flow, lifecycle, and key invariants as inferred from the code.

Explicitly mark assumptions.

Findings

Group by severity: BLOCKER, HIGH, MEDIUM, LOW.

For each:

Location: file path plus function/class/test name

Evidence URL inline

Risk

Recommended change

Local verification step

Missing or unclear invariants

State invariants the code appears to rely on but does not enforce or test.

Test strategy improvements

Specific tests to add or strengthen.

Refactor opportunities

Separate safe mechanical refactors from behavior-changing refactors.

Suggested next steps for Claude Code

Ordered, concrete, locally verifiable steps.

Files inspected and access gaps

List URLs inspected and any paths that need local follow-up.


---

## 4.3 Pre-large-refactor architecture consult

### Context to inject

Required:

```text
- repo root
- current tree at SHA
- subsystem trees
- current implementation blobs
- public API/call-site blobs
- tests
- constraints and goals inline

Optional:

- issue/design doc
- prior PRs
- performance/security/deployment constraints
Template
Markdown
## Task
Advise on a planned large refactor of `{AREA}` before Claude Code starts editing. The goal is to minimize risk, preserve behavior, and produce a staged implementation plan that Claude Code can execute and verify locally.

## Source to review — open these with your browser tool before answering
Repository root:
https://github.com/OWNER/REPO

Stable source snapshot:
https://github.com/OWNER/REPO/tree/HEAD_SHA

Current subsystem paths:
- https://github.com/OWNER/REPO/tree/HEAD_SHA/PATH/TO/AREA
- https://github.com/OWNER/REPO/tree/HEAD_SHA/PATH/TO/CALLERS

Current implementation:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/AREA/file1.ts
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/AREA/file2.ts

Public APIs and call sites:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/API.ts
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/CALLER.ts

Relevant tests:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/AREA/file1.test.ts
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/AREA/integration.test.ts

Related issue/design context:
- https://github.com/OWNER/REPO/issues/ISSUE_NUMBER

## Local context from Claude Code
Refactor goal:
```text
REFACTOR_GOAL

Non-goals:

NON_GOALS

Constraints:

CONSTRAINTS

Known risks:

KNOWN_RISKS
Output requirements

Return an architecture consult with this structure:

Recommended direction

State the preferred architecture and why.

Mention alternatives rejected and why.

Current-state assessment

Identify coupling, invariants, data flow, and risk points in the existing code.

Refactor plan

Break into small, reviewable phases.

Each phase must include:

Goal

Files likely touched

Expected behavior change, if any

Tests Claude Code should run/add

Rollback strategy

Safety rails

Characterization tests

Feature flags

Migration/deployment sequencing

Compatibility shims

Observability/logging, if relevant

Do-not-do list

Specific refactor moves that are likely to be risky or counterproductive.

Open questions

Questions Claude Code should answer locally before editing.

Verification checklist

Concrete local commands/scenarios to verify, phrased as things Claude Code should run, not as things you ran.


---

## 4.4 Broad investigation → research-backed plan

### Context to inject

Required:

```text
- repo root
- tree at SHA
- issue(s), if investigation starts from issue
- PR list or filtered PR list, if current repo activity matters
- search URLs for relevant terms
- key blobs already identified by Claude Code
- symptoms/evidence inline

Optional:

- external docs URLs, if the question depends on a library/framework behavior
- failing logs
- dependency versions
Template
Markdown
## Task
Investigate `{QUESTION_OR_PROBLEM}` and return a research-backed plan for Claude Code. The answer should distinguish evidence from hypotheses. Use the repository URLs below and, where relevant, browse authoritative external documentation for library/framework behavior. Do not rely on memory for version-sensitive claims.

## Source to review — open these with your browser tool before answering
Repository root:
https://github.com/OWNER/REPO

Stable source snapshot:
https://github.com/OWNER/REPO/tree/HEAD_SHA

Relevant open PRs discovery:
https://github.com/OWNER/REPO/pulls

Relevant issues:
- https://github.com/OWNER/REPO/issues/ISSUE_NUMBER

Repository search/discovery:
- https://github.com/OWNER/REPO/search?q=SEARCH_TERM&type=code
- https://github.com/OWNER/REPO/issues?q=SEARCH_TERM

Known relevant files:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/FILE1
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/FILE2

External references, if relevant:
- https://OFFICIAL-DOCS-URL

## Local context from Claude Code
Observed symptoms:
```text
SYMPTOMS

Known environment/dependency versions:

VERSIONS

Prior attempts:

PRIOR_ATTEMPTS
Output requirements

Return an investigation plan with this structure:

What I inspected

URLs opened/used.

Any URLs that were inaccessible or insufficient.

Evidence

Concrete observations from repo/issues/docs.

Include raw URLs inline.

Hypotheses

Rank likely explanations.

For each, state supporting evidence and what would falsify it.

Recommended plan

Ordered steps Claude Code should take locally.

Include expected observations at each step.

Targeted code areas

Files/functions/classes/tests to inspect or modify first.

External facts

Cite official docs URLs inline for any library/framework claims.

Stop conditions

Conditions under which Claude Code should stop and re-consult.


---

## 4.5 Design dispute adjudication

### Context to inject

Required:

```text
- PR or issue discussion URL
- design doc URL, if any
- disputed implementation blob URLs
- concise statement of positions A and B
- constraints inline

Optional:

- benchmark data
- production constraints
- compatibility requirements
- previous decision records
Template
Markdown
## Task
Adjudicate a design dispute about `{TOPIC}`. Evaluate the competing proposals against the repository’s actual code, stated requirements, and constraints. Return a recommendation with tradeoffs, not a compromise by default.

## Source to review — open these with your browser tool before answering
Discussion / PR / issue:
- https://github.com/OWNER/REPO/pull/PR_NUMBER
- https://github.com/OWNER/REPO/issues/ISSUE_NUMBER

Diff or implementation under discussion:
- https://github.com/OWNER/REPO/pull/PR_NUMBER/files
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/DISPUTED_FILE1
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/DISPUTED_FILE2

Relevant current architecture:
- https://github.com/OWNER/REPO/tree/HEAD_SHA/PATH/TO/SUBSYSTEM
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/API_OR_CALLER

## Local context from Claude Code
Proposal A:
```text
PROPOSAL_A

Proposal B:

PROPOSAL_B

Non-negotiable constraints:

CONSTRAINTS

Decision criteria:

DECISION_CRITERIA
Output requirements

Return an adjudication with this structure:

Recommendation

Choose A, B, hybrid, or neither.

Be explicit.

Reasoning

Evaluate correctness, complexity, maintainability, performance, compatibility, migration risk, and testability.

Tradeoff matrix

Rows: proposal A, proposal B, hybrid/alternative.

Columns: benefits, risks, implementation cost, operational risk, test burden.

Repository-specific evidence

Cite raw GitHub URLs inline for code or discussion evidence.

Implementation guidance

Concrete shape of the chosen approach.

Files likely affected.

Tests required.

Risks and mitigations

Include what Claude Code should verify locally.

What would change my recommendation

List missing facts or measurements that would alter the decision.


---

## 4.6 Stuck-bug second opinion

### Context to inject

Required:

```text
- exact error/stack trace inline
- failing test output inline
- reproduction steps inline
- suspected file blobs with line anchors when possible
- recent compare/commit/PR if regression suspected

Optional:

- prior debugging attempts
- known-good/known-bad SHAs
- dependency versions
- relevant issue
Template
Markdown
## Task
Provide a second opinion on a stuck bug. Claude Code has local execution and will verify. Your job is to reason from the visible code and failure evidence, rank likely root causes, and propose targeted checks/fixes.

## Source to review — open these with your browser tool before answering
Suspected implementation:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/SUSPECT_FILE1#LSTART-LEND
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/SUSPECT_FILE2

Tests:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/FAILING_TEST

Recent changes, if regression is suspected:
- https://github.com/OWNER/REPO/compare/KNOWN_GOOD_SHA...KNOWN_BAD_SHA
- https://github.com/OWNER/REPO/commit/SUSPECT_COMMIT_SHA

Related issue, if any:
- https://github.com/OWNER/REPO/issues/ISSUE_NUMBER

## Local context from Claude Code
Failure:
```text
ERROR_OR_STACK_TRACE

Reproduction:

REPRO_STEPS

What Claude Code already tried:

PRIOR_ATTEMPTS

Known-good / known-bad:

KNOWN_GOOD_BAD_INFO

Environment:

ENVIRONMENT
Output requirements

Return a bug analysis with this structure:

Most likely root cause

State the leading hypothesis and confidence: high/medium/low.

Ranked hypotheses

For each:

Why it fits the evidence

What evidence argues against it

Where in code to inspect

One local check to confirm/falsify it

Likely fix direction

Describe the minimal fix.

Mention files/functions likely involved.

Avoid over-broad refactors unless necessary.

Targeted local verification

Exact tests or scenarios Claude Code should run.

Additional assertions or logs to add temporarily, if useful.

Edge cases

Cases that should be covered after the fix.

Uncertainties

Missing facts that matter.

Any URLs/files that were inaccessible.


---

## 4.7 Multi-round plan → execute → feed-results-back loop

### Context to inject

Required each round:

```text
- prior ChatGPT response summary
- actions Claude Code took locally
- updated diff/PR/compare/blob URLs
- test results after action
- new failures or questions
Round 1 template: ask for plan
Markdown
## Task
Create a staged implementation plan for `{GOAL}`. Claude Code will execute locally and return results for follow-up review. Optimize for small steps, local verification, and early detection of wrong assumptions.

## Source to review — open these with your browser tool before answering
Repository snapshot:
https://github.com/OWNER/REPO/tree/HEAD_SHA

Relevant files:
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/FILE1
- https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/FILE2

Relevant PR/issue/design context:
- https://github.com/OWNER/REPO/issues/ISSUE_NUMBER

## Local context from Claude Code
Goal:
```text
GOAL

Constraints:

CONSTRAINTS

Current local status:

STATUS
Output requirements

Return:

Plan overview

Assumptions to verify before editing

Step-by-step execution plan

Each step should be independently testable.

Expected files touched

Verification after each step

Re-consult triggers

When Claude Code should come back for another opinion.


### Follow-up round template: feed results back

```md
## Task
Review the results of the previous plan execution and advise the next step. Do not restart from scratch unless the new evidence invalidates the prior plan.

## Source to review — open these with your browser tool before answering
Updated PR/diff:
- https://github.com/OWNER/REPO/pull/PR_NUMBER
- https://github.com/OWNER/REPO/pull/PR_NUMBER/files

Updated source snapshot:
- https://github.com/OWNER/REPO/tree/NEW_HEAD_SHA

Updated key files:
- https://github.com/OWNER/REPO/blob/NEW_HEAD_SHA/PATH/TO/FILE1
- https://github.com/OWNER/REPO/blob/NEW_HEAD_SHA/PATH/TO/FILE2

## Local context from Claude Code
Prior consultant recommendation:
```text
SUMMARY_OF_PRIOR_CHATGPT_RESPONSE

Actions Claude Code took:

ACTIONS_TAKEN

Local results:

TEST_RESULTS_AND_ERRORS

Current question:

CURRENT_BLOCKER_OR_DECISION
Output requirements

Return:

Assessment of current state

Did execution follow the intended plan?

Did new evidence change the plan?

Findings on updated diff

Severity-tagged, with file/function anchors and raw URLs inline.

Next recommended action

One primary next step.

Alternatives only if meaningfully different.

Local verification

What Claude Code should run/check next.

Whether to continue, stop, or re-consult


---

# 5. Browsing-Reliability Rules

## 5.1 Use explicit open/read instructions

Weak:

```md
Here are some links for context.

Strong:

Markdown
Open these URLs with your browser tool before answering. First read the PR overview, then the `/files` diff, then the commit-pinned blob URLs for surrounding context. Base your findings on the opened URLs, and state if any URL is inaccessible or appears truncated.
5.2 Tell ChatGPT what each URL is for

Weak:

Markdown
https://github.com/OWNER/REPO/pull/123
https://github.com/OWNER/REPO/pull/123/files
https://github.com/OWNER/REPO/tree/HEAD_SHA

Strong:

Markdown
Use these in order:

1. PR intent and discussion:
   https://github.com/OWNER/REPO/pull/123

2. Complete changed-file diff:
   https://github.com/OWNER/REPO/pull/123/files

3. Stable source snapshot for surrounding context:
   https://github.com/OWNER/REPO/tree/HEAD_SHA

4. High-risk files to inspect closely:
   https://github.com/OWNER/REPO/blob/HEAD_SHA/src/auth/session.ts
   https://github.com/OWNER/REPO/blob/HEAD_SHA/src/auth/session.test.ts
5.3 Require an inspected-source inventory

Add to output requirements:

Markdown
Include a `Sources inspected` section listing each URL you used and any URL that was inaccessible, truncated, collapsed, or insufficient.

This reduces silent failures where ChatGPT answers without reading the URLs.

5.4 Require changed-file inventory for long diffs

For large PRs or compare views:

Markdown
Before making findings, inventory the changed files from:
https://github.com/OWNER/REPO/pull/123/files

Classify changed files as high/medium/low review risk. Then inspect high-risk files first. If you cannot inspect the entire diff, state which files you prioritized and which remain unreviewed.
5.5 Ask for triage on /pulls

When giving only the open PR list:

Markdown
Open the open PR list:
https://github.com/OWNER/REPO/pulls

Return:
1. The candidate PRs you found relevant to `{TASK}`.
2. The reason each candidate is or is not relevant.
3. The single PR you selected for deeper review.
4. The follow-up URLs Claude Code should provide or that you opened, especially `https://github.com/OWNER/REPO/pull/<N>` and `https://github.com/OWNER/REPO/pull/<N>/files`.

Do not perform a deep PR review from the list page alone.
5.6 Ask for careful long-diff review
Markdown
The diff may be long. Do not stop after the first visible files. Use this review order:

1. Inventory all visible changed files.
2. Identify high-risk files by path, extension, and domain.
3. Review high-risk implementation files.
4. Review tests related to those files.
5. Review migrations/config/API surfaces.
6. Only then summarize lower-risk mechanical changes.

For any file GitHub hides or truncates, say so and request/use a blob URL.
5.7 Prefer blob URLs for exact code

For code-level questions:

Markdown
Use the PR diff to understand what changed, but use these commit-pinned blob URLs for exact surrounding code:
https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH/TO/FILE

If the diff and blob appear inconsistent, call out the inconsistency and prefer the commit-pinned blob for the HEAD snapshot.
5.8 Make private-access failures explicit
Markdown
Some GitHub URLs may require access. Try to open them. If any URL is inaccessible, do not guess its contents. State that it was inaccessible and use the fallback gist/inline context.
5.9 Tell ChatGPT not to overclaim local execution

Always include:

Markdown
Do not claim to have run commands, opened local files, edited files, or executed tests. Claude Code will verify locally.
5.10 Keep raw URLs inline in findings

Add:

Markdown
For every finding, include the raw URL inline at the point it supports the claim, for example:
`Location: src/auth/session.ts, near validateSession, https://github.com/OWNER/REPO/blob/HEAD_SHA/src/auth/session.ts`
6. Output Format Guidance

Deep consult responses should be optimized for Claude Code actionability.

6.1 Standard severity taxonomy

Use this taxonomy in all review templates.

BLOCKER
- Very likely correctness, security, data-loss, migration, or production incident risk.
- Should block merge or execution until fixed or explicitly accepted.

HIGH
- Credible serious risk with plausible user-facing or operational impact.
- Should usually be fixed before merge unless there is a documented mitigation.

MEDIUM
- Real issue or maintainability/test gap that can cause bugs but is not immediately merge-blocking.

LOW
- Minor cleanup, clarity, local maintainability, or narrow edge case.

NIT
- Style or preference only. Avoid unless the user explicitly asks for polish.
6.2 Finding format

Require this exact shape for findings:

Markdown
### {SEVERITY}: {short title}

- Category: correctness | security | data-loss | migration | API compatibility | concurrency | performance | test gap | maintainability | docs
- Location: `{path}`, `{function/class/test/hunk}`, nearest visible line if available
- Evidence: https://github.com/OWNER/REPO/blob/HEAD_SHA/PATH or https://github.com/OWNER/REPO/pull/PR_NUMBER/files
- Problem:
- Impact:
- Recommended fix:
- What Claude Code should verify locally:
6.3 Merge-gate verdicts

Use one of:

BLOCK
MERGE WITH FIXES
MERGEABLE
INSUFFICIENT INFO

Definitions:

BLOCK:
At least one BLOCKER exists.

MERGE WITH FIXES:
No confirmed BLOCKER, but HIGH issues or important missing verification remain.

MERGEABLE:
No BLOCKER/HIGH issues found from available sources, and remaining issues are MEDIUM/LOW/NIT.

INSUFFICIENT INFO:
The provided URLs/context were inaccessible or inadequate for a responsible answer.
6.4 Local verification section

Every consult should end with a verification checklist:

Markdown
## What Claude Code should verify locally

- Run/inspect: `{specific test or command}`
  - Purpose:
  - Expected result:
  - If it fails:

- Inspect: `{file/function}`
  - Purpose:
  - Risk being checked:

ChatGPT may suggest commands, but must not claim to have run them.

6.5 Access gaps section

Require:

Markdown
## Access gaps / uncertainty

- URLs I could not access:
- Files that appeared truncated/collapsed:
- Assumptions I made:
- Facts Claude Code should verify locally:
6.6 Sentinel wrap discipline

The transport should generate a unique request id {RID} and require exact wrapping.

Template:

Markdown
Wrap your FINAL answer exactly between:
BEGIN_RESPONSE:{RID}
...
END_RESPONSE:{RID}

Extraction rules:

- Accept only text between the first exact BEGIN_RESPONSE:{RID} and the following exact END_RESPONSE:{RID}.
- If no sentinels are found, treat the entire answer as extraction failure and retry once with a shorter reminder.
- If only one sentinel is found, treat as partial failure and surface the raw response for manual review.
- Do not include any text before BEGIN_RESPONSE or after END_RESPONSE in the final extracted payload.

Retry prompt:

Markdown
Your previous response did not follow the required sentinel format.

Return the same substantive answer again, but wrap it exactly as:

BEGIN_RESPONSE:{RID}
...
END_RESPONSE:{RID}

Do not include any text before BEGIN_RESPONSE:{RID} or after END_RESPONSE:{RID}.
7. Current Approach: Ranked Gaps and Fixes
7.1 Gap 1 — Treating GitHub URLs as optional “extra info”
Problem

The current prompt says ChatGPT “MAY” open URLs. This can lead to an answer from memory, general reasoning, or only inline context.

Fix

Use explicit browse-first instructions:

Markdown
Open the URLs below with your browser tool before answering. Use them as the primary source of truth. If a URL is inaccessible or insufficient, say so explicitly.

Keep “MAY use browser” only for capability framing, but add task-specific “open these first” instructions in Source to review.

7.2 Gap 2 — Overusing gist when GitHub already has better context
Problem

Gists lose PR intent, review comments, CI context, file navigation, line anchors, and commit/branch relationships. Gist raw URLs may render code with collapsed newlines.

Fix

Decision rule:

IF open PR exists:
    use /pull/N + /pull/N/files + key blob URLs
ELSE IF branch is pushed:
    use /compare/base...head + tree@HEAD_SHA + key blob URLs
ELSE:
    use gist fallback for local unpushed state

Prompt note:

Markdown
Prefer the GitHub PR/diff/blob URLs over gist when both are available. Use gist only for local unpushed changes or generated local artifacts.
7.3 Gap 3 — No catalog of high-level discovery URLs
Problem

The skill jumps directly to file blobs or gist, missing useful pages like:

https://github.com/OWNER/REPO/pulls
https://github.com/OWNER/REPO/pull/123
https://github.com/OWNER/REPO/pull/123/files
https://github.com/OWNER/REPO/compare/BASE...HEAD
https://github.com/OWNER/REPO/issues/456
Fix

Add a discovery_refs tier:

PR discovery:
- /pulls
- filtered /pulls?q=...

Intent/requirements:
- /pull/N
- /issues/N

Change set:
- /pull/N/files
- /compare/base...head
- /commit/sha

Source snapshot:
- /tree/sha
- /blob/sha/path

Inject discovery refs only when they reduce ambiguity. For deep review, always add concrete review refs.

7.4 Gap 4 — No distinction between discovery, change-set, and source-snapshot refs
Problem

A /pulls list, a /pull/N/files diff, and a /blob/sha/path file serve different purposes. Treating all refs as a flat list makes ChatGPT less reliable.

Fix

Structure refs by purpose:

Markdown
## Source to review — open these with your browser tool before answering

### Intent / discussion
- https://github.com/OWNER/REPO/pull/123
- https://github.com/OWNER/REPO/issues/456

### Change set
- https://github.com/OWNER/REPO/pull/123/files

### Stable source snapshot
- https://github.com/OWNER/REPO/tree/HEAD_SHA

### Close-reading files
- https://github.com/OWNER/REPO/blob/HEAD_SHA/src/auth/session.ts
- https://github.com/OWNER/REPO/blob/HEAD_SHA/src/auth/session.test.ts
7.5 Gap 5 — Branch refs instead of SHA refs
Problem

Branch URLs move. A consult may take long enough that a branch is updated during or after the review.

Fix

Use commit-pinned URLs whenever possible:

Good:
https://github.com/OWNER/REPO/blob/HEAD_SHA/src/file.ts
https://github.com/OWNER/REPO/tree/HEAD_SHA
https://github.com/OWNER/REPO/compare/BASE_SHA...HEAD_SHA

Weaker:
https://github.com/OWNER/REPO/blob/feature-branch/src/file.ts
https://github.com/OWNER/REPO/tree/feature-branch
https://github.com/OWNER/REPO/compare/main...feature-branch

Include metadata:

Markdown
Base SHA: BASE_SHA
Head SHA: HEAD_SHA
Prepared by Claude Code at: TIMESTAMP
7.6 Gap 6 — Large diffs are not triaged
Problem

ChatGPT may inspect early files and miss high-risk later files.

Fix

For PRs over a threshold, require inventory-first review.

Suggested thresholds:

If changed files > 12 or diff lines > 800:
    add long-diff instructions
If changed files > 30 or diff lines > 2000:
    require risk triage and acknowledge non-comprehensive review unless key files are provided

Prompt addition:

Markdown
This is a large diff. First inventory changed files from https://github.com/OWNER/REPO/pull/123/files, then prioritize high-risk files. State which files you inspected closely and which you did not.
7.7 Gap 7 — Missing local execution summary
Problem

ChatGPT cannot run tests. Without local results, it may recommend generic verification.

Fix

Always include concise local verification context when available:

Markdown
Local verification already performed by Claude Code:
```text
- npm test: failing, 2 failures in auth/session.test.ts
- npm run typecheck: passed
- migration dry-run: not run

For stuck bugs, include exact failure output inline.

---

## 7.8 Gap 8 — No scenario-specific output contract

### Problem

A generic answer may be hard for Claude Code to act on.

### Fix

Use scenario-specific templates and require:

```text
- verdict
- severity
- file/function/line anchors
- raw URL evidence
- concrete fix direction
- local verification checklist
- access gaps
7.9 Gap 9 — Failure to tell ChatGPT what not to do
Problem

ChatGPT may overstate certainty, imply it ran tests, or assume private/local context.

Fix

Always include:

Markdown
Do not claim to have run commands, opened local files, edited files, or executed tests. Claude Code will verify locally. If a URL is inaccessible, do not guess its contents.
7.10 Gap 10 — Gist contents are not standardized
Problem

When gist fallback is needed, arbitrary file dumps are hard to review.

Fix

Standardize gist layout:

metadata.txt
changed-files.txt
diff.patch
selected-files/
test-output.txt
notes.txt

metadata.txt:

repo: OWNER/REPO
base_branch: BASE_BRANCH
base_sha: BASE_SHA
head_branch: HEAD_BRANCH
head_sha: HEAD_SHA_OR_LOCAL
working_tree: clean|dirty
unpushed_changes: yes|no
task_scenario: SCENARIO
prepared_at: TIMESTAMP

changed-files.txt:

M path/to/file1.ts
A path/to/file2.ts
D path/to/file3.ts

notes.txt:

Authoritative local context:
- The gist reflects unpushed local changes.
- GitHub URLs reflect only the baseline unless otherwise stated.
- Raw gist rendering may be unreliable; prefer the gist page.
8. Recommended deliver Output Schema

The skill should internally build a structured object before rendering the prompt.

JSON
{
  "rid": "REQ-...",
  "scenario": "PR_MERGE_GATE",
  "repo": {
    "owner": "OWNER",
    "name": "REPO",
    "root_url": "https://github.com/OWNER/REPO",
    "visibility": "public|private|unknown"
  },
  "git": {
    "base_branch": "main",
    "base_sha": "BASE_SHA",
    "head_branch": "feature/foo",
    "head_sha": "HEAD_SHA",
    "head_pushed": true,
    "working_tree_state": "clean|dirty|unknown"
  },
  "refs": {
    "discovery": [
      {
        "kind": "pulls",
        "url": "https://github.com/OWNER/REPO/pulls",
        "purpose": "Find relevant open PRs"
      }
    ],
    "intent": [
      {
        "kind": "pr",
        "url": "https://github.com/OWNER/REPO/pull/123",
        "purpose": "PR description, discussion, checks"
      },
      {
        "kind": "issue",
        "url": "https://github.com/OWNER/REPO/issues/456",
        "purpose": "Requirements and reproduction"
      }
    ],
    "change_set": [
      {
        "kind": "pr_files",
        "url": "https://github.com/OWNER/REPO/pull/123/files",
        "purpose": "Changed-file diff"
      }
    ],
    "snapshot": [
      {
        "kind": "tree",
        "url": "https://github.com/OWNER/REPO/tree/HEAD_SHA",
        "purpose": "Stable source snapshot"
      }
    ],
    "close_reading": [
      {
        "kind": "blob",
        "url": "https://github.com/OWNER/REPO/blob/HEAD_SHA/path/to/file.ts",
        "path": "path/to/file.ts",
        "purpose": "High-risk changed file"
      }
    ],
    "fallback": [
      {
        "kind": "gist",
        "url": "https://gist.github.com/USER/GIST_ID",
        "purpose": "Unpushed local changes"
      }
    ]
  },
  "inline_context": {
    "changed_files_summary": "...",
    "test_summary": "...",
    "failure_output": "...",
    "constraints": "...",
    "prior_attempts": "..."
  }
}

Render refs grouped by purpose, not as a flat list.

9. High-Signal File Selection Rules

When adding blob URLs, select files using this priority order.

9.1 Always include
- Files directly changed by the PR/branch that are security-, auth-, payment-, persistence-, migration-, API-, or concurrency-sensitive
- Public API/interface/schema files
- Database migrations and schema definitions
- Config files affecting deployment, auth, routing, build, or permissions
- Failing tests or tests closest to changed code
- Files named in stack traces
- Entry points for the subsystem under review
9.2 Include when budget allows
- Main callers/callees of changed functions
- Integration tests
- Fixtures and factories if tests are central to the task
- Type definitions that constrain behavior
- Documentation/design docs if requirements matter
9.3 Usually exclude
- Generated files
- Lockfiles, unless dependency resolution is the issue
- Pure formatting churn
- Large snapshots, unless snapshot behavior is under review
- Vendor files
9.4 Suggested limits
Small consult:
- 3-8 blob URLs
- 1-2 inline snippets

Medium consult:
- 8-20 blob URLs
- grouped by subsystem/risk

Large consult:
- PR/compare/tree URLs
- 10-25 highest-risk blob URLs
- require ChatGPT to inventory and triage rather than inspect everything exhaustively
10. Ready-to-Paste Master Prompt Template
Markdown
You are an external consultant for a Claude Code session. Claude Code is the local executor: it reads/edits the repo and runs all tests locally. You do not run anything.

Do not claim to have run commands, opened local files, edited files, or executed tests. You MAY use your web browsing tool to open any GitHub / pull-request / raw URLs given below and read the actual code before answering. For this consult, open the URLs in `Source to review` before answering unless they are inaccessible. If any URL is inaccessible, truncated, collapsed, or insufficient, say so explicitly. Keep every reference URL inline as a raw URL at the point it supports a claim.

Your answer is advisory. Claude Code will re-verify locally.

## Task
{TASK}

## Source to review — open these with your browser tool before answering

### Intent / discussion
{INTENT_REFS}

### Change set
{CHANGE_SET_REFS}

### Stable source snapshot
{SNAPSHOT_REFS}

### Close-reading files
{BLOB_REFS}

### Discovery refs
{DISCOVERY_REFS}

### Fallback refs
{FALLBACK_REFS}

## Local context from Claude Code

Repository:
```text
owner/repo: {OWNER}/{REPO}
visibility: {VISIBILITY}
base branch: {BASE_BRANCH}
base SHA: {BASE_SHA}
head branch: {HEAD_BRANCH}
head SHA: {HEAD_SHA}
head pushed: {HEAD_PUSHED}
working tree: {WORKING_TREE_STATE}
prepared at: {TIMESTAMP}

Changed files:

{CHANGED_FILES}

Local verification:

{LOCAL_VERIFICATION}

Symptoms / logs / stack traces:

{FAILURE_OUTPUT}

Constraints / non-goals:

{CONSTRAINTS}

Prior attempts / known concerns:

{PRIOR_ATTEMPTS_OR_CONCERNS}
Output requirements

Use this structure:

Verdict or recommendation

Use the scenario-appropriate verdict.

State confidence and main rationale.

Findings or analysis

Use severity tags: BLOCKER, HIGH, MEDIUM, LOW, NIT.

For each finding:

Category

Location

Evidence URL inline

Problem

Impact

Recommended fix

What Claude Code should verify locally

Local verification checklist

Concrete tests, commands, inspections, or scenarios Claude Code should perform.

Do not claim you ran them.

Sources inspected

List URLs used.

List inaccessible/truncated/collapsed URLs.

Uncertainties and assumptions

State any assumptions and what would change the conclusion.

Wrap your FINAL answer exactly between:
BEGIN_RESPONSE:{RID}
...
END_RESPONSE:{RID}


---

# 11. Practical Defaults

## 11.1 Default for any open PR consult

```md
Open in order:
1. https://github.com/OWNER/REPO/pull/PR_NUMBER
2. https://github.com/OWNER/REPO/pull/PR_NUMBER/files
3. https://github.com/OWNER/REPO/tree/HEAD_SHA
4. Key blob URLs pinned to HEAD_SHA
11.2 Default for any pushed branch without PR
Markdown
Open in order:
1. https://github.com/OWNER/REPO/compare/BASE_SHA...HEAD_SHA
2. https://github.com/OWNER/REPO/tree/HEAD_SHA
3. Key blob URLs pinned to HEAD_SHA
11.3 Default for any unpushed local change
Markdown
Open in order:
1. https://gist.github.com/USER/GIST_ID
2. https://github.com/OWNER/REPO/tree/BASE_SHA
3. Baseline blob URLs if needed

Treat the gist as authoritative for local changes. Treat GitHub as baseline context.
11.4 Default for bug consult
Markdown
Use inline failure evidence first, then inspect anchored blob URLs, then inspect recent compare/commit if regression is suspected.
11.5 Default for architecture consult
Markdown
Use tree URLs for navigation and blob URLs for exact source. Do not expect ChatGPT to infer a subsystem from the repository root alone.
12. Implementation Checklist for the Skill Author
[ ] Classify consult scenario before rendering prompt.
[ ] Detect open PR for current branch.
[ ] If open PR exists, inject /pull/N and /pull/N/files.
[ ] If no PR but branch is pushed, inject /compare/base...head and /tree/head_sha.
[ ] If local changes are unpushed, create standardized gist fallback.
[ ] Always prefer commit-pinned /blob/<sha>/<path> for close reading.
[ ] Group refs by purpose: discovery, intent, change_set, snapshot, close_reading, fallback.
[ ] Add issue URLs when task mentions issues or requirements.
[ ] Add inline local test/log output for bug or merge-gate consults.
[ ] Add explicit browse-first instruction.
[ ] Add long-diff triage instruction when diff is large.
[ ] Require severity-tagged findings.
[ ] Require raw URLs inline in evidence.
[ ] Require local verification checklist.
[ ] Require sources-inspected and access-gaps sections.
[ ] Require exact sentinel wrapping.