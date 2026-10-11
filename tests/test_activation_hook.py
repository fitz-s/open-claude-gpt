# Tests for the proactive SessionStart hook (cgc_activation.py) — install/remove/status against
# settings.json, and the one command string bin/cgc + cgc_doctor.py both defer to.
#
# Every test drives the module against tmp_path files only (--settings / --activation), mirroring
# tests/test_config.py's importlib.util spec-loading pattern. No test may read or write the
# developer's real ~/.claude/settings.json.
import importlib.util
import json
import os

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load():
    spec = importlib.util.spec_from_file_location("cgc_activation", os.path.join(SCRIPTS, "cgc_activation.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def m():
    return _load()


@pytest.fixture
def paths(tmp_path):
    """A settings.json path (not yet created) and an ACTIVATION.md path under a directory named
    chatgpt-consult, so OURS_MARKER matching works exactly as it would against a real install."""
    settings = tmp_path / "settings.json"
    act_dir = tmp_path / "somewhere" / "chatgpt-consult"
    act_dir.mkdir(parents=True)
    activation = act_dir / "ACTIVATION.md"
    activation.write_text("activation note\n", encoding="utf-8")
    return str(settings), str(activation)


# ---- 1. install into a nonexistent settings.json -----------------------------

def test_install_creates_settings_file(m, paths):
    settings, activation = paths
    outcome, count = m.install(settings, activation)
    assert outcome == "added"
    assert count == 1
    assert os.path.exists(settings)
    data = json.loads(open(settings, encoding="utf-8").read())
    entries = m._ours_entries(data)
    assert len(entries) == 1
    assert entries[0]["command"] == m.build_command(activation)


# ---- 2. install twice: second run is a true no-op -----------------------------

def test_install_twice_is_unchanged_and_byte_identical(m, paths):
    settings, activation = paths
    m.install(settings, activation)
    before = open(settings, "rb").read()
    outcome, count = m.install(settings, activation)
    after = open(settings, "rb").read()
    assert outcome == "unchanged"
    assert count == 0
    assert before == after
    assert not os.path.exists(settings + ".bak")


# ---- 3. install over the old `|| true` form upgrades in place -----------------

def test_install_upgrades_old_silent_form_in_place(m, paths):
    settings, activation = paths
    old_cmd = f"cat '{activation}' 2>/dev/null || true"
    seed = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": old_cmd, "timeout": 7}]},
            ]
        }
    }
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(seed, f)

    outcome, count = m.install(settings, activation)
    assert outcome == "upgraded"
    assert count == 1

    data = json.loads(open(settings, encoding="utf-8").read())
    groups = data["hooks"]["SessionStart"]
    assert len(groups) == 1  # no duplicate entry appended
    hook = groups[0]["hooks"][0]
    assert hook["command"] == m.build_command(activation)
    assert hook["timeout"] == 7  # original key preserved


# ---- 4. unrelated content survives an install ---------------------------------

def test_install_preserves_unrelated_content(m, paths):
    settings, activation = paths
    seed = {
        "some_top_level_key": "keep me",
        "hooks": {
            "SessionStart": [
                {"matcher": "other", "hooks": [{"type": "command", "command": "echo unrelated", "timeout": 3}]},
            ],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pretool"}]},
            ],
        },
    }
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(seed, f)

    m.install(settings, activation)

    data = json.loads(open(settings, encoding="utf-8").read())
    assert data["some_top_level_key"] == "keep me"
    assert data["hooks"]["PreToolUse"] == seed["hooks"]["PreToolUse"]
    starts = data["hooks"]["SessionStart"]
    assert any(g.get("matcher") == "other" and g["hooks"][0]["command"] == "echo unrelated" for g in starts)
    assert any(m.is_ours(h) for g in starts for h in g.get("hooks", []))


# ---- 5. .bak holds the pre-install content -------------------------------------

def test_bak_written_before_mutating_install(m, paths):
    settings, activation = paths
    seed = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "echo x"}]}]}}
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    pre_content = open(settings, encoding="utf-8").read()

    m.install(settings, activation)

    bak = settings + ".bak"
    assert os.path.exists(bak)
    assert open(bak, encoding="utf-8").read() == pre_content


# ---- 6. invalid JSON / bad shape refuses without touching the file ------------

