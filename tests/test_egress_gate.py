# S0 (diff-review): the click-owning process, cdp_consult, must ENFORCE the egress gate itself, so a
# DIRECT `cdp_consult.py submit/followup` cannot bypass the daemon's gate and exfiltrate a secret or
# a private-repo link. These pin that cdp_consult._egress_gate delegates to the same validator the
# daemon uses and fails closed. The secret / no-link paths need no network (pure regex + shape), so
# they exercise the wiring without a live gh call.
import importlib.util
import os

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skill", "scripts")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_gate_{name}", os.path.join(SCRIPTS, name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_egress_gate_refuses_a_secret_even_with_a_public_link():
    cdp = _load("cdp_consult.py")
    ok, reason = cdp._egress_gate(
        "review https://github.com/torvalds/linux\nAWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    assert ok is False and "refused" in reason.lower()


def test_egress_gate_refuses_prose_with_no_code_link():
    cdp = _load("cdp_consult.py")
    ok, reason = cdp._egress_gate("here is a description of my code, please review it")
    assert ok is False and "refused" in reason.lower()


def test_egress_gate_allows_a_followup_with_no_new_link():
    # a follow-up prompt is rule-1-exempt (thread already holds the code) and carries no secret/link.
    cdp = _load("cdp_consult.py")
    ok, _reason = cdp._egress_gate("continuing this consult; here are my local results, next question…")
    assert ok is True
