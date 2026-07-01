#!/usr/bin/env bash
# Remove the open-claude-gpt skill install (and optionally its scratch + Chrome profile).
#   ./uninstall.sh          # remove the installed skill only
#   ./uninstall.sh --purge  # also delete scratch dir + the dedicated Chrome profile
set -euo pipefail

SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
DEST="$SKILLS_DIR/open-claude-gpt"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  rm -rf "$DEST" "${DEST}.bak"
  echo "• removed $DEST"
else
  echo "• nothing installed at $DEST"
fi

if [ "$PURGE" = "1" ]; then
  rm -rf "${CGC_STATE_DIR:-/tmp/cgc}"
  echo "• removed scratch ${CGC_STATE_DIR:-/tmp/cgc}"
  PROFILE="${CGC_PROFILE:-$HOME/.cgc-chrome}"
  read -r -p "Delete the dedicated Chrome profile $PROFILE (logs you out of the consult session)? [y/N] " a
  [ "$a" = "y" ] && { rm -rf "$PROFILE"; echo "• removed $PROFILE"; }
fi
echo "done."
