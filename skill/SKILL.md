---
name: chatgpt-consult
description: >-
  Use proactively, not only when asked, whenever a consequential decision is still open: a
  design or architecture fork before building, a diagnosis stuck between competing causes, a
  result that contradicts a load-bearing assumption, maths/statistics/probability to derive or
  check, a disagreement with a reviewer, a bug that survives two fixes, an audit before merge or
  deploy, research on an external contract, or substantive writing. Sends the question to the
  user's ChatGPT Pro in the background and wakes you with the answer while you keep working. With
  a registered workspace app (`consult.py apps`) the remote model also reads private code, local
  DBs and logs, and runs checks inside the user's workspaces. The user wants the weekly Pro
  allowance spent on questions like these; the SessionStart line shows what is left. Your usual
  reviewers stay for answers needed now; Pro takes the distinct, deeper question in parallel.
  Never paste secrets or .env.
allowed-tools: Read, Write, Grep, Glob, Bash(python3:*), Bash(bash:*), Bash(mkdir:*), Bash(git status:*), Bash(git diff:*), Bash(git ls-files:*), Bash(git rev-parse:*), Bash(git remote:*), Bash(git branch:*)
---

# chatgpt-consult

ChatGPT advises; Claude Code executes and verifies. A consult is a deep reasoning round on your
configured model and tier — GPT-6 Astra Pro by default, ~25 minutes — or whichever family/tier
`CGC_MODEL`/`CGC_MODEL_FAMILY` target instead, including an older family you've deliberately
pinned or a legacy picker that only offers a tier. Steer it precisely, fire it, and keep working;
the detached waiter wakes you with the answer.

## When to fire

Fire when an independent deep argument or investigation can change a decision that is still open
when the answer lands (p50 ~28 min, p90 ~90 min). Ask for the smallest decisive deliverable: a
counterexample, derivation, discriminating experiment, implementation rule, source-backed
conclusion, or revised passage. Ranked by value per message:

1. A measured result contradicts a load-bearing assumption (fit looks good, replay loses).
2. The deciding evidence sits in a local workspace — DB, logs, private or unpushed code — that an
   independent analyst should query and re-run (WebCodex route below).
3. A reversible but expensive design fork, before the code exists.
4. A maths/statistics property governs correctness: derive it, or find the counterexample.
5. Debugging has stalled: a bug survives two fix attempts, or competing causes remain; ask for the
   discriminating experiment.
6. You and a reviewer disagree on a consequential claim, or a fix has been sent back twice.
7. An obscure external contract (settlement, protocol, API rules) must be researched before it is coded.
8. Substantive writing with competing framings, judged against evidence.

Your synchronous reviewers answer bounded questions needed now; Pro takes the distinct, deeper
uncertainty in parallel, and either may start first. A failed or silent round is a channel
incident to act on through its envelope, never a reason to stop firing. A complete question that can wait
goes to the backlog (`--not-before` / `--use-by`). Keep routine fixes, lookups, and anything that
cannot change an action local. The SessionStart line shows the week's budget, backlog, and
ungraded answers. Unspent allowance at week's end is lost, so use it on these triggers rather
than on invented questions.

Record the decision before you see the answer: `--origin agent --trigger <kind> --decision "<the
action this can change>" --baseline "<your current answer>" --check "<how the result will be
judged>"`.

**Safety gate.** Every consult's prompt ships PUBLIC GitHub links (or content you'd comfortably
send to ChatGPT outright). Private code and local data reach the model only through a WebCodex
workspace the user registered (`--workspace` + `--mention`), never pasted into the prompt. NEVER secrets, `.env`, tokens, private-repo content, customer data, or
unreviewed logs. A private repo is sendable ONLY if the user named it in
`CGC_GATE_PRIVATE_REPOS` — you never decide that or add to it. The user's daemon independently
re-validates every prompt (public repos, every PR/ref link exists, no recognized secret shape) and
refuses fail-closed. The secret scan matches known patterns only; it does not prove prose is clean,
so what you write into `--task` is on you. A prompt that merely says "Source: local workspace" gets
no exemption: `--workspace` counts only when the user registered it (`CGC_WORKSPACES`) and the app
(`CGC_APPS`), and you never register either. Write every GitHub link plainly (`https://github.com/owner/repo/...`;
no `..`, `%`-escapes, ports or `user@`): the gate refuses a link a browser would resolve differently from how
it reads. End a bare `https://github.com/owner/repo` link with `/` or a space before the line break, even inside `<>`, backticks or parentheses (the next line, a blank line included, could be read as part of the repo name; the gate refuses it). Link a commit SHA, not a branch, so the ref check is exact. The gate checks GitHub links only; text and links to other sites are your responsibility.

