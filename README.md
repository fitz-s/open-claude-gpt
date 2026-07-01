# Open Claude GPT

> **Use the ChatGPT Pro subscription you already pay for — inside Claude Code.**
> Claude drives locally; a logged-in ChatGPT Pro tab plans, reasons, and reviews in the background. No API key, no per-token bill.

[![CI](https://github.com/fitz-s/open-claude-gpt/actions/workflows/ci.yml/badge.svg)](https://github.com/fitz-s/open-claude-gpt/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)

`open-claude-gpt` is a [Claude Code](https://claude.com/claude-code) skill (and a standalone CLI, `cgc`) that turns a logged-in **ChatGPT Pro** tab into a background coprocessor for your local agent. Claude fires off a self-contained job — a plan, a hard reasoning problem, a code review — keeps working locally, and a detached waiter wakes it when the full answer lands, ready to verify. It drives the **web app you're already logged into** — not the OpenAI API, not Codex — so there's no key and no per-token bill.

## What it's for

Three jobs the Pro tier is worth reaching for while Claude drives:

### 🧭 Plan
Hand ChatGPT the goal + the repo link; get an ordered, critiqued plan — phases, risks, the *"is this even the right approach"* check — **before Claude writes a line**. Claude then executes and reports back into the same thread.

### 🧠 Hard reasoning
Offload the self-contained hard part — a tricky algorithm, a proof or heavy calculation, a concurrency argument, a *"which design actually dominates"* tradeoff — while Claude keeps the main task moving.

### 🔎 Review
Ship a **public GitHub PR/tree link**; get a grounded, file-cited review — correctness, migration safety, concurrency, rollback — with a one-line verdict + confidence. Claude acts on the findings and follows up until it flags nothing.

> *Also: investigation, research-backed decisions, RFC/design docs, long-context consistency audits, a devil's-advocate before an irreversible action, a draft-while-you-build second pair of eyes — any deep, self-contained job qualifies.*

## Why it's different

- **Spend the subscription, not an API budget.** It automates your real ChatGPT session — the frontier **Pro tier** you already pay for (the Pro model itself, not just a higher effort setting). Nothing is billed per token; your normal web-plan limits and availability apply.
- **Background "ultra-everything".** The same idea as Claude Code's built-in `ultra-review` / `ultra-plan`, but powered by your ChatGPT Pro session and run **in the background** — Claude keeps executing locally while a long Pro reasoning run lands, instead of blocking on it.
- **A thread, not a one-shot.** Feed local verification results back and follow up in the same conversation — loop until the answer is clean.
- **Lean on it, then verify.** The consult does real reasoning worth acting on. Claude gives the load-bearing claims a quick local look (the `verify locally:` tags point at what's worth a glance) before shipping — not a per-claim re-audit.
- **Public links, not secrets.** You log into the dedicated Chrome by hand, once; the tool never handles your password or API keys, and ships *links to already-public code*, never private content.

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

- **`deliver`** — resolves GitHub references (repo visibility, PR association, commit-pinned tree/blob links) into a grouped refs file.
- **`prep`** — renders the prompt from a template, wrapping the answer in `BEGIN_RESPONSE:<rid>` / `END_RESPONSE:<rid>` **sentinels** so completion is unambiguous.
- **`submit` / `followup` / `wait` / `status`** — the CDP control plane: open a chat, (optionally) select the model tier, type + send, and poll to completion in a **detached process** that holds zero agent context.

The waiter is bounded so a background task can't hang (default ~15 min, tunable). A **dedicated Chrome profile** is used because CDP is disallowed on Chrome's default profile (anti-cookie-theft, Chrome 136+) and is launched **loopback-bound** — you log into ChatGPT there once; your normal Chrome is untouched.

## Requirements

- **Python 3.8+** and [`websocket-client`](https://pypi.org/project/websocket-client/) — the CDP client, the only hard pip dep
- **Google Chrome / Chromium / Microsoft Edge** — auto-detected on macOS + Linux; override with `CGC_CHROME`
- **A ChatGPT account you log into by hand** — **Pro recommended** (that's the point: use the tier you pay for)
- **[`gh` CLI](https://cli.github.com)** — optional but recommended; `deliver` uses it to resolve PRs + repo visibility

## Install

```bash
git clone https://github.com/fitz-s/open-claude-gpt
cd open-claude-gpt
./install.sh            # copy the skill into ~/.claude/skills, check deps, run doctor
# or: ./install.sh --link   (symlink — edits in the clone go live; good for hacking)
```

Then start the dedicated debug Chrome and log into ChatGPT Pro **once**:

```bash
bin/cgc launch          # opens the dedicated Chrome; log into ChatGPT Pro there, leave it open
bin/cgc doctor --deep   # verify everything, including login state
```

In Claude Code the skill activates automatically — Claude reads its `SKILL.md` and invokes it when a task fits. See [docs/INSTALL.md](docs/INSTALL.md) for on-demand vs. proactive activation.

## First consult

The canonical path — deliver → prep → submit → wait — end to end by hand:

```bash
# 1. Resolve GitHub refs into a grouped refs file, and capture its path
REFS_FILE="$(bin/cgc deliver --repo owner/repo --ref main | python3 -c 'import json,sys; print(json.load(sys.stdin)["refs_file"])')"

# 2. Render the outgoing prompt from that refs file
bin/cgc prep --refs-file "$REFS_FILE"

# 3. Submit the prompt to your logged-in ChatGPT Pro tab
bin/cgc submit

# 4. Run the exact waiter command that `submit` prints, to wait for the answer
```

Durable answers land in `./cgc_answers/answer_<RID>.txt`; `/tmp/cgc` (`CGC_STATE_DIR`) is tool-owned scratch only. In Claude Code you don't run these by hand — the skill orchestrates the whole arc for you.

## Examples

Runnable walkthroughs in [examples/](examples/):

| Use | Example |
| --- | --- |
| 🔎 Review a PR | [review-a-pr.md](examples/review-a-pr.md) |
| 🧭 Plan a feature before building | [plan-a-feature.md](examples/plan-a-feature.md) |
| 🧠 Hard reasoning / a proof | [hard-reasoning.md](examples/hard-reasoning.md) |
| 🏗 Weigh an architecture decision | [architecture-decision.md](examples/architecture-decision.md) |
| 🔬 Research-backed decision | [research-decision.md](examples/research-decision.md) |
| 🐞 Second opinion on a stuck bug | [debug-second-opinion.md](examples/debug-second-opinion.md) |
| 💬 Toggle the model / group in a project | [config-recipes.md](examples/config-recipes.md) |

Copyable `--output-file` / `--output-replace` contracts live in [examples/_specs/](examples/_specs/).

## Auto model-selection (toggleable)

When ChatGPT silently auto-downgrades a chat to a lighter model, a consult gets a weaker answer for free. So the tool **picks your Pro tier before every send and fails closed if it can't** — you always get the model you meant to use.

```bash
CGC_AUTO_MODEL=1        # ON (default): select CGC_MODEL, fail closed if unavailable
CGC_MODEL="Pro"         # which tier to target (set the strongest your plan has)
CGC_AUTO_MODEL=0        # OFF: don't touch the picker, send on whatever is shown
```

Per-consult override: `--model "High"` or `--model skip`.

## Configuration

Everything host- or preference-specific is read from the environment — the public skill ships **no hard-coded identity**. Copy `.env.example` to `.env` and set what you want:

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
- **Ordinary automation of your own logged-in session** — it reads only the ChatGPT answer text, never cookies or cross-site data, and writes the answer to a local file you own. Remote debugging is **loopback-bound and verified by the doctor**.
- **Never send secrets.** The skill refuses to fabricate a code source and is built to ship *links to already-public code*, not to exfiltrate private content. Don't put `.env`, keys, or tokens in a prompt, gist, or context file.
- **ChatGPT advises; Claude verifies.** Claude leans on its reasoning and spot-checks the load-bearing parts before shipping — no line-by-line re-audit, but never merge/ship on its word alone.

Details + threat model: [docs/SECURITY.md](docs/SECURITY.md).

## Troubleshooting

`bin/cgc doctor` diagnoses most issues (deps, browser, port, login, scratch, config). Common fixes: [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Glossary

| Name | What it is |
| --- | --- |
| **Open Claude GPT** | the project |
| **cgc** | the CLI |
| **CGC_*** | environment variables that configure it |
| **consult** | one background ChatGPT job (plan / hard reasoning / review / etc.) |
| **CDP** | Chrome DevTools Protocol — drives the dedicated Chrome tab |
| **waiter** | the detached background process that waits for the wrapped answer |
| **sentinel** | the `BEGIN_RESPONSE` / `END_RESPONSE` wrapper marking a complete answer |

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Run `bin/cgc doctor` and the parse checks before opening a PR.

## License

[MIT](LICENSE).

---

*Not affiliated with OpenAI or Anthropic. "ChatGPT" and "Claude" are trademarks of their respective owners. This tool automates a browser session you have already authenticated — use it in accordance with the terms of the services you use.*
