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
