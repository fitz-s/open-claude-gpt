# Regression tests for the seven lifecycle laws gpt-sol found broken in 188415d. Each test reproduces the
# reviewer's scenario in the same sandbox as tests/test_lifecycle.py (temp HOME, stateful launchctl stub on
# PATH, nothing real touched) and fails if the fix is reverted.
#
#   L1  process, not heartbeat age, decides whether code may be replaced
#   L2  the swap is a transaction; an old tree is never the only copy deleted
#   L3  delete only what the tool wrote; never a directory because of its name
#   L4  resolve before any rm; the config and the journal never go with a deleted tree
#   L5  settings.json fails closed
#   L6  the config write check is the reader's own line splitting
#   L7  a config read failure is not an empty config
#   +   the doctor's websocket probe never meets an HTTP proxy
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

import test_lifecycle as lc
from test_lifecycle import ROOT, SCRIPTS


def _stale_heartbeat(sb, pid, age=60):
    """daemon.json for `pid`, `age` seconds old: daemon_identity() reads it as DOWN."""
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import cgc_spool as s\n"
            "s.heartbeat_write(int(sys.argv[2]), {'protocol': 1})\n"
            "import json; p = s.daemon_path(); d = json.load(open(p)); d['ts'] = time.time() - float(sys.argv[3])\n"
            "json.dump(d, open(p, 'w'))\n")
    r = sb.run([sys.executable, "-c", code, SCRIPTS, str(pid), str(age)])
    assert r.returncode == 0, r.stderr


# ================================================================ L1 process != health

def test_L1_stop_waits_for_a_daemon_whose_heartbeat_is_stale(sb):
    """The reviewer's repro: heartbeat 60 s old (so 'dead' by freshness) + the process drains 10 s more.
    The old stop saw no live daemon, returned 0, and install replaced files under the running process."""
    sb.old_install()
    pid = sb.daemon_up()
    _stale_heartbeat(sb, pid, 60)
    assert not lc._beat_is_live(sb), "precondition: the heartbeat must read as down"
    r = sb.install(STUB_LINGER="4", CGC_STOP_WAIT_S="15")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "prev_daemon_alive=0" in sb.line("bootstrap"), \
        "the old daemon process was still running when the new one started: " + sb.line("bootstrap")


def test_L1_a_lingering_daemon_past_the_deadline_is_refused_with_its_pid(sb):
    sb.old_install()
    pid = sb.daemon_up()
    _stale_heartbeat(sb, pid, 60)
    r = sb.install(STUB_LINGER="30", CGC_STOP_WAIT_S="2")
    assert r.returncode == 1
    assert f"{pid}" in r.stderr and "still running" in r.stderr
    assert (sb.dest / "OLD_MARKER").exists() and not (sb.dest.parent / "chatgpt-consult.bak").exists()


def test_L1_stop_is_decided_by_daemon_lock_even_with_no_heartbeat_file(sb):
    """A daemon that never wrote (or lost) daemon.json still holds daemon.lock for its whole life."""
    sb.old_install()
    sb.daemon_up()
    os.remove(sb.data / "locks" / "daemon.json")
    r = sb.cgc("daemon-pid")
    assert r.returncode == 0 and r.stdout.strip(), "the lock holder must be found without a heartbeat"
    stuck = sb.install(STUB_STUCK="1", CGC_STOP_WAIT_S="2")
    assert stuck.returncode == 1 and "still running" in stuck.stderr


def test_L1_foreground_guard_uses_the_same_test_not_heartbeat_freshness(sb):
    sb.old_install()
    p = lc._fake_foreground_daemon()
    try:
        _stale_heartbeat(sb, p.pid, 600)               # ancient heartbeat, live process
        r = sb.install()
        u = sb.uninstall()
    finally:
        p.kill()
        p.wait()
    for res in (r, u):
        assert res.returncode == 1 and f"{p.pid}" in res.stderr and "outside launchd" in res.stderr, res.stderr
    assert (sb.dest / "OLD_MARKER").exists()


