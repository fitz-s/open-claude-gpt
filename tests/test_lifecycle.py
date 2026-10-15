# Lifecycle: install.sh, uninstall.sh and the daemon-service verbs of bin/cgc, run for real against a
# throwaway HOME with launchctl (and gh) replaced by stubs found first on PATH. Nothing here touches
# the real home, the real launchd, a browser or the network.
#
# The launchctl stub is a small state machine, not a recorder: `bootstrap` starts a live process and
# writes a daemon heartbeat naming it, `bootout` kills it (or, per STUB_* knobs, does not), and every
# call logs what the installed tree looked like at that instant. That is what lets a test say "the
# daemon was gone BEFORE the files changed" instead of "the commands were called".
import importlib.util
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
LABEL = "com.open-claude-gpt.daemon"

STUB_LAUNCHCTL = r'''#!/usr/bin/env bash
# launchctl stand-in. State in $STUB_DIR: `loaded` (pid of the stub daemon), `lastpid`, `log`.
d="$STUB_DIR"
hb() {
  python3 - "$1" "${2:-}" <<'PY'
import os, sys
sys.path.insert(0, os.environ["STUB_SCRIPTS"])
import cgc_spool, cgc_store
if sys.argv[1] == "write":
    cgc_spool.heartbeat_write(int(sys.argv[2]), {
        "protocol": 1, "schema_version": int(os.environ.get("STUB_SCHEMA", cgc_store.SCHEMA_VERSION)),
        "db_path": os.path.abspath(cgc_store.db_path()), "store_uuid": "t", "daemon_instance_id": "t"})
else:
    try:
        os.remove(cgc_spool.daemon_path())
    except OSError:
        pass
PY
}
[ "$1" = print ] && { [ -e "$d/loaded" ]; exit; }
old=0; [ -e "$STUB_TREE/OLD_MARKER" ] && old=1
new=0; [ -f "$STUB_TREE/scripts/chatgpt_contract.json" ] && [ "$old" = 0 ] && new=1
last=0; [ -e "$d/lastpid" ] && kill -0 "$(cat "$d/lastpid")" 2>/dev/null && last=1
settings=0; [ -e "$HOME/.claude/settings.json" ] && grep -q ACTIVATION "$HOME/.claude/settings.json" && settings=1
echo "$1 old_tree=$old new_tree=$new prev_daemon_alive=$last hook_in_settings=$settings" >> "$d/log"
case "$1" in
  bootout|unload)
    [ -n "${STUB_STUCK:-}" ] && exit 0          # says yes, changes nothing: a daemon that will not die
    [ -e "$d/loaded" ] || exit 3
    pid="$(cat "$d/loaded")"; rm -f "$d/loaded"
    if [ -n "${STUB_LINGER:-}" ]; then          # unloaded at once, but the process drains for a while
      ( sleep "$STUB_LINGER"; kill "$pid" 2>/dev/null; hb clear ) >/dev/null 2>&1 </dev/null &
    else
      kill "$pid" 2>/dev/null; hb clear
    fi ;;
  bootstrap|load)
    [ -e "$d/loaded" ] && exit 5
    # a process whose command line names cgc_daemon.py and that holds daemon.lock, as the real daemon does
    python3 -c '
import fcntl, os, sys, time
sys.path.insert(0, sys.argv[1])
import cgc_spool as s
s.ensure_dirs()
f = open(s.daemon_lock_path(), "a+")
fcntl.flock(f, fcntl.LOCK_EX)
open(os.path.join(os.environ["STUB_DIR"], "ready.%d" % os.getpid()), "w").close()
time.sleep(120)' "$STUB_SCRIPTS" cgc_daemon.py >/dev/null 2>&1 </dev/null & pid=$!
    for _ in $(seq 100); do [ -e "$d/ready.$pid" ] && break; sleep 0.05; done
    echo "$pid" > "$d/loaded"; echo "$pid" > "$d/lastpid"
    [ -n "${STUB_NO_HEARTBEAT:-}" ] || hb write "$pid" ;;
esac
exit 0
'''


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sandbox:
    """A throwaway HOME, stubs on PATH, and the knobs the stubs read."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.stubs = tmp / "stubs"
        self.stubs.mkdir()
        self.sd = tmp / "stub-state"
        self.sd.mkdir()
        (self.stubs / "launchctl").write_text(STUB_LAUNCHCTL)
        (self.stubs / "gh").write_text("#!/bin/sh\nexit 1\n")      # present, never authed, never online
        for n in ("launchctl", "gh"):
            os.chmod(self.stubs / n, 0o755)
        self.dest = self.home / ".claude" / "skills" / "chatgpt-consult"
        self.data = self.home / ".local" / "state" / "cgc"
        self.state = self.home / "cgc-state"
        self.settings = self.home / ".claude" / "settings.json"
        self.plist = self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("CGC_") and not k.startswith("STUB_")
                    and k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_SKILLS_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME")}
        self.env.update(
            HOME=str(self.home), PATH=f"{self.stubs}{os.pathsep}{self.env['PATH']}", NO_COLOR="1",
            CGC_STATE_DIR=str(self.state), CGC_DATA_DIR=str(self.data),
            CGC_STORE_DB=str(self.data / "control.db"), CGC_CONFIG=str(self.home / ".config" / "cgc" / "config"),
            CGC_PORT=str(_free_port()), CGC_CHROME=sys.executable,
            CGC_STOP_WAIT_S="3", CGC_START_WAIT_S="4",
            STUB_DIR=str(self.sd), STUB_TREE=str(self.dest), STUB_SCRIPTS=SCRIPTS)
        # the one safety property every test leans on: launchctl IS the stub
        which = subprocess.run(["bash", "-c", "command -v launchctl"], env=self.env,
                               capture_output=True, text=True).stdout.strip()
        assert which == str(self.stubs / "launchctl"), which

    def run(self, argv, stdin="", timeout=120, **env):
        e = dict(self.env, **env)
        return subprocess.run(argv, cwd=ROOT, env=e, input=stdin, capture_output=True, text=True, timeout=timeout)

    def install(self, *args, **env):
        return self.run(["bash", os.path.join(ROOT, "install.sh"), *args], **env)

    def uninstall(self, *args, stdin="n\n", **env):
        return self.run(["bash", os.path.join(ROOT, "uninstall.sh"), *args], stdin=stdin, **env)

    def cgc(self, *args, **env):
        return self.run(["bash", os.path.join(ROOT, "bin", "cgc"), *args],
                        CGC_HOME=env.pop("CGC_HOME", os.path.join(ROOT, "skill")), **env)

    def old_install(self):
        """A previous install: a complete tree plus a marker file the new tree does not have."""
        self.dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-R", os.path.join(ROOT, "skill"), str(self.dest)], check=True)
        (self.dest / "OLD_MARKER").write_text("old\n")

    def daemon_up(self):
        """The state after a real `cgc install-daemon`: a plist under HOME and a live stub process with
        a matching heartbeat."""
        r = self.cgc("install-daemon")
        assert r.returncode == 0, r.stdout + r.stderr
        (self.sd / "log").unlink()               # the setup calls are not part of the scenario
        return int((self.sd / "loaded").read_text())

    def log(self):
        p = self.sd / "log"
        return p.read_text().splitlines() if p.exists() else []

    def calls(self):
        return [ln.split()[0] for ln in self.log()]

    def line(self, verb):
        return next(ln for ln in self.log() if ln.split()[0] == verb)

    def lines(self, verb):
        return [ln for ln in self.log() if ln.split()[0] == verb]

    def store(self, schema=None):
        """A control.db with the journal files uninstall must treat as the send journal."""
        self.data.mkdir(parents=True, exist_ok=True)
        db = self.data / "control.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(schema or _schema()),))
        c.execute("CREATE TABLE IF NOT EXISTS lc_probe (rid TEXT, note TEXT)")
        c.execute("INSERT INTO lc_probe VALUES('REQ-1', 'sent')")
        c.commit()
        c.close()
        shutil.copyfile(db, self.data / "control.db.v4.bak")
        for suffix in ("-wal", "-shm"):
            (self.data / f"control.db{suffix}").write_text("journal\n")
        (self.data / "locks").mkdir(exist_ok=True)
        (self.data / "locks" / "daemon.json").write_text("{}")
        return db

    def cleanup(self):
        for name in ("loaded", "lastpid"):
            p = self.sd / name
            if p.exists():
                try:
                    os.kill(int(p.read_text()), signal.SIGKILL)
                except (OSError, ValueError):
                    pass


def _schema():
    spec = importlib.util.spec_from_file_location("cgc_store_lc", os.path.join(SCRIPTS, "cgc_store.py"))
    sys.path.insert(0, SCRIPTS)
    try:
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    finally:
        sys.path.remove(SCRIPTS)
    return m.SCHEMA_VERSION


def _beat_is_live(sb):
    """Does the heartbeat read as a LIVE daemon (the freshness test the old stop relied on)?"""
    r = sb.run([sys.executable, "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); import cgc_spool as s; "
                "sys.exit(0 if s.daemon_alive() else 1)", SCRIPTS])
    return r.returncode == 0


def _fake_foreground_daemon():
    """A process the way `cgc watch` looks to ps: a python whose command line names cgc_daemon.py."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "cgc_daemon.py"])


