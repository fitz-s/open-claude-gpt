# Second-round review of the lifecycle scripts (gpt-sol on 0de6518). Three findings, each reproduced here in
# the same sandbox as tests/test_lifecycle.py, plus the retry of an install that was killed outright.
#
#   P1  a signal between a rename and the state update used to leave the trees where the variables did not
#       say they were; the rollback now reads the disk by file identity and defers signals over each rename
#   P2  a case-insensitive alias or a hard link of the config file is the config file: protect by identity
#   P3  --purge deletes the plan it printed, re-checked path by path, and rescans nothing
#
# EVERY --link run happens in a scratch `git clone --local --no-hardlinks` of the repo, never in the worktree
# itself: --link points the install at the repo's skill/, and a broken run once wrote a .bak into the repo.
import os
import shutil
import sqlite3
import subprocess

import pytest

from test_lifecycle import ROOT

# ---- a scratch copy of the repo, for the --link runs ---------------------------------------------


@pytest.fixture(scope="module")
def clone(tmp_path_factory):
    """The repo as git sees it (HEAD plus the working-tree files the install reads), cloned locally with
    no hardlinks so that nothing in it shares an inode with the worktree."""
    dst = tmp_path_factory.mktemp("repo") / "repo"
    subprocess.run(["git", "clone", "-q", "--local", "--no-hardlinks", ROOT, str(dst)], check=True,
                   capture_output=True)
    for rel in ("install.sh", "uninstall.sh", "bin/cgc"):
        shutil.copy2(os.path.join(ROOT, rel), dst / rel)
    shutil.rmtree(dst / "skill" / "scripts", ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "skill", "scripts"), dst / "skill" / "scripts",
                    ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def _snapshot(path):
    """Every file under `path` with its size: proof that a --link run did not write into the clone. Python's
    bytecode caches are not part of the tree: any script that imports the tool's modules writes them."""
    out = {}
    for base, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".ruff_cache", ".code-review-graph")]
        for f in files:
            p = os.path.join(base, f)
            if not os.path.islink(p):
                out[os.path.relpath(p, path)] = os.path.getsize(p)
    return out


def _install_from(sb, repo, *args, **env):
    return sb.run(["bash", str(repo / "install.sh"), *args], **env)


# ---- P1: a signal in the three windows of the swap ------------------------------------------------

def _mv_shim(sb, window):
    """A `mv` that does the move and then sends SIGTERM to its parent (the install script), but only for the
    rename named by `window`. Windows: 1 = old tree moved aside, 2 = new tree moved into place,
    3 = an older .bak parked at .bak.prev.<pid>, 4 = the SIGKILL-recovery rename .bak -> DEST."""
    marks = {1: ("*/chatgpt-consult", "*/chatgpt-consult.bak"),
             2: ("*/.chatgpt-consult.new.*", "*/chatgpt-consult"),
             3: ("*/chatgpt-consult.bak", "*/chatgpt-consult.bak.prev.*"),
             4: ("*/chatgpt-consult.bak", "*/chatgpt-consult")}[window]
    shim = sb.stubs / "mv"
    shim.write_text(
        "#!/bin/bash\n"
        'args=(); for a in "$@"; do case "$a" in -*) ;; *) args+=("$a");; esac; done\n'
        'src="${args[0]}"; dst="${args[1]}"\n'
        '/bin/mv "$@"; rc=$?\n'
        f'case "$src" in {marks[0]}) case "$dst" in {marks[1]}) [ "$rc" = 0 ] && kill -TERM $PPID;; esac;; esac\n'
        'exit $rc\n')
    shim.chmod(0o755)


def _assert_old_tree_back(sb, nested_in=()):
    assert (sb.dest / "OLD_MARKER").exists(), "the old tree is not at DEST"
    assert not sb.dest.is_symlink()
    assert not [p for p in sb.dest.parent.iterdir() if ".new." in p.name], "stage left behind"
    for where in (sb.dest, *nested_in):
        for name in ("chatgpt-consult", "chatgpt-consult.bak", "skill"):
            assert not (where / name).exists() or where == sb.dest and name == "skill", \
                f"something was nested inside {where}: {name}"
    assert (sb.sd / "loaded").exists(), "the old daemon was not restarted"
    assert sb.calls()[-1] == "bootstrap" and "old_tree=1" in sb.lines("bootstrap")[-1], sb.log()


