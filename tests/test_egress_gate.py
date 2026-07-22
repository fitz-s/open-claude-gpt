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


# --- URL classification fail-open (diff-review S0): a recognized code URL that does not classify to
# a verifiable owner/repo must FAIL CLOSED, never be silently skipped from visibility verification. ---

def test_classify_decodes_single_percent_encoding_to_a_clean_slug():
    """github.com/%66itz-s/repo is URI-equivalent to github.com/fitz-s/repo, so it must decode to the
    slug 'fitz-s/repo' and be VERIFIED (the old code saw the raw %66 and skipped verification)."""
    spool = _load("cgc_spool.py")
    slugs, refuse = spool._classify_code_urls("see https://github.com/%66itz-s/repo for the code")
    assert refuse is None and "fitz-s/repo" in slugs


def test_validate_refuses_double_encoded_owner_without_network():
    """A double-encoded owner (%2566itz-s -> %66itz-s after one decode) is ambiguous and cannot be a
    real repo segment — refuse before any visibility check (no gh call needed)."""
    spool = _load("cgc_spool.py")
    ok, reason = spool.validate_prompt("review https://github.com/%2566itz-s/private-repo now")
    assert ok is False and "refused" in reason.lower()


def test_validate_refuses_a_code_url_with_no_owner_repo():
    spool = _load("cgc_spool.py")
    ok, reason = spool.validate_prompt("see https://raw.githubusercontent.com/onlyowner and review")
    assert ok is False and "refused" in reason.lower()