def _tree_complete(root):
    return all(os.path.exists(os.path.join(root, "scripts", f)) for f in
               ("cgc_daemon.py", "chatgpt_contract.json", "fixture_dom.js", "cgc_drift.py")) \
        and os.path.exists(os.path.join(root, "SKILL.md"))


# ---- install.sh --------------------------------------------------------------------------------

def test_install_stops_the_daemon_before_replacing_files_then_starts_it_on_the_new_code(sb):
    sb.old_install()
    old_pid = sb.daemon_up()
    r = sb.install()
    assert r.returncode == 0, r.stdout + r.stderr
    assert sb.calls() == ["bootout", "bootstrap"], sb.log()
    # at bootout the OLD tree was still whole; at bootstrap the NEW tree was whole and the old one gone
    assert "old_tree=1" in sb.line("bootout")
    assert "old_tree=0 new_tree=1" in sb.line("bootstrap")
    assert "prev_daemon_alive=0" in sb.line("bootstrap"), "the old daemon process was still running"
    assert _tree_complete(sb.dest) and not (sb.dest / "OLD_MARKER").exists()
    assert (sb.dest.parent / "chatgpt-consult.bak" / "OLD_MARKER").exists()      # the previous install is kept
    new_pid = int((sb.sd / "loaded").read_text())
    assert new_pid != old_pid
    assert "daemon is up" in r.stdout
    assert not [p for p in sb.dest.parent.iterdir() if p.name.startswith(".chatgpt-consult.new")], "stage left behind"