def test_L1_a_reused_pid_that_is_not_the_daemon_does_not_block(sb):
    """daemon.json names a live pid whose command line is not cgc_daemon.py: not a daemon."""
    sb.old_install()
    p = subprocess.Popen(["sleep", "60"])
    try:
        _stale_heartbeat(sb, p.pid, 600)
        r = sb.install()
    finally:
        p.kill()
        p.wait()
    assert r.returncode == 0, r.stdout + r.stderr


# ================================================================ L2 swap is a transaction

def test_L2_failed_new_daemon_rolls_back_and_a_retry_still_has_the_old_install(sb):
    """Repro: STUB_NO_HEARTBEAT=1, then a second install. The old flow deleted the .bak on the retry,
    so the only intact tree was gone after two failures."""
    sb.old_install()
    sb.daemon_up()
    first = sb.install(STUB_NO_HEARTBEAT="1", CGC_START_WAIT_S="2")
    assert first.returncode == 1 and "did not come up" in first.stderr
    assert (sb.dest / "OLD_MARKER").exists(), "the failed upgrade must put the old tree back"
    assert not (sb.dest.parent / "chatgpt-consult.bak").exists()
    assert (sb.sd / "loaded").exists(), "and the old daemon must be running again"
    second = sb.install(STUB_NO_HEARTBEAT="1", CGC_START_WAIT_S="2")
    assert second.returncode == 1
    assert (sb.dest / "OLD_MARKER").exists(), "two failed upgrades must still leave the old install intact"


def test_L2_an_existing_bak_is_parked_not_deleted_until_the_new_daemon_is_verified(sb):
    sb.old_install()
    older = sb.dest.parent / "chatgpt-consult.bak"
    older.mkdir()
    (older / "OLDER_MARKER").write_text("x")
    sb.daemon_up()
    failed = sb.install(STUB_NO_HEARTBEAT="1", CGC_START_WAIT_S="2")
    assert failed.returncode == 1
    assert (older / "OLDER_MARKER").exists() and (sb.dest / "OLD_MARKER").exists(), \
        "a failed upgrade must restore both the live tree and the older backup"
    ok = sb.install()
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert (sb.dest.parent / "chatgpt-consult.bak" / "OLD_MARKER").exists()
    assert not (older / "OLDER_MARKER").exists(), "the older backup goes only after the new daemon is verified"
    assert not [p for p in sb.dest.parent.iterdir() if ".bak.prev." in p.name], "parked backup left behind"


def test_L2_link_mode_failure_after_the_old_tree_is_parked_restores_it(sb, tmp_path):
    """Repro: --link with the final mv failing on the *.new.* stage. A mv shim that fails for the stage
    reproduces the window between 'old tree moved aside' and 'new tree in place'."""
    sb.old_install()
    older = sb.dest.parent / "chatgpt-consult.bak"        # an earlier upgrade's backup: maybe the only intact one
    older.mkdir()
    (older / "OLDER_MARKER").write_text("x")
    sb.daemon_up()
    shim = sb.stubs / "mv"
    shim.write_text('#!/bin/bash\n'
                    'for a in "$@"; do case "$a" in *.new.*) exit 1;; esac; done\n'
                    'exec /bin/mv "$@"\n')
    shim.chmod(0o755)
    r = sb.install("--link")
    assert r.returncode != 0
    assert (sb.dest / "OLD_MARKER").exists() and not sb.dest.is_symlink(), "old install not restored"
    assert (older / "OLDER_MARKER").exists(), "the older backup was deleted by a failed upgrade"
    assert not [p for p in sb.dest.parent.iterdir() if ".bak.prev." in p.name or ".new." in p.name]
    assert (sb.sd / "loaded").exists(), "daemon left stopped"


def test_L2_rollback_stops_the_new_daemon_before_removing_its_tree(sb):
    sb.old_install()
    sb.daemon_up()
    r = sb.install(STUB_SCHEMA="1", CGC_START_WAIT_S="2")        # new daemon starts, serves the wrong schema
    assert r.returncode == 1
    assert sb.calls() == ["bootout", "bootstrap", "bootout", "bootstrap"], sb.log()
    assert "new_tree=1" in sb.lines("bootout")[1] or "new_tree" in sb.lines("bootout")[1]
    assert (sb.dest / "OLD_MARKER").exists()