@pytest.mark.parametrize("window", [1, 2, 3], ids=["old-aside", "new-placed", "prev-parked"])
def test_P1_signal_after_each_rename_ends_with_the_old_tree_at_dest(sb, window):
    sb.old_install()
    older = sb.dest.parent / "chatgpt-consult.bak"           # makes window 3 exist
    older.mkdir()
    (older / "OLDER_MARKER").write_text("x")
    sb.daemon_up()
    _mv_shim(sb, window)
    r = sb.install()
    assert r.returncode == 143, (r.returncode, r.stdout, r.stderr)
    _assert_old_tree_back(sb)
    assert (older / "OLDER_MARKER").exists(), "the older backup was lost or moved"
    assert not [p for p in sb.dest.parent.iterdir() if ".bak.prev." in p.name]


@pytest.mark.parametrize("window", [1, 2], ids=["old-aside", "new-placed"])
def test_P1_signal_after_each_rename_in_link_mode(sb, clone, window):
    """--link: DEST becomes a symlink INTO the repo, and the old tree is moved aside. A rollback that did
    `mv .bak DEST` while DEST was that symlink would have moved the old tree INTO the repo's skill/."""
    sb.old_install()
    sb.daemon_up()
    before = _snapshot(clone)
    _mv_shim(sb, window)
    r = _install_from(sb, clone, "--link")
    assert r.returncode == 143, (r.returncode, r.stdout, r.stderr)
    _assert_old_tree_back(sb)
    assert _snapshot(clone) == before, "the install wrote into the repo"
    assert not (clone / "skill" / "chatgpt-consult").exists() and not (clone / "skill" / "chatgpt-consult.bak").exists()


def test_P1_a_signal_during_the_swap_is_acted_on_after_the_critical_section_not_inside_it(sb):
    """The rename and the state update are one step to a signal: the shim signals the installer in the middle of
    the three renames, and the result is still a clean rollback, not a half-moved pair of trees."""
    sb.old_install()
    sb.daemon_up()
    _mv_shim(sb, 1)
    r = sb.install()
    assert r.returncode == 143
    # at the moment of the rollback's bootstrap, only the old tree exists, whole
    assert "old_tree=1" in sb.lines("bootstrap")[-1]
    assert not (sb.dest.parent / "chatgpt-consult.bak").exists()


def test_P1_rollback_never_renames_onto_an_existing_dest(sb, clone):
    """Rollback finds DEST occupied (the new tree, or a symlink to the repo) and removes it before moving the
    old tree in; in link mode the symlink is removed as a link, never descended into."""
    sb.old_install()
    sb.daemon_up()
    marker = clone / "skill" / "REPO_FILE_THAT_MUST_SURVIVE"
    marker.write_text("x")
    _mv_shim(sb, 2)
    r = _install_from(sb, clone, "--link")
    assert r.returncode == 143
    assert marker.read_text() == "x" and (clone / "skill" / "scripts" / "cgc_daemon.py").exists()
    assert (sb.dest / "OLD_MARKER").exists() and not (sb.dest / "REPO_FILE_THAT_MUST_SURVIVE").exists()


# ---- the retry of an install that was killed outright (SIGKILL cannot be trapped) ----------------

def test_retry_restores_a_leftover_bak_when_nothing_is_at_dest(sb):
    """SIGKILL between 'old tree set aside' and 'new tree in place' leaves the old tree only at .bak, the
    service unloaded and nothing at DEST. The retry must not report success over that."""
    sb.old_install()
    os.rename(sb.dest, str(sb.dest) + ".bak")                # the state the killed run left behind
    r = sb.install()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "interrupted" in r.stdout and "restoring it first" in r.stdout
    assert _tree_new(sb.dest) and (sb.dest.parent / "chatgpt-consult.bak" / "OLD_MARKER").exists()