def test_install_waits_for_a_draining_daemon_to_exit_before_copying(sb):
    sb.old_install()
    sb.daemon_up()
    r = sb.install(STUB_LINGER="2", CGC_STOP_WAIT_S="10")      # job unloaded at once, process lives 2 s more
    assert r.returncode == 0, r.stdout + r.stderr
    assert "prev_daemon_alive=0" in sb.line("bootstrap"), "copied/started while the old process still ran"


def test_install_refuses_a_daemon_that_will_not_stop_and_changes_nothing(sb):
    sb.old_install()
    sb.daemon_up()
    r = sb.install(STUB_STUCK="1", CGC_STOP_WAIT_S="2")
    assert r.returncode == 1
    assert "is still running" in r.stderr and "daemon process" in r.stderr
    assert (sb.dest / "OLD_MARKER").exists() and not (sb.dest.parent / "chatgpt-consult.bak").exists()
    assert "bootstrap" not in sb.calls()


def test_install_failure_after_stop_restores_the_daemon_on_the_old_tree(sb):
    """A copy that fails (read-only skills root) must not leave the daemon stopped."""
    sb.old_install()
    sb.daemon_up()
    os.chmod(sb.dest.parent, 0o555)
    try:
        r = sb.install()
    finally:
        os.chmod(sb.dest.parent, 0o755)
    assert r.returncode != 0
    assert (sb.dest / "OLD_MARKER").exists(), "the previous install must survive a failed copy"
    assert sb.calls() == ["bootout", "bootstrap"], sb.log()
    assert "old_tree=1" in sb.line("bootstrap")
    assert (sb.sd / "loaded").exists(), "daemon left stopped"


