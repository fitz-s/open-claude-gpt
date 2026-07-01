#!/usr/bin/env bash
# Remove the open-claude-gpt skill install (and optionally its scratch + Chrome profile).
#   ./uninstall.sh          # remove the installed skill only
#   ./uninstall.sh --purge  # also delete scratch dir + the dedicated Chrome profile
set -euo pipefail

SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
DEST="$SKILLS_DIR/open-claude-gpt"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

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

if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  rm -rf "$DEST" "${DEST}.bak"
  echo "• removed $DEST"
else
  echo "• nothing installed at $DEST"
fi

if [ "$PURGE" = "1" ]; then
  STATE_DIR="${CGC_STATE_DIR:-/tmp/cgc}"
  if guard_rm_rf "$STATE_DIR" "scratch"; then
    echo "• removed scratch $STATE_DIR"
  fi

  PROFILE="${CGC_PROFILE:-$HOME/.cgc-chrome}"
  read -r -p "Delete the dedicated Chrome profile $PROFILE (logs you out of the consult session)? [y/N] " a
  if [ "$a" = "y" ]; then
    if guard_rm_rf "$PROFILE" "profile"; then
      echo "• removed $PROFILE"
    fi
  fi
fi
echo "done."
