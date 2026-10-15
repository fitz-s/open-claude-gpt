#!/usr/bin/env bash
# open-claude-gpt installer.
# Copies (or symlinks) the skill into ~/.claude/skills/chatgpt-consult, checks
# dependencies, and runs the doctor. Idempotent — safe to re-run to upgrade. An upgrade stops the
# launchd egress daemon first and waits until it has exited, replaces the files, starts it again and
# checks its heartbeat; nothing is replaced under a running daemon, and the consult store is never
# touched.
#
# Everything is rooted at $HOME (skills dir, LaunchAgents, Claude settings, config) and launchctl is
# found on PATH, so `HOME=/tmp/h PATH=/tmp/stubs:$PATH ./install.sh` is a complete dry environment.
#
#   ./install.sh                  # copy the skill into ~/.claude/skills
#   ./install.sh --link           # symlink instead (dev: edits in the repo go live)
#   ./install.sh --dir DIR        # install into a different skills root
#   ./install.sh --force          # keep exit 0 even if the doctor reports not-ready
#   ./install.sh --activation-hook  # also install the OPTIONAL proactive SessionStart hook
#                                  # (backs up settings.json first) — default: not installed
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
NAME="chatgpt-consult"
MODE="copy"
FORCE=0
ACTIVATION_HOOK=0

while [ $# -gt 0 ]; do
  case "$1" in
    --link) MODE="link"; shift ;;
    --dir)  SKILLS_DIR="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --activation-hook) ACTIVATION_HOOK=1; shift ;;
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

# --- refuse to put older code over a newer store ----------------------------
# The store refuses to open at a schema newer than the code understands, so an older checkout
# installed over a newer journal would leave a daemon that can never start. Say so BEFORE anything is
# stopped or replaced. The check is read-only: it neither creates nor migrates the store.
if ! python3 "$REPO/skill/scripts/cgc_doctor.py" --check-store; then
  if [ "$FORCE" = "1" ]; then
    echo "  ! the consult store is not readable by this checkout — --force: continuing" >&2
  else
    echo "ERROR: the consult store cannot be read by this checkout (see above). Nothing was changed." >&2
    echo "       Install from a checkout at least as new as the one that wrote it (or re-run with --force)." >&2
    exit 1
  fi
fi

# --- quiesce the egress daemon BEFORE touching any file ----------------------
# A running daemon imports modules lazily, per round. Replacing the tree under it lets one process
# execute old and new files together (the half-replaced tree), and a round claimed in that window runs
# on a mixture. So the order is: stop, wait until the PROCESS has exited and daemon.lock is free
# (never judged by heartbeat age: a draining or wedged daemon has a stale heartbeat and still runs the
# files), replace, start, verify. A daemon outside launchd (`cgc watch`, `cgc up`) is not ours to kill:
# daemon-stop refuses and names its pid.
# The repo's own cgc does all of this, so a first install and a half-replaced install both work.
CGC="$REPO/bin/cgc"
ctl() { CGC_HOME="$REPO/skill" "$CGC" "$@"; }

STOPPED=0      # 1 while a daemon WE stopped has not been started again
STARTED=0      # 1 once the new daemon was asked to start (a rollback must stop it first)
SWAPPING=0     # 1 from just before the first rename of the swap
DONE=0         # 1 once the new daemon is verified: from there on nothing is rolled back
STAGE="$SKILLS_DIR/.$NAME.new.$$"
ASIDE="$DEST.bak.prev.$$"
OLD_ID=""      # identity (device:inode) of the trees the swap moves: the install being replaced,
PREV_ID=""     # the older backup parked beside it, and the new tree.
NEW_ID=""      # A rollback looks for them on disk by identity; it never trusts a progress variable.

# The identity of a path ITSELF (a symlink is not followed), "device:inode"; empty when it is absent.
# A rename keeps it, so it says which tree is which wherever the renames got to.
ident() {
  python3 -c 'import os, sys
try:
    s = os.lstat(sys.argv[1])
except OSError:
    sys.exit(0)
print("%d:%d" % (s.st_dev, s.st_ino))' "$1" 2>/dev/null || true
}

# Trees that a rename did not leave where it was aimed (see place), found by identity.
STRAYS=()

