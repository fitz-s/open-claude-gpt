---
name: chatgpt-consult
description: >-
  Use proactively whenever ANY deep, self-contained job — code review, architecture/planning, investigation, research, analysis, writing, math, design, a hard second opinion, audit, RFC — can run in parallel with local work and its result isn't needed this second. Offload it to your ChatGPT Pro subscription in the background, keep working, and get woken with the full result, which Claude Code then leans on and spot-checks. A multi-round thread, not a one-shot — follow up with local results. Never send secrets/`.env`/keys.
allowed-tools: Read, Write, Grep, Glob, Bash(python3:*), Bash(bash:*), Bash(curl:*), Bash(mkdir:*), Bash(pbcopy:*), Bash(git status:*), Bash(git diff:*), Bash(git ls-files:*), Bash(git rev-parse:*), Bash(git remote:*), Bash(git branch:*), ScheduleWakeup, mcp__Claude_in_Chrome__list_connected_browsers, mcp__Claude_in_Chrome__select_browser, mcp__Claude_in_Chrome__tabs_context_mcp, mcp__Claude_in_Chrome__tabs_create_mcp, mcp__Claude_in_Chrome__navigate, mcp__Claude_in_Chrome__find, mcp__Claude_in_Chrome__read_page, mcp__Claude_in_Chrome__computer, mcp__Claude_in_Chrome__javascript_tool, mcp__Claude_in_Chrome__get_page_text
---

# chatgpt-consult

Use this when a job calls for planning, hard reasoning, a grounded second opinion, architecture tradeoffs, or difficult debugging, and you have a logged-in ChatGPT session to run it in.

**Safety gate — read before sending anything.** Every consult must be grounded in PUBLIC GitHub links (a public repo/PR/branch) or content you're comfortable sending to ChatGPT outright. NEVER send secrets, `.env` files, tokens, private-repo content, customer data, or unreviewed local logs — check what's actually in a link or file before it goes out, not after.

When the inputs are safe and public, use this proactively: hand it a deep, self-contained job (review, plan, investigation, second opinion, audit, design), **fire it to the cloud, and keep working locally while it runs** — the detached waiter wakes you with the full result when it's done. It is an upgraded `ultraplan`/`ultrareview` that runs *off* your local context budget, *in parallel* with your current task, on a *different model family* (independent blind spots) with a huge context + long reasoning budget and web browsing. It runs on the **ChatGPT Pro subscription you already pay for** (no API bill) — spend it on the three things it's best at: **planning, hard reasoning, and review** of your *hardest, most open-ended* deep work. Calibrate what you send to match, and steer it hard (see "Steer the round"). ChatGPT advises; Claude Code executes and verifies.

## A consult takes ~25 minutes — spend it accordingly
A GPT-5.6 Pro round reasons for ~25 min: it works the problem from several angles and returns something closer to a proof than an answer. Two consequences:

- **Only send work worth that.** Never a question you could answer locally in a minute.
- **Be precise — the question above all.** Depth amplifies whatever you aim it at, so a vague `--task` buys 25 minutes of well-argued irrelevance and you find out only when it lands. Name the decision, the bar for "good", and the sub-questions (see "Steer the round"), and keep the prompt lean — on 5.6 padding buys extra exploration, and "be brief" makes it substitute a shorter artifact. Full law: **[references/gpt-5.6-prompting-principles.md](references/gpt-5.6-prompting-principles.md)**.

Then fire it and go work on something else — the path is detached and wakes you. `await` waits as long as the consult takes; it returns only when there is something to act on.

## Quickstart — the daemon path (default; auto-mode-safe), top to bottom
Prereqs (once per machine): `pip install websocket-client`; `gh auth status` OK; Google Chrome installed (the launcher uses the macOS path). Run each line as its own step and write paths in full — shell variables don't persist between separate Bash calls.

**Why this path.** Claude Code's auto-mode **data-exfiltration classifier** sits ABOVE the permission system and HARD-DENIES any of *your* Bash calls that send data to an external host (chatgpt.com) — so a DIRECT `cdp_consult.py submit`/`wait` is blocked in auto mode, and no allowlist suppresses it (it is a safety classifier, not a permission). The fix: **you only touch LOCAL files.** `deliver` builds the GitHub links from local git alone, `prep` renders a file, `enqueue` writes a job file, `await` polls an answer file — **not one of them opens a network connection**, so the classifier never sees an external send. A daemon running in the user's own login session does the actual send, after re-validating the code is public.

This is enforced, not promised: `tests/test_security.py` runs the whole `deliver → prep → enqueue` chain with `gh` removed from `PATH` entirely and requires it to succeed. If a future change reintroduces a network call on your side, that test fails instead of the classifier denying you mid-consult. **`deliver` therefore does NOT verify repo visibility itself** — it stamps the refs "TO BE VERIFIED AT THE EGRESS GATE" and the daemon does the authoritative `gh` check before anything is sent, failing closed on a private or unverifiable repo exactly as before. Nothing is weakened; the check simply happens where it is authoritative. (Driving it by hand? `cgc deliver` passes `--verify` and checks up front.)

**The daemon is always up — do not check it.** The user installs it once with `cgc install-daemon` (a launchd agent: starts at login, respawns if it dies), and it opens the debug Chrome itself when it needs to. So there is no preflight: **never run `cgc queue`/`doctor` "to make sure" before a consult** — that costs tokens on every round and tells you nothing you'd act on. Just `enqueue`. In the rare case it is genuinely down, `enqueue`/`await` say so in one line (exit 2) — relay `cgc install-daemon` to the user and move on. You never start it yourself.