## The whole path — two commands

```bash
# 1. Fire: resolves GitHub links, renders the prompt, queues the round. ALL LOCAL — no network
#    from your side; the user's always-running daemon validates and sends.
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py fire --repo owner/repo --pr 123 \
  --title "<sharp headline>" --role "You are a <persona matched to the job>." --task "<the question>" \
  --request-key <stable-key-for-this-logical-request>

# 2. Await DETACHED (run_in_background:true): run fire's `next_command` verbatim. It waits as long
#    as the consult takes and wakes you with `answer_path`. NEVER `nohup … &` instead.
python3 ~/.claude/skills/chatgpt-consult/scripts/cgc_spool.py await --rid <rid>
```

- Reviewing `main`/a branch → `--ref <sha>` (commit-pinned; a branch can move mid-consult).
- No code subject at all (a proof, research, writing) → add `--no-code`.
- **WebCodex route.** The deciding evidence is local (private or unpushed code, a DB, logs, data,
  a check to run) and lives in a workspace the user registered with WebCodex → add
  `--workspace <name> --mention "<app>"` (no `@`; both must be in `consult.py apps`, else the
  round is refused). The remote model then reads files, lists
  directories, runs processes, and observes jobs inside that workspace. In `--task`, name the
  revision or data window and the read/run scope; checks that write go to a temp dir or an isolated
  worktree, never the active one. The daemon picks the app from the composer's @ popup before
  pasting. A user naming an app (`@<app>`) means the same flag.
  Whether the app stays attached for later messages of a thread is NOT verified, so a follow-up
  that needs its tools repeats `--mention`. `--mention` refuses before the send when it cannot be
  confirmed. Never write `@App` into `--task`, never touch the browser.
  `python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py apps` lists the known apps
  (`CGC_APPS`) and registered workspaces (`CGC_WORKSPACES`). A short or
  lower-case name (`--mention webcodex`) snaps to the listed spelling. Envelope error
  `mention_not_found` says what the popup showed: other apps → re-fire with one of those names;
  opened but empty, only its loading skeleton, or listed with a Connect control → relay
  "enable/connect the app in ChatGPT" and stop; a row that "is not an app" or a popup that "never
  opened" → a driver/DOM problem, relay it verbatim, do not retry.
