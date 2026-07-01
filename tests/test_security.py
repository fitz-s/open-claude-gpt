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