def test_retry_after_kill_with_a_service_starts_the_daemon_again(sb):
    sb.old_install()
    sb.daemon_up()                                           # a loaded service...
    sb.run(["launchctl", "bootout", "gui/501/com.open-claude-gpt.daemon"])      # ...that the killed run stopped
    (sb.sd / "log").unlink()
    os.rename(sb.dest, str(sb.dest) + ".bak")
    r = sb.install()
    assert r.returncode == 0, r.stdout + r.stderr
    assert sb.calls().count("bootstrap") == 1 and (sb.sd / "loaded").exists()


def _tree_new(root):
    return (root / "scripts" / "chatgpt_contract.json").exists() and not (root / "OLD_MARKER").exists()


# ---- P2: the config file is protected by identity, not by spelling ---------------------------------

def _case_insensitive(path):
    probe = path / "CaseProbe"
    probe.write_text("x")
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.unlink()


def test_P2_config_named_in_another_case_is_the_scratch_file_and_survives_purge(sb):
    """Repro: CGC_CONFIG=$H/state/ACTIVE.JSON. On a case-insensitive filesystem (APFS default) that is the very
    file the scan lists as active.json, so --purge deleted the config. Where the filesystem is case-sensitive
    the two names are two files and nothing aliases; the hard-link test below covers that case."""
    sb.old_install()
    sb.state.mkdir()
    insensitive = _case_insensitive(sb.state)
    (sb.state / "active.json").write_text("CGC_MODEL=Pro\n")
    cfg = sb.state / "ACTIVE.JSON"
    if not insensitive:
        cfg.write_text("CGC_MODEL=Pro\n")                    # a distinct file, nothing to protect
    r = sb.uninstall("--purge", CGC_CONFIG=str(cfg))
    assert r.returncode == 0, r.stdout + r.stderr
    if insensitive:
        assert cfg.read_text() == "CGC_MODEL=Pro\n", "the config file was deleted through its case alias"
        assert "is your config file" in r.stdout
    else:
        assert cfg.exists() and not (sb.state / "active.json").exists()


