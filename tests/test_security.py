#!/usr/bin/env python3
# Created: 2026-07-01
# Last audited: 2026-07-01
# Authority basis: open-claude-gpt OSS packaging — CDP loopback-bind hardening.
"""
Offline unit test for the loopback-vs-routable address classifier used by
cgc_doctor's "loopback bind" check. No browser, no network.
Run: python3 -m pytest tests/test_security.py -q
(also runnable plain: python3 tests/test_security.py)
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")


def _load_doctor():
    spec = importlib.util.spec_from_file_location("_cgc_doctor", os.path.join(SCRIPTS, "cgc_doctor.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_loopback_addresses_classified_true():
    d = _load_doctor()
    for addr in ("127.0.0.1:9333", "127.0.0.1", "[::1]:9333", "::1", "localhost:9333", "localhost"):
        assert d.is_loopback_addr(addr) is True, addr


def test_routable_addresses_classified_false():
    d = _load_doctor()
    for addr in ("0.0.0.0:9333", "0.0.0.0", "::", "*:9333", "*", "192.168.1.5:9333", "10.0.0.1"):
        assert d.is_loopback_addr(addr) is False, addr


def test_empty_and_whitespace_not_loopback():
    d = _load_doctor()
    assert d.is_loopback_addr("") is False
    assert d.is_loopback_addr("   ") is False


if __name__ == "__main__":
    test_loopback_addresses_classified_true()
    test_routable_addresses_classified_false()
    test_empty_and_whitespace_not_loopback()
    print("OK")


# ---- the model guard: verdict and reported model must never contradict ----
#
# Regression: submit printed {"model": "Chat", "modelConfirmed": true} for a --model Pro consult.
# Confirmation scanned every switcher (right — ChatGPT splits model and reasoning effort across two
# menus), but the REPORTED model was always labels[0], and labels[0] is the composer's MODE toggle
# (Chat / Agent / …), not the model pill. Verified live: the project page shows ['Chat', 'Pro'].
# So a correctly pinned Pro run recorded itself as running on "Chat".

import importlib.util as _ilu
import os as _os

_CDP_PATH = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                          "skill", "scripts", "cdp_consult.py")
_spec = _ilu.spec_from_file_location("cdp_consult", _CDP_PATH)
_CDP = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_CDP)


def test_mode_toggle_first_does_not_become_the_reported_model():
    """The exact live layout: mode toggle 'Chat' sorts before the 'Pro' tier switcher."""
    ok, shown = _CDP._model_verdict(["Chat", "Pro"], "Pro")
    assert ok is True
    assert shown == "Pro", "must report the switcher that carries the tier, not the mode toggle"


def test_two_menu_pro_extended_confirms():
    ok, shown = _CDP._model_verdict(["GPT-5.6", "Pro Extended"], "Pro")
    assert ok is True and shown == "Pro Extended"


def test_no_pro_switcher_anywhere_fails_closed():
    """The guard that actually matters: nothing offers a Pro tier, so do not send."""
    ok, shown = _CDP._model_verdict(["Chat", "Medium"], "Pro")
    assert ok is False and shown == "Chat"


def test_non_pro_target_requires_an_exact_first_line_match():
    """Only a Pro target gets family matching, so 'High' never satisfies 'Extra High'."""
    assert _CDP._model_verdict(["High"], "High")[0] is True
    assert _CDP._model_verdict(["Extra High"], "High")[0] is False
    assert _CDP._model_verdict(["Medium"], "High")[0] is False


def test_no_switchers_at_all_is_not_a_confirmation():
    ok, shown = _CDP._model_verdict([], "Pro")
    assert ok is False and shown is None


# ---- UI-contract detection: a DOM change must fail in seconds, not in an hour ----
#
# Two complete 25-minute consults were lost silently because ChatGPT moved its turn markup and this
# tool kept polling a selector that matched nothing. Counting user turns proved only that the page
# grew a message; it could not tell "my prompt landed" from "something appeared", and said nothing
# about whether the reply was still readable.

class _FakeClient:
    """Replays a scripted sequence of _contract_js observations."""

    def __init__(self, frames):
        self.frames = list(frames)

    def eval(self, _expr):
        import json as _j
        return _j.dumps(self.frames[0] if len(self.frames) == 1 else self.frames.pop(0))


def _fam(name, rid_landed, assistants=0):
    return {"adapter": name, "ridLanded": rid_landed, "users": 1, "assistants": assistants}


def test_contract_picks_the_adapter_that_carries_this_rid(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    c = _FakeClient([{"families": [_fam("data-turn-v1", True, assistants=1),
                                   _fam("legacy-author-role-v1", False)], "generating": False}])
    verdict, adapter, _ = _CDP._await_contract(c, "REQ-20260719-000000-abcdef")
    assert verdict == "ok" and adapter == "data-turn-v1"


def test_rid_never_lands_is_unknown_send_and_must_not_auto_resend(monkeypatch):
    """Not seeing the rid does NOT prove nothing was sent. Resending could duplicate a 25-minute
    consult, so this is the human-inspection outcome, not the retry outcome."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    c = _FakeClient([{"families": [_fam("data-turn-v1", False),
                                   _fam("legacy-author-role-v1", False)], "generating": False}])
    verdict, adapter, _ = _CDP._await_contract(c, "REQ-20260719-000000-abcdef")
    assert verdict == "unknown_send" and adapter is None


