#!/usr/bin/env bash
# Remove open-claude-gpt: the launchd daemon, the SessionStart hook entry, then the skill code.
#   ./uninstall.sh          # stop+remove the daemon service, drop our hook entry, remove the skill.
#                           # The consult store (control.db, its backups) is KEPT and listed.
#   ./uninstall.sh --purge  # additionally delete the tool's own files listed on screen: the store files,
#                           # lock files, drift bundles, scratch files; and (after a prompt) the dedicated
#                           # Chrome profile. Never a whole directory because of its name.
#   ./uninstall.sh --dir DIR   # skills root the skill was installed into (install.sh --dir)
#   ./uninstall.sh --force     # carry on when settings.json is not valid JSON (the hook entry is then
#                              # yours to delete by hand; the uninstall prints the step)
#
# Order matters and is fixed. (0) Every path is resolved and checked BEFORE anything changes; a refusal
# there leaves the machine exactly as it was. (1) The daemon is stopped, judged by its process and its
# lock, never by heartbeat age, and its launchd service removed, because a live daemon imports from the
# tree being deleted. (2) Our SessionStart entry leaves settings.json (other hooks untouched,
# settings.json.bak kept). (3) Only then is code deleted. (4) The send journal is deleted ONLY under
# --purge, only as the exact files the tool writes, and the paths are printed first.
#
# Your config file is never deleted, purge or not.
#
# Everything is rooted at $HOME (skills dir, LaunchAgents, Claude settings, state) and launchctl is
# found on PATH, so `HOME=/tmp/h PATH=/tmp/stubs:$PATH ./uninstall.sh` is a complete dry environment.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS="$REPO/skill/scripts"
SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
PURGE=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --force) FORCE=1; shift ;;
    --dir)   SKILLS_DIR="${2:?--dir needs a directory}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
# Must match install.sh's NAME — the skill installs as "chatgpt-consult".
DEST="$SKILLS_DIR/chatgpt-consult"
ctl() { CGC_HOME="$REPO/skill" "$REPO/bin/cgc" "$@"; }