def test_P2_config_hard_linked_to_a_scratch_file_survives_purge(sb):
    sb.old_install()
    sb.state.mkdir()
    (sb.state / "daemon.log").write_text("CGC_MODEL=Pro\n")
    cfgdir = sb.home / ".config" / "cgc"
    cfgdir.mkdir(parents=True)
    cfg = cfgdir / "config"
    os.link(sb.state / "daemon.log", cfg)
    r = sb.uninstall("--purge", CGC_CONFIG=str(cfg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert cfg.read_text() == "CGC_MODEL=Pro\n"
    assert "is your config file" in r.stdout


def test_P2_config_reached_through_a_symlink_survives_purge(sb):
    sb.old_install()
    sb.state.mkdir()
    (sb.state / "chrome.log").write_text("CGC_MODEL=Pro\n")
    cfg = sb.home / "cfg-link"
    os.symlink(sb.state / "chrome.log", cfg)
    r = sb.uninstall("--purge", CGC_CONFIG=str(cfg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert (sb.state / "chrome.log").read_text() == "CGC_MODEL=Pro\n"


def test_P2_inside_checks_use_identity_too(sb):
    """A config file inside the code tree is refused even when the tree is named in another case."""
    sb.old_install()
    cfg = sb.dest / "myconfig"
    cfg.write_text("CGC_MODEL=Pro\n")
    if not _case_insensitive(sb.dest.parent):
        pytest.skip("needs a case-insensitive filesystem to alias the code tree's name")
    alias = sb.dest.parent / "CHATGPT-CONSULT" / "myconfig"
    r = sb.uninstall("--purge", CGC_CONFIG=str(alias))
    assert r.returncode == 1 and "config file" in r.stderr and cfg.exists()


def test_P2_a_journal_inside_a_case_aliased_code_tree_is_refused(sb):
    if not _case_insensitive(sb.home):
        pytest.skip("needs a case-insensitive filesystem")
    sb.old_install()
    inside = sb.dest / "cgc-data"
    inside.mkdir()
    c = sqlite3.connect(inside / "control.db")
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.commit()
    c.close()
    alias = sb.dest.parent / "CHATGPT-CONSULT" / "cgc-data" / "control.db"
    r = sb.uninstall(CGC_STORE_DB=str(alias), CGC_DATA_DIR=str(alias.parent))
    assert r.returncode == 1 and "lies inside" in r.stderr and (inside / "control.db").exists()


# ---- P3: --purge deletes the plan that was shown, nothing it finds later ---------------------------

def test_P3_a_file_created_between_plan_and_delete_is_not_deleted(sb):
    """Repro: a chrome.log appearing after the plan was printed. The old delete step rescanned the directory and
    took it along. The 'daemon stop' that sits between the plan and the delete is where a daemon the user
    restarted by hand would write it; a launchctl stub that drops the file at that moment reproduces it."""
    sb.old_install()
    sb.state.mkdir()
    (sb.state / "daemon.log").write_text("planned")
    stub = sb.stubs / "launchctl"
    sb.cgc("install-daemon")
    body = stub.read_text()
    stub.write_text(body.replace(
        'case "$1" in\n  bootout|unload)',
        'case "$1" in\n  bootout|unload)\n    echo "written after the plan" > "' + str(sb.state) + '/chrome.log"', 1))
    r = sb.uninstall("--purge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (sb.state / "daemon.log").exists(), "the planned file should have been deleted"
    assert (sb.state / "chrome.log").read_text() == "written after the plan\n", \
        "a file that was not in the shown plan was deleted"


def test_P3_a_planned_file_replaced_by_another_file_is_left_alone(sb):
    sb.old_install()
    sb.state.mkdir()
    victim = sb.state / "daemon.log"
    victim.write_text("planned")
    stub = sb.stubs / "launchctl"
    sb.cgc("install-daemon")
    swap = (f'    rm -f "{victim}"; echo "someone else\'s file" > "{victim}"\n')
    stub.write_text(stub.read_text().replace('case "$1" in\n  bootout|unload)',
                                             'case "$1" in\n  bootout|unload)\n' + swap, 1))
    r = sb.uninstall("--purge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert victim.read_text() == "someone else's file\n"
    assert "not the file that was planned" in r.stderr


def test_P3_a_planned_file_replaced_by_a_symlink_or_directory_is_left_alone(sb):
    sb.old_install()
    sb.state.mkdir()
    target = sb.home / "precious"
    target.write_text("keep")
    (sb.state / "daemon.log").write_text("planned")
    (sb.state / "chrome.log").write_text("planned")
    stub = sb.stubs / "launchctl"
    sb.cgc("install-daemon")
    swap = (f'    rm -f "{sb.state}/daemon.log"; ln -s "{target}" "{sb.state}/daemon.log"\n'
            f'    rm -f "{sb.state}/chrome.log"; mkdir "{sb.state}/chrome.log"; echo x > "{sb.state}/chrome.log/inner"\n')
    stub.write_text(stub.read_text().replace('case "$1" in\n  bootout|unload)',
                                             'case "$1" in\n  bootout|unload)\n' + swap, 1))
    r = sb.uninstall("--purge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert target.read_text() == "keep" and (sb.state / "chrome.log" / "inner").exists()
    assert (sb.state / "daemon.log").is_symlink()


def test_P3_the_delete_step_rescans_nothing_and_reads_only_the_plan_file():
    src = open(os.path.join(ROOT, "uninstall.sh"), encoding="utf-8").read()
    delete = src[src.index("if mode == \"delete\":"):src.index("def plan_file")]
    assert "listdir" not in delete and "os.walk" not in delete and "glob" not in delete, \
        "the delete step must work from the printed plan, never rescan"
    assert 'plan_py delete "$PLAN_FILE"' in src


def test_P3_the_plan_the_user_saw_is_the_plan_that_runs(sb):
    sb.old_install()
    sb.store()
    r = sb.uninstall("--purge")
    assert r.returncode == 0, r.stdout + r.stderr
    shown = r.stdout.split("deleting EXACTLY these paths")[1].split("(the code tree")[0]
    for name in ("control.db", "control.db-wal", "control.db.v4.bak"):
        assert str(sb.data / name) in shown and not (sb.data / name).exists()
    assert (sb.data / "locks").exists() is False or not any((sb.data / "locks").iterdir())


# ---- the clone itself is a faithful scratch copy -----------------------------------------------------

def test_the_link_runs_use_a_clone_that_shares_no_inode_with_the_worktree(clone):
    a = os.stat(os.path.join(ROOT, "skill", "scripts", "cgc_doctor.py"))
    b = os.stat(clone / "skill" / "scripts" / "cgc_doctor.py")
    assert (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino)
    assert os.path.realpath(clone) != os.path.realpath(ROOT)


def test_link_install_in_the_clone_leaves_the_worktree_untouched(sb, clone):
    before = _snapshot(ROOT)
    r = _install_from(sb, clone, "--link")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sb.dest.is_symlink() and os.path.realpath(sb.dest) == os.path.realpath(clone / "skill")
    assert _snapshot(ROOT) == before


# ---- the recovery rename and the record that the daemon is owed a start are one deferred step -----

def test_recovery_signal_after_bak_to_dest_still_restarts_the_daemon(sb, clone):
    """After a SIGKILL the retry first moves .bak back to DEST. A TERM right after that rename used to land
    before 'this run owes the daemon a start' was recorded: the old tree was back and the daemon stayed
    unloaded. Run in a scratch clone with the reviewer's shim (TERM after .bak -> DEST)."""
    sb.old_install()
    sb.cgc("install-daemon")                                              # the plist, and a loaded service...
    sb.run(["launchctl", "bootout", "gui/501/com.open-claude-gpt.daemon"])    # ...that the killed run had stopped
    (sb.sd / "log").unlink()
    os.rename(sb.dest, str(sb.dest) + ".bak")                             # the state a SIGKILL left behind
    _mv_shim(sb, 4)
    before = _snapshot(clone)
    r = _install_from(sb, clone)
    assert r.returncode == 143, (r.returncode, r.stdout, r.stderr)
    assert (sb.dest / "OLD_MARKER").exists() and not (sb.dest.parent / "chatgpt-consult.bak").exists()
    assert (sb.sd / "loaded").exists(), "the daemon was left unloaded after the signal: " + r.stderr
    assert sb.calls() == ["bootstrap"], sb.log()
    assert _snapshot(clone) == before


# ---- place verifies its result: something planted at the target is detected, never trusted ----------

def _plant_shim(sb, plans):
    """A `mv` that plants a symlink at the target, to a foreign directory, JUST BEFORE it runs the real move,
    for each (src_glob, dst_glob, foreign_dir) in `plans` (each plan fires once). This is the window between
    place's pre-check and its rename."""
    lines = ["#!/bin/bash",
             'args=(); for a in "$@"; do case "$a" in -*) ;; *) args+=("$a");; esac; done',
             'src="${args[0]}"; dst="${args[1]}"']
    for i, (sg, dg, foreign) in enumerate(plans):
        lines.append(f'if [ ! -e "$STUB_DIR/planted.{i}" ]; then case "$src" in {sg}) case "$dst" in {dg}) '
                     f'touch "$STUB_DIR/planted.{i}"; ln -s "{foreign}" "$dst";; esac;; esac; fi')
    lines.append('exec /bin/mv "$@"')
    shim = sb.stubs / "mv"
    shim.write_text("\n".join(lines) + "\n")
    shim.chmod(0o755)


def test_place_detects_a_symlink_planted_at_the_target_and_the_rollback_recovers_the_tree(sb):
    """Repro (reviewer): a symlink to $HOME/foreign is planted at .bak after the pre-check. mv then moves the
    old tree INTO foreign/. The install must fail loudly, report both paths, and bring the tree back."""
    sb.old_install()
    foreign = sb.home / "foreign"
    foreign.mkdir()
    sb.daemon_up()
    _plant_shim(sb, [("*/chatgpt-consult", "*/chatgpt-consult.bak", foreign)])
    r = sb.install()
    assert r.returncode != 0, "an install whose rename landed in the wrong place reported success"
    assert "running doctor" not in r.stdout, "the install went on to report success after a misplaced rename"
    assert "did not put it there" in r.stderr
    assert str(sb.dest) in r.stderr and f"{sb.dest}.bak" in r.stderr and f"{sb.dest}.bak/chatgpt-consult" in r.stderr
    assert (sb.dest / "OLD_MARKER").exists(), "the tree that went into the foreign directory was not recovered"
    assert not (foreign / "chatgpt-consult").exists(), "our tree was left inside a directory that is not ours"
    assert (sb.sd / "loaded").exists(), "the old daemon was not restarted"


def test_place_failure_that_cannot_be_rolled_back_reports_where_the_old_tree_is(sb):
    """The reviewer's second shim as well: the rename back is hijacked too. Nothing may continue or claim
    success, and the old tree must be reported where it ended up."""
    sb.old_install()
    foreign, foreign2 = sb.home / "foreign", sb.home / "foreign2"
    foreign.mkdir()
    foreign2.mkdir()
    sb.daemon_up()
    _plant_shim(sb, [("*/chatgpt-consult", "*/chatgpt-consult.bak", foreign),
                     ("*/chatgpt-consult.bak/chatgpt-consult", "*/chatgpt-consult", foreign2)])
    r = sb.install()
    assert r.returncode != 0
    assert "could not restore the previous install" in r.stderr
    # the report names the path the tree is reachable at; DEST is now a symlink to foreign2, so that path
    # is DEST/chatgpt-consult and it resolves to foreign2/chatgpt-consult
    reported = r.stderr.split("The old tree is at: ")[1].split(". The older backup")[0]
    assert reported == f"{sb.dest}/chatgpt-consult", reported
    assert os.path.realpath(reported) == os.path.realpath(foreign2 / "chatgpt-consult")
    assert (foreign2 / "chatgpt-consult" / "OLD_MARKER").exists(), "the report points at a place the tree is not"
    assert "running doctor" not in r.stdout, "the install went on to report success"


def test_place_planted_symlink_at_dest_in_link_mode_never_writes_into_the_repo(sb, clone):
    """--link: the staged tree is a symlink INTO the repo. A symlink planted at DEST makes the final rename
    move that symlink into the foreign directory instead. The install fails, and nothing was written into
    the repo; the old tree is reported."""
    sb.old_install()
    foreign = sb.home / "foreign"
    foreign.mkdir()
    sb.daemon_up()
    before = _snapshot(clone)
    _plant_shim(sb, [("*/.chatgpt-consult.new.*", "*/chatgpt-consult", foreign)])
    r = _install_from(sb, clone, "--link")
    assert r.returncode != 0
    assert "did not put it there" in r.stderr
    assert _snapshot(clone) == before, "the install wrote into the repo"
    assert not (clone / "skill" / "chatgpt-consult").exists() and not (clone / "skill" / "chatgpt-consult.bak").exists()
    assert (sb.dest.parent / "chatgpt-consult.bak" / "OLD_MARKER").exists() or (sb.dest / "OLD_MARKER").exists()


# ---- uninstall: the delete step re-checks the config at delete time, not only at plan time ------------

def test_uninstall_delete_skips_a_path_that_became_the_config_after_the_plan(sb):
    """The plan was printed when the config file was elsewhere. By the time the delete runs, the config path
    is a hard link to a planned scratch file. The delete re-checks identity and leaves it."""
    sb.old_install()
    sb.state.mkdir()
    victim = sb.state / "daemon.log"
    victim.write_text("CGC_MODEL=Pro\n")
    cfg = sb.home / "later-config"
    stub = sb.stubs / "launchctl"
    sb.cgc("install-daemon")
    stub.write_text(stub.read_text().replace(
        'case "$1" in\n  bootout|unload)',
        f'case "$1" in\n  bootout|unload)\n    ln "{victim}" "{cfg}"', 1))
    r = sb.uninstall("--purge", CGC_CONFIG=str(cfg))
    assert r.returncode == 0, r.stdout + r.stderr
    assert victim.exists() and cfg.read_text() == "CGC_MODEL=Pro\n"
    assert "is your config file; left alone" in r.stderr