def test_invalid_json_refuses_and_leaves_file_untouched(m, paths):
    settings, activation = paths
    with open(settings, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    before = open(settings, "rb").read()

    with pytest.raises(m.SettingsError):
        m.install(settings, activation)

    assert open(settings, "rb").read() == before
    assert not os.path.exists(settings + ".bak")


def test_hooks_not_a_dict_refuses_and_leaves_file_untouched(m, paths):
    settings, activation = paths
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"hooks": "not-a-dict"}, f)
    before = open(settings, "rb").read()

    with pytest.raises(m.SettingsError):
        m.install(settings, activation)

    assert open(settings, "rb").read() == before
    assert not os.path.exists(settings + ".bak")


# ---- 7. remove after install; unrelated groups untouched; idempotent ----------

def test_remove_after_install_prunes_ours_only(m, paths):
    settings, activation = paths
    seed = {
        "hooks": {
            "SessionStart": [
                {"matcher": "other", "hooks": [{"type": "command", "command": "echo unrelated"}]},
            ]
        }
    }
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    m.install(settings, activation)

    outcome, count = m.remove(settings)
    assert outcome == "removed"
    assert count == 1

    data = json.loads(open(settings, encoding="utf-8").read())
    starts = data["hooks"]["SessionStart"]
    assert len(starts) == 1
    assert starts[0]["matcher"] == "other"
    assert not any(m.is_ours(h) for g in starts for h in g.get("hooks", []))

    outcome2, count2 = m.remove(settings)
    assert outcome2 == "absent"
    assert count2 == 0


def test_remove_prunes_empty_group_and_session_start_and_hooks(m, paths):
    settings, activation = paths
    m.install(settings, activation)  # only our entry present, nothing else

    outcome, count = m.remove(settings)
    assert outcome == "removed"
    assert count == 1

    data = json.loads(open(settings, encoding="utf-8").read())
    assert "hooks" not in data  # pruned all the way up: group, SessionStart, hooks


def test_remove_on_nonexistent_settings_is_absent(m, tmp_path):
    settings = str(tmp_path / "no-such-settings.json")
    outcome, count = m.remove(settings)
    assert outcome == "absent"
    assert count == 0
    assert not os.path.exists(settings)


# ---- 8. status: configured / broken / absent -----------------------------------

def test_status_configured_after_install(m, paths):
    settings, activation = paths
    m.install(settings, activation)
    r = m.status(settings, activation)
    assert r["state"] == "configured"
    assert r["stale"] is False


def test_status_broken_when_activation_file_deleted(m, paths):
    settings, activation = paths
    m.install(settings, activation)
    os.remove(activation)
    r = m.status(settings, activation)
    assert r["state"] == "broken"
    assert activation in r["detail"]


def test_status_absent_with_no_ours_entry(m, paths):
    settings, activation = paths
    seed = {"hooks": {"SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "echo x"}]}]}}
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(seed, f)
    r = m.status(settings, activation)
    assert r["state"] == "absent"


# ---- 9. detection is path-agnostic (a different skills root is still ours) ----

def test_status_and_install_recognize_entry_under_different_skills_root(m, paths):
    settings, activation = paths
    other_path = "/somewhere/else/chatgpt-consult/ACTIVATION.md"
    seed = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": f"cat '{other_path}' 2>/dev/null || true", "timeout": 10}]},
            ]
        }
    }
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(seed, f)

    r = m.status(settings, activation)
    assert r["state"] in ("broken", "configured")  # recognized as OURS either way
    assert r["entries"] == 1

    outcome, count = m.install(settings, activation)
    assert outcome == "upgraded"
    assert count == 1
    data = json.loads(open(settings, encoding="utf-8").read())
    starts = data["hooks"]["SessionStart"]
    assert len(starts) == 1  # upgraded in place, not appended as a second entry
    assert starts[0]["hooks"][0]["command"] == m.build_command(activation)


# ---- 10. the emitted command: no `|| true`, correct quoting, refuses on ' -----

def test_command_has_no_silent_true_fallback(m, paths):
    _settings, activation = paths
    cmd = m.build_command(activation)
    assert "|| true" not in cmd
    assert "|| echo" in cmd


