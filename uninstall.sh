#!/usr/bin/env bash
# Remove open-claude-gpt: the launchd daemon, the SessionStart hook entry, then the skill code.
#   ./uninstall.sh          # stop+remove the daemon service, drop our hook entry, remove the skill.
#                           # The consult store (control.db, its backups) is KEPT and listed.
#   ./uninstall.sh --purge  # additionally delete the store files listed on screen, the scratch dir,
#                           # and (after a prompt) the dedicated Chrome profile
#   ./uninstall.sh --dir DIR   # skills root the skill was installed into (install.sh --dir)
#
# Order matters and is fixed: (1) the daemon is stopped and its service removed, because a live daemon
# imports from the tree being deleted; (2) our SessionStart entry leaves settings.json (other hooks
# untouched, settings.json.bak kept); (3) only then is code deleted; (4) the send journal is deleted
# ONLY under --purge, and the paths deleted are printed first.
#
# Everything is rooted at $HOME (skills dir, LaunchAgents, Claude settings, state) and launchctl is
# found on PATH, so `HOME=/tmp/h PATH=/tmp/stubs:$PATH ./uninstall.sh` is a complete dry environment.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$REPO/skill/scripts"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
PURGE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --dir)   SKILLS_DIR="${2:?--dir needs a directory}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
# Must match install.sh's NAME — the skill installs as "chatgpt-consult".
DEST="$SKILLS_DIR/chatgpt-consult"

# guard_rm_rf PATH KIND
#   KIND is "scratch" or "profile" — both must additionally look like a
#   dedicated cgc dir (basename contains "cgc", or the dir contains a tool
#   marker file/dir) before they're eligible for deletion.
# Refuses (prints a message, returns 1, does NOT abort the script) when the
# resolved path is empty, "/", "$HOME", or "$HOME/", or doesn't look like a
# dedicated cgc dir.
guard_rm_rf() {
  raw_path="$1"
  kind="$2"

  if [ -z "$raw_path" ]; then
    echo "• refusing to delete: path is empty" >&2
    return 1
  fi

  # Resolve to an absolute path without requiring the path to exist.
  resolved="$raw_path"
  if [ -e "$raw_path" ]; then
    resolved="$(cd "$raw_path" 2>/dev/null && pwd -P || echo "$raw_path")"
  fi

  # Strip a single trailing slash (but keep "/" itself as "/").
  case "$resolved" in
    */) [ "$resolved" != "/" ] && resolved="${resolved%/}" ;;
  esac

  home_resolved="$HOME"
  case "$home_resolved" in
    */) [ "$home_resolved" != "/" ] && home_resolved="${home_resolved%/}" ;;
  esac

  if [ -z "$resolved" ] || [ "$resolved" = "/" ] || [ "$resolved" = "$home_resolved" ]; then
    echo "• refusing to delete '$raw_path' (resolves to '$resolved') — looks like / or \$HOME" >&2
    return 1
  fi

  if [ "$kind" = "scratch" ] || [ "$kind" = "profile" ]; then
    base="$(basename "$resolved")"
    has_marker=0
    case "$base" in
      *cgc*) has_marker=1 ;;
    esac
    if [ "$has_marker" != "1" ] && [ -e "$resolved/.cgc" ]; then
      has_marker=1
    fi
    if [ "$has_marker" != "1" ]; then
      echo "• refusing to delete '$resolved' — doesn't look like a dedicated cgc $kind dir (basename has no 'cgc' and no .cgc marker found)" >&2
      return 1
    fi
  fi

  echo "• will remove: $resolved"
  rm -rf "$resolved"
  return 0
}

# What the store layer itself resolves, so this can never disagree with the daemon about where the
# journal is (config-file overrides included). Prints "dir<TAB>path" lines: `data` = the durable data
# dir, `path` = each existing store file in it (control.db, -wal/-shm, pre-migration backups, locks,
# drift bundles), `scratch` = the tool-owned scratch dir. Opens nothing, creates nothing.
resolve_state() {
  python3 - "$SCRIPTS" <<'PYEOF'
import glob, os, sys
sys.path.insert(0, sys.argv[1])
import cgc_store
db = cgc_store.db_path()
print("scratch\t" + cgc_store.CGC_STATE_DIR)
if db != ":memory:":
    d = os.path.dirname(os.path.abspath(db))
    print("data\t" + d)
    found = [db, db + "-wal", db + "-shm", db + ".migrate.lock", os.path.join(d, ".relocation.lock")]
    # "locks" and "incidents" are generic names: only claim them in a directory that is the tool's own
    # (an explicit CGC_STORE_DB can point the store into any directory).
    if "cgc" in os.path.basename(d):
        found += [os.path.join(d, "locks"), os.path.join(d, "incidents")]
    found += sorted(glob.glob(glob.escape(db) + ".v*.bak*"))
    found += sorted(glob.glob(os.path.join(glob.escape(d), ".control.db.relocating-*")))
    for p in found:
        if os.path.lexists(p):
            print("path\t" + p)
PYEOF
}