# The planner. One program decides what may be deleted, so the listing and the deletion cannot
# disagree. `plan` scans and prints the plan; `delete PLANFILE` executes exactly that plan: it re-checks
# each listed path (still a regular file, still the same file, still named the way the tool names it)
# and deletes it, and it NEVER scans again, so a file that appeared after the plan was shown is not
# touched. Rows are tab-separated `kind<TAB>path<TAB>note` with "-" for an empty field:
#   refuse   note = why the whole uninstall must stop before changing anything
#   note     path = a directory left alone, note = why (shown under --purge)
#   journal / lock / incident / scratch   a file the tool wrote and --purge would delete;
#            note = its fingerprint "device:inode:size:mtime_ns:sha256", which the delete step must find
#            again. An inode alone does not identify a file: Linux filesystems hand a freed inode number
#            to the next file created, so `rm x; echo y > x` leaves the same device:inode, and a plan
#            that trusted it would delete the file someone put there. The size, the nanosecond mtime and a
#            SHA-256 of the WHOLE content (logs, answers and fixtures grow past any prefix: a change after
#            the first megabyte must still show) are what tell the two files apart. The file is opened
#            once, fstat'ed before and after the read on that same descriptor, and a file that changed
#            while it was read, or cannot be read, has no fingerprint and is never planned. This verifies;
#            it is not an atomic delete: a same-user process that swaps the file between the final check
#            and the unlink can still win that race, which is out of scope.
#   settings state (ok|absent|parse|blocked) in the note: what happened reading Claude's settings.json
#   profile  path = the Chrome profile to offer for deletion ("-" if none), note = why not
#   deleted / failed   (delete mode)
# Paths are compared by FILE IDENTITY (device and inode), never by their spelling: on a case-insensitive
# filesystem ACTIVE.JSON and active.json are one file, and a hard link or a symlink is the same file under
# another name. Then it applies, in order:
#   - a journal file is only ever one of the exact names the store writes beside its database
#     (db, -wal, -shm, .migrate.lock, .migrated, .v<n>.bak[.corrupt-*|.*.tmp], relocation leftovers),
#     a regular file, and the database and its backups must carry a SQLite header;
#   - locks/, incidents/ and spool/ are the tool's subdirectories: only files named the way the tool
#     names them are touched, one by one; a foreign file is left alone and so is its directory;
#   - nothing is ever removed because a directory's NAME looks like ours, and no directory is removed
#     recursively here; an emptied directory goes only if it is one of the tool's own subdirectories
#     (locks, incidents/I-*, spool and its subdirectories) or a default location (~/.local/state/cgc,
#     /tmp/cgc); a custom directory such as ~/Documents is left in place even when it ends up empty;
#   - files are touched only in the default locations (~/.local/state/cgc, /tmp/cgc, and below them) or
#     strictly below $HOME; $HOME itself, anything above it, and / never;
#   - the config file is never planned, and neither it nor the journal may lie inside a tree this
#     uninstall deletes.
plan_py() {
  python3 - "$SCRIPTS" "$1" "$DEST" "$PURGE" "${2:-}" <<'PYEOF'
import os, re, stat, sys

scripts, mode, dest, purge = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
planfile = sys.argv[5]
sys.path.insert(0, scripts)
import cgc_activation, cgc_config, cgc_store      # noqa: E402  (importing applies the config file, as every script does)

out, todo, refusals = [], [], []
FILE_KINDS = ("journal", "lock", "incident", "scratch")


def emit(kind, path="-", note="-"):
    out.append((kind, path or "-", note or "-"))


def refuse(msg):
    refusals.append(msg)
    emit("refuse", "-", msg)


def refuse_if_deleting(msg):
    """Something that makes DELETING unsafe. Without --purge nothing in the store is deleted, so it is
    only a note: it must not stop the service, hook and code from being removed."""
    if purge:
        refuse(msg)


def real(p):
    return os.path.realpath(p)


def fid(p):
    """(st_dev, st_ino) of what `p` names, following symlinks; None when it does not exist."""
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def same(a, b):
    """Do two paths name the same file or directory? By identity, so a case alias (APFS), a hard link or
    a symlink counts. Only when one of them does not exist does it fall back to the resolved spelling."""
    ia, ib = fid(a), fid(b)
    if ia is not None and ib is not None:
        return ia == ib
    return real(a) == real(b)


def within(p, d, strict=False):
    """Is `p` inside directory `d` (or `d` itself, unless strict)? Walks p's ancestors, from p itself up
    to /, and compares each to d by identity, so `Documents/x` is inside `documents`."""
    did, rd = fid(d), real(d)
    cur, first = real(p), True
    while True:
        if (did is not None and fid(cur) == did) or (did is None and cur == rd):
            return not (strict and first)
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur, first = parent, False


CHUNK = 1 << 20


def fingerprint(p, st=None):
    """What a planned file is: "dev:ino:size:mtime_ns:sha256-of-all-of-it". The delete step compares it to the
    one in the plan, so a different file that reuses the inode, or the same file with other content under an
    unchanged size and mtime, is not mistaken for the planned one.

    The file is opened ONCE (never following a symlink, never blocking on a fifo), and the stat taken before
    and after the read is the fstat of that descriptor, so the numbers and the bytes describe one file even
    if the name is swapped meanwhile. None when it is not a regular file, cannot be read, or changed while it
    was read: it is then neither planned nor deleted. `st` (an lstat the caller already holds) must agree
    with the descriptor, or the name was swapped between the caller's look and ours."""
    import hashlib
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(p, flags)
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        if st is not None and (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            return None
        h = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            h.update(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    key = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)      # noqa: E731
    if key(before) != key(after) or total != before.st_size:
        return None                         # it changed while being read: not a stable file
    return f"{before.st_dev}:{before.st_ino}:{before.st_size}:{before.st_mtime_ns}:{h.hexdigest()}"


def tabbed(p):
    return "\t" in p or "\n" in p


HOME = real(os.path.expanduser("~"))
DEF_DATA = real(os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "cgc"))
DEF_STATE = real("/tmp/cgc")


def allowed(d):
    """A directory this uninstall may delete tool files from: a default location or below one, or strictly
    below $HOME. Never $HOME itself, a directory above it, or / (an unrelated directory elsewhere)."""
    return within(d, DEF_DATA) or within(d, DEF_STATE) or within(d, HOME, strict=True)


def lstat_regular(p):
    """The lstat of `p` if it is a regular file (a symlink or a directory is not), else None."""
    try:
        st = os.lstat(p)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def sqlite_header(p):
    try:
        if os.path.getsize(p) == 0:
            return True                                  # SQLite creates the file before its first write
        with open(p, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def journal_rx(b):
    e = re.escape(b)
    named = re.compile(e + r"(-wal|-shm|\.migrate\.lock|\.migrated|\.v\d+\.bak(\.corrupt-\d+|\.\w+\.tmp)?)?"
                       r"|\.relocation\.lock|\.control\.db\.relocating-[0-9a-f]{32}|\.corrupt-[0-9a-f]{32}")
    header = re.compile(e + r"(\.migrated|\.v\d+\.bak)?")
    return named, header


RID = r"REQ-\d{8}-\d{6}-[0-9a-f]{6}"
LOCKS = re.compile(rf"daemon\.json(\.tmp-\d+-\d+)?|daemon\.lock|browser\.lock|lease\.{RID}|conv\.[0-9a-f-]{{36}}")
SCRATCH = re.compile(rf"answer_({RID}|followup)\.txt(\.raw|\.\w+\.tmp)?|prompt_{RID}\.md|refs_\d{{8}}-\d{{6}}(-[0-9a-f]{{6}})?\.md"
                     rf"|_wait_{RID}\.txt(\.raw)?|active\.json(\.lock|\.tmp-\d+)?|daemon\.log|chrome\.log|\.doctor_probe")
SPOOLED = re.compile(r"REQ-[\w-]+\.(log|json)(\.tmp-[\d-]+)?")
INCIDENT = re.compile(r"I-\d{8}-\d{6}-[a-z]+-[0-9a-f]{6}")
INCIDENT_FILES = re.compile(r"fixture\.html|diagnosis\.json")
TOOL_DIRS = {"locks", "incidents", "spool", "logs", "pending", "processing", "done", "status"}

config = cgc_config.config_path()
db = cgc_store.db_path()
state = cgc_store.CGC_STATE_DIR
data = cgc_store.data_dir()
legacy = os.path.join(state, "control.db")
DBNAMES = {os.path.basename(db), "control.db"} if db != ":memory:" else {"control.db"}


def name_ok(kind, p):
    """Is `p` named the way the tool names a file of this kind? (Used by the plan and again by the delete.)"""
    b = os.path.basename(p)
    parent = os.path.basename(os.path.dirname(p))
    if kind == "journal":
        return any(journal_rx(n)[0].fullmatch(b) for n in DBNAMES)
    if kind == "lock":
        return parent == "locks" and bool(LOCKS.fullmatch(b))
    if kind == "incident":
        return bool(INCIDENT.fullmatch(parent)) and bool(INCIDENT_FILES.fullmatch(b))
    if kind == "scratch":
        return bool(SCRATCH.fullmatch(b) or (parent in TOOL_DIRS and SPOOLED.fullmatch(b)))
    return False


def header_ok(kind, p):
    """The database and its backups must be SQLite files; every other journal name is not checked."""
    if kind != "journal":
        return True
    b = os.path.basename(p)
    return not any(journal_rx(n)[1].fullmatch(b) for n in DBNAMES) or sqlite_header(p)


# ================================================================================ delete: the plan only
if mode == "delete":
    with open(planfile, encoding="utf-8") as f:
        rows = [ln.rstrip("\n").split("\t") for ln in f if ln.strip()]
    if any(r[0] == "refuse" for r in rows):
        sys.exit(1)
    gone = []
    for row in rows:
        if len(row) != 3 or row[0] not in FILE_KINDS:
            continue
        kind, p, ident = row
        st = lstat_regular(p)
        if st is None:
            if os.path.lexists(p):
                print("failed\t" + p + "\tno longer a regular file; left alone")
            continue                       # already gone (e.g. it lay inside the code tree removed a step ago)
        if same(p, config):
            # at plan time `p` was not the config file; it is now (a link or alias made since). The
            # config is never deleted, so the plan is not honoured for this path.
            print("failed\t" + p + "\tis your config file; left alone")
        elif fingerprint(p, st) != ident:
            print("failed\t" + p + "\tis not the file that was planned (replaced or changed since); left alone")
        elif not name_ok(kind, p) or not allowed(os.path.dirname(p)) or not header_ok(kind, p):
            print("failed\t" + p + "\tno longer matches what the tool writes; left alone")
        else:
            try:
                os.unlink(p)
                gone.append(p)
                print("deleted\t" + p + "\t" + kind)
            except OSError as e:
                print("failed\t" + p + "\t" + str(e))
    # directories the tool made: each goes only if empty once its own files are gone, never recursively
    dirs = set()
    for p in gone:
        d = os.path.dirname(p)
        while d and not same(d, HOME) and not within(HOME, d):
            dirs.add(d)
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    for d in sorted(dirs, key=len, reverse=True):
        if not os.path.isdir(d) or os.path.islink(d) or not allowed(d):
            continue
        b = os.path.basename(d.rstrip(os.sep))
        if not (same(d, DEF_DATA) or same(d, DEF_STATE) or b in TOOL_DIRS or INCIDENT.fullmatch(b)):
            continue
        try:
            os.rmdir(d)
            print("deleted\t" + d + "\tempty directory")
        except OSError:
            pass
    sys.exit(0)


# ================================================================================ plan: scan, then print
def plan_file(kind, p, seen):
    st = lstat_regular(p)
    if st is None or not name_ok(kind, p):
        return False
    key = (st.st_dev, st.st_ino, os.path.basename(p))
    if key in seen:
        return False
    if tabbed(p):
        refuse_if_deleting(f"{p} has a tab or newline in its path and cannot be listed safely; remove it by hand.")
        return False
    fp = fingerprint(p, st)
    if fp is None:
        refuse_if_deleting(f"{p} cannot be read or is changing, so it cannot be planned for deletion; "
                           "remove it by hand if it is yours.")
        return False
    seen.add(key)
    todo.append((kind, p, fp))
    return True


def plan_dir(d, ok, kind, seen):
    """Plan the tool-named files directly in `d` (one tool subdirectory)."""
    if not os.path.isdir(d) or os.path.islink(d):
        return
    if not allowed(d):
        emit("note", d, "is $HOME, above it, or outside it and not the tool's default location; "
                        "no files are deleted there")
        return
    for n in sorted(os.listdir(d)):
        if ok.fullmatch(n):
            plan_file(kind, os.path.join(d, n), seen)


def plan_journal(dbpath, seen):
    d = real(os.path.dirname(os.path.abspath(dbpath)))
    if not os.path.isdir(d):
        return
    named, header = journal_rx(os.path.basename(dbpath))
    found = []
    for n in sorted(os.listdir(d)):
        if not named.fullmatch(n):
            continue
        p = os.path.join(d, n)
        if lstat_regular(p) is None:
            refuse_if_deleting(f"{p} is not a regular file; the store never writes it. Left alone.")
        elif header.fullmatch(n) and not sqlite_header(p):
            refuse_if_deleting(f"{p} does not start with a SQLite header, so it is not the store's "
                               "database. Left alone.")
        else:
            found.append(p)
    if found and not allowed(d):
        refuse_if_deleting(f"store files {found[0]} ... sit in {d}, which is $HOME, above it, or outside it "
                           "and not a default location; this uninstall will not delete there. "
                           "Remove them by hand.")
        return
    for p in found:
        plan_file("journal", p, seen)


# ---- Claude's settings.json: what may happen to the hook entry ---------------------------------
sp = cgc_activation.default_settings_path()
try:
    with open(sp, encoding="utf-8") as f:
        raw = f.read()
except FileNotFoundError:
    emit("settings", sp, "absent")
except UnicodeDecodeError as e:
    emit("settings", sp, f"parse: not UTF-8 ({e.reason})")
except OSError as e:
    emit("settings", sp, f"blocked: cannot read it ({e.strerror or e})")
else:
    import json
    try:
        json.loads(raw)
    except ValueError as e:
        emit("settings", sp, f"parse: {e}")
    else:
        try:
            cgc_activation.read_settings(sp)
        except cgc_activation.SettingsError as e:
            emit("settings", sp, f"blocked: {e}")
        else:
            emit("settings", sp, "ok")

# ---- the journal (kept unless --purge) and the tool's files around it --------------------------
seen = set()
if db != ":memory:":
    plan_journal(db, seen)
if not same(legacy, db):
    plan_journal(legacy, seen)
journal = [p for k, p, _ in todo if k == "journal"]

dbdir = real(os.path.dirname(os.path.abspath(db))) if db != ":memory:" else data
plan_dir(os.path.join(dbdir, "locks"), LOCKS, "lock", seen)
for root in (data, state):
    inc = os.path.join(root, "incidents")
    if os.path.isdir(inc) and not os.path.islink(inc) and allowed(inc):
        for n in sorted(os.listdir(inc)):
            sub = os.path.join(inc, n)
            if INCIDENT.fullmatch(n) and os.path.isdir(sub) and not os.path.islink(sub):
                plan_dir(sub, INCIDENT_FILES, "incident", seen)
spool = os.environ.get("CGC_SPOOL_DIR") or os.path.join(state, "spool")
for sub in ("logs", "pending", "processing", "done", "status"):
    plan_dir(os.path.join(spool, sub), SPOOLED, "scratch", seen)
plan_dir(state, SCRATCH, "scratch", seen)

# The config file is the user's, never the tool's scratch. It is recognised by IDENTITY: on a
# case-insensitive filesystem CGC_CONFIG=.../ACTIVE.JSON is the file the scan listed as active.json, and a
# hard link is the same file again; neither may be deleted.
kept = [(k, p, i) for k, p, i in todo if same(p, config)]
todo = [t for t in todo if t not in kept]
for _, p, _ in kept:
    emit("note", p, "is your config file; it is never deleted")
for kind, path, ident in todo:
    emit(kind, path, ident)

# ---- nothing protected may sit inside a tree this uninstall deletes -----------------------------
trees = [t for t in (dest, dest + ".bak") if os.path.lexists(t) and not os.path.islink(t)]
for t in trees:
    if within(config, t):
        refuse(f"your config file {config} lies inside {t}, which this uninstall deletes. Move the config first.")
    if not purge:
        for p in journal:
            if within(p, t):
                refuse(f"the store file {p} lies inside {t}, which this uninstall deletes, and no --purge was given. "
                       "Move the store out first (or pass --purge to delete it).")

# ---- the Chrome profile (offered only under --purge) --------------------------------------------
profile = os.environ.get("CGC_PROFILE") or os.path.expanduser("~/.cgc-chrome")
rp = real(profile)
if not os.path.lexists(profile):
    emit("profile", "-", "none present")
elif not within(rp, HOME, strict=True):
    emit("profile", "-", f"{rp} is not strictly inside $HOME; left alone")
elif not same(rp, os.path.expanduser("~/.cgc-chrome")) and not os.path.isfile(os.path.join(rp, "Local State")):
    emit("profile", "-", f"{rp} is not the default profile and holds no Chrome 'Local State'; left alone")
else:
    held = [config, state, data, sp, dest] + journal
    inside = [x for x in held if within(x, rp)]
    if inside:
        emit("profile", "-", f"{rp} contains {inside[0]}, which must not be deleted; left alone")
    elif tabbed(rp):
        emit("profile", "-", f"{rp} has a tab or newline in its path; left alone")
    else:
        emit("profile", rp, "ok")

for row in out:
    print("\t".join(row))
PYEOF
}

# --- 0. resolve and check everything BEFORE any change ------------------------------------------
PLAN_FILE="$(mktemp "${TMPDIR:-/tmp}/cgc-uninstall-plan.XXXXXX")"
trap 'rm -f -- "$PLAN_FILE"' EXIT
if ! PLAN="$(plan_py plan)"; then
  echo "ERROR: could not work out what to remove (see above). Nothing was changed." >&2
  exit 1
fi
printf '%s\n' "$PLAN" > "$PLAN_FILE"
REFUSALS=(); JOURNAL=(); TODO=(); NOTES=()
SETTINGS_NOTE="absent"; SETTINGS_PATH="-"; PROFILE_PATH="-"; PROFILE_NOTE="-"
while IFS=$'\t' read -r kind path note; do
  case "$kind" in
    refuse)   REFUSALS+=("$note") ;;
    note)     NOTES+=("$path: $note") ;;
    journal)  JOURNAL+=("$path"); TODO+=("$path") ;;
    lock|incident|scratch) TODO+=("$path") ;;
    settings) SETTINGS_PATH="$path"; SETTINGS_NOTE="$note" ;;
    profile)  PROFILE_PATH="$path"; PROFILE_NOTE="$note" ;;
  esac
