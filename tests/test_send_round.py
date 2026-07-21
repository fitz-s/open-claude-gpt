# Tests for the send_round boundary (skill/scripts/cgc_send.py), phase 2.
# cdp_send is stubbed, so these run with no browser. The invariant under test: only a clean accept
# clears a committed click; every other post-click outcome (and any post-click crash) becomes
# possibly_accepted and is never auto-resent. Pre-click failures leave the round `ready`/`blocked`.
import importlib.util
import os

import pytest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load(name, db_path):
    os.environ["CGC_STORE_DB"] = str(db_path)
    import sys
    sys.path.insert(0, SCRIPTS)
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def env(tmp_path):
    store_mod = _load("cgc_store", tmp_path / "control.db")
    send_mod = _load("cgc_send", tmp_path / "control.db")
    s = store_mod.Store(str(tmp_path / "control.db"))
    s.create_round("REQ-1", "submit")
    s.set_state("REQ-1", store_mod.READY)
    yield store_mod, send_mod, s
    s.close()


def _run(send_mod, s, cdp_send):
    return send_mod.send_round(s, "REQ-1", cdp_send, rendered_prompt="the bytes",
                               prompt_sha256="a" * 64, daemon_instance_id="d1", browser_epoch=1)


def test_clean_accept(env):
    store_mod, send_mod, s = env

    def cdp(prompt, on_before_click):
        assert prompt == "the bytes"
        on_before_click()                       # clicks
        return {"outcome": "accepted", "conversation": "conv-9"}

    res = _run(send_mod, s, cdp)
    assert res["state"] == store_mod.ACCEPTED
    assert s.get_round("REQ-1")["state"] == store_mod.ACCEPTED
    # the exact bytes were recorded at the click
    assert s.get_round("REQ-1")["rendered_prompt"] == "the bytes"


def test_unknown_send_becomes_possibly_accepted(env):
    store_mod, send_mod, s = env

    def cdp(prompt, on_before_click):
        on_before_click()                       # clicked...
        return {"outcome": "unknown_send", "reason": "RID never observed after click"}

    res = _run(send_mod, s, cdp)
    assert res["state"] == store_mod.POSSIBLY_ACCEPTED
    r = s.recover()
    assert "REQ-1" in r["uncertain"] and "REQ-1" not in r["dispatchable"]


def test_pre_click_blocker_leaves_round_blocked_not_uncertain(env):
    store_mod, send_mod, s = env

    def cdp(prompt, on_before_click):
        # detects a login wall BEFORE clicking → never calls on_before_click
        return {"outcome": "blocker", "reason": "login"}

    res = _run(send_mod, s, cdp)
    assert res["state"] == store_mod.BLOCKED
    assert s.get_round("REQ-1")["state"] == store_mod.BLOCKED
    # nothing was sent, so it is NOT uncertain
    assert "REQ-1" not in s.recover()["uncertain"]


def test_pre_click_failure_leaves_round_ready(env):
    store_mod, send_mod, s = env

    def cdp(prompt, on_before_click):
        return {"outcome": "presend_fail", "reason": "composer not ready"}

    res = _run(send_mod, s, cdp)
    assert res["state"] == store_mod.READY
    assert s.get_round("REQ-1")["state"] == store_mod.READY  # retryable, nothing sent


def test_crash_after_click_is_uncertain(env):
    store_mod, send_mod, s = env

    def cdp(prompt, on_before_click):
        on_before_click()                       # committed sending...
        raise RuntimeError("browser died mid-send")

    res = _run(send_mod, s, cdp)
    assert res["outcome"] == "crashed" and res["state"] == store_mod.POSSIBLY_ACCEPTED
    assert "REQ-1" in s.recover()["uncertain"]


def test_crash_before_click_leaves_round_ready(env):
    store_mod, send_mod, s = env

    def cdp(prompt, on_before_click):
        raise RuntimeError("failed while selecting model, before any click")

    res = _run(send_mod, s, cdp)
    assert res["outcome"] == "presend_crash" and res["state"] == store_mod.READY
    assert s.get_round("REQ-1")["state"] == store_mod.READY
    assert "REQ-1" not in s.recover()["uncertain"]


def test_double_click_is_rejected(env):
    store_mod, send_mod, s = env

    def cdp(prompt, on_before_click):
        on_before_click()
        on_before_click()                       # a bug in the adapter — must be caught
        return {"outcome": "accepted"}

    # the second on_before_click raises inside cdp_send → send_round catches it as a post-commit crash
    res = _run(send_mod, s, cdp)
    assert res["state"] == store_mod.POSSIBLY_ACCEPTED  # committed once, then crashed → uncertain
