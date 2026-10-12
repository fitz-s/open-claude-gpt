# The reader names a user turn by the FIRST `CGC_RID:<rid>` token in its text, and _paste_prompt types
# each @mention label BEFORE the prompt. So whatever we send could put a foreign marker first: a
# connected app named `CGC_RID:<other>`, or a legacy `followup --prompt-file` whose body discusses one
# while its footer is this round's pair. The other rid's waiter or retrieve would then read this round's
# answer as its own. Patching the reader again cannot close that; the invariant is enforced on the WRITE
# side: the turn we are about to send must parse, under the reader's own rule, as this round's rid —
# checked at input, at enqueue, and (the backstop) before the click, by one shared helper.
import argparse
import importlib.util
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)

import cdp_consult as c  # noqa: E402

R1, R2 = "REQ-20260924-103001-127adc", "REQ-20260924-161402-ddaec7"
CONV = "00000000-0000-4000-8000-000000000001"
CODE = "https://github.com/acme/widgets"


def _consult():
    spec = importlib.util.spec_from_file_location("consult_selfcheck", os.path.join(SCRIPTS, "consult.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _rendered(rid, body="review " + CODE):
    """What `prep` emits for a round: the head marker first, the body, the sentinel pair last."""
    return f"CGC_RID:{rid}\n\n# Title\n\n{body}\n\nBEGIN_RESPONSE:{rid}\n<full answer>\nEND_RESPONSE:{rid}\n"


def _legacy(rid, body="review " + CODE):
    """A prompt-file rendered before the marker existed: the sentinel pair is its only rid."""
    return f"# Title\n\n{body}\n\nBEGIN_RESPONSE:{rid}\n<full answer>\nEND_RESPONSE:{rid}\n"


# ---- the shared helper -------------------------------------------------------------------------

def test_our_template_followed_by_a_body_quoting_a_foreign_marker_passes():
    """The head marker comes first, so a body that quotes another round's marker cannot take over."""
    text = _rendered(R2, body=f"the old prompt began CGC_RID:{R1}\nBEGIN_RESPONSE:{R1}\nold\nEND_RESPONSE:{R1}")
    assert c._turn_rid_conflicts(R2, text, []) == []
    assert c._refuse_ambiguous_rid(R2, text, ["WebCodex Demo"], "submit") is None


def test_a_legacy_prompt_with_no_marker_and_a_matching_tail_passes():
    assert c._turn_rid_conflicts(R2, _legacy(R2), []) == []
    assert c._turn_rid_conflicts(R2, _legacy(R2), ["WebCodex Demo"]) == []


def test_a_prompt_body_quoting_a_foreign_marker_with_no_head_marker_is_refused():
    """The legacy `followup --prompt-file` shape: the body discusses R1's marker, the footer is R2's."""
    text = _legacy(R2, body=f"you wrote CGC_RID:{R1} earlier; continue")
    assert c._turn_rid_conflicts(R2, text, []) == [R1]
    msg = c._refuse_ambiguous_rid(R2, text, [], "follow-up")
    assert msg.startswith("CGC_ERROR rid_ambiguous") and R1 in msg and R2 in msg


def test_a_mention_name_carrying_a_foreign_marker_is_refused_even_with_our_head_marker():
    """The label is typed ahead of the prompt, so it is the first token whatever the template says."""
    for names in ([f"CGC_RID:{R1}"], ["WebCodex Demo", f"CGC_RID:{R1}"], [f"x CGC_RID:{R1}"]):
        assert c._turn_rid_conflicts(R2, _rendered(R2), names) == [R1], names


def test_a_prompt_the_reader_cannot_name_at_all_is_refused():
    msg = c._refuse_ambiguous_rid(R2, "no marker and no pair at all", [], "submit")
    assert msg and "no rid" in msg


def test_the_automatic_continue_prompt_passes_because_it_has_its_own_pair():
    import cgc_backend
    text = cgc_backend._continue_prompt(R2)
    assert c._turn_canonical_rid(text) == R2
    assert c._turn_rid_conflicts(R2, text, []) == []
    assert c._turn_rid_conflicts(R2, text, ["WebCodex Demo"]) == []


def test_a_refire_rewrite_still_parses_as_the_new_rid():
    """refire_round replaces the old rid everywhere, so the marker and the pair move together."""
    consult = _consult()
    old = consult.PROMPT_TEMPLATE.format(title="t", role="r", task="review " + CODE, refs_block="",
                                         context_block="", output_body="o", rid=R1)
    new = old.replace(R1, R2)
    assert R1 not in new and c._turn_canonical_rid(new) == R2
    assert c._turn_rid_conflicts(R2, new, ["WebCodex Demo"]) == []


def test_every_real_template_passes_with_and_without_a_mention():
    consult = _consult()
    for tpl in (consult.PROMPT_TEMPLATE, consult.FOLLOWUP_TEMPLATE):
        text = tpl.format(title="t", role="r", task=f"discuss CGC_RID:{R1} please", refs_block="",
                          context_block="", output_body="o", rid=R2)
        assert c._turn_rid_conflicts(R2, text, []) == []
        assert c._turn_rid_conflicts(R2, text, ["WebCodex Demo"]) == []


# ---- input: a mention name cannot carry the marker -----------------------------------------------

@pytest.mark.parametrize("name", [f"CGC_RID:{R1}", "cgc_rid:anything", "My CGC_RID app", "Cgc_Rid"])
def test_a_mention_name_containing_the_marker_is_refused_at_the_cli(name):
    consult = _consult()
    with pytest.raises(argparse.ArgumentTypeError, match="CGC_RID"):
        consult._mention_name(name)
    parser = argparse.ArgumentParser()
    parser.add_argument("--mention", action="append", type=consult._mention_name)
    with pytest.raises(SystemExit):
        parser.parse_args(["--mention", name])


def test_an_ordinary_mention_name_is_still_accepted():
    consult = _consult()
    assert consult._mention_name("webcodex") == "WebCodex Demo"
    assert consult._mention_name("Some Other App") == "Some Other App"


def test_a_configured_app_with_the_marker_is_not_offered(monkeypatch):
    consult = _consult()
    monkeypatch.setenv("CGC_APPS", f"WebCodex Demo, CGC_RID:{R1}, Other App")
    assert consult.known_apps() == ["WebCodex Demo", "Other App"]


# ---- enqueue: refused at `fire`, not later by the daemon ------------------------------------------

@pytest.fixture
def spool(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CGC_STORE_DB", str(tmp_path / "control.db"))
    spec = importlib.util.spec_from_file_location("cgc_spool_selfcheck", os.path.join(SCRIPTS, "cgc_spool.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _enq(rid, path, **over):
    d = dict(rid=rid, prompt_file=str(path), kind="submit", project_url="https://chatgpt.com/",
             conversation="auto", parent=None, model="Pro", out=None, poll=20, timeout=870, quiet=True)
    d.update(over)
    return argparse.Namespace(**d)


def test_enqueue_refuses_a_prompt_that_would_read_as_another_rid(spool, tmp_path, capsys):
    pf = tmp_path / "p.md"
    pf.write_text(_legacy(R2, body=f"you wrote CGC_RID:{R1} earlier"))
    assert spool.cmd_enqueue(_enq(R2, pf)) == 2
    assert "rid_ambiguous" in capsys.readouterr().err
    import cgc_store
    with cgc_store.Store() as s:
        assert s.get_round(R2) is None, "a refused prompt must never reach the store"


def test_enqueue_refuses_a_mention_that_would_read_as_another_rid(spool, tmp_path, capsys):
    pf = tmp_path / "p.md"
    pf.write_text(_rendered(R2))
    assert spool.cmd_enqueue(_enq(R2, pf, mentions=[f"CGC_RID:{R1}"])) == 2
    assert "rid_ambiguous" in capsys.readouterr().err


def test_enqueue_accepts_the_real_shapes(spool, tmp_path):
    pf = tmp_path / "p.md"
    pf.write_text(_rendered(R2, body=f"discussing CGC_RID:{R1}\nreview {CODE}"))
    assert spool.cmd_enqueue(_enq(R2, pf, mentions=["WebCodex Demo"])) == 0
    pf1 = tmp_path / "legacy.md"
    pf1.write_text(_legacy(R1))
    assert spool.cmd_enqueue(_enq(R1, pf1)) == 0


# ---- the driver: the backstop, before the click ---------------------------------------------------

class _Reached(Exception):
    """The fake CDP constructor was reached: every check before the browser passed."""


@pytest.fixture
def driver(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(c, "CGC_STATE_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(c, "_egress_gate", lambda p: (True, "ok"))
    monkeypatch.setattr(c, "_has_sendable_source", lambda p: True)
    monkeypatch.setattr(c, "_ensure_chrome", lambda port: None)
    monkeypatch.setattr(c, "_resolve_conv", lambda conv: CONV)

    def fake_cdp(*a, **k):
        raise _Reached()
    monkeypatch.setattr(c, "CDP", fake_cdp)
    return tmp_path


def _submit(tmp, rid, text, mention=()):
    pf = tmp / "s.md"
    pf.write_text(text)
    ns = types.SimpleNamespace(rid=rid, prompt_file=str(pf), project_url="https://chatgpt.com/", model="skip",
                               model_family="skip", allow_model_mismatch=False, reuse_tab=False, no_gate=True,
                               port=9333, mention=list(mention), watch=False, out=None)
    return c.cmd_submit(ns)


def _followup(tmp, rid, text, mention=(), pass_rid=True):
    pf = tmp / "f.md"
    pf.write_text(text)
    ns = types.SimpleNamespace(no_gate=True, conversation=CONV, prompt_file=str(pf), task=None,
                               rid=rid if pass_rid else None, model="skip", model_family="skip",
                               allow_model_mismatch=False, port=9333, mention=list(mention), watch=False, out=None)
    return c.cmd_followup(ns)


def test_submit_refuses_a_foreign_mention_before_it_touches_the_browser(driver, capsys):
    rc = _submit(driver, R2, _rendered(R2), mention=[f"CGC_RID:{R1}"])
    assert rc == c.EXIT_NOT_SENT_PRECLICK
    err = capsys.readouterr().err
    assert "rid_ambiguous" in err and R1 in err and "NOT submitting" in err


def test_followup_refuses_a_legacy_prompt_file_that_quotes_a_foreign_marker(driver, capsys):
    """`followup --prompt-file` with no --rid parses the footer's rid (R2) from the file, while the
    reader would name the turn by the quoted marker (R1): refused before the click, not sent."""
    text = _legacy(R2, body=f"you wrote CGC_RID:{R1} earlier; continue")
    rc = _followup(driver, None, text, pass_rid=False)
    assert rc == c.EXIT_NOT_SENT_PRECLICK
    err = capsys.readouterr().err
    assert "rid_ambiguous" in err and R1 in err


def test_submit_and_followup_let_a_good_prompt_through_to_the_browser(driver):
    with pytest.raises(_Reached):
        _submit(driver, R2, _rendered(R2, body=f"discussing CGC_RID:{R1}"), mention=["WebCodex Demo"])
    with pytest.raises(_Reached):
        _followup(driver, R2, _rendered(R2), mention=["WebCodex Demo"])
    with pytest.raises(_Reached):
        _followup(driver, R2, _legacy(R2))                              # legacy: no marker, matching tail


def test_the_automatic_continue_prompt_reaches_the_browser(driver):
    import cgc_backend
    with pytest.raises(_Reached):
        _followup(driver, R2, cgc_backend._continue_prompt(R2), mention=["WebCodex Demo"])