done <<<"$PLAN"

if [ "${#REFUSALS[@]}" -gt 0 ]; then
  echo "ERROR: refusing to uninstall. Nothing was changed:" >&2
  printf '  - %s\n' "${REFUSALS[@]}" >&2
  exit 1
fi
case "$SETTINGS_NOTE" in
  blocked*)
    echo "ERROR: Claude settings.json ($SETTINGS_PATH) cannot be edited safely: ${SETTINGS_NOTE#blocked: }" >&2
    echo "       Our SessionStart hook entry must leave it BEFORE the code goes, so nothing was changed." >&2
    echo "       Fix the file (permissions, shape), then re-run uninstall.sh." >&2
    exit 1 ;;
  parse*)
    if [ "$FORCE" != "1" ]; then
      echo "ERROR: Claude settings.json ($SETTINGS_PATH) is not valid JSON (${SETTINGS_NOTE#parse: })." >&2
      echo "       Claude Code ignores such a file, so no hook of ours can be firing from it, but it may hold" >&2
      echo "       our entry. Fix or repair the file, or re-run with --force and delete the entry by hand." >&2
      echo "       Nothing was changed." >&2
      exit 1
    fi ;;
esac

# --- 1. the daemon: stop it, wait until its process is gone, remove its launchd service -----------
# Before any file goes: a live daemon imports modules lazily from the tree about to be deleted. The
# test is the process and its lock (bin/cgc daemon-stop), not the heartbeat. A daemon outside launchd
# (`cgc watch`, `cgc up`) is the user's own process: it is named and the uninstall stops.
echo "• stopping the egress daemon service"
if ! ctl uninstall-daemon; then
  echo "ERROR: the egress daemon is still running (see above). Nothing else was removed; re-run when it has stopped." >&2
  exit 1