def test_install_refuses_a_daemon_running_outside_launchd(sb):
    sb.old_install()
    p = _fake_foreground_daemon()
    try:
        beat = sb.run([sys.executable, "-c",
                       "import sys;sys.path.insert(0,sys.argv[1]);import cgc_spool as s;s.heartbeat_write(int(sys.argv[2]))",
                       SCRIPTS, str(p.pid)])
        assert beat.returncode == 0, beat.stderr
        r = sb.install()
    finally:
        p.kill()
        p.wait()
    assert r.returncode == 1
    assert f"pid {p.pid}" in r.stderr and "outside launchd" in r.stderr
    assert (sb.dest / "OLD_MARKER").exists()
    assert sb.calls() == []


def test_first_install_with_no_daemon_installs_and_reports_cannot_send(sb):
    r = sb.install()
    assert r.returncode == 0, r.stdout + r.stderr
    assert sb.calls() == [], "no daemon loaded: launchd must not be asked to stop or start anything"
    assert _tree_complete(sb.dest)
    assert "can_send  NO" in r.stdout and "daemon" in r.stdout
    assert "installed yes" in r.stdout
    assert "cgc install-daemon" in r.stdout


def test_install_refuses_older_code_over_a_newer_store_before_touching_anything(sb):
    sb.old_install()
    sb.daemon_up()
    sb.store(schema=_schema() + 1)
    r = sb.install()
    assert r.returncode == 1
    assert "Nothing was changed" in r.stderr
    assert sb.calls() == [], "the daemon must not be stopped for an install that cannot proceed"
    assert (sb.dest / "OLD_MARKER").exists()


def test_install_never_touches_the_store(sb):
    db = sb.store()
    # the database and its pre-migration backup, byte for byte (-shm is SQLite's own index of the WAL:
    # any reader, the read-only doctor included, may rewrite it, and it holds no journal content)
    watched = [db, sb.data / "control.db.v4.bak", sb.data / "control.db-wal"]
    before = [p.read_bytes() for p in watched]
    assert sb.install().returncode == 0
    assert [p.read_bytes() for p in watched] == before


def test_install_link_mode_links_the_repo_skill(sb):
    r = sb.install("--link")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sb.dest.is_symlink() and os.path.realpath(sb.dest) == os.path.realpath(os.path.join(ROOT, "skill"))


def test_install_fails_when_the_new_daemon_does_not_come_up(sb):
    sb.old_install()
    sb.daemon_up()
    r = sb.install(STUB_NO_HEARTBEAT="1", CGC_START_WAIT_S="2")
    assert r.returncode == 1
    assert "did not come up" in r.stderr
    forced = sb.install("--force", STUB_NO_HEARTBEAT="1", CGC_START_WAIT_S="2")
    assert forced.returncode == 0, forced.stdout + forced.stderr


def test_install_fails_when_the_new_daemon_serves_another_schema(sb):
    sb.old_install()
    sb.daemon_up()
    r = sb.install(STUB_SCHEMA="1", CGC_START_WAIT_S="2")
    assert r.returncode == 1
    assert "schema" in r.stderr


# ---- bin/cgc daemon-service verbs ---------------------------------------------------------------