```bash
# 0. Nothing. Do NOT check the daemon, do not run doctor — the daemon is installed once
#    (`cgc install-daemon`) and launchd keeps it running forever, restarting it if it dies.
#    Just enqueue. If it somehow isn't running, `await` tells you in one line (exit 2).

# 1. Deliver the code as a link (public-PR example). Note "refs_file" in the JSON.
#    LOCAL-ONLY: builds links from local git; it does NOT call gh, so the classifier never sees it.
#    Provenance ("is this repo public?") is verified by the daemon's gate before the send.
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py deliver --repo owner/repo --pr 123

# 2. Prep the prompt (writes the prompt file; capture request_id from the JSON). See "Steer the round".
#    --title + --role + --task are the steering levers — pass a sharp title and a job-matched role.
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py prep --refs-file <refs_file> \
  --title "<sharp headline>" --role "You are a <persona matched to the job>." --task "<the question>"

# 3. Enqueue the job — a LOCAL write. The user's daemon validates (re-checks the repo is PUBLIC) + sends.
python3 ~/.claude/skills/chatgpt-consult/scripts/cgc_spool.py enqueue --rid <request_id> --prompt-file <prompt_file>

# 4. Await DETACHED — dispatch with run_in_background:true, then go do other work; it wakes you on done.
#    `await` is a LOCAL file poll (no network), so the classifier never blocks it. It waits as long
#    as the consult takes — no wrapper, no slicing, no re-running. NEVER `nohup … & disown`: an
#    untracked process never wakes you.
python3 ~/.claude/skills/chatgpt-consult/scripts/cgc_spool.py await --rid <request_id> --out /tmp/cgc/answer_<request_id>.txt

# 5. On wake: read the answer, then verify locally.
#    Read /tmp/cgc/answer_<request_id>.txt    (enqueue's default --out; it also prints the exact await line)
#    To continue the thread, prep --followup then enqueue --kind followup --conversation auto (NOT a new
#    plain prep+enqueue, which opens a NEW conversation). See "Follow-up rounds".
```
Error → cause: `await` returns exactly three things, because exactly three are actionable. **`0`** = the answer is on disk and its path is printed — go read it. **`3`** = a human must act in the ChatGPT window (login/captcha/rate-limit/model, or a **GPT-5.6 safeguard refusal** — its synchronous cyber/bio classifier occasionally intervenes on legitimate dual-use work like vuln/security review); the user acts, then re-enqueue. **`1`** = broken, and the job-log path is printed. "Still running" is not a return value: a consult routinely takes ~25 minutes and `await` simply keeps waiting. It gives up only after **60 minutes**, which is far past anything the work explains — so that is a malfunction, not a slow answer, and re-running the wait cannot fix it. `enqueue` refuses locally (exit 2) on a missing code link or a detected secret. The daemon's own `GATE REFUSED` (via `cgc queue` / its log) = the repo isn't gh-confirmed public, a gist link (`CGC_GATE_ALLOW_GIST=1` to allow), or a secret — read its FIRST WORD: `refused:` is a verdict (fix the input; re-enqueueing cannot help), `unverified:` means the check itself failed (`gh` timed out) and nothing is known, so re-enqueue to retry. **On any failure `await` prints `CGC_LOG <path>` — the full send+wait transcript, streamed live. Read it before guessing.** Its heartbeat is the diagnosis: `ac=0` throughout = ChatGPT never produced an assistant turn (nothing was sent, or the page changed shape); `begin=true end=false` = the answer is being written right now. Full detail in **Pipeline A**; the DIRECT submit/wait fallback (interactive mode only) and the no-debug-profile path are in **[references/mcp-fallback.md](references/mcp-fallback.md)**.

## Roles (keep distinct)
- **ChatGPT = external advisor.** Gives plans / reviews / risk analysis — treat its output as advisory input.
- **Claude Code = local executor + verification authority.** Reads/edits the repo, runs all tests, owns correctness.
- Feed ChatGPT context, lean on its judgment, and give the load-bearing parts a quick local look; it advises with weight, Claude Code applies it.

## HARD RULE — link-first, gist-last (read this before delivering ANY code)
If the code is on GitHub in ANY form — a **PR**, a pushed branch, a commit, a tag, `main` — deliver the GitHub **link** for ChatGPT to browse; it carries the diff + intent + discussion + CI + navigable code in one surface. Reserve a PUBLIC gist/upload for content that is genuinely not on GitHub (unpushed or private-inaccessible state) — never a private or unreviewed one.
- **Reviewing a PR → `consult.py deliver --repo <owner/repo> --pr <N>`.** That yields the PR **link** + `/pull/N/files` diff — that IS the review surface; do NOT dump every changed file as blobs. Add `--blobs` ONLY for a giant PR whose aggregate diff won't render (then it appends per-file permalinks, capped 25 + an overflow pointer). **Never gist or upload a PR.** Reviewing `main`/a branch → `--ref main` (a `/tree` link it browses on its own) — the link alone is usually enough; add a `--file` only for a specific needle, never to enumerate the set.
- A PUBLIC gist/upload is **only** for genuinely unpushed-local or private-inaccessible content, and only once you're comfortable sending that content externally. If a PR or pushed ref exists, that path is wrong — stop and use the link.
- **ENFORCED (fail-closed, NO override):** `prep` refuses to render and `submit` refuses to send a non-followup consult that has no actual code **link** — `CGC_ERROR no_code_source`. **`--refs-file` (a GitHub/PUBLIC gist link from `deliver`) is REQUIRED**; a `--context-file` of abstracted prose does NOT satisfy it — *prose about the code is not the code*, and ChatGPT answers blind ("no file access / can't cite file:line", the recurring failure). The flow is always: `deliver` → pass its `refs_file` to `prep --refs-file`. `--context-file` is supplement-only (failure bundles, perf numbers, external design positions). **There is no bypass flag.** Even a *design/architecture/strategy* consult about an existing repo must ship the repo link — don't abstract the codebase into a description; for a broad design question use `deliver --repo <owner/repo> --ref <sha>` (a whole-repo `/tree` link — no need to pick files). If you genuinely have only a design doc and no repo, `gh gist create <doc>` and pass the PUBLIC gist link as `--refs-file`.
- **Exception — a consult with no code subject at all:** a maths proof, a research or writing question, a self-contained analysis. Render it with **`prep --no-code`** (no `--refs-file` needed). This is not a loophole: anything about a repo, a diff, or a design expressed in code still MUST ship the link, because prose about code gets a blind answer. The daemon's gate keeps enforcing everything else on these prompts — secrets refused, and any repo they do cite must be gh-confirmed public.