# where_is ID: the first of the places the swap uses (and any stray) whose own identity is ID.
where_is() {
  local id="$1" p
  [ -n "$id" ] || return 0
  for p in "$DEST" "$DEST.bak" "$ASIDE" "$STAGE" ${STRAYS[@]+"${STRAYS[@]}"}; do
    if [ "$(ident "$p")" = "$id" ]; then printf '%s' "$p"; return 0; fi
  done
  return 0
}
show_where() { local w; w="$(where_is "$1")"; printf '%s' "${w:-(not found)}"; }

# Rename SRC to DST, and PROVE it landed there. A bare mv moves SRC INTO an existing directory, or through
# a symlink to one, into whatever that points at (the repo, for a --link install); so DST must be free
# when we start, `mv -n` never overwrites, and since neither stops a directory or symlink that appears at
# DST between the check and the rename, the result is checked by file identity: the path DST itself (not
# what a symlink there points at) must now be the tree that was SRC. If not, say where it is and fail;
# the caller aborts the swap. Only a concurrent writer to the skills directory can get here, and that is
# detected, not defended against further.
place() {
  local src="$1" dst="$2" id inside
  id="$(ident "$src")"
  if [ -z "$id" ]; then
    echo "ERROR: cannot move $src to $dst: $src does not exist." >&2
    return 1
  fi
  if [ -e "$dst" ] || [ -L "$dst" ]; then
    echo "ERROR: refusing to move $src onto $dst: it already exists." >&2
    return 1
  fi
  if ! mv -n -- "$src" "$dst" 2>/dev/null; then
    # a mv without -n, or a real failure: retry once plainly if nothing moved and DST is still free
    if [ "$(ident "$src")" = "$id" ] && [ ! -e "$dst" ] && [ ! -L "$dst" ]; then mv -- "$src" "$dst" || true; fi
  fi
  if [ "$(ident "$dst")" = "$id" ]; then return 0; fi
  inside="$dst/$(basename -- "$src")"
  if [ "$(ident "$inside")" = "$id" ]; then
    STRAYS+=("$inside")
    echo "ERROR: moving $src to $dst did not put it there: something appeared at $dst meanwhile and the tree went INTO it, to $inside." >&2
  elif [ "$(ident "$src")" = "$id" ]; then
    echo "ERROR: moving $src to $dst failed; the tree is still at $src." >&2
  else
    echo "ERROR: moving $src to $dst left nothing at $dst and nothing at $src; look for the tree by name." >&2
  fi
  return 1
}

# Signals are exit requests everywhere except where they must not cut in. While deferred, one is only
# noted; allow_signals restores the handlers and then acts on a noted one, so the work between the two
# is never interrupted by a signal and a signal is never lost. The rollback runs deferred as well: a
# second Ctrl-C must not stop it half way.
PENDING=""
defer_signals() { PENDING=""; trap 'PENDING=130' INT; trap 'PENDING=143' TERM; trap 'PENDING=129' HUP; }
allow_signals() {
  trap 'exit 130' INT; trap 'exit 143' TERM; trap 'exit 129' HUP
  if [ -n "$PENDING" ]; then exit "$PENDING"; fi
}

# Put everything back as it was before the swap. It reads the state from the disk: the new tree is
# whatever carries the new tree's identity, the old tree is wherever the old identity is, and nothing is
# ever renamed onto an existing path. Exits non-zero (and says where the old tree is) if it cannot.
rollback() {
  local old new prev
  if [ "$STARTED" = 1 ]; then              # the new-code daemon must be gone before its tree is removed
    rc=0; ctl daemon-stop || rc=$?
    if [ "$rc" = 1 ]; then
      echo "ERROR: the new daemon would not stop; leaving the trees as they are." >&2
      return 1
    fi
    STARTED=0
  fi
  old="$(where_is "$OLD_ID")"
  new="$(where_is "$NEW_ID")"
  prev="$(where_is "$PREV_ID")"
  if [ -n "$new" ] && [ "$new" = "$DEST" ]; then rm -rf -- "$DEST" || return 1; fi
  if [ -n "$old" ] && [ "$old" != "$DEST" ]; then place "$old" "$DEST" || return 1; fi
  if [ -n "$prev" ] && [ "$prev" != "$DEST.bak" ]; then place "$prev" "$DEST.bak" || return 1; fi
  [ -z "$OLD_ID" ] || [ "$(ident "$DEST")" = "$OLD_ID" ]
}

