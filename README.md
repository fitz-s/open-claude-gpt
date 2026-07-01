# chatgpt-consult

[![CI](https://github.com/YOUR_USER/chatgpt-consult/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_USER/chatgpt-consult/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

**Use your ChatGPT Pro subscription *with* Claude Code — spend the plan you already pay for on planning, hard reasoning, and review, running in the background while Claude keeps working.**

`chatgpt-consult` is a [Claude Code](https://claude.com/claude-code) skill (also a standalone CLI) that turns a logged-in **ChatGPT Pro** tab into a background coprocessor for your local agent. Claude fires a self-contained job to ChatGPT — a plan, a hard reasoning problem, a code review — keeps doing local work, and is woken by a detached waiter when the full answer lands. **ChatGPT advises; Claude stays the source of truth and verifies every claim before acting on it.**

No API keys, no per-token bill: it drives the **ChatGPT web app you're already logged into**, through an external Chrome DevTools client. If you pay for ChatGPT Pro, this is how you put that subscription to work next to Claude.

---

## Why

You already pay for ChatGPT Pro and for Claude Code. This lets them work *together* instead of you copy-pasting between two tabs:

- **Spend the subscription, not an API budget.** It automates your real ChatGPT session — the same Pro plan you use in the browser. Nothing is billed per token.
- **Runs in the background.** Submit, keep coding, get woken on completion. The wait loop is a detached shell process holding zero agent context, so polling never reloads Claude's context.
- **A thread, not a one-shot.** Feed local verification results back and follow up in the same conversation — loop until the answer is clean.
- **Claude verifies.** Every claim ChatGPT returns is a hypothesis Claude checks locally (`verify locally: <check>` tags make this explicit) before anything is merged, shipped, or declared done.

## What to use it for

Three things ChatGPT Pro is worth reaching for while Claude drives:

### 🧭 Plan
Hand ChatGPT the goal + the repo link and get an ordered, critiqued plan back — phases, risks, the "is this even the right approach" check — before Claude writes a line. Then Claude executes and reports results back into the same thread.

### 🧠 Hard reasoning
Offload the self-contained hard part: a tricky algorithm, a proof or heavy calculation, a concurrency/ordering argument, a "which of these designs actually dominates" tradeoff. Claude keeps the main task moving while ChatGPT thinks.

### 🔎 Review
Ship a **public GitHub PR/tree link** and get a grounded, file-cited review — correctness, migration safety, concurrency, rollback — with a one-line verdict + confidence. Claude verifies each finding against the actual repo, then follows up until it flags nothing.

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

A **dedicated Chrome profile** is used because CDP is disallowed on Chrome's default profile (anti-cookie-theft, Chrome 136+). You log into ChatGPT there once; your normal Chrome is untouched.

## Auto model-selection (toggleable)

When ChatGPT auto-downgrades a chat to a lighter model, a consult silently gets a weaker answer. So this **picks your Pro tier before every send and fails closed if it can't** — you always get the model you meant to use.

It's a switch:

```bash
CGC_AUTO_MODEL=1                 # ON (default): select CGC_MODEL, fail closed if unavailable
CGC_MODEL="Pro Extended"         # which tier to target (set the strongest your plan has)
CGC_AUTO_MODEL=0                 # OFF: don't touch the picker, send on whatever is shown
```

Per-consult override: `--model "High"` or `--model skip`. On a plan you have but not `Pro Extended`, just set `CGC_MODEL` to it.

## Requirements

- **Python 3.8+** and [`websocket-client`](https://pypi.org/project/websocket-client/) (the CDP client — the only hard pip dep)
- **Google Chrome / Chromium / Microsoft Edge** (auto-detected on macOS + Linux; override with `CGC_CHROME`)
- **A ChatGPT account you log into by hand** — **Pro recommended** (that's the point: use the tier you pay for)
- **[`gh` CLI](https://cli.github.com)** (optional but recommended — `deliver` uses it to resolve PRs + repo visibility)

## Install

```bash
git clone https://github.com/YOUR_USER/chatgpt-consult
cd chatgpt-consult
./install.sh            # copies the skill into ~/.claude/skills/chatgpt-consult, checks deps, runs doctor
# or: ./install.sh --link   (symlink — edits in the clone go live; good for hacking)
```

Then start the dedicated debug Chrome and log into ChatGPT Pro **once**:

```bash
bin/cgc launch          # opens the dedicated Chrome; log into ChatGPT Pro there, leave it open
bin/cgc doctor --deep   # verify everything, including login state
```

See [docs/INSTALL.md](docs/INSTALL.md).

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

In Claude Code you don't run these by hand — the skill activates automatically and Claude orchestrates deliver → prep → submit → wait → verify → followup.

## Configuration

Everything host- or preference-specific is read from the environment — the public skill ships no hard-coded identity. Copy `.env.example` to `.env` and set what you want:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CGC_PROJECT_URL` | new chat | ChatGPT URL a fresh consult opens — set to *your* project to group consults |
| `CGC_AUTO_MODEL` | `1` | auto-pick the model tier before sending (fail closed) — on/off |
| `CGC_MODEL` | `Pro Extended` | which tier auto-select targets |
| `CGC_PORT` | `9333` | remote-debugging port for the dedicated Chrome |
| `CGC_PROFILE` | `~/.cgc-chrome` | dedicated Chrome profile dir |
| `CGC_CHROME` | auto-detect | explicit browser binary |
| `CGC_STATE_DIR` | `/tmp/cgc` | scratch dir for prompt/refs/answer files |

Full reference + prompt customization: [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Security model

- **The agent never handles your credentials.** You log into the dedicated Chrome by hand, once; the session persists in that profile.
- **Ordinary browser automation against your own logged-in session** — it reads only the ChatGPT answer text, never cookies or cross-site data, and writes the answer to a local file you own.
- **Never send secrets.** The skill refuses to fabricate a code source and is built to ship *links to already-public code*, not to exfiltrate private content. Don't put `.env`, keys, or tokens in a prompt, gist, or context file.
- **ChatGPT is advisory.** Claude verifies every claim before merging, shipping, or declaring done.

Details + threat model: [docs/SECURITY.md](docs/SECURITY.md).

## Troubleshooting

`bin/cgc doctor` diagnoses most issues (deps, browser, port, login, scratch, config). Common fixes: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Run `bin/cgc doctor` and the parse checks before opening a PR.

## License

[MIT](LICENSE).

---

*Not affiliated with OpenAI or Anthropic. "ChatGPT" and "Claude" are trademarks of their respective owners. This tool automates a browser session you have already authenticated — use it in accordance with the terms of the services you use.*
