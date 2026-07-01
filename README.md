# Open Claude GPT

> **`open-claude-gpt`** — use your **ChatGPT Pro** subscription *with* **Claude Code**. Open-source bridge between the two: Claude drives, ChatGPT thinks in the background.
>
> Put the frontier reasoning model you **already pay for** — **ChatGPT Pro** (the Pro not xhigh) — to work inside your Claude Code workflow.

[![CI](https://github.com/fitz-s/open-claude-gpt/actions/workflows/ci.yml/badge.svg)](https://github.com/fitz-s/open-claude-gpt/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

`open-claude-gpt` is a [Claude Code](https://claude.com/claude-code) skill (also a standalone CLI, `cgc`) that turns a logged-in **ChatGPT Pro** tab into a background coprocessor for your local agent. Claude fires a self-contained job to ChatGPT — a plan, a hard reasoning problem, a code review — keeps doing local work, and is woken by a background waiter when the full answer lands.

Three jobs it's good for: **planning** (an ordered, critiqued plan before Claude writes a line), **hard reasoning** (a tricky algorithm, proof, or tradeoff analysis offloaded while Claude keeps moving), and **grounded review** (a file-cited review of a public PR/tree link). ChatGPT does the deep thinking; Claude puts it to work — a quick sanity-check on the load-bearing parts before shipping, not a line-by-line re-audit.

It drives the **ChatGPT web app you're already logged into** — not the OpenAI API, not Codex — so there's no API key and no per-token API bill; your normal ChatGPT web-plan limits and availability still apply.

Send **public links, not secrets** — you log into the dedicated Chrome by hand, once, and the tool never handles your password or API keys.

Think of it as a **background "ultra-everything"** for Claude Code: the same idea as its built-in `ultra-review` / `ultra-plan`, but powered by your ChatGPT Pro session and — crucially — run **in the background**, so Claude keeps executing locally while a long Pro reasoning run lands, instead of blocking on it. And it's not review-or-plan only: any deep, self-contained job qualifies (plan, hard reasoning, review, investigation, research, design, audit).

---

## Names you'll see

| Name | What it is |
| --- | --- |
| **Open Claude GPT** | the project |
| **cgc** | the CLI |
| **CGC_*** | environment variables that configure it |
| **consult** | one background ChatGPT job (plan / hard reasoning / review / etc.) |
| **CDP** | Chrome DevTools Protocol — drives the dedicated Chrome tab |
| **waiter** | the background process that waits for the wrapped answer |
| **sentinel** | the `BEGIN_RESPONSE` / `END_RESPONSE` wrapper marking a complete answer |

## Why

You already pay for ChatGPT Pro and for Claude Code. This lets them work *together* instead of you copy-pasting between two tabs:

- **Spend the subscription, not an API budget.** It automates your real ChatGPT session — the same Pro plan you use in the browser. Nothing is billed per token.
- **Web app, not API.** Driving the web app (not the API, not Codex) means no per-token API bill — your ChatGPT web-plan limits and availability apply, same as using ChatGPT by hand.
- **Runs in the background.** Submit, keep coding, get woken on completion. The wait loop is a detached shell process holding zero agent context, so polling never reloads Claude's context.
- **A thread, not a one-shot.** Feed local verification results back and follow up in the same conversation — loop until the answer is clean.
- **High-value output — lean on it.** The consult does real reasoning worth acting on; treat it as a strong collaborator, not a suggestion box. Claude gives the load-bearing claims a quick local look (the `verify locally:` tags point at what's worth a glance) before shipping — not a per-claim re-audit.

## What to use it for

Three things ChatGPT Pro is worth reaching for while Claude drives:

### 🧭 Plan
Hand ChatGPT the goal + the repo link and get an ordered, critiqued plan back — phases, risks, the "is this even the right approach" check — before Claude writes a line. Then Claude executes and reports results back into the same thread.

### 🧠 Hard reasoning
Offload the self-contained hard part: a tricky algorithm, a proof or heavy calculation, a concurrency/ordering argument, a "which of these designs actually dominates" tradeoff. Claude keeps the main task moving while ChatGPT thinks.

### 🔎 Review
Ship a **public GitHub PR/tree link** and get a grounded, file-cited review — correctness, migration safety, concurrency, rollback — with a one-line verdict + confidence. Claude acts on the findings, spot-checks the load-bearing ones, and follows up until it flags nothing.

(Plus: investigation, research-backed decisions, RFC/design docs, long-context consistency audits, a devil's-advocate before an irreversible action, a draft-while-you-build second pair of eyes.)

## How it works

```
 Claude Code ──deliver──▶ GitHub links (PR/tree/blob)         (consult.py)
     │        ──prep─────▶ rendered prompt + sentinels
     │        ──submit───▶ ┌──────────────────────────┐
     │                     │  external CDP client       │──▶ your logged-in
     │                     │  (websocket-client)        │     ChatGPT Pro tab
     │        ◀──wait──────│  detached poller           │◀── ChatGPT answer
     ▼                                                        between BEGIN/END sentinels
 verify locally ──followup──▶ (same thread, next round)      (cdp_consult.py)
```

- **`consult.py deliver`** — resolves GitHub references (repo visibility, PR association, commit-pinned tree/blob links) into a grouped refs file.
- **`consult.py prep`** — renders the outgoing prompt from a template, wrapping the answer in `BEGIN_RESPONSE:<rid>` / `END_RESPONSE:<rid>` sentinels so completion is unambiguous.
- **`cdp_consult.py submit` / `followup` / `wait` / `status`** — the CDP control plane: open a chat, (optionally) select the model tier, type + send, and poll to completion in a detached process.

The waiter is bounded so a background task can't hang: default ~15 minutes, tunable — see [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for the full timeout math and how to raise it for a big review.

A **dedicated Chrome profile** is used because CDP is disallowed on Chrome's default profile (anti-cookie-theft, Chrome 136+). You log into ChatGPT there once; your normal Chrome is untouched.

## Auto model-selection (toggleable)

When ChatGPT auto-downgrades a chat to a lighter model, a consult silently gets a weaker answer. So this **picks your Pro tier before every send and fails closed if it can't** — you always get the model you meant to use.

It's a switch:

```bash
CGC_AUTO_MODEL=1                 # ON (default): select CGC_MODEL, fail closed if unavailable
CGC_MODEL="Pro"         # which tier to target (set the strongest your plan has)
CGC_AUTO_MODEL=0                 # OFF: don't touch the picker, send on whatever is shown
```

Per-consult override: `--model "High"` or `--model skip`. On a plan you have but not `Pro`, just set `CGC_MODEL` to it.

## Requirements

- **Python 3.8+** and [`websocket-client`](https://pypi.org/project/websocket-client/) (the CDP client — the only hard pip dep)
- **Google Chrome / Chromium / Microsoft Edge** (auto-detected on macOS + Linux; override with `CGC_CHROME`)
- **A ChatGPT account you log into by hand** — **Pro recommended** (that's the point: use the tier you pay for)
- **[`gh` CLI](https://cli.github.com)** (optional but recommended — `deliver` uses it to resolve PRs + repo visibility)

## Install

```bash
git clone https://github.com/fitz-s/open-claude-gpt
cd open-claude-gpt
./install.sh            # copies the skill into ~/.claude/skills/open-claude-gpt, checks deps, runs doctor
# or: ./install.sh --link   (symlink — edits in the clone go live; good for hacking)
```

Then start the dedicated debug Chrome and log into ChatGPT Pro **once**:

```bash
bin/cgc launch          # opens the dedicated Chrome; log into ChatGPT Pro there, leave it open
bin/cgc doctor --deep   # verify everything, including login state
```

See [docs/INSTALL.md](docs/INSTALL.md).

## First consult

The canonical path — deliver → prep → submit → wait — end to end:

```bash
# 1. Resolve GitHub refs into a grouped refs file, and capture its path from the JSON output
REFS_FILE="$(bin/cgc deliver --repo owner/repo --ref main | python3 -c 'import json,sys; print(json.load(sys.stdin)["refs_file"])')"

# 2. Render the outgoing prompt from a template, using that refs file
bin/cgc prep --refs-file "$REFS_FILE"

# 3. Submit the prompt to your logged-in ChatGPT Pro tab
bin/cgc submit

# 4. Copy the exact waiter command that `submit` prints, and run it to wait for the answer
```

Durable answers land in `./cgc_answers/answer_<RID>.txt`; `/tmp/cgc` (`CGC_STATE_DIR`) is tool-owned scratch only.

## Examples

Runnable walkthroughs in [examples/](examples/):

| Use | Example |
| --- | --- |
| 🔎 Review a PR | [examples/review-a-pr.md](examples/review-a-pr.md) |
| 🧭 Plan a feature before building | [examples/plan-a-feature.md](examples/plan-a-feature.md) |
| 🧠 Hard reasoning / a proof | [examples/hard-reasoning.md](examples/hard-reasoning.md) |
| 🏗 Weigh an architecture decision | [examples/architecture-decision.md](examples/architecture-decision.md) |
| 🔬 Research-backed decision | [examples/research-decision.md](examples/research-decision.md) |
| 🐞 Second opinion on a stuck bug | [examples/debug-second-opinion.md](examples/debug-second-opinion.md) |
| 💬 Toggle the model / group in a project | [examples/config-recipes.md](examples/config-recipes.md) |

In Claude Code you don't run these by hand — the skill activates automatically and Claude orchestrates deliver → prep → submit → wait → verify → followup. Copyable `--output-file` / `--output-replace` contracts live in [examples/_specs/](examples/_specs/).

## Configuration

Everything host- or preference-specific is read from the environment — the public skill ships no hard-coded identity. Copy `.env.example` to `.env` and set what you want:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CGC_PROJECT_URL` | new chat | ChatGPT URL a fresh consult opens — set to *your* project to group consults |
| `CGC_AUTO_MODEL` | `1` | auto-pick the model tier before sending (fail closed) — on/off |
| `CGC_MODEL` | `Pro` | which tier auto-select targets |
| `CGC_PORT` | `9333` | remote-debugging port for the dedicated Chrome |
| `CGC_PROFILE` | `~/.cgc-chrome` | dedicated Chrome profile dir |
| `CGC_CHROME` | auto-detect | explicit browser binary |
| `CGC_STATE_DIR` | `/tmp/cgc` | scratch dir for prompt/refs/answer files |

Full reference + prompt customization: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Security model

- **The agent never handles your credentials.** You log into the dedicated Chrome by hand, once; the session persists in that profile.
- **Ordinary browser automation against your own logged-in session** — it reads only the ChatGPT answer text, never cookies or cross-site data, and writes the answer to a local file you own.
- **Never send secrets.** The skill refuses to fabricate a code source and is built to ship *links to already-public code*, not to exfiltrate private content. Don't put `.env`, keys, or tokens in a prompt, gist, or context file.
- **ChatGPT is a strong advisor.** Claude leans on its reasoning and spot-checks the load-bearing parts before shipping — no line-by-line re-audit.

Details + threat model: [docs/SECURITY.md](docs/SECURITY.md).

## Troubleshooting

`bin/cgc doctor` diagnoses most issues (deps, browser, port, login, scratch, config). Common fixes: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Run `bin/cgc doctor` and the parse checks before opening a PR.

## License

[MIT](LICENSE).

---

*Not affiliated with OpenAI or Anthropic. "ChatGPT" and "Claude" are trademarks of their respective owners. This tool automates a browser session you have already authenticated — use it in accordance with the terms of the services you use.*