def test_rid_landed_but_no_assistant_signal_is_selector_drift(monkeypatch):
    """The exact failure that cost two rounds: the send worked, the READER is broken. That is a tool
    defect — distinct from 'the model produced no answer' — and it must be named within seconds."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    monkeypatch.setattr(_CDP, "_GENERATION_SIGNAL_S", 0.01)
    c = _FakeClient([{"families": [_fam("data-turn-v1", True, assistants=0)], "generating": False}])
    verdict, adapter, _ = _CDP._await_contract(c, "REQ-20260719-000000-abcdef")
    assert verdict == "selector_drift" and adapter == "data-turn-v1"


def test_generating_indicator_alone_satisfies_the_contract(monkeypatch):
    """A Pro round streams reasoning stubs before any assistant node settles, so the busy indicator
    must count as a live assistant side or every Pro consult would be called drift."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    c = _FakeClient([{"families": [_fam("data-turn-v1", True, assistants=0)], "generating": True}])
    verdict, _, _ = _CDP._await_contract(c, "REQ-20260719-000000-abcdef")
    assert verdict == "ok"


def test_both_schemas_are_kept_separate_never_unioned():
    """Unioning the two selector families double-counts wrapper and content nodes when both
    attributes exist at different DOM levels, and interleaves their order."""
    names = [n for n, _u, _a in _CDP._ADAPTERS]
    assert names == ["data-turn-v1", "legacy-author-role-v1"]
    for _n, u, a in _CDP._ADAPTERS:
        assert "," not in u and "," not in a, "each adapter must query ONE schema"


# ---- the agent path must make NO network call, provably ----
#
# The architecture's central claim is that the agent touches only local files and the daemon does
# everything external. `consult.py deliver` quietly broke it: every `gh` call in it reaches
# github.com, and Claude Code's auto-mode classifier correctly denied the command. The answer to a
# safety classifier blocking you is never "tell users to disable the classifier" — it is to stop
# making the call. This test makes the claim executable instead of aspirational.

import json
import subprocess as _sp

import pytest
import sys as _sys
import tempfile as _tf

_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_CONSULT = _os.path.join(_REPO, "skill", "scripts", "consult.py")
_SPOOL = _os.path.join(_REPO, "skill", "scripts", "cgc_spool.py")


def _no_network_env(tmp):
    """A PATH with no `gh` on it at all, so any attempt to reach GitHub fails loudly."""
    env = dict(_os.environ)
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    env["CGC_STATE_DIR"] = str(tmp)
    env["CGC_SPOOL_DIR"] = str(_os.path.join(str(tmp), "spool"))
    return env


