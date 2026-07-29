---
name: chatgpt-consult
description: >-
  Offload a deep, self-contained job — code review, architecture/planning, hard reasoning,
  research, a second opinion, an audit — to the user's ChatGPT Pro subscription in the background,
  keep working locally, and get woken with the full result. Use when the job is worth ~25 min of
  frontier reasoning AND you have other work meanwhile. Do NOT use for anything answerable locally
  in minutes, for routine checks, or when the answer is needed immediately. Inputs must be PUBLIC
  GitHub links or content safe to send externally — never secrets/`.env`/keys/private code.
allowed-tools: Read, Write, Grep, Glob, Bash(python3:*), Bash(bash:*), Bash(mkdir:*), Bash(git status:*), Bash(git diff:*), Bash(git ls-files:*), Bash(git rev-parse:*), Bash(git remote:*), Bash(git branch:*)
---

# chatgpt-consult

ChatGPT advises; Claude Code executes and verifies. A consult is a ~25-minute GPT-5.6 Pro
reasoning round: only send work worth that, steer it precisely, fire it, and keep working — the
detached waiter wakes you with the answer.

**Safety gate.** Every consult ships PUBLIC GitHub links (or content you'd comfortably send to
ChatGPT outright). NEVER secrets, `.env`, tokens, private-repo content, customer data, or
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
- `human_action` set → relay exactly that to the user (login/captcha/rate-limit/safeguard
  refusal), then do `next_command` once it's cleared.
- `retryable:true` → re-run the SAME fire (use the same `--request-key`). `retryable:false` with
  state `possibly_accepted` → the send may have landed: NEVER re-fire; follow `next_command`
  (retrieve by conversation).
- Nothing useful → `log_path` is the full send+wait transcript; read it before guessing.
- "Still running" is not an outcome — a round takes ~25 min and await just keeps waiting. It gives
  up only after 60+ min, which means broken, not slow.

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
| re-fire after `possibly_accepted` | NEVER — the first send may have landed (duplicate risk) |

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
again) until clean when asked; cap 3 rounds without user approval unless told otherwise.

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
- For any code/architecture/PR review, pass the scaffold
  `references/deep-review-output.md` as `--output-file` with `--output-replace` — its order
  (verify the approach → correctness → per-file → superior realization → go/no-go) is the depth
  mandate, by default, unasked. A custom output shape goes in `--output-file` (add
  `--output-replace` when it defines its own findings format). Full prompting law:
  `references/gpt-5.6-prompting-principles.md`.

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

## References (read only when needed)

- `references/deep-review-output.md` — the default deep-review output contract (use routinely).
- `references/gpt-5.6-prompting-principles.md` — prompting law for 5.6-class models.
- `references/injection-and-prompting.md` — every code-delivery URL form + file-selection rules.
- `references/mcp-fallback.md` — LAST-RESORT backend when the CDP debug profile cannot exist;
  also documents the platform constraints (CSP, scanner, 50k cap) that shaped this design.
- `docs/ARCHITECTURE.md` — why daemon+CDP+sentinels; the classifier rationale and egress model.
