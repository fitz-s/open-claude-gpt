#!/usr/bin/env bash
# chatgpt-consult installer.
# Copies (or symlinks) the skill into ~/.claude/skills/chatgpt-consult, checks
# dependencies, and runs the doctor. Idempotent — safe to re-run to upgrade.
#
#   ./install.sh            # copy the skill into ~/.claude/skills
#   ./install.sh --link     # symlink instead (dev: edits in the repo go live)
#   ./install.sh --dir DIR  # install into a different skills root
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
NAME="chatgpt-consult"
MODE="copy"

while [ $# -gt 0 ]; do
  case "$1" in
    --link) MODE="link"; shift ;;
    --dir)  SKILLS_DIR="$2"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

DEST="$SKILLS_DIR/$NAME"
echo "chatgpt-consult → $DEST  (mode: $MODE)"

# --- dependency preflight (non-fatal warnings; doctor re-checks in detail) ----
command -v python3 >/dev/null || { echo "ERROR: python3 not found" >&2; exit 1; }
if ! python3 -c "import websocket; assert hasattr(websocket,'create_connection')" 2>/dev/null; then
  echo "• installing Python dep: websocket-client"
  python3 -m pip install --user websocket-client >/dev/null 2>&1 \
    || echo "  ! could not auto-install; run: pip install websocket-client" >&2
fi
command -v gh >/dev/null || echo "  ! gh CLI not found — install from https://cli.github.com (used by deliver)"

# --- install the skill payload ------------------------------------------------
mkdir -p "$SKILLS_DIR"
if [ -e "$DEST" ] || [ -L "$DEST" ]; then
  echo "• existing install found — backing up to ${DEST}.bak"
  rm -rf "${DEST}.bak"
  mv "$DEST" "${DEST}.bak"
fi

if [ "$MODE" = "link" ]; then
  ln -s "$REPO/skill" "$DEST"
else
  cp -R "$REPO/skill" "$DEST"
fi
chmod +x "$DEST"/scripts/*.sh "$DEST"/scripts/*.py 2>/dev/null || true

echo "• installed. running doctor…"
echo
CGC_STATE_DIR="${CGC_STATE_DIR:-/tmp/cgc}" python3 "$DEST/scripts/cgc_doctor.py" || true

cat <<EOF

Next steps
  1. Start the dedicated debug Chrome and log into ChatGPT once:
       bash "$DEST/scripts/cdp_launch.sh"
  2. (optional) Point consults at your own ChatGPT project:
       export CGC_PROJECT_URL="https://chatgpt.com/g/g-p-<id>-<slug>/project"
  3. In Claude Code, the skill auto-activates. Or drive it directly:
       bin/cgc doctor         # re-check health anytime

Config lives in the environment — see docs/CONFIGURATION.md and .env.example.
EOF