def test_deliver_produces_refs_with_gh_entirely_absent(tmp_path):
    env = _no_network_env(tmp_path)
    head = _sp.run(["git", "-C", _REPO, "rev-parse", "HEAD"],
                   capture_output=True, text=True).stdout.strip()
    r = _sp.run([_sys.executable, _CONSULT, "deliver", "--repo", "acme/widgets", "--ref", head],
                capture_output=True, text=True, env=env, cwd=_REPO, timeout=60)
    assert r.returncode == 0, r.stderr
    state = json.loads(r.stdout)
    assert state["refs_file"] and _os.path.exists(state["refs_file"])
    assert state["visibility"] == "deferred", "provenance is deferred to the gate, not skipped"
    body = open(state["refs_file"], encoding="utf-8").read()
    assert "github.com/acme/widgets" in body


def test_the_whole_agent_path_runs_with_gh_entirely_absent(tmp_path):
    """deliver -> prep -> enqueue, end to end, with no way to reach GitHub. If any of these ever
    needs the network again, this fails rather than surfacing later as a classifier denial."""
    env = _no_network_env(tmp_path)
    head = _sp.run(["git", "-C", _REPO, "rev-parse", "HEAD"],
                   capture_output=True, text=True).stdout.strip()
    d = json.loads(_sp.run([_sys.executable, _CONSULT, "deliver", "--repo", "acme/widgets",
                            "--ref", head],
                           capture_output=True, text=True, env=env, cwd=_REPO, timeout=60).stdout)
    p = _sp.run([_sys.executable, _CONSULT, "prep", "--refs-file", d["refs_file"],
                 "--title", "t", "--task", "probe"],
                capture_output=True, text=True, env=env, cwd=_REPO, timeout=60)
    assert p.returncode == 0, p.stderr
    st = json.loads(p.stdout)
    e = _sp.run([_sys.executable, _SPOOL, "enqueue", "--rid", st["request_id"],
                 "--prompt-file", st["prompt_file"]],
                capture_output=True, text=True, env=env, cwd=_REPO, timeout=60)
    assert e.returncode == 0, e.stderr
    assert json.loads(e.stdout)["queued"] is True


def test_deliver_defaults_to_offline_and_only_verify_opts_in():
    """The default is what makes the invariant hold by construction. A flag the agent must remember
    to pass is a footgun, so the network path is the one that has to be asked for."""
    src = open(_CONSULT, encoding="utf-8").read()
    assert "_GH_ALLOWED = False" in src
    assert '_GH_ALLOWED = bool(getattr(a, "verify", False))' in src


# ---- a failure must name itself, or a human gets sent to fix the wrong thing ----
#
# A consult died with a bare `composer_not_ready`. The page had simply not finished loading, but the
# only guidance available said "tab not on ChatGPT or login lapsed", so the agent asked its human to
# go re-log-in — while the session was perfectly valid. An error that cannot distinguish its own
# causes turns into wasted human time.

def test_slow_page_is_not_reported_as_a_login_problem():
    code, msg = _CDP._composer_failure(
        {"onChatGPT": True, "loginWall": False, "captcha": False,
         "readyState": "loading", "path": "/g/g-p-x/project"})
    assert code == 1, "a slow page is ours to retry, not a human action"
    assert "Login is NOT the problem" in msg
    assert "readyState" in msg and "loading" in msg


def test_a_real_login_wall_is_reported_as_human_action():
    code, msg = _CDP._composer_failure({"onChatGPT": True, "loginWall": True})
    assert code == 3 and "log into ChatGPT" in msg
    assert "never types credentials" in msg


def test_captcha_is_human_action_too():
    code, msg = _CDP._composer_failure({"onChatGPT": True, "captcha": True})
    assert code == 3 and "challenge" in msg


def test_wrong_page_is_named_as_such():
    code, msg = _CDP._composer_failure({"onChatGPT": False, "path": "/somewhere/else"})
    assert code == 1 and "not on chatgpt.com" in msg