def test_L2_force_keeps_the_new_install_and_does_not_delete_the_older_backup_unverified(sb):
    sb.old_install()
    older = sb.dest.parent / "chatgpt-consult.bak"
    older.mkdir()
    (older / "OLDER_MARKER").write_text("x")
    sb.daemon_up()
    r = sb.install("--force", STUB_NO_HEARTBEAT="1", CGC_START_WAIT_S="2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (sb.dest / "OLD_MARKER").exists() and (sb.dest.parent / "chatgpt-consult.bak" / "OLD_MARKER").exists()
    kept = [p for p in sb.dest.parent.iterdir() if ".bak.prev." in p.name]
    assert len(kept) == 1 and (kept[0] / "OLDER_MARKER").exists(), "the unverified upgrade deleted the older backup"
    assert kept[0].name in r.stdout, "the message must say where the older backup is"


# ================================================================ L3 delete only what we own

def test_L3_store_pointed_at_a_user_directory_deletes_nothing_else(sb):
    """Repro: CGC_STORE_DB=$HOME/Documents/control.db (directory name 'Documents', a precious file in it).
    The old purge rm -rf'd the whole directory."""
    sb.old_install()
    docs = sb.home / "Documents"
    docs.mkdir()
    (docs / "precious.txt").write_text("irreplaceable")
    (docs / "notes").mkdir()
    (docs / "notes" / "a.md").write_text("x")
    db = docs / "control.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.commit()
    c.close()
    r = sb.uninstall("--purge", CGC_STORE_DB=str(db), CGC_DATA_DIR=str(docs))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (docs / "precious.txt").read_text() == "irreplaceable" and (docs / "notes" / "a.md").exists()
    assert docs.is_dir()
    assert not db.exists(), "the store's own database is what --purge is for"


def test_L3_store_path_that_is_a_user_directory_is_never_removed_recursively(sb):
    """The reviewer's literal repro: CGC_STORE_DB=$HOME/Documents, the DIRECTORY itself, with precious.txt
    in it. The old purge ran rm -rf on whatever CGC_STORE_DB named."""
    sb.old_install()
    docs = sb.home / "Documents"
    docs.mkdir()
    (docs / "precious.txt").write_text("irreplaceable")
    (docs / "sub").mkdir()
    (docs / "sub" / "deep.txt").write_text("x")
    r = sb.uninstall("--purge", CGC_STORE_DB=str(docs))
    assert (docs / "precious.txt").read_text() == "irreplaceable" and (docs / "sub" / "deep.txt").exists()
    assert r.returncode == 1 and "not a regular file" in r.stderr
    assert sb.dest.exists(), "a refusal must change nothing"
    plain = sb.uninstall(CGC_STORE_DB=str(docs))               # without --purge nothing in the store is deleted
    assert plain.returncode == 0 and (docs / "precious.txt").exists()


def test_L3_store_set_to_home_itself_is_refused_without_touching_anything(sb):
    sb.old_install()
    (sb.home / "keep.txt").write_text("x")
    c = sqlite3.connect(sb.home / "control.db")
    c.execute("CREATE TABLE t (a)")
    c.commit()
    c.close()
    r = sb.uninstall("--purge", CGC_STORE_DB=str(sb.home / "control.db"), CGC_DATA_DIR=str(sb.home))
    assert (sb.home / "keep.txt").exists() and sb.home.is_dir()
    assert r.returncode == 1 and "will not delete" in r.stderr
    assert sb.dest.exists(), "a refusal must change nothing"


def test_L3_shared_directory_named_cgc_keeps_foreign_locks_and_files(sb):
    """Repro: a project directory whose basename contains 'cgc', holding foreign locks/. The old rule
    treated 'cgc' in the basename as proof of ownership and removed locks/ and incidents/ wholesale."""
    sb.old_install()
    shared = sb.home / "project-cgc"
    (shared / "locks").mkdir(parents=True)
    (shared / "locks" / "their.lock").write_text("not ours")
    (shared / "locks" / "daemon.lock").write_text("")
    (shared / "incidents" / "my-notes").mkdir(parents=True)
    (shared / "incidents" / "my-notes" / "todo.txt").write_text("not ours")
    (shared / "README.md").write_text("their project")
    c = sqlite3.connect(shared / "control.db")
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.commit()
    c.close()
    r = sb.uninstall("--purge", CGC_STORE_DB=str(shared / "control.db"), CGC_DATA_DIR=str(shared))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (shared / "locks" / "their.lock").read_text() == "not ours"
    assert (shared / "incidents" / "my-notes" / "todo.txt").exists() and (shared / "README.md").exists()
    assert not (shared / "control.db").exists() and not (shared / "locks" / "daemon.lock").exists()


def test_L3_a_symlinked_or_non_sqlite_database_is_not_deleted(sb):
    sb.old_install()
    sb.data.mkdir(parents=True)
    target = sb.home / "elsewhere.txt"
    target.write_text("not a database")
    os.symlink(target, sb.data / "control.db")
    (sb.data / "control.db.v3.bak").write_text("not a sqlite file either")
    r = sb.uninstall("--purge")
    assert r.returncode == 1 and sb.dest.exists(), r.stdout + r.stderr
    assert target.exists() and (sb.data / "control.db.v3.bak").exists()


def test_L3_default_locations_are_cleaned_of_the_tools_real_file_names(sb):
    """The names are the ones a live install leaves in /tmp/cgc and ~/.local/state/cgc."""
    sb.old_install()
    sb.store()
    inc = sb.data / "incidents" / "I-20261014-101500-composer-a1b2c3"
    inc.mkdir(parents=True)
    for n in ("diagnosis.json", "fixture.html"):
        (inc / n).write_text("x")
    spool = sb.state / "spool" / "logs"
    spool.mkdir(parents=True)
    (spool / "REQ-20261014-192330-a959d4.log").write_text("x")
    names = ["answer_REQ-20261008-214024-8153db.txt", "answer_REQ-20261008-214024-8153db.txt.raw",
             "prompt_REQ-20261014-181040-181760.md", "refs_20261014-181040-ab12cd.md", "refs_20261014-181040.md",
             "daemon.log", "chrome.log", "active.json", "active.json.lock"]
    for n in names:
        (sb.state / n).write_text("x")
    (sb.state / "keep-me.txt").write_text("foreign")
    r = sb.uninstall("--purge")
    assert r.returncode == 0, r.stdout + r.stderr
    left = sorted(p.name for p in sb.state.iterdir())
    assert left == ["keep-me.txt"], left
    assert not inc.exists() and not (sb.data / "incidents").exists() and not spool.exists()


def test_L3_a_custom_store_directory_is_not_removed_even_when_it_ends_up_empty(sb):
    sb.old_install()
    docs = sb.home / "Documents"
    docs.mkdir()
    c = sqlite3.connect(docs / "control.db")
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.commit()
    c.close()
    r = sb.uninstall("--purge", CGC_STORE_DB=str(docs / "control.db"), CGC_DATA_DIR=str(docs))
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (docs / "control.db").exists()
    assert docs.is_dir(), "a directory the user named is theirs, empty or not"


def test_L3_purge_deletes_exactly_the_tool_shaped_files_and_no_directory_recursively(sb):
    sb.old_install()
    sb.store()
    (sb.data / "locks" / "lease.REQ-20261014-120000-a1b2c3").write_text("")
    (sb.data / "locks" / "stranger.txt").write_text("foreign")
    (sb.data / "control.db.v4.bak.corrupt-1760000000").write_bytes(b"SQLite format 3\x00" + b"\0" * 80)
    (sb.data / "notes.txt").write_text("foreign")
    r = sb.uninstall("--purge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (sb.data / "control.db").exists() and not (sb.data / "control.db-wal").exists()
    assert not (sb.data / "control.db.v4.bak.corrupt-1760000000").exists()
    assert not (sb.data / "locks" / "lease.REQ-20261014-120000-a1b2c3").exists()
    assert (sb.data / "locks" / "stranger.txt").exists() and (sb.data / "notes.txt").exists(), \
        "a foreign file survives, and so do the directories that hold it"


# ================================================================ L4 resolve before any rm

def test_L4_journal_inside_the_code_tree_without_purge_is_refused_up_front(sb):
    """Repro: the store under DEST/cgc-data. A non-purge uninstall deleted the code tree, journal and all."""
    sb.old_install()
    inside = sb.dest / "cgc-data"
    inside.mkdir()
    c = sqlite3.connect(inside / "control.db")
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.commit()
    c.close()
    sb.cgc("install-daemon")
    r = sb.uninstall(CGC_STORE_DB=str(inside / "control.db"), CGC_DATA_DIR=str(inside))
    assert r.returncode == 1 and "lies inside" in r.stderr and "Nothing was changed" in r.stderr
    assert (inside / "control.db").exists() and sb.dest.exists()
    assert sb.plist.exists(), "the refusal must come before the daemon is stopped or its service removed"
    assert "bootout" not in sb.calls()


def test_L4_config_inside_the_code_tree_is_refused_even_with_purge(sb):
    sb.old_install()
    cfg = sb.dest / "myconfig"
    cfg.write_text("CGC_MODEL=Pro\n")
    r = sb.uninstall("--purge", CGC_CONFIG=str(cfg))
    assert r.returncode == 1 and "config file" in r.stderr
    assert cfg.exists() and sb.dest.exists()


def test_L4_purge_never_deletes_the_config_file_even_in_the_scratch_dir(sb):
    """Repro: CGC_STATE_DIR=$HOME/.config/cgc --purge. The config file lives there; the old rm -rf of the
    scratch directory took it along."""
    sb.old_install()
    cfgdir = sb.home / ".config" / "cgc"
    cfgdir.mkdir(parents=True)
    cfg = cfgdir / "config"
    cfg.write_text("CGC_MODEL=Pro\n")
    (cfgdir / "daemon.log").write_text("log")
    r = sb.uninstall("--purge", CGC_STATE_DIR=str(cfgdir), CGC_CONFIG=str(cfg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert cfg.read_text() == "CGC_MODEL=Pro\n", "the config file was deleted"
    assert not (cfgdir / "daemon.log").exists(), "the tool's own scratch file is still cleaned"


def test_L4_chrome_profile_holding_protected_paths_is_not_offered(sb):
    sb.old_install()
    prof = sb.home / ".cgc-chrome"
    prof.mkdir()
    (prof / "Local State").write_text("{}")
    cfg = prof / "cgc-config"
    cfg.write_text("CGC_MODEL=Pro\n")
    r = sb.uninstall("--purge", stdin="y\n", CGC_CONFIG=str(cfg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert prof.exists() and cfg.exists() and "must not be deleted" in r.stdout


def test_L4_chrome_profile_outside_home_or_unrecognised_is_left_alone(sb, tmp_path):
    sb.old_install()
    elsewhere = tmp_path / "somedir"
    elsewhere.mkdir()
    (elsewhere / "x").write_text("x")
    r = sb.uninstall("--purge", stdin="y\n", CGC_PROFILE=str(elsewhere))
    assert r.returncode == 0 and (elsewhere / "x").exists()
    inside = sb.home / "Pictures"
    inside.mkdir()
    (inside / "p.jpg").write_text("x")
    r = sb.uninstall("--purge", stdin="y\n", CGC_PROFILE=str(inside))
    assert (inside / "p.jpg").exists() and "left alone" in r.stdout


# ================================================================ L5 settings fail closed

@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 file")
def test_L5_unreadable_settings_stops_before_anything_is_deleted(sb):
    """Repro: chmod 000 settings.json. The old script treated the failure to read as 'nothing to remove'
    and deleted the code, leaving a live hook that points at it."""
    sb.old_install()
    sb.cgc("install-daemon")
    lc._seed_settings(sb)
    os.chmod(sb.settings, 0)
    try:
        r = sb.uninstall()
        forced = sb.uninstall("--force")
    finally:
        os.chmod(sb.settings, 0o600)
    for res in (r, forced):
        assert res.returncode == 1 and "cannot be edited safely" in res.stderr, res.stderr
    assert sb.dest.exists() and sb.plist.exists(), "nothing may change when settings.json cannot be checked"
    assert "bootout" not in sb.calls()
    assert "ACTIVATION" in sb.settings.read_text()


def test_L5_unsafe_shape_stops_even_with_force(sb):
    sb.old_install()
    sb.settings.parent.mkdir(parents=True, exist_ok=True)
    sb.settings.write_text(json.dumps({"hooks": {"SessionStart": "not-a-list"}}))
    r = sb.uninstall("--force")
    assert r.returncode == 1 and "cannot be edited safely" in r.stderr
    assert sb.dest.exists()


def test_L5_invalid_json_is_the_only_skippable_case_and_only_with_force(sb):
    sb.old_install()
    sb.settings.parent.mkdir(parents=True, exist_ok=True)
    sb.settings.write_text("{ nope")
    no = sb.uninstall()
    assert no.returncode == 1 and sb.dest.exists() and "--force" in no.stderr
    yes = sb.uninstall("--force")
    assert yes.returncode == 0 and not sb.dest.exists()
    assert "delete that entry by hand" in yes.stderr and sb.settings.read_text() == "{ nope"


# ================================================================ L6 / L7 config

@pytest.fixture
def cfgmod(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_CONFIG", str(tmp_path / "cgc" / "config"))
    monkeypatch.delenv("CGC_APPS", raising=False)
    sys.path.insert(0, SCRIPTS)
    import importlib
    import cgc_config
    importlib.reload(cgc_config)
    yield cgc_config, tmp_path / "cgc" / "config"
    sys.path.remove(SCRIPTS)


@pytest.mark.parametrize("sep", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\n", "\r", "\r\n"])
def test_L6_every_character_the_reader_splits_on_is_refused(cfgmod, sep):
    """Repro: CGC_APPS 'safe\\vCGC_GATE_ALLOW_GIST=1'. \\v is not \\n or \\r but str.splitlines() ends a
    line there, so the reader would load a second key the writer never validated."""
    m, p = cfgmod
    with pytest.raises(ValueError, match="line break"):
        m.set_key("CGC_APPS", f"safe{sep}CGC_GATE_ALLOW_GIST=1")
    assert not p.exists()


def test_L6_writer_and_reader_agree_on_what_a_line_is(cfgmod):
    """A hand-edited file where a \\v joins two settings. The reader sees two lines; a writer that does not
    never finds the key it is replacing, appends a second one, and the reader (setdefault: first wins) keeps
    the OLD value, so the `set` silently does nothing."""
    m, p = cfgmod
    p.parent.mkdir(parents=True)
    p.write_bytes("CGC_MODEL=Pro\x0bCGC_APPS=old\n".encode())
    m.set_key("CGC_APPS", "new")
    stored = list(m._parse(p.read_text(encoding="utf-8")))
    assert stored == [("CGC_MODEL", "Pro"), ("CGC_APPS", "new")], stored
    assert len(p.read_text().splitlines()) == 2


def test_L6_the_cli_surfaces_the_refusal(cfgmod, tmp_path):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "cgc_config.py"), "set", "CGC_APPS",
                        "safe\vCGC_GATE_ALLOW_GIST=1"],
                       env=dict(os.environ, CGC_CONFIG=str(tmp_path / "c2")), capture_output=True, text=True)
    assert r.returncode == 2 and "CGC_ERROR" in r.stderr and not (tmp_path / "c2").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 000 file")
def test_L7_an_unreadable_config_aborts_the_write_and_leaves_the_file_intact(cfgmod):
    """Repro: chmod 000 config then set. The old reader swallowed the OSError, started from an empty list,
    and replaced the file with ONE line, destroying every other setting."""
    m, p = cfgmod
    p.parent.mkdir(parents=True)
    p.write_text("CGC_MODEL=Pro\nCGC_PORT=9400\n")
    os.chmod(p, 0)
    try:
        with pytest.raises(OSError):
            m.set_key("CGC_APPS", "x")
        assert m._main(["set", "CGC_APPS", "x"]) == 1
    finally:
        os.chmod(p, 0o600)
    assert p.read_text() == "CGC_MODEL=Pro\nCGC_PORT=9400\n"
    assert [q.name for q in p.parent.iterdir() if q.name.startswith(".config.")] == []


def test_L7_a_missing_config_file_is_the_only_empty_config(cfgmod):
    m, p = cfgmod
    m.set_key("CGC_APPS", "x")
    assert p.read_text() == "CGC_APPS=x\n"


def test_L7_a_config_that_is_not_utf8_aborts_instead_of_being_replaced(cfgmod):
    m, p = cfgmod
    p.parent.mkdir(parents=True)
    p.write_bytes(b"CGC_MODEL=\xff\xfe\n")
    with pytest.raises(ValueError):
        m.set_key("CGC_APPS", "x")
    assert p.read_bytes() == b"CGC_MODEL=\xff\xfe\n"


# ================================================================ doctor --deep and HTTP_PROXY

def test_doctor_deep_websocket_bypasses_http_proxy_for_loopback(monkeypatch):
    """This machine has HTTP_PROXY set. websocket-client reads it and tunnels 127.0.0.1 through the
    proxy; ws_connect hands it a socket we opened, which skips proxy resolution. A listener that records
    whether anything reached it as a CONNECT is the proxy."""
    pytest.importorskip("websocket")
    proxy = socket.socket()
    proxy.bind(("127.0.0.1", 0))
    proxy.listen(5)
    proxy_hits = []

    def serve_proxy():
        while True:
            try:
                c, _ = proxy.accept()
            except OSError:
                return
            proxy_hits.append(c.recv(4096))
            c.close()
    threading.Thread(target=serve_proxy, daemon=True).start()

    target = socket.socket()
    target.bind(("127.0.0.1", 0))
    target.listen(5)
    port = target.getsockname()[1]
    seen = []

    def serve_ws():
        c, _ = target.accept()
        seen.append(c.recv(4096))
        c.close()
    threading.Thread(target=serve_ws, daemon=True).start()

    for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(k, f"http://127.0.0.1:{proxy.getsockname()[1]}")
    for k in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)
    sys.path.insert(0, SCRIPTS)
    try:
        import importlib
        import cgc_doctor
        importlib.reload(cgc_doctor)
        with pytest.raises(Exception):            # the fake target closes without a handshake
            cgc_doctor.ws_connect(f"ws://127.0.0.1:{port}/devtools/page/x", 2)
    finally:
        sys.path.remove(SCRIPTS)
    time.sleep(0.2)
    assert seen and seen[0].startswith(b"GET /devtools/page/x"), f"never reached the target: {seen}"
    assert proxy_hits == [], f"the loopback websocket went through the HTTP proxy: {proxy_hits}"
    proxy.close()
    target.close()


def test_L2_a_signal_during_the_swap_rolls_back_like_a_failure(sb):
    """Ctrl-C / SIGTERM between 'old tree parked' and 'daemon verified' must not strand the old tree at
    .bak with the daemon down. A slow `cp` (a shim) holds the window open long enough to signal it."""
    import signal as sig
    sb.old_install()
    sb.daemon_up()
    shim = sb.stubs / "cp"
    shim.write_text('#!/bin/bash\nsleep 3\nexec /bin/cp "$@"\n')
    shim.chmod(0o755)
    p = subprocess.Popen(["bash", os.path.join(ROOT, "install.sh")], cwd=ROOT, env=sb.env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 20
    while time.time() < deadline and "bootout" not in sb.calls():
        time.sleep(0.1)
    assert "bootout" in sb.calls(), "install never reached the daemon stop"
    time.sleep(0.5)
    p.send_signal(sig.SIGTERM)
    out, err = p.communicate(timeout=60)
    assert p.returncode != 0
    assert (sb.dest / "OLD_MARKER").exists(), err
    assert (sb.sd / "loaded").exists(), "daemon left stopped after an interrupted install: " + err
    assert not [q for q in sb.dest.parent.iterdir() if ".new." in q.name]
