# @mention of a ChatGPT app/plugin (e.g. "WebCodex Demo"): an agent asks for it with one flag,
# `fire --mention NAME`, and the rest is mechanical — spec -> daemon argv -> composer popup pick
# before the paste. These pin each hop offline (fake CDP, no browser).
import argparse
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CDP = _load("cdp_consult")


class _FakeComposer:
    """Answers the JS the paste path evaluates. `offers` = app labels the @ popup shows."""

    def __init__(self, offers):
        self.offers = [o.lower() for o in offers]
        self.html = ""
        self.text = ""

    def eval(self, js, timeout=None):
        if "readyState" in js:
            return True
        if "selectAll" in js:
            self.html = self.text = ""
            return 0
        if "var want=" in js:
            want = json.loads(js.split("var want=", 1)[1].split(";", 1)[0])
            if want in self.offers:
                self.html = self.html.replace("@" + want, "") + f'<span data-mention>{want}</span>'
                self.text = self.text.lower().replace("@" + want, want)
                return want
            return None
        if "execCommand('insertText'" in js:
            chunk = json.loads(js.split("execCommand('insertText',false,", 1)[1].rsplit(");", 1)[0])
            self.text += chunk
            self.html += chunk.lower()
            return self.html if "return d.innerHTML" in js else len(self.text)
        if "return d?d.innerHTML" in js:
            return self.html
        if "return d?(d.innerText" in js:
            return self.text
        raise AssertionError("unexpected js: " + js[:80])


def test_mention_is_picked_before_the_prompt(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    c = _FakeComposer(["WebCodex Demo"])
    CDP._paste_prompt(c, "review this", ["WebCodex Demo"])
    assert "data-mention" in c.html
    assert c.text.startswith("webcodex demo") and c.text.endswith("review this")


def test_unknown_mention_fails_closed_before_the_click(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    monkeypatch.setattr(CDP, "_MENTION_WAIT_S", 0.05)
    c = _FakeComposer([])
    with pytest.raises(CDP._MentionFailure) as e:
        CDP._paste_prompt(c, "review this", ["Nope App"])
    assert "mention_not_found" in str(e.value)
    assert isinstance(e.value, CDP._PreClickFailure), "never an uncertain post-click failure"
    assert c.text == "", "no half-typed @name left in the composer"


def test_no_mention_is_the_old_paste():
    c = _FakeComposer([])
    CDP._paste_prompt(c, "plain")
    assert c.text == "plain"


def test_fire_freezes_mentions_into_the_request(tmp_path, monkeypatch):
    consult = _load("consult")
    monkeypatch.setattr(consult, "CGC_STATE_DIR", str(tmp_path))
    import cgc_spool as spool
    seen = {}
    monkeypatch.setattr(spool, "cmd_enqueue", lambda a: seen.update(vars(a)) or 0)
    ns = argparse.Namespace(
        repo_dir=ROOT, repo=None, pr=None, ref=None, compare=None, pulls=False, blobs=False,
        files=None, issues=None, base=None, verify=False, allow_nonpublic=False, backend="cdp",
        window_template=None, task="probe", title="t", role=None, followup=False, no_code=True,
        context_file=None, refs_file=None, output_file=None, output_replace=False,
        target_chars=32000, hard_chars=40000, expect_minutes=25, model="Pro", out=None,
        project_url="https://chatgpt.com/", conversation=None, mention=["WebCodex Demo"])
    assert isinstance(consult.cmd_fire(ns), dict)
    assert seen["mentions"] == ["WebCodex Demo"]


def test_mention_flag_strips_the_at_sign():
    consult = _load("consult")
    assert consult._mention_name(" @WebCodex  Demo ") == "WebCodex Demo"
    with pytest.raises(argparse.ArgumentTypeError):
        consult._mention_name("@")


def test_mentions_route_the_request_but_leave_old_fingerprints_alone():
    backend = _load("cgc_backend")
    base = dict(kind="submit", logical_sha="x", project_url="u", model="Pro", model_family="Latest",
                parent=None, conversation=None)
    plain = backend._request_fingerprint(argparse.Namespace(**base), "p")
    empty = backend._request_fingerprint(argparse.Namespace(**base, mentions=[]), "p")
    tagged = backend._request_fingerprint(argparse.Namespace(**base, mentions=["WebCodex Demo"]), "p")
    assert plain == empty, "a request with no mention keeps its pre-existing fingerprint"
    assert tagged != plain, "same key + a different app is a different request"
    assert "mention_not_found" in backend._NOT_SENT_BLOCK, "a missing app needs a human, not a retry"


def test_daemon_passes_mentions_on_the_argv(monkeypatch):
    daemon = _load("cgc_daemon")
    got = {}

    class _R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(daemon.subprocess, "run", lambda cmd, **kw: got.setdefault("cmd", cmd) and _R())
    daemon._make_run_cdp()("submit", rid="REQ-20260924-000000-abcdef", prompt="p",
                           project_url="https://chatgpt.com/", model="Pro", model_family="Latest",
                           mentions=["WebCodex Demo"])
    i = got["cmd"].index("--mention")
    assert got["cmd"][i + 1] == "WebCodex Demo"
