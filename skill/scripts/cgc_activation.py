#!/usr/bin/env python3
# Created: 2026-09-17
# Authority basis: open-claude-gpt proactive-activation hook — single source of truth for the
#   SessionStart snippet `bin/cgc activation-hook` prints/installs, so the CLI's print/--install/
#   --remove/--status and cgc_doctor.py's "proactive activation" check all agree on one definition.
"""
cgc_activation.py — the proactive SessionStart hook: print it, install it, remove it, check it.

`~/.claude/skills/chatgpt-consult/ACTIVATION.md` is a short always-relevant note. A user can make
Claude Code consider a consult at the start of EVERY session by adding a SessionStart hook to
their own Claude settings.json that `cat`s that file into context. This module is the ONE place
that owns the hook command string and the logic to detect/add/upgrade/remove it in settings.json —
nothing else should duplicate the snippet text.

    cgc_activation.py print                    write the ready-to-paste snippet to stdout; changes
                                                nothing
    cgc_activation.py status  [--json]         report configured / broken / absent / unreadable
    cgc_activation.py install [--json]         idempotently add or upgrade the hook
    cgc_activation.py remove  [--json]         remove our entry again
    cgc_activation.py snapshot                 what the installed hook runs: ACTIVATION.md, then ONE
                                                line of weekly budget/backlog/grading state

    --settings PATH     default: ${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json
    --activation PATH   default: ACTIVATION.md next to this script's skill root

Installing is opt-in only — nothing calls `install` unless the user asks for it
(`bin/cgc activation-hook --install`, or `install.sh --activation-hook`). Printing the snippet
(the default of `cgc activation-hook`) never touches settings.json.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile

# Identity lives in the COMMAND TEXT, not the activation path. A path-shaped marker only matches
# when the resolved ACTIVATION.md happens to sit under a directory literally named
# "chatgpt-consult" — true for the installed skill (~/.claude/skills/chatgpt-consult/ACTIVATION.md)
# but FALSE for a raw repo clone's <repo>/skill/ACTIVATION.md. Matching on path alone therefore
# made a repo-local `install` non-idempotent (never recognized its own prior entry, so it kept
# appending) and made `status` lie (report `absent` with N copies already installed). Two markers,
# either sufficient:
#   MARKER_CURRENT — the literal every command THIS module emits contains, regardless of path.
#   MARKER_LEGACY  — the old path-shaped marker, so a hook from the previously-printed snippet
#                    (the `|| true` form, or one installed under a different skills root) is still
#                    recognized and upgraded in place rather than duplicated.
# Residual gap, accepted rather than solved: a legacy `|| true` entry whose path contains NEITHER
# marker (seeded from a clone not named "chatgpt-consult") is undetectable — install would append
# a second entry beside it. No released snippet ever produced that shape, since the snippet always
# used the installed skill's own ACTIVATION.md path.
MARKER_CURRENT = "WARNING: chatgpt-consult ACTIVATION.md missing at"
MARKER_LEGACY = "chatgpt-consult/ACTIVATION.md"
# The snapshot form names this script's subcommand. It repeats MARKER_CURRENT in its echo fallback
# today, but identity must not hang on the wording of a warning: edit that text and every installed
# hook would be orphaned (install would append a second entry beside it).
MARKER_SNAPSHOT = "cgc_activation.py' snapshot"

HERE = os.path.dirname(os.path.abspath(__file__))
SELF = os.path.join(HERE, "cgc_activation.py")
DEFAULT_ACTIVATION = os.path.join(os.path.dirname(HERE), "ACTIVATION.md")


def default_settings_path() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return os.path.expanduser(os.path.join(base, "settings.json"))


class SettingsError(Exception):
    """Settings.json could not be parsed, or its shape isn't one we can safely edit."""


class QuoteError(Exception):
    """The activation path can't be safely single-quoted for the shell."""


def _missing_warning(p: str) -> str:
    return f"{MARKER_CURRENT} {p} — run: cgc doctor"


def build_command(activation_path: str) -> str:
    """The one hook command string: `snapshot` prints the note plus one state line; if python3 or
    the script is unavailable, fall back to `cat` of the note, and failing that a visible WARNING.
    Deliberately `|| echo ...` and not `|| true`: a missing/unreadable ACTIVATION.md must show up
    in the injected context, not vanish silently. Never exits non-zero, so it can't fail the
    user's session start. The WARNING literal (MARKER_CURRENT) is still in the command, so a hook
    installed in the previous `cat ... || echo` form is recognized and upgraded in place."""
    for q in (activation_path, SELF):
        if "'" in q:
            raise QuoteError(f"path contains a single quote, cannot shell-quote it safely: {q!r}")
    p = activation_path
    return (f"python3 '{SELF}' snapshot --activation '{p}' 2>/dev/null || cat '{p}' 2>/dev/null "
            f"|| echo '{_missing_warning(p)}'")