## When to use it
The win is **offload + parallelism**: fire a deep, self-contained job to the cloud and keep working; the detached waiter brings the result back. Run 2–3 consults at once, so the 10–30 min latency is free — you're never idle waiting.

**Discriminant:** reach for it when a job is *deep + self-contained + you have other work to do meanwhile*. Skip it only for things Claude Code answers in seconds, or where you'd sit idle with nothing else to do.

**Ultra-everything — this is the default, not a rare move.** It is not a code-only reviewer; it's a general-purpose ChatGPT Pro brain you can run in the background. *Anything* deep + self-contained qualifies: research a library/approach/prior art, analyze a dataset or a log corpus, draft or critique a design doc / RFC / strategy memo, work a proof or a hard calculation, untangle an architecture or product question, write or review prose. **When you catch yourself about to spend a long stretch of local context on a deep, self-contained sub-problem, fire it to a consult and keep working the main task** — offloading is the intended habit. You don't need the user to ask; reach for it on your own whenever the shape fits.

**Common jobs** (non-exhaustive): high-risk PR-merge gate (adversarial review), pre-refactor architecture plan, deep module/subsystem review, broad-investigation / research-backed decision, library/approach or prior-art research, dataset/log-corpus analysis, a proof or hard calculation, a design doc / RFC / strategy memo, design-dispute or stuck-bug second opinion, long-context consistency audit, devil's-advocate before an irreversible action, design/DX/naming sounding board, multi-round plan→execute→feed-back loop, background second-pair-of-eyes, draft-while-you-build. (Fuller playbook in [SKILL-NOTES](SKILL-NOTES.md).)

**Invariant:** ChatGPT is a high-value advisor — lean on it and act on its output; give the load-bearing claims (and anything it tags `verify locally`) a quick local look before shipping. That's a sanity-check, not a line-by-line re-audit — don't burn runtime re-deriving what the consult already reasoned through. (Independent of this: never send secrets/`.env`/keys.)