fi

# --- 2. the SessionStart hook: only the entry cgc_activation installed ---------------------------
# JSON-aware (other hooks and settings are untouched; settings.json.bak keeps the previous file).
if [ "${SETTINGS_NOTE%%:*}" = "parse" ]; then
  echo "  ! --force: $SETTINGS_PATH is not valid JSON, so it was not edited. If it holds our hook entry" >&2
  echo "    (a command mentioning ACTIVATION.md / cgc_activation.py), delete that entry by hand." >&2
else
  echo "• removing our SessionStart hook entry"
  if ! python3 "$SCRIPTS/cgc_activation.py" remove; then
    echo "ERROR: could not edit Claude settings.json (see above). The skill was NOT removed." >&2
    echo "       The daemon service is already gone. Fix settings.json, then re-run uninstall.sh." >&2
    exit 1
  fi
fi

# --- 3. the code --------------------------------------------------------------------------------
if [ -e "$DEST" ] || [ -L "$DEST" ] || [ -e "${DEST}.bak" ] || [ -L "${DEST}.bak" ]; then
  rm -rf -- "$DEST" "${DEST}.bak"
  echo "• removed $DEST (and ${DEST}.bak)"
else
  echo "• nothing installed at $DEST"
fi

# --- 4. the send journal: kept unless --purge ---------------------------------------------------
if [ "$PURGE" != "1" ]; then
  if [ "${#JOURNAL[@]}" -gt 0 ]; then
    echo "• KEPT — the consult store (send journal, round history). Not deleted without --purge:"
    printf '    %s\n' "${JOURNAL[@]}"
  fi
  echo "• also kept: scratch files, the Chrome profile, and your config file. --purge deletes the first two."
  echo "done."
  exit 0