def test_command_quotes_path_with_space(m, tmp_path):
    act_dir = tmp_path / "a b" / "chatgpt-consult"
    act_dir.mkdir(parents=True)
    activation = str(act_dir / "ACTIVATION.md")
    cmd = m.build_command(activation)
    assert f"'{activation}'" in cmd


def test_command_refuses_path_with_single_quote(m, tmp_path):
    activation = str(tmp_path / "a'b" / "chatgpt-consult" / "ACTIVATION.md")
    with pytest.raises(m.QuoteError):
        m.build_command(activation)


# ---- identity must not depend on the activation path's spelling ---------------
# (regression: OURS_MARKER used to be path-shaped, so an activation path with no literal
# "chatgpt-consult" directory in it — e.g. <repo>/skill/ACTIVATION.md from a repo clone whose
# skill lives at skill/, not chatgpt-consult/ — was never recognized as ours: install kept
# appending duplicates and status reported "absent" over N installed copies.)

def test_install_idempotent_when_activation_path_has_no_chatgpt_consult_dir(m, tmp_path):
    settings = str(tmp_path / "settings.json")
    act_dir = tmp_path / "skill"  # deliberately NOT named chatgpt-consult
    act_dir.mkdir()
    activation = str(act_dir / "ACTIVATION.md")
    (act_dir / "ACTIVATION.md").write_text("note\n", encoding="utf-8")

    outcome1, count1 = m.install(settings, activation)
    assert (outcome1, count1) == ("added", 1)

    outcome2, count2 = m.install(settings, activation)
    assert (outcome2, count2) == ("unchanged", 0)

    outcome3, count3 = m.install(settings, activation)
    assert (outcome3, count3) == ("unchanged", 0)

    data = json.loads(open(settings, encoding="utf-8").read())
    starts = data["hooks"]["SessionStart"]
    assert len(starts) == 1
    assert len(starts[0]["hooks"]) == 1


def test_status_configured_when_activation_path_has_no_chatgpt_consult_dir(m, tmp_path):
    settings = str(tmp_path / "settings.json")
    act_dir = tmp_path / "skill"
    act_dir.mkdir()
    activation = str(act_dir / "ACTIVATION.md")
    (act_dir / "ACTIVATION.md").write_text("note\n", encoding="utf-8")

    m.install(settings, activation)
    r = m.status(settings, activation)
    assert r["state"] == "configured"
    assert r["entries"] == 1


def test_is_ours_matches_current_marker_regardless_of_path(m):
    cmd = m.build_command("/wherever/this/repo/lives/skill/ACTIVATION.md")
    assert m.is_ours({"command": cmd})


def test_is_ours_still_matches_legacy_path_marker(m):
    # Old printed snippet, old `|| true` shape, under a different skills root entirely.
    cmd = "cat '/opt/other-root/chatgpt-consult/ACTIVATION.md' 2>/dev/null || true"
    assert m.is_ours({"command": cmd})


# ---- status distinguishes "could not tell" from "genuinely absent" ------------

def test_status_unreadable_for_invalid_json(m, paths):
    settings, activation = paths
    with open(settings, "w", encoding="utf-8") as f:
        f.write("not json {")
    r = m.status(settings, activation)
    assert r["state"] == "unreadable"
    assert r["state"] != "absent"
    assert settings in r["detail"]


def test_status_unreadable_for_bad_hooks_shape(m, paths):
    settings, activation = paths
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"hooks": "not-a-dict"}, f)
    r = m.status(settings, activation)
    assert r["state"] == "unreadable"


def test_status_unreadable_json_output_has_detail_and_no_crash(m, paths):
    settings, activation = paths
    with open(settings, "w", encoding="utf-8") as f:
        f.write("{broken")
    r = m.status(settings, activation)  # must not raise
    assert r["settings_path"] == settings
    assert "detail" in r