def test_install_daemon_writes_a_plist_under_home_and_loads_it(sb):
    r = sb.cgc("install-daemon")
    assert r.returncode == 0, r.stdout + r.stderr
    import plistlib
    with open(sb.plist, "rb") as f:
        pl = plistlib.load(f)
    assert pl["Label"] == LABEL and pl["KeepAlive"] is True and pl["RunAtLoad"] is True
    assert sb.calls() == ["bootstrap"]
    again = sb.cgc("install-daemon")                       # re-run: restart through stop, never a double load
    assert again.returncode == 0, again.stdout + again.stderr
    assert sb.calls() == ["bootstrap", "bootout", "bootstrap"]


def test_uninstall_daemon_keeps_the_plist_while_the_daemon_is_still_running(sb):
    sb.cgc("install-daemon")
    r = sb.cgc("uninstall-daemon", STUB_STUCK="1", CGC_STOP_WAIT_S="2")
    assert r.returncode == 1
    assert sb.plist.exists(), "removing the plist of a daemon that is still running orphans it"
    ok = sb.cgc("uninstall-daemon")
    assert ok.returncode == 0 and not sb.plist.exists()


def test_daemon_stop_and_start_exit_codes(sb):
    assert sb.cgc("daemon-stop").returncode == 10            # nothing loaded
    assert sb.cgc("daemon-start").returncode == 10           # nothing installed
    sb.cgc("install-daemon")
    assert sb.cgc("daemon-pid").returncode == 0
    assert sb.cgc("daemon-stop").returncode == 0
    assert sb.cgc("daemon-pid").returncode == 1
    assert sb.cgc("daemon-start").returncode == 0
    assert sb.cgc("daemon-stop", STUB_STUCK="1").returncode == 1


# ---- uninstall.sh ------------------------------------------------------------------------------

