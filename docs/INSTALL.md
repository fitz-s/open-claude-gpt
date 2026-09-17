# Installation

## Prerequisites

- **Python 3.8+**
- **`websocket-client`** — `pip install websocket-client` (the installer does this if missing)
- **Chrome / Chromium / Edge** — auto-detected on macOS + Linux; set `CGC_CHROME` otherwise
- **A ChatGPT account** (Pro/Plus; Pro recommended for the top model tiers)
- **`gh` CLI** (optional) — `deliver` uses it to resolve PRs + repo visibility

## Install the skill

```bash
git clone https://github.com/fitz-s/open-claude-gpt
cd open-claude-gpt
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
| `--activation-hook` | Also install the optional proactive `SessionStart` hook (see below). Off by default. |

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

## Activation: on-demand vs. proactive

**On-demand (default, nothing to configure).** Claude Code reads the skill's
`SKILL.md` frontmatter `description` and invokes the skill on its own when a task
fits ("offload a deep, self-contained job to ChatGPT in the background…"). This
works the moment the skill is installed — the agent knows when to use it.

**Proactive (optional).** If you also want Claude to *consider offloading a consult
at the start of every session* — the aggressive "background ultra-everything"
default — add a `SessionStart` hook that injects the skill's activation note
(`~/.claude/skills/chatgpt-consult/ACTIVATION.md`). The installer does **not** do
this for you by default, because it edits *your own* Claude settings; opt in
yourself, either by hand or with the CLI:

```bash
bin/cgc activation-hook              # print a ready-to-paste snippet — changes nothing
bin/cgc activation-hook --install    # add or upgrade it for you
bin/cgc activation-hook --remove     # remove it again
bin/cgc activation-hook --status     # report configured / broken / absent / unreadable
```

`--install` is idempotent (safe to re-run; a second run with nothing to change
touches nothing) and copies your existing `settings.json` to `settings.json.bak`
before writing, atomically, the first time it actually changes something. It edits
only its own `SessionStart` entry — every other hook and setting in the file is
left untouched. `--install` also works as `install.sh --activation-hook`, run once
at install time instead of as a separate step.

If you'd rather edit `settings.json` by hand, merge the printed `SessionStart`
entry into the `hooks` object of `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json`
(keep any existing entries) and delete it to undo. `ACTIVATION.md` is just the text
the hook prints into context each session — edit it to tune how strongly Claude is
nudged.

`cgc doctor` reports this hook's state under "proactive activation": `configured`
when it's present and the note is readable, a **warning** when the hook is present
but `ACTIVATION.md` is missing or unreadable (a silent no-op otherwise — this is
the failure mode the CLI-managed install exists to make visible), and a clean pass
when it's simply not installed (on-demand activation still works either way).

**Scope limit: main session only.** This hook fires on `SessionStart` for the main
Claude Code session — startup, resume, `/clear`, and `/compact`. A subagent spawned
via the Agent tool starts with a fresh context and never receives it; subagents can
only reach this skill through on-demand activation (Claude reading `SKILL.md`'s
description). There is no proactive path for subagents.

## Upgrade

```bash
cd open-claude-gpt && git pull && ./install.sh
```

The prior install is moved to `~/.claude/skills/chatgpt-consult.bak` first.

## Uninstall

```bash
./uninstall.sh          # remove the installed skill
./uninstall.sh --purge  # also remove scratch dir + (prompted) the Chrome profile
```