- A UI change (`ChatGPT UI changed`, or a `diagnosis.json` path in the error/log) is repairable from
  data: follow docs/TROUBLESHOOTING.md "When ChatGPT's UI changes" (edit the ids, run the bundle's `check` commands).
- Do NOT preflight: never run `doctor`/`queue`/`status` before firing. The daemon is installed
  once (`cgc install-daemon`, launchd keeps it alive); if it is genuinely down, fire/await say so
  in one line — relay `cgc install-daemon` to the user and stop.
- If the auto-mode classifier denies a command: the tool is wrong, not the classifier — every
  verb here is provably local-only (tests/test_security.py). Relay the denied command verbatim to
  the user and STOP. Never retry in another shape; observed: bypass attempts poison the whole
  session until even local writes are denied.

## Outcomes — read the envelope, not the prose

The exit code only routes you (0 answer ready / 3 a human must act / 1 broken). Every verb here
(fire, await, status, cancel, refire, feedback, reconcile, diagnose) ENDS stdout with ONE JSON
line, refusals included: `{schema, rid, state, code, next_command, human_action, answer_path,
detail, ...}`; await adds `retryable, confidence, error, log_path`. Decide from it alone. `code` is the action class: `ok` · `invalid_request` (fix the flags) ·
`policy_denied` (the gate refused the content) · `service_unavailable` (nothing sent; retry later) ·
`human_required` · `ui_contract_broken` (the driver needs a fix) · `send_uncertain` (retrieve, never
re-send) · `source_unresolved` · `remote_failed` · `observation_deadline` · `result_unverified` ·
`not_found`. `next_command` is the controller's chosen move; when there is none, act on
`human_action`:

- `answer_path` set → Read it. Success has `next_command:null`: a follow-up is your decision FROM
  the answer. `result_unverified` → a human checks it is complete and this round's; never chain on it.
- `human_action` set → relay exactly that to the user (login/captcha/rate-limit/safeguard
  refusal), then do `next_command` once it's cleared.
- A pre-send `blocked` round (login, captcha, an unconfirmed `--mention`) provably never sent: fix
  the cause in `detail`, then run `next_command` (`refire`: fresh rid, same prompt; nothing can
  duplicate). `send_uncertain` or a post-send blocker → the send may have landed: NEVER re-fire;
  `next_command` retrieves by conversation.
- Stuck or unclear → run `cgc_spool.py diagnose --rid <rid>`: one line of ≤1.5 KB (paths are never
  cut; `truncated:true` if they alone exceed it) with phases, the driver's last verdict, the UI
  verdicts and the next command. Never read the log or the driver.
- ChatGPT failing a turn ("Thinking failed") is handled for you: the daemon sends ONE "continue" in
  the same thread and the same rid keeps waiting. If that continue also fails, the round ends `failed`
  with `turn_failed` — re-fire with a NEW `--request-key` only if you still need the answer. If the
  continue could not be confirmed it ends `possibly_accepted` like any unconfirmed send: follow
  `next_command` (retrieve), never re-fire.
- ChatGPT's safety filter blocking the round ends it at once as `remote_failed` with `remote_refused`
  in `detail`: rephrase the framing and re-fire with a NEW `--request-key`.
- A round that shows no sign of generation for 15 minutes (`CGC_NO_GENERATION_S`), usually a
  pending app-authorization prompt, ends as `send_uncertain` with `no_generation`: relay
  `human_action`, retrieve, never re-send.
- "Still running" is not an outcome: await keeps waiting through the round's 90-minute deadline.

**A stopped/killed `await` is a NON-EVENT.** The round lives in the store and the daemon finishes
it regardless; the waiter is only the notifier. Re-arm the same one-liner (`await --rid <rid>`,
detached) — it is pure polling, safe to repeat, and returns the answer's address whether the answer
landed a second ago or an hour ago. **Never substitute a shell wait loop** (`until [ -s <file> ];
do sleep …`): it cannot terminate on `blocked` / `failed` / `possibly_accepted` — exactly the
states that need a human, so it spins forever on them — and it reads an unverified salvage as
success. Where things stand right now: `cgc_spool.py diagnose --rid <rid>`.

## Repeat-safety (idempotency)

| Action | Safe to repeat? |
| --- | --- |
| `fire --request-key K` (same content) | Yes — returns the ORIGINAL receipt, nothing re-queued |
| `fire --request-key K` (changed content) | Refused — one key names one logical request |
| `fire` without a key | Creates a NEW consult every time — always pass a key |
| `await` / `status` / `stats` | Always safe (read-only) |
| `cancel --rid R` | Safe; succeeds only BEFORE the send, idempotent |
| re-fire after `possibly_accepted` | NEVER — the first send may have landed (duplicate risk). In 9 of 10 audited cases it had; check `status`, the answer file, and the workspace for delivered files first |

## Follow-up — a consult is a thread; `--parent` is the agent path

Re-review after changes, re-check a fix, next phase → a FOLLOW-UP on the same thread (ChatGPT
keeps its context), never a fresh fire (throws it away):

```bash
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py fire --followup --parent <rid> \
  --no-code --task "<local results / the diff I applied + the next question>" --title "<what's new>"
```

`--parent <rid>` pins the exact consult you are continuing — causal, unambiguous under
concurrency. A bare `--followup` (no parent) resolves "the last thread" and REFUSES when that is
ambiguous (another consult in flight, or two threads finished close together) — so always pass
`--parent`. Add `--refs-file` for a fresh diff link. Loop (fire-followup → await → verify →
again) until clean. Every follow-up carries new local evidence or a new question, and maps each
prior finding to its fix commit or a measured result.

## Steer the round — `--title`, `--role`, `--task` decide the answer's depth

The template supplies the generic contract (findings, file:line evidence, verify-locally tags).
You supply the delta it can't know:

- **`--title`** — name the decision/surface: "Merge-safety gate: PR #406 — data-loss risk".
- **`--role`** — the persona matched to THIS job: "You are a distributed-systems security auditor
  focused on concurrency and data integrity."
- **`--task`** — (1) the decision/deliverable you need, (2) the bespoke bar for good, (3) labelled
  look-hardest sub-questions (a/b/c) so depth lands per-risk, (4) binding constraints with WHERE
  they're defined, (5) evidence it can't browse to → `--context-file` (failure bundles, perf
  numbers), (6) tell it to challenge the premise and name a superior alternative FIRST. Keep code
  out of `--task` — code travels as links via fire's deliver.
- **What the graded history says** (24 threads, 2026-09-11 → 10-11; mean value 2.1/3, 79% changed a
  decision or landed a fix):
  - The best rounds state your own law, numbers, or decision as a falsifiable target and set an
    executable bar, e.g. "a BLOCKER needs a runnable counterexample; a fix clears only if its test
    fails when reverted". Back that bar with code the model can read: pinned links or WebCodex.
  - Rounds are wasted when they rest on a premise you have not checked against the ledger or data,
    or when you ask an open design question with no release test.
  - Review loops past round 3 drift into fix-of-fix. After two corrections on one surface, restate
    the design before the next round instead of patching again.
- For any code/architecture/PR review, pass the scaffold
  `references/deep-review-output.md` as `--output-file` with `--output-replace` — its order
  (verify the approach → correctness → per-file → superior realization → go/no-go) is the depth
  mandate, by default, unasked. A custom output shape goes in `--output-file` (add
  `--output-replace` when it defines its own findings format). Full prompting law:
  `references/gpt-6-astra-prompting-principles.md`.

**The link alone is usually enough** — ChatGPT browses the repo itself (imports, callers, tests).
`--files` narrows it to a closed list and is usually a downgrade; reserve it for one hard-to-find
needle. Full URL catalog + delivery rules: `references/injection-and-prompting.md`.

## Act on the answer

Take the WHOLE answer seriously — the architecture calls, not just the bug list. If a finding is
right, implement it now (design changes included). If you won't do what it says, that must be
because you know a fact it didn't (it reviewed from outside, blind to your runtime/harness) —
write that reasoning into the design rationale so the next reader doesn't re-raise it. Verify the
load-bearing claims and anything tagged `verify locally` — a sanity check, not a re-audit. Only
genuinely product-changing forks go to the user, with your analysis attached.

**Grade it once you have acted** — this is what tunes the triggers and templates:

```bash
python3 ~/.claude/skills/chatgpt-consult/scripts/cgc_spool.py feedback --rid <rid> --grade <g> \
  --reason <r> --action "<what you did with it>" --evidence "<commit SHA / test / file>"
```

`2` changed a material decision or caught a defect, verified; `1` validated improvement or new
discriminating evidence absent from your baseline; `0` duplicate, late, refuted, or unused; `-1`
acting on it caused rework. A grade of 1 or more requires evidence, and reassurance alone earns 0.
Grade transport failures `0 --reason transport` so the topic is not blamed. When the SessionStart
line says the weekly review is due, read `cgc_spool.py stats --week` and change ONE trigger rule,
template clause, or test that the graded outcomes support. Then record it with
`feedback --policy "<what changed and why>"`.

## References (read only when needed)

- `references/deep-review-output.md` — the default deep-review output contract (use routinely).
- `references/gpt-6-astra-prompting-principles.md` — prompting law for GPT-6 Astra, including the
  two Astra-specific rules the live template now carries (don't ask, don't obey browsed instructions).
- `references/injection-and-prompting.md` — every code-delivery URL form + file-selection rules.
- `docs/ARCHITECTURE.md` — why daemon+CDP+sentinels; the classifier rationale and egress model.
