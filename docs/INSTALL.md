# Installation

## Prerequisites

- **Python 3.8+**
- **`websocket-client`** — `pip install websocket-client` (the installer does this if missing)
- **Chrome / Chromium / Edge** — auto-detected on macOS + Linux; set `CGC_CHROME` otherwise
- **A ChatGPT account** (Pro/Plus; Pro recommended for the top model tiers)
- **`gh` CLI** (optional) — `deliver` uses it to resolve PRs + repo visibility

## Install the skill

```bash
git clone https://github.com/YOUR_USER/chatgpt-consult
cd chatgpt-consult
./install.sh
```

`install.sh`:

1. checks `python3` + auto-installs `websocket-client` if missing,
2. copies `skill/` into `~/.claude/skills/chatgpt-consult` (backing up any prior install to `.bak`),
3. makes the scripts executable,
4. runs `cgc doctor`.

Options:

| Flag | Effect |
| --- | --- |
| `--link` | Symlink instead of copy — edits in the clone go live. Good for development. |
| `--dir DIR` | Install into a different skills root (default `~/.claude/skills`, or `$CLAUDE_SKILLS_DIR`). |

## First-run setup: log into ChatGPT once

The agent never types your credentials. You log into a **dedicated** Chrome
profile one time; the session persists there.

```bash
bin/cgc launch          # opens the dedicated debug Chrome
# → log into ChatGPT (Pro/Plus) in that window, then leave it open
bin/cgc doctor --deep   # confirms login state via CDP
```

Why a separate profile: since Chrome 136, remote debugging is disallowed on the
default profile (anti-cookie-theft). The dedicated profile lives at
`~/.cgc-chrome` (`CGC_PROFILE`) and does not touch your normal Chrome.

## Verify

```bash
bin/cgc doctor          # deps, browser, port, scratch, skill files, config
bin/cgc doctor --deep   # + ChatGPT login state (needs Chrome up)
bin/cgc doctor --json   # machine-readable, for CI
```

A green run means the skill is ready; in Claude Code it activates automatically.

## Upgrade

```bash
cd chatgpt-consult && git pull && ./install.sh
```

The prior install is moved to `~/.claude/skills/chatgpt-consult.bak` first.

## Uninstall

```bash
./uninstall.sh          # remove the installed skill
./uninstall.sh --purge  # also remove scratch dir + (prompted) the Chrome profile
```
