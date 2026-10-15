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
2. refuses (before changing anything) if the consult store was written by a newer schema than this
   checkout understands,
3. **stops the launchd egress daemon and waits until its process has exited and `daemon.lock` is
   free**,
4. copies `skill/` to a staging directory, parks the current install as `.bak` and moves the staged
   tree into `~/.claude/skills/chatgpt-consult`,
5. **starts the daemon again and checks its heartbeat** names this install's store and schema,
6. runs `cgc doctor`.

Nothing is replaced under a running daemon, so no process ever executes a half-old, half-new tree.
"Is the daemon gone" is decided by the process and its lock, never by how old its heartbeat is: a
daemon that is draining or wedged has a stale heartbeat and is still executing the files. If a daemon
process will not go away, `install.sh` changes nothing and names its pid. A daemon you started
yourself (`cgc watch`, `cgc up`) is not under launchd; `install.sh` refuses to continue while one runs.

The swap is a transaction. If anything fails after the old tree was moved aside (the copy, a rename, the
new daemon not coming up, Ctrl-C, SIGTERM, a closed terminal), the old tree is put back and the old
daemon restarted. Two rules make that hold when the failure lands in the middle of the swap:

- **A signal never cuts between a rename and the bookkeeping for it.** The three renames (park an older
  `.bak`, move the current install to `.bak`, move the staged tree into place) run with signals deferred;
  one that arrives meanwhile is acted on right after, so it always finds a state it can undo. The same
  holds for the recovery rename below (`.bak` back to the install path) together with the note that the
  daemon is owed a start.
- **The rollback reads the disk, not its own variables.** It identifies the trees by file identity
  (device and inode, which a rename keeps): the old tree is wherever the old identity is, and it is moved
  back to `chatgpt-consult` only after whatever sits there (the new tree, or the `--link` symlink, removed
  as a link and never descended into) has been removed. Nothing is ever renamed onto an existing path, so
  a rollback cannot nest one tree inside another or write into your clone of the repo.

Every rename is also **verified, not trusted**. Each one is `mv -n` (never overwrites) after a check that
the target is free, and afterwards the target path itself (not what a symlink there would lead to) must
carry the identity of the tree that was moved. If something appeared at the target between the check
and the rename, so that the tree went *into* it, the install does not go on: it names both paths and the
place the tree ended up, fails, and rolls back from there (the stray location is searched too). If even
that cannot be put right it says where the old tree is and exits non-zero; it never reports success.

**Out of scope:** another process of the same user writing into the skills directory while an install
runs. The install detects the damage described above (a misplaced or missing tree) and stops; it does
not try to out-race such a writer or to lock the directory against it.

An existing `.bak` is a previous install, possibly the only intact one: it is parked, and deleted only
after the new daemon has been verified. `--force` keeps a new install whose daemon did not come up, and
then keeps that older backup too, at `chatgpt-consult.bak.prev.<pid>`. `install.sh` never touches the
consult store (`control.db`).

**Known limit: SIGKILL.** A `kill -9` (or a power loss) cannot be trapped. If it lands after the old tree
was moved to `.bak` and before the new one is in place, nothing is left at `chatgpt-consult`, the service
is unloaded and the old tree is only at `chatgpt-consult.bak`. Re-running `install.sh` detects exactly that
(a `.bak` and nothing at the install path), says so, moves the old tree back first, and then upgrades
normally, starting the daemon again if a service is installed. Stale `.chatgpt-consult.new.<pid>` stage
directories of dead runs are cleaned at the same time.

If the daemon does not come up on the new code the upgrade fails and rolls back (`--force` keeps the new
install anyway); run `cgc daemon-start` to retry. The daemon verbs are `cgc daemon-stop`,
`cgc daemon-start [--any]`, `cgc daemon-pid`, and (first install) `cgc install-daemon`.

Every path is rooted at `$HOME` (skills dir, `~/Library/LaunchAgents`, `~/.claude/settings.json`,
`~/.config/cgc`, `~/.local/state/cgc`) and `launchctl` is looked up on `PATH`, so
`HOME=/tmp/h PATH=/tmp/stubs:$PATH ./install.sh` runs the whole thing in a sandbox. The lifecycle tests
do exactly that.

Options:

