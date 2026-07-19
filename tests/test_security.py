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
