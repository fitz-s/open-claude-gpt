# Open Claude GPT

> **Use the ChatGPT Pro subscription you already pay for — inside Claude Code.**
> Claude drives locally; a logged-in ChatGPT Pro tab plans, reasons, and reviews in the background. No API key, no per-token bill.
>
> Put the frontier reasoning model you **already pay for** — **ChatGPT Pro** (the Pro not xhigh) — to work inside your Claude Code workflow.

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

- **Spend the subscription, not an API budget.** It automates your real ChatGPT session — the same Pro plan you use in the browser. Nothing is billed per token; your normal web-plan limits and availability apply.
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

Every wait is bounded so nothing can hang. A GPT-6 Astra Pro consult reasons for ~25 min, which is the timeout everywhere — see [Timeouts](docs/CONFIGURATION.md#timeouts). A **dedicated Chrome profile** is used because CDP is disallowed on Chrome's default profile (anti-cookie-theft, Chrome 136+) and is launched **loopback-bound** — you log into ChatGPT there once; your normal Chrome is untouched.

## Requirements

- **Python 3.8+** and [`websocket-client`](https://pypi.org/project/websocket-client/) — the CDP client, the only hard pip dep
- **Google Chrome / Chromium / Microsoft Edge** — auto-detected on macOS + Linux; override with `CGC_CHROME`
- **A ChatGPT account you log into by hand** — **Pro recommended** (that's the point: use the tier you pay for)
- **[`gh` CLI](https://cli.github.com)** — optional but recommended; `deliver` uses it to resolve PRs + repo visibility

## Know the risks before installing

Two things you accept by using this, stated plainly (details: [docs/SECURITY.md](docs/SECURITY.md)):

1. **Account/ToS risk is yours.** This tool programmatically drives your ChatGPT session and reads
   its output. OpenAI's consumer terms prohibit automatic/programmatic extraction of output, and
   accounts can be suspended for violations. The tool hides nothing (visible browser, no hidden
   endpoints, never bypasses login/CAPTCHA/rate limits), but that limits blast radius — it does not
   create permission. If the account matters to you beyond this tool, weigh that first.
2. **The gate guarantees exactly what it checks.** The daemon refuses secrets it can recognize,
   non-public repo links, and dead refs — fail-closed. It cannot semantically vet arbitrary prose
   you (or an agent) put in a task or context file. Keep secrets out of those fields; the scan is a
   backstop, not a reader.

## Install

```bash
git clone https://github.com/fitz-s/open-claude-gpt
cd open-claude-gpt
./install.sh            # copy the skill into ~/.claude/skills, check deps, run doctor
# or: ./install.sh --link   (symlink — edits in the clone go live; good for hacking.
#     NOTE: with --link, code changes are live for daemon workers immediately — restart the
#     daemon after pulling control-plane changes: launchctl kickstart -k gui/$(id -u)/com.open-claude-gpt.daemon)
```

Then start the dedicated debug Chrome and log into ChatGPT Pro **once**:

```bash
bin/cgc launch          # opens the dedicated Chrome; log into ChatGPT Pro there (once per machine)
bin/cgc install-daemon  # installs the egress daemon via launchd — run ONCE; it then starts at
                        # login and respawns if it dies, so nothing ever has to start it again
bin/cgc doctor --deep   # verify everything, including login state and daemon
```

In Claude Code the skill activates automatically — Claude reads its `SKILL.md` and invokes it when a task fits. See [docs/INSTALL.md](docs/INSTALL.md) for on-demand vs. proactive activation.

## First consult

The canonical path — fire → await — end to end by hand:

```bash
# 1. Fire it — ONE call: resolves the GitHub links, renders the prompt, queues the job.
#    The daemon validates the refs are public and does the actual send.
bin/cgc fire --repo owner/repo --ref main \
  --title "Audit main" --role "You are a staff reviewer." --task "Review main for correctness risks."

# 2. Run the exact `cgc await` line that `fire` prints — it waits for the answer
#    and prints the answer file's path when the consult lands.
```

(`deliver`, `prep`, and `enqueue` still exist as separate verbs for debugging or editing the
prompt in between; direct `submit`/`followup` are retired — the daemon is the sole send path.)

Answers land in `$CGC_STATE_DIR` (default `/tmp/cgc`, e.g. `answer_<RID>.txt`). That directory is
tool-owned and not durable across reboots — copy an answer you want to keep to your own path. In
Claude Code you don't run these by hand — the skill orchestrates the whole arc for you.

## Running under Claude Code's auto mode

Claude Code's `auto` permission mode has a data-exfiltration classifier sitting **above** the permission system — it hard-denies any agent Bash call that sends data to an external host, including `chatgpt.com`, and a `permissions.allow` entry can't suppress it (it isn't a permission check). So the direct `submit`/`wait` path fails there. The fix: move the send off the agent entirely. The agent only touches the local SQLite store — `cgc enqueue` writes a queued round to `control.db`, `cgc await` polls that row until it's terminal — neither touches the network, so the classifier never sees them. The actual send to ChatGPT happens in a daemon running in **your** login session, which independently re-verifies every job is public-only and secret-free before sending — a validating gate, not a way around the classifier. Install it once with **`cgc install-daemon`**: it registers a launchd agent that starts at login and respawns if it dies, and it opens the debug Chrome itself when needed. After that one command, no one — you or the agent — ever has to start or check it again. (`cgc watch` still runs it in the foreground for debugging; `cgc uninstall-daemon` reverses the install.)

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

When ChatGPT silently auto-downgrades a chat to a lighter model, a consult gets a weaker answer for free. So the tool **pins both the model and its reasoning tier before every send and fails closed if it can't**. Two dimensions, because since GPT-6 (2026-09-03) the composer picks the model and the tier in the same menu, and `Pro` on `GPT-5.5` would otherwise pass a tier-only check.

That proves what was **requested**. What **answered** is a separate fact, read separately: when the answer lands, the tool takes the `data-message-model-slug` ChatGPT stamps on that very turn — `gpt-6-pro`, `gpt-5-6-thinking` — and reports it as `model_slug`, beside but never merged with the pre-send `model_badge`. Name the producers you accept in `CGC_MODEL_SLUG` and a round served by anything else comes back for a human instead of as an auto-consumable answer. Sometimes the provider stamps nothing; that is `attribution: unknown` — not a mismatch, and not a failed consult.

```bash
CGC_AUTO_MODEL=1        # ON (default): select CGC_MODEL + CGC_MODEL_FAMILY, fail closed if unavailable
CGC_MODEL="Pro"         # which reasoning tier to target (set the strongest your plan has)
CGC_MODEL_FAMILY="Latest"   # which model to pin; "skip" to leave the model alone
CGC_MODEL_SLUG=""       # producers you accept, e.g. "gpt-6-*,gpt-5-6-pro"; empty = record, don't judge
CGC_AUTO_MODEL=0        # OFF: don't touch the picker, send on whatever is shown
```

Per-consult override: `--model "High"` / `--model skip`, `--model-family "GPT-5.6 Sol"` / `--model-family skip`. An older build with no model radios ignores the family setting instead of refusing.

## Configuration

**Point consults at your ChatGPT project** — the one thing most people set — with no shell-rc or `settings.json` editing:

```bash
cgc set-project "https://chatgpt.com/g/g-p-<id>-<slug>/project"   # every consult opens here
cgc set-project --clear                                           # back to a plain new chat
```

It persists in the tool's own config (`~/.config/cgc/config`); a `CGC_PROJECT_URL` env var still overrides it. Everything else host- or preference-specific is read from the environment — the public skill ships **no hard-coded identity**. Copy `.env.example` to `.env`, or set any of these:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CGC_PROJECT_URL` | new chat | ChatGPT URL a fresh consult opens — set to *your* project to group consults |
| `CGC_AUTO_MODEL` | `1` | auto-pick the model and its tier before sending (fail closed) — on/off |
| `CGC_MODEL` | `Pro` | which reasoning tier auto-select targets |
| `CGC_MODEL_FAMILY` | `Latest` | which model to pin (GPT-6 picker); `skip` to leave the model alone |
| `CGC_PORT` | `9333` | remote-debugging port for the dedicated Chrome |
| `CGC_PROFILE` | `~/.cgc-chrome` | dedicated Chrome profile dir |
| `CGC_CHROME` | auto-detect | explicit browser binary |
| `CGC_STATE_DIR` | `/tmp/cgc` | scratch dir for prompt/refs/answer files (not durable) |
| `CGC_DATA_DIR` | `~/.local/state/cgc` | durable data dir holding the consult store (`control.db`) |
| `CGC_SPOOL_DIR` | `$CGC_STATE_DIR/spool` | per-round send+wait logs (deletable scratch — coordination locks live under `$CGC_DATA_DIR/locks`, not here) |
| `CGC_GATE_ALLOW_GIST` | `0` | let the daemon's egress gate accept gist links (it can't cheaply prove one is public) |

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