| Flag | Effect |
| --- | --- |
| `--link` | Symlink instead of copy — edits in the clone go live. Good for development. |
| `--dir DIR` | Install into a different skills root (default `~/.claude/skills`, or `$CLAUDE_SKILLS_DIR`). |
| `--force` | Continue past a dependency range miss, an unreadable store, a daemon that did not come up, or a not-ready doctor. |
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
bin/cgc doctor          # deps, browser, port, daemon, store, scratch, skill files, config
bin/cgc doctor --deep   # + ChatGPT login state (needs Chrome up)
bin/cgc doctor --json   # machine-readable, for CI
```

The doctor ends in three separate verdicts, because "the files are there" and "a consult can be sent"
are different claims:

| Verdict | True when |
| --- | --- |
| `installed` | every file the code loads is present and non-empty (including `chatgpt_contract.json`, `fixture_dom.js`, `cgc_drift.py`), `SKILL.md` exists, Python and `websocket-client` are usable |
| `can_send` | `installed`, **and** the egress daemon's heartbeat is live and names this install's store and schema, **and** the debug Chrome port answers (with `--deep`, ChatGPT is also logged in), and the scratch dir is writable |
| `can_read` | the store opens read-only at a schema this code understands (the doctor never creates or migrates it) |

Overall **ready is `can_send`**: with no daemon there is no way to send, so it is not ready. Exit
status: `0` ready, `3` installed but cannot send yet (start the daemon / Chrome / log in), `1` the
install is incomplete (or a `--secure` advisory failed). A daemon process that is alive but has a stale
heartbeat is reported as exactly that (hung, draining or starting), not as "not running". The process
probe reads `/proc/<pid>/stat` and `/proc/<pid>/cmdline` where `/proc` exists and uses `ps -ww` otherwise
(plain `ps` truncates the command to `$COLUMNS`, which cut the daemon's script name off the end of a long
interpreter path on CI); a process that cannot be inspected at all counts as running. A fresh `install.sh` therefore ends "installed,
cannot send yet" until you run `cgc install-daemon` once.

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

The daemon is stopped, the files are replaced, the daemon is started on the new code (see above). The
prior install is kept as `~/.claude/skills/chatgpt-consult.bak`.

## Uninstall

```bash
./uninstall.sh          # remove the service, the hook entry and the code; KEEP the consult store
./uninstall.sh --purge  # also delete the consult store (listed first), scratch, and (prompted) the Chrome profile
```

In this order:

0. every path is resolved and checked first (a refusal here changes nothing),
1. the launchd service is stopped (waiting until the daemon process has exited and `daemon.lock` is
   free) and its plist removed; a daemon running outside launchd is named and stops the uninstall,
2. our `SessionStart` entry is removed from `settings.json` (every other hook and setting is left
   as it was; `settings.json.bak` keeps the previous file),
3. only then is the code deleted.

`settings.json` fails closed. If it is unreadable (permissions), has a shape the editor will not touch,
or any other read error occurs, the uninstall stops before changing anything. Only a file that is
readable but not valid JSON can be skipped, and only with `--force`: Claude Code ignores such a file, so
no hook of ours can be firing from it, and the uninstall prints the manual step.

The **send journal is kept** unless you pass `--purge`: `control.db` records which prompts were sent,
and deleting it forgets that, so a resend of an uncertain round can no longer be recognised. Without
`--purge` the uninstall prints the paths it kept (`control.db`, `-wal`, `-shm`, the
`control.db.v<n>.bak` pre-migration backups under `~/.local/state/cgc` or `$CGC_DATA_DIR`). If the
store, or your config file, lies inside the code tree about to be deleted, the uninstall refuses up
front instead of taking it along.

With `--purge` it prints exactly which paths it is about to delete, then deletes **only those paths, as
printed, one by one**: the delete step is handed the printed plan (with each file's identity) and
re-checks only the listed paths (still a regular file, still the same file, still named the way the tool
names it, and still not your config file: that last test is repeated at delete time, so a path that
became a link to the config after the plan was shown is skipped too). "The same file" is a fingerprint
of device, inode, size, nanosecond mtime and a SHA-256 of the file's first MiB, never the inode alone:
Linux filesystems hand a freed inode number to the next file created, so `rm x; echo y > x` leaves the
same inode, and a plan that trusted it would delete the file someone put there. It never scans again, so a file that appeared after the plan was shown, for example a
`chrome.log` written by something you restarted meanwhile, is not touched. What the plan contains
is only files the tool wrote: the database and its `-wal`/`-shm`/`.v<n>.bak` (each a regular file, the database
and backups with a valid SQLite header), lock files the daemon creates, drift bundles
(`incidents/I-*/diagnosis.json`, `fixture.html`) and scratch files (`answer_*`, `prompt_*`, `refs_*`,
`daemon.log`, `chrome.log`, `active.json`, spool logs). It never runs `rm -rf` on a directory because
of its name, and it leaves a directory alone unless it is a default location (`~/.local/state/cgc`,
`/tmp/cgc`) or strictly inside `$HOME`. A foreign file in any of these directories survives, and so
does the directory. A directory is removed only if it is one of the tool's own (`locks`, `incidents`,
`spool`) or a default location, and only when it ends up empty; a custom `CGC_DATA_DIR`/`CGC_STATE_DIR`
you chose stays in place. Your config file is never removed, in any mode, and it is recognised by file
identity (device and inode), not by how its path is spelled: on a case-insensitive filesystem (the macOS
default) `CGC_CONFIG=.../ACTIVE.JSON` is the very file the scan lists as `active.json`, and a hard link
or a symlink to a scratch file is the same file again. The same identity test decides every "is this
inside the tree being deleted" check. The Chrome profile is offered
for deletion only if it is `~/.cgc-chrome` (or a directory with a Chrome `Local State`) strictly inside
`$HOME` and contains none of your config, store, scratch or settings; you must answer `y`.

To stop only the daemon: `cgc uninstall-daemon` (also waits for the process to exit and keeps the
plist if it will not).