# ---- install preserves an existing settings.json's permission mode ------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_install_preserves_existing_file_mode(m, paths):
    settings, activation = paths
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({}, f)
    os.chmod(settings, 0o644)

    m.install(settings, activation)

    import stat
    mode = stat.S_IMODE(os.stat(settings).st_mode)
    assert mode == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_backup_inherits_the_private_mode_of_the_file_it_copies(m, paths):
    # A 0600 settings.json must not leave a 0644 duplicate of itself next to it.
    settings, activation = paths
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({}, f)
    os.chmod(settings, 0o600)

    m.install(settings, activation)

    import stat
    assert stat.S_IMODE(os.stat(settings + ".bak").st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_install_new_file_defaults_to_0600(m, paths):
    settings, activation = paths
    assert not os.path.exists(settings)

    m.install(settings, activation)

    import stat
    mode = stat.S_IMODE(os.stat(settings).st_mode)
    assert mode == 0o600


# ---- the snapshot hook: upgrade path, output, and the error fallback ---------------------------
# The hook runs `cgc_activation.py snapshot` (ACTIVATION.md + one state line) instead of `cat`. It
# must upgrade the previous `cat || echo` form in place, never fail a session start, and never write
# to the store it reads.

import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402


def test_install_upgrades_the_previous_cat_form_to_the_snapshot_form(m, paths):
    settings, activation = paths
    prev = (f"cat '{activation}' 2>/dev/null || echo 'WARNING: chatgpt-consult ACTIVATION.md "
            f"missing at {activation} — run: cgc doctor'")
    with open(settings, "w", encoding="utf-8") as f:
        json.dump({"hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command", "command": prev, "timeout": 10}]}]}}, f)
    assert m.status(settings, activation)["stale"] is True
    assert m.install(settings, activation) == ("upgraded", 1)
    data = json.loads(open(settings, encoding="utf-8").read())
    hooks = data["hooks"]["SessionStart"]
    assert len(hooks) == 1 and len(hooks[0]["hooks"]) == 1, "upgraded in place, not appended"
    cmd = hooks[0]["hooks"][0]["command"]
    assert cmd == m.build_command(activation) and "snapshot" in cmd
    assert m.install(settings, activation) == ("unchanged", 0)
    assert m.status(settings, activation)["stale"] is False


def test_snapshot_command_is_recognized_as_ours_even_without_the_warning_text(m, tmp_path):
    hook = {"command": f"python3 '{m.SELF}' snapshot --activation '{tmp_path / 'A.md'}'"}
    assert m.is_ours(hook)


def test_the_snapshot_command_keeps_a_visible_fallback_chain(m, paths):
    _s, activation = paths
    cmd = m.build_command(activation)
    assert "|| true" not in cmd
    assert cmd.index("snapshot") < cmd.index("cat '") < cmd.index("echo '")


def _run_snapshot(activation, env_over):
    env = {**os.environ, "PATH": "/usr/bin:/bin", **env_over}
    return subprocess.run([sys.executable, os.path.join(SCRIPTS, "cgc_activation.py"), "snapshot",
                           "--activation", activation], capture_output=True, text=True, env=env,
                          timeout=10)


def _store_env(tmp_path):
    return {"CGC_STORE_DB": str(tmp_path / "d" / "control.db"), "CGC_DATA_DIR": str(tmp_path / "d"),
            "CGC_STATE_DIR": str(tmp_path / "s")}


def _seed_env(monkeypatch, tmp_path, **extra):
    """Point THIS process at the test's own store (monkeypatch undoes it, and conftest's isolation
    stays in force afterwards) and return the same settings for a subprocess."""
    env = {**_store_env(tmp_path), **extra}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    sys.path.insert(0, SCRIPTS)
    return env


def test_snapshot_prints_the_note_then_one_state_line(tmp_path, paths, monkeypatch):
    _s, activation = paths
    env = _seed_env(monkeypatch, tmp_path, CGC_WEEKLY_BUDGET="50")
    import cgc_store
    with cgc_store.Store() as st:
        st.create_round("REQ-20260707-120000-0d0001", "submit", prompt="# T\n\nb")
        st.set_state("REQ-20260707-120000-0d0001", cgc_store.READY)
        aid = st.begin_send("REQ-20260707-120000-0d0001", "p", "h" * 64, daemon_instance_id="d")
        st.mark_accepted(aid, "conv-1")
        st.mark_waiting("REQ-20260707-120000-0d0001")
        st.finish("REQ-20260707-120000-0d0001", cgc_store.COMPLETED_VERIFIED, result_text="a")
        st.create_round("REQ-20260707-120000-0d0002", "submit", prompt="# Later\n\nb",
                        not_before="2999-01-01T00:00:00+00:00")
    r = _run_snapshot(activation, env)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "activation note"
    assert len(lines) == 2, "the note, then exactly ONE state line"
    assert lines[1] == ("Consult budget: 1 of 50 used (rolling 7d) · 49 available · 1 ready in backlog"
                        " · 1 answers awaiting grade (cgc feedback) · p50 0s · weekly review due")


