#!/usr/bin/env bash
# open-claude-gpt installer.
# Copies (or symlinks) the skill into ~/.claude/skills/chatgpt-consult, checks
# dependencies, and runs the doctor. Idempotent — safe to re-run to upgrade.
#
#   ./install.sh            # copy the skill into ~/.claude/skills
#   ./install.sh --link     # symlink instead (dev: edits in the repo go live)
#   ./install.sh --dir DIR  # install into a different skills root
#   ./install.sh --force    # keep exit 0 even if the doctor reports not-ready
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
NAME="chatgpt-consult"
MODE="copy"
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --link) MODE="link"; shift ;;
    --dir)  SKILLS_DIR="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

DEST="$SKILLS_DIR/$NAME"
echo "open-claude-gpt → $DEST  (mode: $MODE)"

# --- dependency preflight ----------------------------------------------------
# The SAME pinned range CI tests (websocket-client>=1.6,<2): the installed product must not run a
# dependency version outside the tested contract. Verified by distribution VERSION, not by an
# import probe — an old/wrong install can still expose the probed symbol.
WSC_RANGE='websocket-client>=1.6,<2'
WSC_CHECK='
import sys
try:
    from importlib.metadata import version
except ImportError:  # py3.7 fallback never runs (floor is 3.8) but be safe
    sys.exit(1)
try:
    v = version("websocket-client")
except Exception:
    sys.exit(1)
major, minor = (int(x) for x in v.split(".")[:2])
sys.exit(0 if (major, minor) >= (1, 6) and major < 2 else 1)
'
command -v python3 >/dev/null || { echo "ERROR: python3 not found" >&2; exit 1; }
if ! python3 -c "$WSC_CHECK" 2>/dev/null; then
  echo "• installing Python dep: $WSC_RANGE"
  python3 -m pip install --user "$WSC_RANGE" >/dev/null 2>&1 || true
  if ! python3 -c "$WSC_CHECK" 2>/dev/null; then
    if [ "$FORCE" = "1" ]; then
      echo "  ! websocket-client not in the tested range (>=1.6,<2) — --force: continuing anyway" >&2
    else
      echo "ERROR: websocket-client is missing or outside the tested range (>=1.6,<2)." >&2
      echo "       Fix: python3 -m pip install --user '$WSC_RANGE'   (or re-run with --force)" >&2
      exit 1
    fi
  fi
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

# --- restart the egress daemon so it picks up the new code -------------------
# An upgrade that leaves the OLD daemon running is a version split-brain: the CLI
# writes through new code while the daemon serves old code (observed live: the old
# daemon rebuilt an empty pre-relocation store and consults silently stalled). So a
# failed restart FAILS the upgrade — the daemon's heartbeat identity check would
# refuse every enqueue anyway; better to say so now than per-consult later.
DAEMON_LABEL="com.open-claude-gpt.daemon"
if [ "$(uname -s)" = "Darwin" ] && launchctl print "gui/$(id -u)/$DAEMON_LABEL" >/dev/null 2>&1; then
  echo "• restarting the egress daemon (picks up the upgraded code)"
  if ! launchctl kickstart -k "gui/$(id -u)/$DAEMON_LABEL"; then
    if [ "$FORCE" = "1" ]; then
      echo "  ! could not restart the daemon — --force: continuing; run: cgc install-daemon" >&2
    else
      echo "ERROR: could not restart the egress daemon — the OLD code would keep serving consults." >&2
      echo "       Fix: launchctl kickstart -k \"gui/\$(id -u)/$DAEMON_LABEL\"   (or: cgc install-daemon)" >&2
      exit 1
    fi
  fi
fi

echo "• installed. running doctor…"
echo
DOCTOR_STATUS=0
CGC_STATE_DIR="${CGC_STATE_DIR:-/tmp/cgc}" python3 "$DEST/scripts/cgc_doctor.py" || DOCTOR_STATUS=$?

cat <<EOF

Next steps
  1. Start the dedicated debug Chrome and log into ChatGPT once:
       bash "$DEST/scripts/cdp_launch.sh"
  2. (optional) Point consults at your own ChatGPT project:
       export CGC_PROJECT_URL="https://chatgpt.com/g/g-p-<id>-<slug>/project"
  3. In Claude Code the skill auto-activates ON DEMAND: Claude reads its SKILL.md
     description and invokes it when a task fits. Nothing else is required.
  4. (optional) PROACTIVE background offloading — if you want Claude to reach for a
     consult on its own every session, add a SessionStart hook that injects this
     skill's activation note. This edits YOUR OWN Claude settings, so the installer
     does NOT do it for you — print the ready-to-paste snippet with:
       bin/cgc activation-hook
     (See docs/INSTALL.md → "Proactive activation". The note lives at
      $DEST/ACTIVATION.md.)
     bin/cgc doctor         # re-check health anytime

Config lives in the environment — see docs/CONFIGURATION.md and .env.example.
EOF

if [ "$DOCTOR_STATUS" -ne 0 ]; then
  if [ "$FORCE" = "1" ]; then
    echo
    echo "• installed, but NOT ready — see doctor output above (--force: continuing anyway)"
    exit 0
  fi
  echo
  echo "installed, but NOT ready — see doctor output above" >&2
  exit "$DOCTOR_STATUS"
fi