def _seed_settings(sb):
    sb.settings.parent.mkdir(parents=True, exist_ok=True)
    other = {"matcher": "", "hooks": [{"type": "command", "command": "echo some-other-session-hook"}]}
    original = {"model": "opus", "hooks": {"SessionStart": [other],
                                           "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "true"}]}]}}
    sb.settings.write_text(json.dumps(original, indent=2) + "\n")
    r = sb.run([sys.executable, os.path.join(SCRIPTS, "cgc_activation.py"), "install",
                "--activation", str(sb.dest / "ACTIVATION.md")])
    assert r.returncode == 0, r.stderr
    assert "ACTIVATION" in sb.settings.read_text()
    return original


def test_uninstall_stops_daemon_then_removes_hook_then_code_and_keeps_the_journal(sb):
    sb.old_install()
    sb.cgc("install-daemon")
    original = _seed_settings(sb)
    sb.store()
    (sb.sd / "log").unlink()
    r = sb.uninstall()
    assert r.returncode == 0, r.stdout + r.stderr
    # order: the daemon was booted out while the code and the hook were still there
    assert sb.calls() == ["bootout"]
    assert "old_tree=1" in sb.line("bootout") and "hook_in_settings=1" in sb.line("bootout")
    assert not sb.plist.exists() and not (sb.sd / "loaded").exists()
    # only our entry left settings.json; the rest is byte-for-byte what it was, and a .bak holds the prior file
    assert json.loads(sb.settings.read_text()) == original
    assert "ACTIVATION" in (sb.settings.parent / "settings.json.bak").read_text()
    assert not sb.dest.exists()
    # the send journal stays, and the message names it
    for name in ("control.db", "control.db-wal", "control.db-shm", "control.db.v4.bak"):
        assert (sb.data / name).exists(), name
        assert str(sb.data / name) in r.stdout, name
    assert "KEPT" in r.stdout and "--purge" in r.stdout
    assert sqlite3.connect(sb.data / "control.db").execute("SELECT note FROM lc_probe").fetchone() == ("sent",)


def test_uninstall_purge_names_and_deletes_exactly_the_journal_and_scratch(sb, tmp_path):
    sb.old_install()
    sb.store()
    sb.state.mkdir()
    (sb.state / "answer_REQ-20261014-120000-a1b2c3.txt").write_text("a")
    (sb.state / "daemon.log").write_text("log")
    config = sb.home / ".config" / "cgc" / "config"
    config.parent.mkdir(parents=True)
    config.write_text("CGC_MODEL=Pro\n")
    other = sb.data / "unrelated-file"                       # not the tool's: --purge must not take it
    other.write_text("keep")
    profile = sb.home / ".cgc-chrome"
    profile.mkdir()
    r = sb.uninstall("--purge", stdin="n\n")
    assert r.returncode == 0, r.stdout + r.stderr
    listed = r.stdout.split("--purge: deleting EXACTLY these paths")[1]
    for name in ("control.db", "control.db-wal", "control.db-shm", "control.db.v4.bak", "locks"):
        assert str(sb.data / name) in listed, name
        assert not (sb.data / name).exists(), name
    assert str(sb.state / "answer_REQ-20261014-120000-a1b2c3.txt") in listed
    assert sb.state.is_dir() and list(sb.state.iterdir()) == [], \
        "a custom scratch directory is emptied of the tool's files but left in place"
    assert other.exists(), "an unrelated file in the data dir was deleted"
    assert config.exists() and profile.exists()              # answered n: the login survives
    gone = sb.uninstall("--purge", stdin="y\n")
    assert gone.returncode == 0 and not profile.exists()


def test_uninstall_purge_never_touches_a_scratch_dir_outside_home(sb, tmp_path):
    sb.old_install()
    plain = tmp_path / "work"                                  # outside $HOME, not the default /tmp/cgc
    plain.mkdir()
    (plain / "mine.txt").write_text("x")
    (plain / "daemon.log").write_text("tool-shaped, but the directory is not ours")
    r = sb.uninstall("--purge", CGC_STATE_DIR=str(plain))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (plain / "mine.txt").exists() and (plain / "daemon.log").exists()
    assert f"left alone: {plain}" in r.stdout


def test_uninstall_stops_before_deleting_anything_when_the_daemon_will_not_stop(sb):
    sb.old_install()
    sb.cgc("install-daemon")
    _seed_settings(sb)
    r = sb.uninstall(STUB_STUCK="1", CGC_STOP_WAIT_S="2")
    assert r.returncode == 1
    assert sb.dest.exists() and sb.plist.exists() and "ACTIVATION" in sb.settings.read_text()


def test_uninstall_does_not_delete_code_when_the_hook_cannot_be_removed(sb):
    sb.old_install()
    _seed_settings(sb)
    os.chmod(sb.settings.parent, 0o555)                      # settings.json cannot be rewritten
    try:
        r = sb.uninstall()
    finally:
        os.chmod(sb.settings.parent, 0o755)
    assert r.returncode == 1
    assert sb.dest.exists(), "code was removed while the hook still points at it"
    assert "NOT removed" in r.stderr


def test_uninstall_stops_on_a_settings_file_that_does_not_parse_unless_forced(sb):
    sb.old_install()
    sb.settings.parent.mkdir(parents=True, exist_ok=True)
    sb.settings.write_text("{ not json")
    r = sb.uninstall()
    assert r.returncode == 1 and "not valid JSON" in r.stderr and "--force" in r.stderr
    assert sb.dest.exists(), "code was deleted although the hook entry could not be checked"
    forced = sb.uninstall("--force")
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert sb.settings.read_text() == "{ not json"           # never rewritten
    assert "delete that entry by hand" in forced.stderr and not sb.dest.exists()


def test_uninstall_leaves_a_foreground_daemon_alone_and_stops(sb):
    sb.old_install()
    p = _fake_foreground_daemon()
    try:
        sb.run([sys.executable, "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);import cgc_spool as s;s.heartbeat_write(int(sys.argv[2]))",
                SCRIPTS, str(p.pid)])
        r = sb.uninstall()
    finally:
        p.kill()
        p.wait()
    assert r.returncode == 1 and "outside launchd" in r.stderr
    assert sb.dest.exists()


def test_uninstall_with_nothing_installed_succeeds(sb):
    r = sb.uninstall()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nothing installed" in r.stdout and sb.calls() == []
