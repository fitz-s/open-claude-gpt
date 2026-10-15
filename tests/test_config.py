# Tests for the persistent per-user config store (cgc_config.py) + `cgc set-project`.
import importlib.util
import os
import sys

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load():
    spec = importlib.util.spec_from_file_location("cgc_config", os.path.join(SCRIPTS, "cgc_config.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    p = tmp_path / "cgc" / "config"
    monkeypatch.setenv("CGC_CONFIG", str(p))
    monkeypatch.delenv("CGC_PROJECT_URL", raising=False)
    return _load(), str(p)


def test_config_path_precedence(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setenv("CGC_CONFIG", "/explicit/path")
    assert m.config_path() == "/explicit/path"
    monkeypatch.delenv("CGC_CONFIG")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert m.config_path() == str(tmp_path / "cgc" / "config")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert m.config_path().endswith("/.config/cgc/config")


def test_set_then_load_roundtrip(cfg, monkeypatch):
    m, p = cfg
    m.set_key("CGC_PROJECT_URL", "https://chatgpt.com/g/g-p-abc-x/project")
    assert os.path.exists(p)
    monkeypatch.delenv("CGC_PROJECT_URL", raising=False)
    m.load_config()
    assert os.environ["CGC_PROJECT_URL"] == "https://chatgpt.com/g/g-p-abc-x/project"


def test_env_wins_over_file(cfg, monkeypatch):
    m, _ = cfg
    m.set_key("CGC_PROJECT_URL", "https://file.example/proj")
    monkeypatch.setenv("CGC_PROJECT_URL", "https://env.example/win")
    m.load_config()  # setdefault must NOT overwrite a real env var
    assert os.environ["CGC_PROJECT_URL"] == "https://env.example/win"


def test_set_replaces_existing_key(cfg):
    m, p = cfg
    m.set_key("CGC_PROJECT_URL", "https://one/")
    m.set_key("CGC_PROJECT_URL", "https://two/")
    body = open(p, encoding="utf-8").read()
    assert body.count("CGC_PROJECT_URL=") == 1
    assert "https://two/" in body and "https://one/" not in body


def test_parse_ignores_comments_blanks_and_non_cgc(cfg):
    m, p = cfg
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write(
        "# a comment\n\nCGC_MODEL=Pro\nNOTCGC=nope\nCGC_PORT=9400\n")
    pairs = dict(m._parse(open(p, encoding="utf-8").read()))
    assert pairs == {"CGC_MODEL": "Pro", "CGC_PORT": "9400"}
    assert "NOTCGC" not in pairs


def test_set_rejects_non_cgc_key(cfg):
    m, _ = cfg
    with pytest.raises(ValueError):
        m.set_key("PATH", "/evil")


def test_missing_file_is_silent(tmp_path, monkeypatch):
    m = _load()
    monkeypatch.setenv("CGC_CONFIG", str(tmp_path / "does_not_exist"))
    monkeypatch.delenv("CGC_PROJECT_URL", raising=False)
    m.load_config()  # must not raise
    assert "CGC_PROJECT_URL" not in os.environ


# ---- validation: known keys, no newlines, typed values ------------------------------------------

def test_set_rejects_a_value_that_would_inject_a_second_line(cfg):
    m, p = cfg
    for bad in ("https://x/\nCGC_GATE_PRIVATE_REPOS=*", "a\rb", "a\0b"):
        with pytest.raises(ValueError, match="newline|carriage|NUL"):
            m.set_key("CGC_PROJECT_URL", bad)
    assert not os.path.exists(p), "a refused write must not create or touch the file"


def test_set_rejects_a_malformed_key_and_a_value_the_scripts_cannot_parse(cfg):
    m, _ = cfg
    for key in ("CGC_", "CGC_lower", "CGC_A B", "cgc_port", "CGC_X\n"):
        with pytest.raises(ValueError):
            m.set_key(key, "1")
    for key, bad in (("CGC_PORT", "ninety"), ("CGC_PORT", "70000"), ("CGC_PORT", "0"),
                     ("CGC_WEEKLY_BUDGET", "lots"), ("CGC_AUTO_MODEL", "maybe")):
        with pytest.raises(ValueError, match=key):
            m.set_key(key, bad)
    m.set_key("CGC_PORT", "9400")
    m.set_key("CGC_AUTO_MODEL", "off")
    m.set_key("CGC_WEEKLY_BUDGET", "50")


def test_unknown_cgc_key_is_stored_with_a_warning(cfg, capsys):
    m, p = cfg
    m.set_key("CGC_PROJCT_URL", "https://typo.example/")          # the typo is the point
    assert "CGC_PROJCT_URL=https://typo.example/" in open(p, encoding="utf-8").read()
    assert "not a setting" in capsys.readouterr().err
    m.set_key("CGC_PROJECT_URL", "https://ok.example/")
    assert capsys.readouterr().err == ""


def test_show_warns_about_unknown_keys_on_stderr(cfg, capsys):
    m, p = cfg
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write("CGC_MODEL=Pro\nCGC_TYPO=1\n")
    assert m._main(["show"]) == 0
    out, err = capsys.readouterr()
    assert "CGC_MODEL=Pro" in out and "CGC_TYPO=1" in out
    assert "CGC_TYPO" in err and "CGC_MODEL" not in err


def test_load_skips_a_stored_value_that_would_crash_the_scripts(cfg, monkeypatch, capsys):
    m, p = cfg
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w", encoding="utf-8").write("CGC_PORT=ninety\nCGC_MODEL=High\n")   # hand-edited
    monkeypatch.delenv("CGC_PORT", raising=False)
    monkeypatch.delenv("CGC_MODEL", raising=False)
    m.load_config()
    assert "CGC_PORT" not in os.environ and os.environ["CGC_MODEL"] == "High"
    assert "ignoring CGC_PORT" in capsys.readouterr().err
    monkeypatch.delenv("CGC_MODEL", raising=False)


def test_every_cgc_key_the_scripts_read_is_known():
    """A setting the code starts reading must be added to cgc_config.KNOWN, or `set` calls it a typo."""
    import ast
    import glob
    m = _load()
    read = set()
    for f in glob.glob(os.path.join(SCRIPTS, "*.py")):
        for n in ast.walk(ast.parse(open(f, encoding="utf-8").read())):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ("get", "setdefault", "pop", "getenv") and n.args
                    and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
                    and n.args[0].value.startswith("CGC_")):
                read.add(n.args[0].value)
            elif (isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant)
                  and isinstance(n.slice.value, str) and n.slice.value.startswith("CGC_")):
                read.add(n.slice.value)
    read.add("CGC_MODEL_SLUG")            # read through _ATTR_GATE_ENV
    # runtime plumbing the daemon passes to its own children, and the config file's own location
    read -= {"CGC_DAEMON_INSTANCE", "CGC_CONFIG"}
    assert read <= set(m.KNOWN), sorted(read - set(m.KNOWN))
    assert set(m.KNOWN) <= read, sorted(set(m.KNOWN) - read)


# ---- concurrent writers --------------------------------------------------------------------------

_WRITER = '''
import sys
sys.path.insert(0, sys.argv[1])
import cgc_config
tag, n = sys.argv[2], int(sys.argv[3])
for i in range(n):
    cgc_config.set_key(f"CGC_{tag}_{i}", f"v{i}")
'''


def test_two_processes_updating_different_keys_both_land(tmp_path):
    """The old read-modify-write used one fixed `<config>.tmp`: two writers read the same file, each
    wrote its own temp over the other's, and one update vanished (or the replace hit a missing temp)."""
    import subprocess
    cfgp = tmp_path / "cgc" / "config"
    env = dict(os.environ, CGC_CONFIG=str(cfgp))
    n = 40
    tags = ("AAA", "BBB", "CCC")
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, SCRIPTS, tag, str(n)], env=env,
                              stderr=subprocess.PIPE, text=True) for tag in tags]
    errs = [p.communicate()[1] for p in procs]
    assert [p.returncode for p in procs] == [0, 0, 0], errs
    stored = dict(line.split("=", 1) for line in cfgp.read_text().splitlines())
    want = {f"CGC_{tag}_{i}": f"v{i}" for tag in tags for i in range(n)}
    assert stored == want, f"lost updates: {sorted(set(want) - set(stored))[:5]}"
    assert [p.name for p in cfgp.parent.iterdir() if p.name.startswith(".config.")] == [], "temp file left behind"


def test_two_processes_updating_the_same_key_leave_one_whole_line(tmp_path):
    import subprocess
    cfgp = tmp_path / "cgc" / "config"
    env = dict(os.environ, CGC_CONFIG=str(cfgp))
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import cgc_config\n"
            "for i in range(60): cgc_config.set_key('CGC_PROJECT_URL', sys.argv[2] + str(i))\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, SCRIPTS, tag], env=env) for tag in ("x", "y")]
    assert [p.wait() for p in procs] == [0, 0]
    lines = cfgp.read_text().splitlines()
    assert len(lines) == 1 and lines[0].startswith("CGC_PROJECT_URL=") and lines[0][-2:] in ("59", "9")


def test_write_goes_through_a_unique_temp_and_keeps_the_file_mode(cfg):
    m, p = cfg
    m.set_key("CGC_MODEL", "Pro")
    os.chmod(p, 0o640)
    m.set_key("CGC_MODEL", "High")
    assert oct(os.stat(p).st_mode & 0o777) == "0o640"
    src = open(os.path.join(SCRIPTS, "cgc_config.py"), encoding="utf-8").read()
    assert 'p + ".tmp"' not in src and "mkstemp" in src and "os.replace" in src and "flock" in src