def test_snapshot_is_read_only_and_never_creates_a_store(tmp_path, paths):
    _s, activation = paths
    env = _store_env(tmp_path)
    r = _run_snapshot(activation, env)
    assert r.returncode == 0 and r.stdout == "activation note\n", "no store -> just the note"
    assert not os.path.exists(env["CGC_STORE_DB"]) and not os.path.exists(env["CGC_DATA_DIR"])


def test_snapshot_does_not_write_to_the_store(tmp_path, paths, monkeypatch):
    _s, activation = paths
    env = _seed_env(monkeypatch, tmp_path)
    import cgc_store
    cgc_store.Store().close()
    db = env["CGC_STORE_DB"]
    before = open(db, "rb").read()
    os.chmod(db, 0o400)
    try:
        r = _run_snapshot(activation, env)
    finally:
        os.chmod(db, 0o600)
    assert r.returncode == 0 and r.stdout.startswith("activation note\n")
    assert open(db, "rb").read() == before, "snapshot must not modify the database file"


@pytest.mark.parametrize("breakage", ["garbage", "newer_schema", "wrong_tables"])
def test_snapshot_survives_any_store_error_and_still_prints_the_note(tmp_path, paths, breakage):
    _s, activation = paths
    env = _store_env(tmp_path)
    os.makedirs(env["CGC_DATA_DIR"])
    db = env["CGC_STORE_DB"]
    if breakage == "garbage":
        open(db, "wb").write(b"this is not a sqlite database" * 50)
    else:
        c = sqlite3.connect(db)
        if breakage == "newer_schema":
            c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.execute("INSERT INTO meta VALUES('schema_version','99')")
        else:
            c.execute("CREATE TABLE unrelated (x)")
        c.commit()
        c.close()
    r = _run_snapshot(activation, env)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "activation note\n", "the note survives; the state line is simply omitted"


def test_snapshot_with_a_missing_note_prints_the_visible_warning_and_exits_zero(tmp_path):
    missing = str(tmp_path / "nope" / "ACTIVATION.md")
    r = _run_snapshot(missing, _store_env(tmp_path))
    assert r.returncode == 0
    assert "WARNING: chatgpt-consult ACTIVATION.md missing at " + missing in r.stdout


def test_snapshot_is_bounded_when_the_store_is_locked(tmp_path, paths, monkeypatch):
    """An exclusive writer lock must cost the hook its status line, not the session its start."""
    import time
    _s, activation = paths
    env = _seed_env(monkeypatch, tmp_path)
    import cgc_store
    cgc_store.Store().close()
    c = sqlite3.connect(env["CGC_STORE_DB"], isolation_level=None, timeout=0)
    c.execute("PRAGMA locking_mode=EXCLUSIVE")
    c.execute("BEGIN EXCLUSIVE")
    try:
        t0 = time.monotonic()
        r = _run_snapshot(activation, env)
        took = time.monotonic() - t0
    finally:
        c.execute("ROLLBACK")
        c.close()
    assert r.returncode == 0 and r.stdout.startswith("activation note\n")
    assert took < 1.5, f"snapshot took {took:.2f}s against a locked store"


def test_the_installed_hook_command_runs_and_never_fails_the_session(m, paths, tmp_path):
    """End to end through /bin/sh, exactly as Claude Code runs it — including with python3 unusable."""
    _s, activation = paths
    cmd = m.build_command(activation)
    env = {**os.environ, **_store_env(tmp_path)}
    ok = subprocess.run(cmd, shell=True, capture_output=True, text=True, env=env, timeout=10)
    assert ok.returncode == 0 and ok.stdout.startswith("activation note")
    # No python3 and no cat on PATH: every external command fails, the `echo` builtin still runs, and
    # the visible WARNING reaches the session instead of a silent empty hook or a non-zero exit.
    dead = subprocess.run(cmd, shell=True, executable="/bin/sh", capture_output=True, text=True,
                          env=dict(env, PATH="/nonexistent"), timeout=10)
    assert dead.returncode == 0 and "WARNING: chatgpt-consult ACTIVATION.md missing at" in dead.stdout