**Take the WHOLE answer seriously and THINK — don't reduce a deep consult to a bug-list.** A consult's highest-value output is usually not the defects but the architecture call, the "here's a design that dominates yours", the reframing of the premise. The failure mode: fix the easy bugs, nod at the design-level points, move on (and "recording them for later" is the same failure wearing a hat — a to-do list is not engagement). Engage each substantive finding on its merits:
- If it's right, **implement it now** — design changes included, not just the defects.
- If you're NOT going to do what it says, that must be because you **thought about it and know something the consult didn't** — the consult reviewed from outside (often from a GitHub link, blind to your runtime, your harness, your constraints, the product's economics). Your job is to **produce that reasoning as a real answer**: *why* the suggestion rests on an assumption that's false here, grounded in the specific fact it missed — and write it where it belongs (the design rationale / the relevant doc), so it stands as the considered response and the next reader (or next consult) doesn't re-raise it. That is acting on the consult, not deferring it.
- Only genuinely large, product-changing forks go back to the **user to decide** — with your analysis attached, not as an open question you punted.

A consult that made you *think harder and write down why the obvious-looking alternative doesn't hold* was used well. One you turned into a patch-list was not.

## Steer the round — `--title`, `--role`, `--task` are the highest-leverage inputs
Read references/gpt-5.6-prompting-principles.md first. The fixed template supplies the generic contract — verdict + confidence, file:line findings with evidence URLs, concrete fixes, local checks, an end-to-end depth mandate, plain prose. **You write the part the template can't know, and that is what turns a generic Q&A into a deep round.** Three levers, every consult:
- **`--title`** (one line, above Role) — name the specific decision/surface, e.g. "Merge-safety gate: PR #406 — data-loss & migration-ordering risk". A sharp title frames the whole answer and primes depth; a vague one wastes the slot. Always pass one.
- **`--role`** — the expert persona matched to THIS job, not a default reviewer: "You are a distributed-systems security auditor focused on concurrency and data integrity", "You are an API designer who optimizes for back-compat", "You are an SRE doing a pre-deploy risk pass". The persona steers where the depth lands. (The fixed relationship — Claude Code executes/verifies, ChatGPT advises — is appended automatically; you only supply the persona.)
- **`--task`** — the question only, carrying the *delta* the template can't know. Write that delta, not a restatement of the contract:

1. **The decision or deliverable you need** — for a *judgment* consult (review / dispute / bug): the verdict you'll act on ("merge or block PR #406", "pick A vs B", "the leading root cause"). For a *generative* consult (plan / design / RFC / investigation): the artifact and its shape ("a staged refactor plan, lowest-risk phase first", "an API design with a tradeoff table").
2. **The bespoke bar for THIS consult** — the specific thing that makes the answer right or wrong here, not "be correct" (the template owns that): "safe only if no migration-ordering risk", "p99 stays under 200ms at 10× load", "the plan never breaks the public API mid-sequence".
3. **Where to look hardest** — the risks/angles you're unsure of, so depth lands where it matters; list them as concrete labelled sub-questions (a/b/c) — "(a) writer-path concurrency, (b) public-API back-compat, (c) the DST boundary" — so the model spends budget per-risk, not evenly.
4. **The constraints that bind** — invariants, perf budget, compatibility, conventions the change must respect. When a constraint is a project-specific term or law (an invariant name, a domain rule, a "project law"), point to WHERE it's defined (a doc/section/link) so the model can check compliance instead of guessing what it means.
5. **The evidence the model can't browse to** — code on GitHub goes by link (see File delivery); failure bundles (stack trace, failing assertion, repro, known-good/bad SHAs, what you already tried), competing design positions, and perf numbers aren't on GitHub, so put them in `--context-file`. A stuck-bug or dispute consult with no evidence bundle gets a guess.
6. **Challenge the premise, not just the code** — ChatGPT Pro's biggest wins come from disagreeing with your framing. For anything with an architecture or a plan, tell it to verify the APPROACH itself FIRST (is this the right design at all?) and to name a SUPERIOR alternative if one exists, with why it dominates — before it reviews the implementation as given. Invite the adversarial, design-level answer explicitly; don't let it rubber-stamp.

The `--task` is the question — keep code out of it (diffs/file bodies go through `deliver` as links, never pasted into `--task`). Length follows the problem, not a word count: every sentence must be *delta* (the bar, the per-risk sub-questions, the binding laws, the alternatives to weigh) — a simple consult is a few sentences, a genuinely deep one (several bespoke bars + labelled look-hardest angles + design-challenge) is legitimately a dense paragraph. The only thing to cut is any sentence that restates the template's contract (format, "be rigorous", "cite sources"). Need output beyond the findings list (a tradeoff matrix, ordered phases, ranked hypotheses)? Pass it via `--output-file`, not `--task`. If that spec ADDS to the standard findings list, prepend (the default). If it defines **its own findings shape or severity scale** (e.g. a numbered multi-section review with its own per-file line), pass **`--output-replace`** so it replaces the default contract instead of colliding with a second one. Let the model choose how to investigate.

**Default deep-review arc — don't wait to be told.** For any code / architecture / rebuild / PR-merge review, pass the ready scaffold `references/deep-review-output.md` as `--output-file` with `--output-replace`. Its order is load-bearing: **§1 verify the approach/plan is even correct (before opening any file) → §2 core correctness → §3 validation soundness → §4 per-file findings (every file) → §5 ideal-vs-actual + superior realizations → §6 go/no-go.** Verifying the plan first and naming a superior realization is the *skill's* job — produce that depth by default; the user should not have to spell out "check the plan, then every file, then the ideal difference, then a better realization."

Worked example — judgment consult (PR merge gate):
```bash
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py prep --refs-file <refs_file> \
  --title "Merge-safety gate: PR #406 — data-loss, migration-ordering, back-compat" \
  --role "You are a distributed-systems reviewer focused on concurrency and data integrity." \
  --task "Decide whether PR #406 is safe to merge to main. Safe only if there's no data-loss, migration-ordering, or back-compat risk and the new EMOS offset path keeps settlement math correct. Look hardest at writer/settlement concurrency and any city crossing the DST boundary. Must preserve existing settings.json keys and the public reactor API."
```
Worked example — generative consult (pre-refactor plan; pair with `--output-file` for the per-phase shape):
```bash
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py prep --refs-file <refs_file> --output-file <shape> \
  --title "Pre-refactor plan: extract the settlement engine out of reactor/" \
  --role "You are a staff engineer planning a behavior-preserving refactor under a live concurrency constraint." \
  --task "Recommend a staged plan to extract the settlement engine out of reactor/, and whether to do it at all. Lowest-risk behavior-preserving step first; never break the public reactor API or settings.json keys mid-sequence; p99 settlement latency must not regress. Hardest part: the writer/settlement concurrency coupling."
```

## Hard platform facts this design is built on (do not re-test)
1. **chatgpt.com CSP blocks page→localhost.** Any fetch/beacon/ws from the ChatGPT tab to `http://127.0.0.1` is refused by the site CSP before leaving the page. ⇒ No loopback/Monitor push-wake. Wake is **agent-side scheduled polling**.
2. **`javascript_tool` return values are privacy-scanned** — a return containing URL / cookie / query-string / UUID-like data is blocked. ⇒ JS may read/change the DOM but must return **metadata only** (booleans/counts/indices). Answer content NEVER comes back through a `javascript_tool` return.
3. **Routing page content through a file/localhost side-channel to dodge that scanner is forbidden** (not user-authorizable). Answer content comes back ONLY via `get_page_text`.
4. **Tool output is hard-capped at 50000 chars.** `get_page_text` is the sanctioned reader (not scanned, references intact) but is capped and has no offset → use the DOM windowing in `retrieval_window.js`.

## Two backends — prefer CDP
There are two ways to drive the page. **Prefer Backend A (CDP) whenever the debug profile is up** — it is dramatically cheaper.

- **Backend A — CDP (preferred).** An external Chrome DevTools client (`scripts/cdp_consult.py`) drives a *dedicated* Chrome debug profile. The page CSP, the `javascript_tool` scanner, the 50000-char cap, and DOM windowing **all vanish** — a DevTools client is not the page and is not the MCP. Best of all the wait runs as a **detached background Bash** (`cdp_consult.py wait`) that polls *outside* agent context and, on completion, extracts the full answer to a local file and **exits → re-invokes the agent (the wake)**. Main agent context is touched exactly twice: submit, then `Read` the answer file. No `ScheduleWakeup`, no per-wake context reload, no windowing, no scanner. Requires a one-time setup (a separate Chrome profile + ChatGPT Pro login) because CDP is disallowed on Chrome's default profile (anti-cookie-theft, Chrome 136+).
- **Backend B — MCP + ScheduleWakeup (fallback, zero setup).** Use it only when the debug profile cannot be set up; it pays all four taxes above. Full steps: **[references/mcp-fallback.md](references/mcp-fallback.md)**.

**Selecting the backend:** default to Backend A. Within it there are two ways to drive the page, and they differ ONLY in who issues the external send:
- **A1 — daemon (`enqueue`/`await`) — the DEFAULT, and the ONLY one that works in auto mode.** You write a local job file and poll a local answer file; the user's daemon (installed once via `cgc install-daemon`, always running) does the send behind a validating public-only gate. The auto-mode data-exfiltration classifier (fact 5 below) never sees an agent-issued external send, so it never blocks. This is the Quickstart path.
- **A2 — direct (`submit`/`wait`) — interactive-mode fallback only.** The agent drives chatgpt.com itself. Simpler (no daemon), but the classifier HARD-DENIES it in auto mode, so use it only when a human is approving tool calls, or when driving by hand. `submit` self-heals Step 0 (starts the debug Chrome if down, checks login); it aborts with `CGC_ERROR login_needed` only when the user must log into ChatGPT Pro (the agent never types credentials).

Fall back to Backend B (MCP) only if the user declines the dedicated-profile setup entirely.

5. **Auto-mode data-exfiltration classifier.** In `auto` permission mode a safety classifier ABOVE the permission system hard-denies any agent Bash call that sends data to an external host — including a direct `submit`/`wait` to chatgpt.com — and no `permissions.allow` entry suppresses it. ⇒ Default to the A1 daemon path, where the only agent actions are local file I/O.

## Pipeline A — CDP backend (preferred; setup once, then near-zero-cost waits)
0. **Step 0 is automatic — `submit` self-heals it (no LLM step).** Before sending, `submit` runs the gate (`cdp_launch.sh` in `CGC_GATE` mode): it starts the dedicated debug Chrome if it's down and probes login, silently when all is well. It aborts the submit with `CGC_ERROR login_needed` *only* when the user must log into ChatGPT Pro in the window that opened — then ask the user, and re-run submit. **The profile persists the session across Chrome restarts**, so login is one-time *per machine*, not per run (verified); their normal Chrome is untouched; the debug Chrome is launched bound to loopback (`--remote-debugging-address=127.0.0.1`) with loopback-scoped `--remote-allow-origins` (not `*`), and `doctor` verifies the port is actually listening on loopback only. Pass `submit --no-gate` only if you manage the debug Chrome yourself. (Manual gate: `bash ~/.claude/skills/chatgpt-consult/scripts/cdp_launch.sh`.)
1. **Deliver + Prep.** If ChatGPT needs to see code, run `consult.py deliver --repo-dir <repo> [--files ...]` first (see **File delivery**); pass the resulting `refs_file` to prep as `--refs-file` (prefer a PR/blob link over a PUBLIC gist). Then `consult.py prep` — reuse `request_id` and `prompt_file`. The `--expect-minutes`/wake-plan fields are irrelevant here (no ScheduleWakeup).
2. **Submit (control plane):**
   ```bash
   python3 ~/.claude/skills/chatgpt-consult/scripts/cdp_consult.py submit --rid <request_id> --prompt-file <prompt_file>   # --model defaults to "Pro"
   ```
   **Opens its OWN dedicated tab** (default — so concurrent consults never clobber each other), confirms the model, inserts the sentinel block via `execCommand` (no early submit), sends. Prints `{ok, userMsgs, model, modelConfirmed, conversation_id}`. **You MUST capture `conversation_id`** (the `/c/<id>`) and pass it to `wait --conversation <id>` — that pins the waiter to this exact tab so it returns THIS consult's answer and no other's. (Verified: 2 tabs each return their own answer independently.)
   - **Concurrency:** fire several consults at once — each gets its own tab + `conversation_id` + waiter; the waiter **auto-closes its tab** after retrieving (so tabs don't accumulate). Pass `--reuse-tab` only to navigate the single existing tab (no concurrency). If multiple tabs are open and a waiter isn't pinned with `--conversation`, it errors `ambiguous_target` on purpose — always pin.
   - **Model:** `--model` defaults to **`Pro`** (any Pro tier satisfies a `Pro*` target); navigate resets the model so submit re-selects it. Selection is **fully automated and two-menu aware** — the composer has separate model and reasoning-effort switchers, and the Pro tier lives in the model menu, so submit tries *each* switcher's menu until the target is actually selected, then confirms. **Fail-closed: if it cannot select the target it does NOT send** (`CGC_ERROR model_not_selectable`, exit 3) — the tier truly isn't offered by this account/project. Override `--model Medium`/etc., `--model skip` to keep the current model, or `--allow-model-mismatch` to send anyway. `composer_not_ready`/`no_page_target` → tab not on ChatGPT or login lapsed; tell the user.
3. **Wait + auto-retrieve (detached — this is the efficiency win):**
   ```bash
   python3 ~/.claude/skills/chatgpt-consult/scripts/cdp_consult.py wait --rid <request_id> --conversation <conversation_id> \
     --out /tmp/cgc_answer_<request_id>.txt
   ```
   **No wrapper, and do not slice the wait.** Earlier versions of this file required `timeout 899` around every waiter and `--timeout 870` inside it, on the belief that an unbounded background task is blocked and a bounded one is killed at 900s. **Both were measured false:** an unbounded background waiter is accepted, and an unbounded 1000s task ran to completion. The wrapper only ever truncated healthy consults. Let the waiter wait.
   **Pin with `--conversation <conversation_id>` (from submit) AND pass the real `--rid`.** The conversation id selects the exact tab; the rid is then *verified* against that tab's `BEGIN_RESPONSE:<rid>` echo — mismatch → it errors `rid_mismatch` instead of returning the wrong request's answer. (`--rid auto` reads the rid off the pinned tab if you didn't keep it, but prefer passing both.) Without a conversation pin, multiple open tabs → `ambiguous_target` by design. This combination (own conversation + verified rid) makes cross-request collision impossible.
   Dispatch this with **`run_in_background: true`**. It polls the DOM every 20 s *with no agent context loaded*, and on completion writes the full answer (sentinels stripped, references intact, no 50k cap) and exits. Its exit re-invokes you = the wake. Do NOT `ScheduleWakeup` and do NOT poll yourself. `--timeout` defaults to `3600` — that is not a budget for the consult but the point past which the job is stuck rather than slow. Also: `--settle-seconds 300`, `--min-unwrapped 1500`. **Exit codes:** `0` = wrapped answer retrieved, OR a best-effort salvage at timeout (no wrapper but a substantial answer present → written to `--out` plus a sibling `<out>.raw`, logged `CGC_UNWRAPPED` — verify it isn't cut off before trusting it) · `4` = genuine no-answer timeout (nothing usable present) · `3` = blocker (login/captcha/rate-limit) · `2` = usage. See "sentinel_missing / CGC_UNWRAPPED / .raw" in [docs/TROUBLESHOOTING.md](../docs/TROUBLESHOOTING.md).
   - **`done` is SENTINEL-driven** — it fires when `END_RESPONSE:<rid>` appears after `BEGIN_RESPONSE:<rid>` in the answer node's text (read via `textContent`, so a backgrounded tab can't collapse it; not gated on the stop-button, which the ChatGPT UI can keep showing). A Thinking model streams short reasoning-summary stubs for a long time BEFORE the real answer — that is NOT the answer; the waiter keeps polling.
   - **Be patient — ~25 min is the NORMAL duration for a GPT-5.6 Pro consult (a huge 150-file PR review, longer), and ChatGPT may show `gen=False` with the answer not yet streamed.** The heartbeat `len` now tracks the LARGEST message of the CURRENT turn (not a trailing 1-char placeholder, the old "stuck at len=1" misread), so a growing or non-trivial `len`, or `gen=True`, means the model is alive — **do not kill the waiter.** (GPT-5.6 also pauses generation for several seconds mid-stream while its safeguard classifiers review output — another reason a brief stall is not a stall.) It keeps polling and returns when the answer lands; let it run to `--timeout` (raise it for very large reviews).
   - **If the model skips the wrapper** (it sometimes does on short follow-ups), the waiter takes the whole last message as the answer once it's ≥ `--min-unwrapped` chars (logs `CGC_UNWRAPPED` — **verify it isn't cut off** before trusting it). A small stable message is treated as a streaming stub, not a stall, so the waiter keeps waiting rather than quitting. At timeout it makes one last rescue grab of any substantial message present.
   - **If the wake never comes:** the re-invoke rides on THIS session staying alive — if the session is compacted/closed/idle behind other work, the notification can be lost. **The answer is still written to `--out` regardless.** The waiter logs a self-diagnosing heartbeat (`CGC_WAIT alive: gen/len/done/begin/end/ac`) to its task `.output` — `tail` it (`begin=true end=false` = the model wrote BEGIN but not END / still going; `end=true` = it landed). Any session can also self-check with `status --rid <id> --conversation <id> --out <answer_file>` or just `Read` the `--out` file once `CGC_DONE` appears. **Always pass `--out` to `status`** — once the waiter retrieves the answer it auto-closes the tab, so a bare `status` would error `conversation_not_found` (looks like a failure when the consult actually SUCCEEDED); with `--out` it instead reports `{"done":true,"retrieved":true,...}` off the answer file. Pass `--conversation` (not bare `--rid auto`) so it never attaches to the wrong tab. **Simplest reliable self-check: `Read` the `--out` file — non-empty = done.**
4. **Read the answer** with `Read /tmp/cgc_answer_<request_id>.txt` (local-file Read is unscanned and uncapped — the whole answer arrives in one read). Then treat it as advisory and verify locally (Roles section). On done, `wait` also prints a **`CGC_NEXT`** line with the exact commands to **continue this thread** — once you've verified and a question, disagreement, or next phase emerges, follow up rather than starting cold (see **Follow-up rounds**).

If Backend A is live, you never need the MCP fallback.

## Follow-up rounds — a consult is a thread, not a one-shot
The highest-value consults are a loop: get the answer → act locally → **report back into the same thread**. ChatGPT keeps the whole conversation's context and model, so a follow-up is cheap and lands deeper than a fresh consult. Follow up to: feed local test/verification results back ("your fix passed except this one DST case — real bug or test artifact?"), resolve a finding you couldn't reproduce, hand it the diff you applied for a re-check, or push to the next phase of a plan.

**In auto mode, a follow-up goes through the daemon too** (the direct `followup` below is the interactive-mode fallback — it drives chatgpt.com from your Bash call, which the classifier blocks in auto mode). Three local steps, continuing the SAME thread:
```bash
# render the follow-up prompt (note the new rid) …
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py prep --followup --task "<local results + next question>" --title "<what's new>"
# … enqueue it against the active thread, then await (LOCAL, detached):
python3 ~/.claude/skills/chatgpt-consult/scripts/cgc_spool.py enqueue --rid <r2> --kind followup --conversation auto --prompt-file <rendered_prompt_file>
python3 ~/.claude/skills/chatgpt-consult/scripts/cgc_spool.py await --rid <r2> --out /tmp/cgc/answer_<r2>.txt
```
`--conversation auto` continues the active thread; on done `await` prints this exact recipe again (`CGC_NEXT`). The interactive-mode direct form follows.

**Follow-up is now TWO commands — as easy as round 1 (submit + wait). Make it a reflex after every consult you act on.** `submit` records the live thread, so follow-up needs **zero bookkeeping**: no conversation id, no rid, no prompt file to track. **ALWAYS continue with `followup` — NEVER `prep`+`submit` again** (a fresh submit opens a NEW conversation and throws away ChatGPT's context; it is the #1 way "follow-up" silently fails). You do NOT need `--keep-tab` — the thread persists at `/c/<id>` and `followup`/`wait` re-open it automatically if the tab closed.

```bash
# Round 1 — submit + wait. (submit records the active thread; it also prints this exact line.)
python3 ~/.claude/skills/chatgpt-consult/scripts/cdp_consult.py wait --out /tmp/cgc_answer_<rid1>.txt --rid <rid1>
# … read the answer, apply it locally, run the tests/checks …

# Round 2 — ONE backgrounded command: renders + sends + WAITS for the answer, then exits (the
# exit is the wake). --watch folds the wait in, so there is NO separate wait step to forget or
# mis-arm. --conversation defaults to 'auto' (the active thread); --task is the only required field.
python3 ~/.claude/skills/chatgpt-consult/scripts/cdp_consult.py followup \
  --task "<local results + the next question>" \
  --title "<what's new>" \
  --watch --out /tmp/cgc_answer_<rid2>.txt \
  --context-file /tmp/cgc_localresults.md   # optional: what you ran, results, where reality diverged
#   → dispatch with run_in_background:true. On wake, Read /tmp/cgc_answer_<rid2>.txt.
```
That's the whole round — ONE backgrounded `followup --watch` call. (Without `--watch` it just sends and prints a `wait` line to run separately; the legacy `prep --followup` → `followup --prompt-file` two-step also still works.)

> **NEVER arm a bare `until [ -s <file> ]; do sleep …` file-watcher as the background task.** Nothing writes that answer file except a *running waiter* — `followup --watch` (or `wait`). A file-watcher with no waiter behind it spins until timeout and the answer never lands, which looks exactly like a "stuck waiter." The background task you launch must BE `followup --watch …` or `wait …`, never a file-existence poll.

**Steer the follow-up like round 1.** A follow-up is not a lighter ask — give `--task` a sharp framing that names the new bar and the specific sub-questions, and if you're asking for a new plan/design, tell it to challenge whether that approach is right. The follow-up template already carries the depth, finding-shape, and verify-locally mandate forward, so you supply only the delta + the local results (`--context-file`).

**When the user says "follow up till it flags nothing", RUN THE LOOP** — `followup --task` → run the printed `wait` → read → apply locally → `followup` again — on the same auto-resolved thread, until the answer is clean. Do not stop after one round and do not restart a new thread. **Cap: 3 rounds without user approval** UNLESS the user explicitly asked to loop till convergence (then keep going, reporting each round). Each round's answer is still advisory — verify locally before acting, same as round 1.

## Fixed configuration
- **ChatGPT project (all consults go here):** `$CGC_PROJECT_URL (your ChatGPT project, or a plain new chat)`
- **Model:** **Pro** (the `Pro`/`Pro Extended` switcher tier, which post-GA runs the **GPT-5.6** family, Sol-class) — `cdp_consult.py submit` confirms/sets it automatically (`--model` default `Pro`; any Pro tier satisfies it); check `modelConfirmed` in its output. Selection targets the switcher *label*, so it's robust to the underlying 5.x model version; if a 5.6 web build renames the tier, set `CGC_MODEL`.
- **Fresh conversation inside the project per consult** (keeps `get_page_text` ≈ one Q+A; inherits project instructions).
- **One deadline: 60 minutes** (`STUCK_AFTER_S`, the default `--timeout` on `enqueue`/`await`/`wait`/`followup`). A GPT-5.6 Pro round reasons ~25 min — that is how long the work TAKES, an expectation, and nothing is killed for reaching it. 60 min is the separate question of when waiting stops being explained by the work; past it the job is broken, so read its log instead of waiting again. There is no second clock: no wrapper, no slicing, no "still running" return.
- Other defaults: chunk target 32000 / hard 40000; wake plan from prep (`--expect-minutes` 25 → first wake ~21 min, then re-poll); max ~8 chunks and max 3 consult rounds without user approval.

## Safety boundary
Allowed: visible ChatGPT UI; visible browser actions; reading the answer via get_page_text; local repo reads/edits/tests.
Forbidden: hidden ChatGPT endpoints; bypassing login/CAPTCHA/rate limits; sending secrets/`.env`/keys/tokens or unrelated personal data; **gisting (even PUBLIC) or uploading content that is already on GitHub (deliver the link instead — see HARD RULE);** routing answer content through any channel other than get_page_text; letting ChatGPT decide final correctness; continuing past selector drift when an action is ambiguous.
Stop and tell the user on: login, CAPTCHA, rate limit, payment/account prompt, ambiguous confirmation, selector drift, extension disconnect.

## File delivery to ChatGPT (it has no local access)
ChatGPT cannot read the local repo, and **the agent CANNOT auto-upload local files** (`file_upload` is sandboxed to user-attached files — verified 3×). **If the code is on GitHub, hand ChatGPT links it browses — not a gist, not a paste.** (When a PUBLIC gist genuinely is the right path — unpushed or private-inaccessible content — see the HARD RULE above.) The full catalog of *every* injection method (whole `/pulls` list, `/pull/N`, `/pull/N/files`, `/commit`, `/tree@sha`, `/compare`, blob, line-anchored blob, issue, raw, PUBLIC gist, inline, search, actions) with when-to-use + failure modes, plus the high-signal file-selection rules, live in **[references/injection-and-prompting.md](references/injection-and-prompting.md)** — its §2 (URL catalog) and §9 (file selection) are the live value. Treat its §4 *prompt/output* templates as legacy (pre-GPT-5.6, process-heavy): the live `PROMPT_TEMPLATE` is authoritative, and §4 is useful only as raw material for an `--output-file` contract, not as a prompt to send.

**Prefer a link over an upload.** A GitHub link (`/pull/N`, `/pull/N/files`, `/compare`, `/tree@ref`, `/blob@ref`) carries the diff, intent, discussion, CI, and navigable surrounding code — far more than a single uploaded file or a PUBLIC gist. Upload/gist is only for unpushed/private state, and even then only PUBLIC or content you're comfortable sending externally.

**The link alone is usually enough — let ChatGPT explore.** It's ChatGPT with a browser: hand it the PR / repo / `/tree@sha` entry point and it navigates the repo itself — finds the imports, callers, tests, and related files on its own, often beyond what you'd think to list. So default to *just the entry-point link*. Enumerating the file set with `--files` narrows it to a closed list and is usually a **downgrade** (it reads as "look ONLY at these"); reserve `--files` for one specific hard-to-find needle the link wouldn't lead it to — never as the routine way to deliver code. The prompt already tells it these are starting points, not a whitelist.

**`deliver` is AGENT-DRIVEN — YOU choose the reference the task needs**, deliver just renders it grouped. Different tasks point at different things: another repo, `main`, a specific PR number, a compare range. Pass them explicitly:
```bash
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py deliver \
  [--repo owner/repo]   # a DIFFERENT repo than the local origin
  [--pr 123]            # specific PR  → /pull/123 + /pull/123/files
  [--ref main]          # branch/tag/SHA for /tree + /blob links (e.g. review current main)
  [--compare A...B]     # explicit range
  [--files src/x.ts ...] [--issues 42 ...] [--pulls]   # blobs / issues / open-PR list
```
Pick to match intent — e.g. "review PR #123 of acme/api" → `--repo acme/api --pr 123`; "audit `main` of this repo's auth dir" → `--ref main` (point the `--task` at the auth dir and let it browse there itself; add `--files` only for a needle); "which open PR is risky?" → `--pulls`. Prefer commit-pinned `--ref <sha>` for reproducibility (a 20-min consult can outlive a moving branch).

**`--pr N` delivers the PR link + `/pull/N/files` diff by default — that is the review surface.** Add **`--blobs`** only for a giant PR whose aggregate `.diff`/`.patch` won't render (those views error on hundreds of files/commits): it then appends per-file blob permalinks at the head SHA via `gh` (capped 25 + an overflow pointer to the `/files` tab). The prompt tells ChatGPT to switch routes if one fails, so per-file blobs are insurance, not the default.

**Auto-detect is only a convenience fallback:** with NO explicit target, deliver inspects the *local current branch* → `auto-pr` (open PR) / `auto-compare` (pushed) / `needs_gist` (unpushed). Don't rely on auto when the task is about a different repo, `main`, or a named PR — drive it. **PR-over-gist is ENFORCED in code, not just advice:** even `--ref <sha>` (no `--pr`) and the auto-detect gist-fallback resolve the commit's associated PR via `commits/<sha>/pulls` and lead with that public PR link (`out.associated_pr`) — a pushed commit that has a PR NEVER falls to gist.

Output JSON `{mode, slug, groups, refs_file, needs_gist, note, visibility}` (`visibility: "deferred"` — it makes no network call; the daemon's gate verifies); refs come grouped by **purpose** (Intent / Change set / Snapshot / Close-reading / Discovery / Fallback) — the grouping is what makes ChatGPT browse reliably. Pass `refs_file` to prep via `--refs-file` (OMIT `--context-file` when code is delivered by link). The template's default output is a review findings-list, so **reviews need `--output-file` least**; pass `--output-file` for the *generative/adjudicative* consults whose answer shape differs — a plan's ordered phases, a design's tradeoff matrix, a bug's ranked hypotheses (scenario contracts in the reference doc); add `--output-replace` when that spec defines its own findings shape/severity so it doesn't collide with the default contract. PUBLIC gist fallback: `gh gist create <files>` → `gh api gists/<id> --jq '.files[].raw_url'`; prefer the gist *page* over raw (browse collapses gist-raw newlines). Never paste whole files; never put secrets/`.env`/keys in a prompt, gist, link, or upload.

## Backend B — MCP fallback (zero setup, pays all four taxes)
When the debug profile can't be set up, drive the page through the Claude-in-Chrome MCP instead. The full fallback pipeline (prep `--backend mcp` → preflight → submit → ScheduleWakeup poll-wake → DOM-windowed retrieval → close the loop) lives in **[references/mcp-fallback.md](references/mcp-fallback.md)** — read it only on that path; it pays the page-CSP, scanner, 50k-cap, and per-wake-reload taxes the CDP backend avoids.

## Rules for the agent
- **Observe by script throughout** (this keeps the skill fully automated and cheap): read state with scanner-safe `javascript_tool` returns (metadata only) and answer content with `get_page_text`; locate and operate controls with `find` / `read_page` / `javascript_tool` (query the DOM, click via JS, read button text). If one control genuinely can't be driven by script (e.g. the model menu), ask the user to do that single step — a screenshot would burn image tokens and leave the step un-automated.
- For answer content, call `get_page_text` after `window.__cgcWin.show(i)`. Never ask `javascript_tool` to return answer content. `javascript_tool` = control plane (metadata only); `get_page_text` = content plane; scheduled polling = wake plane — never cross them.