on_exit() {
  status=$?
  set +e
  trap - EXIT
  defer_signals
  rm -rf -- "$STAGE"
  if [ "$status" -ne 0 ] && [ "$DONE" != 1 ]; then
    ok=1
    if [ "$SWAPPING" = 1 ] || [ "$STARTED" = 1 ]; then
      echo "• install did not finish — restoring the previous install" >&2
      rollback || { ok=0; echo "ERROR: could not restore the previous install (see above). The old tree is at: $(show_where "$OLD_ID"). The older backup is at: $(show_where "$PREV_ID")." >&2; }
    fi
    if [ "$STOPPED" = 1 ] && [ "$ok" = 1 ]; then
      echo "• restarting the egress daemon on the previous install" >&2
      ctl daemon-start --any >/dev/null 2>&1 || echo "  ! could not restart the daemon: run cgc daemon-start" >&2
    fi
  fi
  exit "$status"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# Stop the daemon. A signal that arrives while it is being stopped must not leave it stopped with nobody
# to start it again, so the stop and the note that it happened are one deferred step.
defer_signals
rc=0
ctl daemon-stop || rc=$?
case "$rc" in
  0)  STOPPED=1 ;;
  10) ;;                                  # nothing loaded, nothing running
  *)  echo "ERROR: cannot make sure no egress daemon is running (see above) — not replacing files under it." >&2
      echo "       Nothing was replaced." >&2
      exit 1 ;;
esac
allow_signals
if [ "$STOPPED" = 1 ]; then echo "• stopped the egress daemon (it starts again on the new code)"; fi

# --- install the skill payload ------------------------------------------------
# Build the new tree beside the old one, then swap with renames. A failed copy (disk full,
# permissions) never touches the previous install, and a failure after the old tree is parked rolls
# it back. The stage is a dot-directory so a skill scanner never sees a second copy of SKILL.md.
# An existing .bak is a PREVIOUS install, possibly the only intact one (an earlier upgrade may have
# failed): it is parked, not deleted, and dropped only once the new daemon is verified.
mkdir -p "$SKILLS_DIR"
for s in "$SKILLS_DIR/.$NAME.new."*; do   # stages of installs that died (SIGKILL cannot be trapped)
  if [ -e "$s" ] || [ -L "$s" ]; then kill -0 "${s##*.new.}" 2>/dev/null || rm -rf -- "$s"; fi
done
# An install killed (SIGKILL) between "old tree set aside" and "new tree in place" leaves the old tree
# only at .bak and nothing at DEST. Put it back first, so this run is an ordinary upgrade and a failure
# of it restores a working install. That run also stopped the daemon: this one starts it again.
if [ ! -e "$DEST" ] && [ ! -L "$DEST" ] && { [ -e "$DEST.bak" ] || [ -L "$DEST.bak" ]; }; then
  echo "• an earlier install was interrupted (the old tree is at $DEST.bak, nothing at $DEST) — restoring it first"
  # The rename and the note "this run owes the daemon a start" are one step to a signal: between them a TERM
  # would leave the old tree back in place and the daemon unloaded with nobody to start it.
  defer_signals
  place "$DEST.bak" "$DEST"
  # that run had stopped the daemon before it died; this one starts it again if there is a service to start
  if [ -e "$HOME/Library/LaunchAgents/com.open-claude-gpt.daemon.plist" ]; then STOPPED=1; fi
  allow_signals
fi
OLD_ID="$(ident "$DEST")"
PREV_ID="$(ident "$DEST.bak")"
rm -rf -- "$STAGE"
if [ "$MODE" = "link" ]; then
  ln -s "$REPO/skill" "$STAGE"