# --- 1. the daemon: stop it, wait until it has exited, remove its launchd service ----------------
# Before any file goes: a live daemon imports modules lazily from the tree about to be deleted.
echo "• stopping the egress daemon service"
if ! CGC_HOME="$REPO/skill" "$REPO/bin/cgc" uninstall-daemon; then
  echo "ERROR: the egress daemon would not stop. Nothing else was removed; re-run when it has stopped." >&2
  exit 1
fi
# A daemon outside launchd (`cgc watch`, `cgc up`) is the user's own process: not ours to kill.
if live="$(CGC_HOME="$REPO/skill" "$REPO/bin/cgc" daemon-pid 2>/dev/null)"; then
  echo "ERROR: an egress daemon (pid $live) is still running outside launchd — it would run on deleted code." >&2
  echo "       Stop it (Ctrl-C the foreground \`cgc watch\`, or: kill $live), then re-run uninstall.sh." >&2
  exit 1
fi

# --- 2. the SessionStart hook: only the entry cgc_activation installed ---------------------------
# JSON-aware (other hooks and settings are untouched; settings.json.bak keeps the previous file).
# A settings.json that does not parse is ignored whole by Claude Code, so no hook of ours is firing
# from it: say so and go on. Any other failure to edit a parseable file stops the uninstall, because
# the live hook would then run on deleted code.
echo "• removing our SessionStart hook entry"
hook_state="$(python3 "$SCRIPTS/cgc_activation.py" status --json 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["state"])' 2>/dev/null)" || hook_state="unknown"
if [ "$hook_state" = "unreadable" ]; then
  echo "  ! Claude settings.json does not parse, so it was not edited. If it holds our hook (a command" >&2
  echo "    mentioning ACTIVATION.md / cgc_activation.py), delete that entry by hand." >&2
elif ! python3 "$SCRIPTS/cgc_activation.py" remove; then
  echo "ERROR: could not edit Claude settings.json (see above). The skill was NOT removed." >&2
  echo "       Fix it or remove the hook by hand, then re-run uninstall.sh." >&2
  exit 1
fi

# --- 3. the code --------------------------------------------------------------------------------
if [ -e "$DEST" ] || [ -L "$DEST" ] || [ -e "${DEST}.bak" ] || [ -L "${DEST}.bak" ]; then
  rm -rf "$DEST" "${DEST}.bak"
  echo "• removed $DEST (and ${DEST}.bak)"
else
  echo "• nothing installed at $DEST"
fi

# --- 4. the send journal: kept unless --purge ---------------------------------------------------
STATE="$(resolve_state)" || { echo "ERROR: could not resolve the store location; store left untouched." >&2; exit 1; }
DATA_DIR=""; SCRATCH=""; JOURNAL=()
while IFS=$'\t' read -r kind val; do
  case "$kind" in
    data)    DATA_DIR="$val" ;;
    scratch) SCRATCH="$val" ;;
    path)    JOURNAL+=("$val") ;;
  esac
done <<<"$STATE"

if [ "$PURGE" != "1" ]; then
  if [ "${#JOURNAL[@]}" -gt 0 ]; then
    echo "• KEPT — the consult store (send journal, round history, answers' index). Not deleted without --purge:"
    printf '    %s\n' "${JOURNAL[@]}"
  fi
  echo "• also kept: scratch ${SCRATCH:-/tmp/cgc}, the Chrome profile, and your config file. --purge deletes the first two."
  echo "done."
  exit 0
fi

echo "• --purge: deleting EXACTLY these paths (there is no undo):"
if [ "${#JOURNAL[@]}" -gt 0 ]; then
  printf '    %s\n' "${JOURNAL[@]}"
else
  echo "    (no store files found)"
fi
echo "    $SCRATCH  (scratch: answer files, spool, daemon.log; only if it looks like a cgc dir)"
for p in ${JOURNAL[@]+"${JOURNAL[@]}"}; do
  rm -rf -- "$p"
done
# The data dir itself goes only if that left it empty: it may be shared with something else.
[ -n "$DATA_DIR" ] && rmdir "$DATA_DIR" 2>/dev/null && echo "• removed empty $DATA_DIR" || true

if guard_rm_rf "$SCRATCH" "scratch"; then
  echo "• removed scratch $SCRATCH"
fi

PROFILE="${CGC_PROFILE:-$HOME/.cgc-chrome}"
a=""
read -r -p "Delete the dedicated Chrome profile $PROFILE (logs you out of the consult session)? [y/N] " a || a=""
if [ "$a" = "y" ]; then
  if guard_rm_rf "$PROFILE" "profile"; then
    echo "• removed $PROFILE"
  fi
fi
echo "done."
