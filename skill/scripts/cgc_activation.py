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

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ACTIVATION = os.path.join(os.path.dirname(HERE), "ACTIVATION.md")


def default_settings_path() -> str:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return os.path.expanduser(os.path.join(base, "settings.json"))


class SettingsError(Exception):
    """Settings.json could not be parsed, or its shape isn't one we can safely edit."""


class QuoteError(Exception):
    """The activation path can't be safely single-quoted for the shell."""


def build_command(activation_path: str) -> str:
    """The one hook command string — `cat` the note, or a visible WARNING if it's missing.
    Deliberately `|| echo ...` and not `|| true`: a missing/unreadable ACTIVATION.md must show up
    in the injected context, not vanish silently. Never exits non-zero, so it can't fail the
    user's session start."""
    if "'" in activation_path:
        raise QuoteError(f"activation path contains a single quote, cannot shell-quote it safely: {activation_path!r}")
    p = activation_path
    return (f"cat '{p}' 2>/dev/null || echo 'WARNING: chatgpt-consult ACTIVATION.md missing at "
            f"{p} — run: cgc doctor'")


def is_ours(hook) -> bool:
    if not isinstance(hook, dict):
        return False
    cmd = hook.get("command") or ""
    return MARKER_CURRENT in cmd or MARKER_LEGACY in cmd


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
start of every session. It injects: {activation}

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

    a = p.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