def test_composer_wait_stops_early_on_a_login_wall(monkeypatch):
    """No point burning 60s of hydration wait on a page that is showing a login form."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    calls = []

    class _C:
        def eval(self, _e):
            calls.append(1)
            return json.dumps({"composer": False, "onChatGPT": True, "loginWall": True})

    ready, st = _CDP._await_composer(_C(), seconds=60)
    assert ready is False and st["loginWall"] is True
    assert len(calls) == 1, "must bail on the first observation, not poll for a minute"


# ---- loopback must never traverse an HTTP proxy ----
#
# ROOT CAUSE of the failure that killed consults for a whole day. These scripts reach the debug
# browser over loopback HTTP, and urllib honours http_proxy. With a local proxy running and no
# 127.0.0.1 exemption in no_proxy, /json returned the proxy's HTML, json.load threw, and the daemon
# concluded Chrome could not open a tab — so it restarted a healthy browser in a loop, destroying
# whatever consults were in flight.

@pytest.mark.parametrize("script", ["cdp_consult.py", "cgc_daemon.py"])
def test_scripts_install_a_proxy_free_opener(script):
    src = open(_os.path.join(_REPO, "skill", "scripts", script), encoding="utf-8").read()
    assert "ProxyHandler({})" in src and "install_opener" in src, (
        f"{script} reaches the CDP endpoint over loopback; without a proxy-free opener an "
        f"http_proxy in the environment silently hijacks those calls")
    assert src.index("install_opener") < src.index("def "), (
        "the opener must be installed at import time, before any urlopen can run")


def test_a_proxy_free_opener_actually_drops_proxy_handling(monkeypatch):
    """Not just that the line exists — that it does the thing. Passing an EMPTY ProxyHandler makes
    build_opener drop proxy handling altogether, while the default opener carries the environment's
    proxies and would send a loopback request to them."""
    import urllib.request
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:59999")
    monkeypatch.setenv("no_proxy", "example.com")     # deliberately NOT exempting 127.0.0.1

    default = urllib.request.build_opener()
    carried = [h.proxies for h in default.handlers if type(h).__name__ == "ProxyHandler"]
    assert carried and carried[0].get("http") == "http://127.0.0.1:59999", (
        "precondition: without the fix, urllib would route loopback through this proxy")

    ours = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    assert not [h for h in ours.handlers if type(h).__name__ == "ProxyHandler"], (
        "the proxy-free opener must carry no proxy handling at all")


# ---- an uncertain send must not lose its address --------------------------------------------

def test_url_transition_proves_a_send_only_from_no_thread_into_one():
    """The one witness that survives when the DOM turn schema moves. A thread is minted BY a send,
    so ""->/c/<id> is proof; a reused tab already in its thread proves nothing (sending does not
    change the id), and a jump between two threads is navigation, not our click."""
    assert _CDP._send_proven_by_url("", "conv-new") is True
    assert _CDP._send_proven_by_url("", "") is False
    assert _CDP._send_proven_by_url("conv-old", "conv-old") is False
    assert _CDP._send_proven_by_url("conv-old", "conv-new") is False


def test_poll_conversation_waits_out_the_post_send_url_transition(monkeypatch):
    """The URL lands a beat after the message; reading it once returns '' and loses the address."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)

    class C:
        def __init__(self): self.n = 0
        def conversation_id(self):
            self.n += 1
            return "conv-9" if self.n >= 3 else ""

    assert _CDP._poll_conversation(C(), seconds=5) == "conv-9"


def test_every_post_click_submit_envelope_carries_the_conversation():
    """Recovery is addressed BY conversation. An unconfirmed send that reports none is unrecoverable
    by construction — the field failure where a still-generating answer became a manual paste."""
    import inspect
    src = inspect.getsource(_CDP.cmd_submit)
    head, _, tail = src.partition('"reason": "unknown_send"')
    assert tail, "the unknown_send envelope must still exist"
    assert '"conversation_id": conv' in tail.split("return 3")[0], \
        "unknown_send must report where the tab is"
    drift = src.partition('"reason": "selector_drift"')[2].split("return 1")[0]
    assert '"conversation_id": conv' in drift, "selector_drift must report its conversation too"