def is_ours(hook) -> bool:
    if not isinstance(hook, dict):
        return False
    cmd = hook.get("command") or ""
    return MARKER_CURRENT in cmd or MARKER_LEGACY in cmd or MARKER_SNAPSHOT in cmd


def _validate_shape(data) -> None:
    if not isinstance(data, dict):
        raise SettingsError("top-level JSON is not an object")
    hooks = data.get("hooks")
    if "hooks" in data and not isinstance(hooks, dict):
        raise SettingsError('"hooks" key exists but is not an object')
    if isinstance(hooks, dict) and "SessionStart" in hooks and not isinstance(hooks["SessionStart"], list):
        raise SettingsError('"hooks.SessionStart" exists but is not an array')


def read_settings(path: str):
    """Returns (data, existed). Raises SettingsError on unparseable JSON or an unsafe shape —
    the caller must not write a single byte in that case."""
    if not os.path.exists(path):
        return {}, False
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        raise SettingsError(f"cannot read {path}: {e}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SettingsError(f"{path}: invalid JSON — {e}")
    _validate_shape(data)
    return data, True


def _write_atomic(path: str, data) -> None:
    """Backup the existing file to <path>.bak, then write the new content atomically (temp file +
    os.replace) in the same directory. Only called when something is actually changing.

    mkstemp creates the temp file 0600, which os.replace would otherwise carry over the original
    file's mode — silently tightening an existing 0644 settings.json to 0600. That permission
    change isn't ours to make unasked, so an EXISTING file's mode is preserved; 0600 only applies
    when we're creating settings.json fresh (a new file holding config is right to start private).

    The backup is copy2, not copyfile: settings.json can be 0600 because it holds the user's own
    configuration, and a copy that widens that to the umask default leaves a readable duplicate of
    a private file sitting next to it."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    existing_mode = None
    if os.path.exists(path):
        existing_mode = stat.S_IMODE(os.stat(path).st_mode)
        shutil.copy2(path, path + ".bak")
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".cgc_activation_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if existing_mode is not None:
            os.chmod(tmp, existing_mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _ours_entries(data):
    """All hook dicts in hooks.SessionStart[*].hooks that are ours. Tolerant of malformed groups
    (skipped, never raised on) — the shape we insist on is validated up front in read_settings."""
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return []
    starts = hooks.get("SessionStart")
    if not isinstance(starts, list):
        return []
    out = []
    for group in starts:
        if not isinstance(group, dict):
            continue
        group_hooks = group.get("hooks")
        if isinstance(group_hooks, list):
            out.extend(h for h in group_hooks if is_ours(h))
    return out


def install(settings_path: str, activation_path: str):
    """Returns (outcome, count). outcome is one of 'added' / 'unchanged' / 'upgraded'."""
    cmd = build_command(activation_path)
    data, _existed = read_settings(settings_path)
    ours = _ours_entries(data)

    if not ours:
        hooks = data.setdefault("hooks", {})
        starts = hooks.setdefault("SessionStart", [])
        starts.append({"matcher": "", "hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
        _write_atomic(settings_path, data)
        return "added", 1

    changed = 0
    for h in ours:
        if h.get("command") != cmd:
            h["command"] = cmd
            changed += 1
    if changed == 0:
        return "unchanged", 0
    _write_atomic(settings_path, data)
    return "upgraded", changed


def remove(settings_path: str):
    """Returns (outcome, count). outcome is 'removed' or 'absent'."""
    data, existed = read_settings(settings_path)
    if not existed:
        return "absent", 0
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return "absent", 0
    starts = hooks.get("SessionStart")
    if not isinstance(starts, list):
        return "absent", 0

    removed = 0
    new_starts = []
    for group in starts:
        if not isinstance(group, dict):
            new_starts.append(group)
            continue
        group_hooks = group.get("hooks")
        if not isinstance(group_hooks, list):
            new_starts.append(group)
            continue
        kept = [h for h in group_hooks if not is_ours(h)]
        removed += len(group_hooks) - len(kept)
        if kept:
            new_group = dict(group)
            new_group["hooks"] = kept
            new_starts.append(new_group)
        # else: the group's hooks list became empty — drop the whole group.

    if removed == 0:
        return "absent", 0

    if new_starts:
        hooks["SessionStart"] = new_starts
    else:
        del hooks["SessionStart"]
        if not hooks:
            del data["hooks"]
    _write_atomic(settings_path, data)
    return "removed", removed


def status(settings_path: str, activation_path: str) -> dict:
    """Never raises, never writes. One of four states: configured / broken / absent / unreadable.

    `unreadable` (settings.json exists but can't be parsed or has an unsafe shape) is distinct
    from `absent` (parsed fine, genuinely no ours-entry) on purpose: collapsing them into `absent`
    would have status claim "no hook installed" about a file it never actually looked inside —
    the exact silent-misreport class this module exists to eliminate. Bad JSON here also means
    Claude Code itself ignores the whole settings file, which is worth surfacing on its own."""
    try:
        data, _existed = read_settings(settings_path)
    except SettingsError as e:
        return {
            "settings_path": settings_path,
            "activation_path": activation_path,
            "entries": 0,
            "state": "unreadable",
            "detail": str(e),
            "stale": False,
        }

    ours = _ours_entries(data)
    result = {"settings_path": settings_path, "activation_path": activation_path, "entries": len(ours)}

    if not ours:
        result["state"] = "absent"
        result["stale"] = False
        return result

    try:
        with open(activation_path, encoding="utf-8") as f:
            act_ok = bool(f.read().strip())
    except OSError:
        act_ok = False

    result["state"] = "configured" if act_ok else "broken"
    if not act_ok:
        result["detail"] = f"activation file missing, unreadable, or empty: {activation_path}"

    try:
        desired = build_command(activation_path)
        result["stale"] = any(h.get("command") != desired for h in ours)
    except QuoteError:
        result["stale"] = False
    return result


# ---- the SessionStart snapshot ------------------------------------------------

SNAPSHOT_BUDGET_S = 0.8   # wall-clock cap on the whole line; the hook must never slow a session start


def _dur(secs) -> str:
    secs = int(secs)
    return (f"{secs}s" if secs < 60 else f"{secs // 60}m" if secs < 3600
            else f"{secs // 3600}h{secs % 3600 // 60:02d}m")


def snapshot_line():
    """The one line, or None when there is nothing to say (no store yet). Reads the store through a
    `mode=ro` URI connection: it cannot create the DB, migrate it, or write a byte, and it touches no
    network and no browser. The figures come from cgc_store.week_view, the same function behind
    `stats --week`, so the two surfaces cannot disagree."""
    import sqlite3
    import urllib.parse
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import cgc_store as st
    path = st.db_path()
    if path == ":memory:" or not os.path.exists(path):
        return None
    db = sqlite3.connect("file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro",
                         uri=True, timeout=0.3)
    try:
        w = st.week_view(db)
    finally:
        db.close()
    b = w["budget"]
    parts = [f"Consult budget: {b['spent']} of {b['weekly']} used (rolling 7d)" if b["weekly"] is not None
             else f"Consult budget: {b['spent']} used (rolling 7d; CGC_WEEKLY_BUDGET unset)"]
    if b["available"] is not None:
        parts.append(f"{b['available']} available")
    if w["backlog"]:
        parts.append(f"{len(w['backlog'])} ready in backlog")
    if w["ungraded"]:
        parts.append(f"{len(w['ungraded'])} answers awaiting grade (cgc feedback)")
    if w["latency_p50_s"] is not None:
        parts.append(f"p50 {_dur(w['latency_p50_s'])}")
    if b["spent"] and not w["policy_update"]["happened"]:
        parts.append("weekly review due")
    return " · ".join(parts)


def cmd_snapshot(a) -> int:
    """What the installed SessionStart hook runs. Prints ACTIVATION.md, then ONE state line. Whatever
    goes wrong — unreadable store, locked DB, a schema this code doesn't know, a timeout — the note
    has already been printed and the line is simply omitted. Exits 0 always."""
    activation = a.activation or DEFAULT_ACTIVATION
    try:
        with open(activation, encoding="utf-8") as f:
            note = f.read()
    except (OSError, ValueError):   # missing, unreadable, or not UTF-8: all the same to the session
        note = _missing_warning(activation) + "\n"
    try:
        sys.stdout.write(note)
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001 — a closed pipe must not turn into a failed session start
        return 0
    armed = False
    try:
        import signal

        def _expire(*_):
            raise TimeoutError("snapshot budget exceeded")
        signal.signal(signal.SIGALRM, _expire)
        signal.setitimer(signal.ITIMER_REAL, SNAPSHOT_BUDGET_S)
        armed = True
    except (ImportError, AttributeError, ValueError):
        pass  # no SIGALRM here (Windows): the sqlite timeout is the only bound
    try:
        line = snapshot_line()
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)   # disarm before anything else can be interrupted
        if line:
            sys.stdout.write(line + "\n")
    except BaseException:  # noqa: BLE001 — a status line must never be able to fail the session
        pass
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        sys.stdout.flush()
    except BaseException:  # noqa: BLE001 — e.g. a closed pipe
        pass
    return 0


# ---- CLI ---------------------------------------------------------------------

def _paths(a) -> "tuple[str, str]":
    settings = a.settings or default_settings_path()
    activation = a.activation or DEFAULT_ACTIVATION
    return settings, activation


def cmd_print(a) -> int:
    settings, activation = _paths(a)
    try:
        cmd = build_command(activation)
    except QuoteError as e:
        sys.stderr.write(f"cgc_activation: {e}\n")
        return 1
    print(f"""Proactive activation (OPTIONAL) — this only PRINTS a snippet by default; it does not
change any settings unless you pass --install.

On demand, the skill already activates on its own (Claude reads its SKILL.md description).
Add the hook below ONLY if you also want Claude to consider offloading a consult at the
start of every session. It injects: {activation}, then one line of this week's consult budget,
backlog and grading state (read-only, local, never fails the session)

Merge this into the "hooks" object of your Claude settings.json
({settings}) — keep any existing SessionStart entries:

  "SessionStart": [
    {{
      "matcher": "",
      "hooks": [
        {{ "type": "command", "command": {json.dumps(cmd)}, "timeout": 10 }}
      ]
    }}
  ]

Or let this tool do it for you (backs up settings.json to settings.json.bak first):
  cgc activation-hook --install   # add or upgrade the hook
  cgc activation-hook --remove    # remove it again
  cgc activation-hook --status    # report configured / broken / absent / unreadable

See docs/INSTALL.md → "Activation: on-demand vs. proactive".""")
    return 0


def cmd_status(a) -> int:
    settings, activation = _paths(a)
    r = status(settings, activation)
    if a.json:
        print(json.dumps(r, indent=2))
        return 0
    stale_note = " (stale — run `cgc activation-hook --install` to refresh)" if r.get("stale") else ""
    if r["state"] == "configured":
        print(f"configured — hook present, {activation} readable{stale_note}")
    elif r["state"] == "broken":
        print(f"broken — hook present but {r['detail']}{stale_note}")
    elif r["state"] == "unreadable":
        print(f"unreadable — {r['detail']}")
    else:
        print("absent — no proactive hook installed (on-demand activation still works)")
    return 0


def cmd_install(a) -> int:
    settings, activation = _paths(a)
    try:
        outcome, count = install(settings, activation)
    except (SettingsError, QuoteError) as e:
        sys.stderr.write(f"cgc_activation: refusing — {e}\n")
        return 1
    if a.json:
        print(json.dumps({"outcome": outcome, "count": count, "settings_path": settings}, indent=2))
        return 0
    if outcome == "added":
        print(f"added — SessionStart hook installed in {settings}")
    elif outcome == "upgraded":
        print(f"upgraded — {count} entry(ies) updated to the current hook form in {settings}")
    else:
        print(f"unchanged — hook already up to date in {settings}")
    return 0


def cmd_remove(a) -> int:
    settings, _activation = _paths(a)
    try:
        outcome, count = remove(settings)
    except SettingsError as e:
        sys.stderr.write(f"cgc_activation: refusing — {e}\n")
        return 1
    if a.json:
        print(json.dumps({"outcome": outcome, "count": count, "settings_path": settings}, indent=2))
        return 0
    if outcome == "removed":
        print(f"removed — {count} entry(ies) dropped from {settings}")
    else:
        print(f"absent — nothing to remove in {settings}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="cgc_activation.py")
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--settings", help="settings.json path (default: ${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json)")
    parent.add_argument("--activation", help="ACTIVATION.md path (default: next to this script's skill root)")
    parent.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("print", parents=[parent], help="write the ready-to-paste snippet to stdout; changes nothing").set_defaults(fn=cmd_print)
    sub.add_parser("status", parents=[parent], help="report configured / broken / absent / unreadable").set_defaults(fn=cmd_status)
    sub.add_parser("install", parents=[parent], help="idempotently add or upgrade the hook").set_defaults(fn=cmd_install)
    sub.add_parser("remove", parents=[parent], help="remove our entry again").set_defaults(fn=cmd_remove)
    sub.add_parser("snapshot", parents=[parent],
                   help="print ACTIVATION.md and one line of weekly consult state (what the hook runs)"
                   ).set_defaults(fn=cmd_snapshot)

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