else
  cp -R "$REPO/skill" "$STAGE"
  chmod +x "$STAGE"/scripts/*.sh "$STAGE"/scripts/*.py 2>/dev/null || true
fi
NEW_ID="$(ident "$STAGE")"
[ -z "$OLD_ID" ] || echo "• existing install found — keeping it as ${DEST}.bak"
SWAPPING=1
defer_signals                              # the three renames are not interrupted; a signal waits for them
if [ -n "$OLD_ID" ]; then
  if [ -n "$PREV_ID" ]; then place "$DEST.bak" "$ASIDE"; fi
  place "$DEST" "$DEST.bak"
fi
place "$STAGE" "$DEST"
allow_signals                              # a signal that came meanwhile acts now: on_exit rolls the swap back

# --- start the egress daemon on the new code ----------------------------------
# An upgrade that leaves the OLD daemon running is a version split-brain: the CLI writes through new
# code while the daemon serves old code (observed live: the old daemon rebuilt an empty pre-relocation
# store and consults silently stalled). The daemon was stopped above, so this starts a fresh process
# and then checks that its heartbeat names this install's store schema. A start that does not come up
# FAILS the upgrade and rolls back to the previous install and daemon: the daemon's identity check
# would refuse every enqueue anyway, so say so now.
KEEP_PREV=0
if [ "$STOPPED" = 1 ]; then
  echo "• starting the egress daemon"
  STARTED=1
  if ! ctl daemon-start; then
    if [ "$FORCE" = "1" ]; then
      echo "  ! the daemon did not come up — --force: keeping the new install; run: cgc daemon-start" >&2
      KEEP_PREV=1
    else
      echo "ERROR: the egress daemon did not come up on the new code (log: ${CGC_STATE_DIR:-/tmp/cgc}/daemon.log)." >&2
      echo "       Rolling back to the previous install. Retry with --force to keep the new one anyway." >&2
      exit 1
    fi
  fi
  STOPPED=0
fi
DONE=1
if [ -n "$PREV_ID" ]; then
  if [ "$KEEP_PREV" = 1 ]; then
    echo "• an older backup is kept at $ASIDE (the new daemon was not verified)"
  elif [ "$(ident "$ASIDE")" = "$PREV_ID" ]; then
    rm -rf -- "$ASIDE"                     # the only place an old tree is ever deleted: after verification
  fi
fi

echo "• installed. running doctor…"
echo
DOCTOR_STATUS=0
CGC_STATE_DIR="${CGC_STATE_DIR:-/tmp/cgc}" python3 "$DEST/scripts/cgc_doctor.py" || DOCTOR_STATUS=$?

# --- optional: install the proactive SessionStart hook -----------------------
# Opt-in only, per --activation-hook. Default install.sh behavior is unchanged: without the
# flag this block does not run and nothing about settings.json is touched.
if [ "$ACTIVATION_HOOK" = "1" ]; then
  echo
  echo "• --activation-hook: installing the proactive SessionStart hook"
  python3 "$DEST/scripts/cgc_activation.py" install || echo "  ! activation hook install failed — see above" >&2
fi

cat <<EOF

Next steps
  1. Start the dedicated debug Chrome and log into ChatGPT once:
       bash "$DEST/scripts/cdp_launch.sh"
     Then install the egress daemon once (it starts at login and respawns if it dies):
       bin/cgc install-daemon
  2. (optional) Point consults at your own ChatGPT project:
       export CGC_PROJECT_URL="https://chatgpt.com/g/g-p-<id>-<slug>/project"
  3. In Claude Code the skill auto-activates ON DEMAND: Claude reads its SKILL.md
     description and invokes it when a task fits. Nothing else is required.
  4. (optional) PROACTIVE background offloading — if you want Claude to reach for a
     consult on its own every session, add a SessionStart hook that injects this
     skill's activation note. This edits YOUR OWN Claude settings, so the installer
     does NOT do it for you by default — two ways to opt in:
       bin/cgc activation-hook              # print the snippet (changes nothing)
       bin/cgc activation-hook --install    # add it for you (backs up settings.json first)
     (See docs/INSTALL.md → "Activation: on-demand vs. proactive". The note lives at
      $DEST/ACTIVATION.md.)
  5. bin/cgc doctor         # re-check health anytime

Config lives in the environment — see docs/CONFIGURATION.md and .env.example.
EOF

# Doctor exit: 0 ready; 3 installed but cannot send yet (no daemon / Chrome / login: the next steps
# above); anything else = the install itself is incomplete.
if [ "$DOCTOR_STATUS" -eq 3 ]; then
  echo
  echo "• installed. Consults cannot be sent yet — see can_send in the doctor output above."
  exit 0
fi
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