fi

echo "• --purge: deleting EXACTLY these paths (files the tool wrote; there is no undo):"
if [ "${#TODO[@]}" -gt 0 ]; then
  printf '    %s\n' "${TODO[@]}"
else
  echo "    (no store or scratch files found)"
fi
[ "${#NOTES[@]}" -gt 0 ] && printf '    left alone: %s\n' "${NOTES[@]}"
echo "    (the code tree above is already gone; your config file is never deleted)"
# The delete step is handed the plan that was just shown and nothing else: it re-checks each listed path
# and deletes only those, so a file created since (a new chrome.log) or swapped since is not touched.
RESULT="$(plan_py delete "$PLAN_FILE")" || RESULT=""
while IFS=$'\t' read -r kind path note; do
  case "$kind" in
    failed) echo "  ! not deleted: $path: $note" >&2 ;;
    deleted) [ "$note" = "empty directory" ] && echo "• removed empty directory $path" ;;
  esac
done <<<"$RESULT"

if [ "$PROFILE_PATH" != "-" ]; then
  a=""
  read -r -p "Delete the dedicated Chrome profile $PROFILE_PATH (logs you out of the consult session)? [y/N] " a || a=""
  if [ "$a" = "y" ]; then
    rm -rf -- "$PROFILE_PATH"
    echo "• removed $PROFILE_PATH"
  fi
elif [ "$PROFILE_NOTE" != "none present" ]; then
  echo "• Chrome profile: $PROFILE_NOTE"
fi
echo "done."
