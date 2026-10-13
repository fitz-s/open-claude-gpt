---
name: chatgpt-consult
description: >-
  Use proactively, not only when asked, whenever a consequential decision is still open: a
  design or architecture fork before building, a diagnosis stuck between competing causes, a
  result that contradicts a load-bearing assumption, maths/statistics/probability to derive or
  check, a disagreement with gpt-sol or a reviewer, an audit before merge or deploy, research on
  an external contract, or substantive writing. Sends the question to the user's ChatGPT Pro in
  the background and wakes you with the answer while you keep working. With --mention "WebCodex
  Demo" the remote model also reads private code, local DBs and logs, and runs checks inside the
  user's registered workspaces. The user wants the weekly Pro allowance spent on questions like
  these; the SessionStart line shows what is left. gpt-sol stays the synchronous reviewer for
  answers needed now; Pro takes the distinct, deeper question. Never paste secrets or .env.
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
5. Debugging has stalled between competing causes; ask for the discriminating experiment.
6. You and gpt-sol (or a reviewer) disagree on a consequential claim.
7. An obscure external contract (settlement, protocol, API rules) must be researched before it is coded.
8. Substantive writing with competing framings, judged against evidence.

gpt-sol is the synchronous standing reviewer and answers bounded questions needed now; Pro takes
the distinct, deeper uncertainty, and either may start first. A complete question that can wait
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
re-validates every prompt (public repos, existing refs, no secret shapes) and refuses fail-closed.

## The whole path — two commands

```bash
# 1. Fire: resolves GitHub links, renders the prompt, queues the round. ALL LOCAL — no network
#    from your side; the user's always-running daemon validates and sends.
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py fire --repo owner/repo --pr 123 \
  --title "<sharp headline>" --role "You are a <persona matched to the job>." --task "<the question>" \
  --request-key <stable-key-for-this-logical-request>

# 2. Await DETACHED (run_in_background:true) — copy the exact line fire prints. It waits as long
#    as the consult takes and wakes you with the answer path. NEVER `nohup … &` instead.
#    The rid is the whole handle: the answer's path was chosen when the round was created and is
#    recorded on it, so await reads it back and PRINTS it. You never pass or track a path.
python3 ~/.claude/skills/chatgpt-consult/scripts/cgc_spool.py await --rid <rid>
```

- Reviewing `main`/a branch → `--ref <sha>` (commit-pinned; a branch can move mid-consult).
- No code subject at all (a proof, research, writing) → add `--no-code`.
- **WebCodex route.** The deciding evidence is local (private or unpushed code, a DB, logs, data,
  a check to run) and lives in a workspace the user registered with WebCodex → add
  `--workspace <name> --mention "WebCodex Demo"` (no `@`). The remote model then reads files, lists
  directories, runs processes, and observes jobs inside that workspace. In `--task`, name the
  revision or data window and the read/run scope; checks that write go to a temp dir or an isolated
  worktree, never the active one. The daemon picks the app from the composer's @ popup before
  pasting. A user naming an app (`@WebCodex Demo`) means the same flag.
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
- Do NOT preflight: never run `doctor`/`queue`/`status` before firing. The daemon is installed
  once (`cgc install-daemon`, launchd keeps it alive); if it is genuinely down, fire/await say so
  in one line — relay `cgc install-daemon` to the user and stop.
- If the auto-mode classifier denies a command: the tool is wrong, not the classifier — every
  verb here is provably local-only (tests/test_security.py). Relay the denied command verbatim to
  the user and STOP. Never retry in another shape; observed: bypass attempts poison the whole
  session until even local writes are denied.

## Outcomes — read the envelope, not the prose

The exit code only routes you (0 answer ready / 3 a human must act / 1 broken); the RETURN is an
address. `await`'s LAST stdout line is a JSON envelope — `{schema:1, rid, parent_rid, state,
retryable, human_action, next_command, answer_path, log_path, confidence, error}` — and every
outcome names the artifact it produced. Act on the fields, never on the code alone:

- `answer_path` set → Read it. `confidence:"unverified"` → a human verifies the answer is complete
  and belongs to this round before you act on it; never auto-chain a follow-up on it.
- Success carries `next_command:null` by design: a follow-up is a decision you make FROM the
  answer, never a step the envelope proposes.
- `human_action` set → relay exactly that to the user (login/captcha/rate-limit/safeguard
  refusal), then do `next_command` once it's cleared.
- A `blocked` round marked `(pre-send)` (login, captcha, a `--mention` that could not be confirmed) was
  proven never sent by the driver's own refusal. Fix the cause in `error`, then run its `next_command`,
  `cgc_spool.py refire --rid <rid>` (a fresh rid, the same prompt and spec): nothing can duplicate. Never
  retrieve it, there is no answer to find. Otherwise `retryable:true` → re-run the SAME fire (same
  `--request-key`). `retryable:false` with state `possibly_accepted` (or a `(post-send)` blocker) → the
  send may have landed: NEVER re-fire; follow `next_command` (retrieve by conversation).
- Nothing useful → `log_path` is the full send+wait transcript; read it before guessing.
- ChatGPT failing a turn ("Thinking failed") is handled for you: the daemon sends ONE "continue" in
  the same thread and the same rid keeps waiting. If that continue also fails, the round ends `failed`
  with `turn_failed` — re-fire with a NEW `--request-key` only if you still need the answer. If the
  continue could not be confirmed it ends `possibly_accepted` like any unconfirmed send: follow
  `next_command` (retrieve), never re-fire.
- "Still running" is not an outcome: await keeps waiting through the round's 90-minute deadline.

**A stopped/killed `await` is a NON-EVENT.** The round lives in the store and the daemon finishes
it regardless; the waiter is only the notifier. Re-arm the same one-liner (`await --rid <rid>`,
detached) — it is pure polling, safe to repeat, and returns the answer's address whether the answer
landed a second ago or an hour ago. **Never substitute a shell wait loop** (`until [ -s <file> ];
do sleep …`): it cannot terminate on `blocked` / `failed` / `possibly_accepted` — exactly the
states that need a human, so it spins forever on them — and it reads an unverified salvage as
success. If you want to know where things stand right now, `cgc_spool.py status --rid <rid>` gives
you `state`, `out_path`, `answer_on_disk`, and `conversation` in one read.

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
