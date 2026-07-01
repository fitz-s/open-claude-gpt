# chatgpt-consult

**Offload your hardest, most open-ended work to a frontier ChatGPT reasoning model running in the background — while your coding agent keeps working locally, then verifies the result.**

`chatgpt-consult` is a [Claude Code](https://claude.com/claude-code) skill (it also works as a standalone CLI) that turns a logged-in ChatGPT Pro tab into a background coprocessor. Your agent fires a deep task — a code review, an architecture critique, an investigation, a research question — to ChatGPT, keeps doing local work, and is woken by a detached waiter when the full answer lands. **ChatGPT advises; your local agent stays the source of truth and verifies every claim before acting on it.**

It drives ChatGPT through an external **Chrome DevTools Protocol** client against a browser *you* logged into once — no ChatGPT API, no scraping of hidden endpoints, no credentials ever handled by the agent.

---

## Why

A frontier reasoning model is worth reaching for on exactly the work a coding agent burns the most local context on: whole-PR reviews, "is this design right before I build it", gnarly root-cause hunts, literature/tradeoff research. But doing that inline stalls the agent and floods its context. `chatgpt-consult` makes offloading the *default*:

- **Runs in the background.** Submit, keep working, get woken on completion. The wait loop is a detached shell process holding zero agent context, so polling never triggers a context reload.
- **Link-first, never paste.** Code is delivered as **GitHub links** (PR / compare / tree / blob) that ChatGPT browses itself — carrying diff, intent, discussion, CI, and navigable surrounding code. A pushed commit with a PR *always* resolves to that public PR link; gist is a genuine last resort for unpushed/private state.
- **A thread, not a one-shot.** Feed local verification results back and follow up in the same conversation, keeping ChatGPT's context and model tier — loop until the answer is clean.
- **Fail-closed on model + code.** It refuses to send a consult with no real code link, and refuses to send on the wrong model tier, rather than silently degrading.

## How it works

```
 Claude Code ──deliver──▶ GitHub links (PR/tree/blob)         (consult.py)
     │        ──prep─────▶ rendered prompt + sentinels
     │        ──submit───▶ ┌──────────────────────────┐
     │                     │  external CDP client       │──▶ dedicated Chrome
     │                     │  (websocket-client)        │     (your ChatGPT login)
     │        ◀──wait──────│  detached poller           │◀── ChatGPT answer
     ▼                                                        between BEGIN/END sentinels
 verify locally ──followup──▶ (same thread, next round)      (cdp_consult.py)
```

- **`consult.py deliver`** — resolves GitHub references (repo visibility, PR association, commit-pinned tree/blob links) into a grouped refs file.
- **`consult.py prep`** — renders the outgoing prompt from a template, wrapping the expected answer in `BEGIN_RESPONSE:<rid>` / `END_RESPONSE:<rid>` sentinels so completion is unambiguous.
- **`cdp_consult.py submit` / `followup` / `wait` / `status`** — the CDP control plane: open a chat, select the model tier, type + send, and poll to completion in a detached process.

Why a dedicated Chrome profile: CDP is disallowed on Chrome's default profile (anti-cookie-theft, Chrome 136+), so the skill uses a separate `--user-data-dir` you log into once. Your normal Chrome is untouched.

## Requirements

- **Python 3.8+** and [`websocket-client`](https://pypi.org/project/websocket-client/) (the CDP client — the only hard pip dep)
- **Google Chrome / Chromium / Microsoft Edge** (auto-detected on macOS + Linux; override with `CGC_CHROME`)
- **A ChatGPT account you log into by hand** (Pro/Plus — Pro recommended for the top model tiers)
- **[`gh` CLI](https://cli.github.com)** (optional but recommended — `deliver` uses it to resolve PRs and repo visibility)

## Install

```bash
git clone https://github.com/YOUR_USER/chatgpt-consult
cd chatgpt-consult
./install.sh            # copies the skill into ~/.claude/skills/chatgpt-consult, checks deps, runs doctor
# or: ./install.sh --link   (symlink — edits in the clone go live; good for hacking)
```

Then start the dedicated debug Chrome and log into ChatGPT **once**:

```bash
bin/cgc launch          # opens the dedicated Chrome; log into ChatGPT in that window, leave it open
bin/cgc doctor --deep   # verify everything, including login state
```

See [docs/INSTALL.md](docs/INSTALL.md) for details and manual steps.

## Quickstart (drive it by hand)

```bash
# 1. resolve code links for a PR (always prefers the public PR link over a gist)
bin/cgc deliver --repo owner/repo --pr 123

# 2. render the prompt (pass the refs file printed by deliver)
bin/cgc prep --task "Review this PR for correctness + migration safety" \
             --title "Review PR 123" --refs-file /tmp/cgc/refs_*.md

# 3. submit, then run the detached waiter it prints (in Claude Code this is backgrounded automatically)
bin/cgc submit --rid <RID> --prompt-file /tmp/cgc/prompt_<RID>.md
```

In Claude Code you don't do this by hand — the skill activates automatically and the agent orchestrates deliver → prep → submit → wait → verify → followup. See [examples/](examples/).

## Configuration

Everything host- or preference-specific is read from the environment — the public skill ships no hard-coded identity. Copy `.env.example` to `.env` and set what you want:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CGC_PROJECT_URL` | new chat | ChatGPT URL a fresh consult opens — set to *your* project to group consults |
| `CGC_MODEL` | `Pro Extended` | model tier the composer selects before sending |
| `CGC_PORT` | `9333` | remote-debugging port for the dedicated Chrome |
| `CGC_PROFILE` | `~/.cgc-chrome` | dedicated Chrome profile dir |
| `CGC_CHROME` | auto-detect | explicit browser binary |
| `CGC_STATE_DIR` | `/tmp/cgc` | scratch dir for prompt/refs/answer files |

Full reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md). Prompt templates are customizable too — see [Customizing prompts](docs/CONFIGURATION.md#customizing-prompts).

## Security model

- **The agent never handles your credentials.** You log into the dedicated Chrome by hand, once; the session persists in that profile.
- **This is ordinary browser automation against your own logged-in session** — it reads only the ChatGPT answer text, never cookies or cross-site data, and writes the answer to a local file you own.
- **Never send secrets.** The skill refuses to fabricate a code source and is built to ship *links to already-public code*, not to exfiltrate private content. Don't put `.env`, keys, or tokens in a prompt, gist, or context file.
- **ChatGPT is advisory.** Your local agent verifies every claim before merging, shipping, or declaring done.

Details + threat model: [docs/SECURITY.md](docs/SECURITY.md).

## Troubleshooting

`bin/cgc doctor` diagnoses most issues (deps, browser, port, login, scratch, config). Common fixes are in [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Run `bin/cgc doctor` and the parse checks before opening a PR.

## License

[MIT](LICENSE).

---

*Not affiliated with OpenAI or Anthropic. "ChatGPT" and "Claude" are trademarks of their respective owners. This tool automates a browser session you have already authenticated — use it in accordance with the terms of the services you use.*
