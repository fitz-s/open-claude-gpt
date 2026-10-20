# App approval cards ("Allow ChatGPT to use WebCodex Demo? [Deny] [Allow once]") stall a round
# until answered. The waiter answers them in the consult's own tab for allowlisted apps only. These run
# the real card-finder JS in node against a mock DOM (no browser, no network).
import importlib.util
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "cdp_consult", os.path.join(ROOT, "skill", "scripts", "cdp_consult.py"))
CDP = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(CDP)

# el(text, children) builds a node; buttons are nodes with tag 'button'. querySelectorAll('button') on
# any node returns its visible-or-not button descendants; parentElement links are wired by mk().
_DOM = r"""
function node(tag,text,kids){var n={tag:tag,own:text||'',children:kids||[],
  get innerText(){return [this.own].concat(this.children.map(function(k){return k.innerText})).join('\n')},parentElement:null,clicked:0,
  hidden:false,getBoundingClientRect:function(){return this.hidden?{width:0,height:0}:{width:1,height:1}},
  click:function(){this.clicked++},
  querySelectorAll:function(){var o=[];(function w(x){x.children.forEach(function(k){if(k.tag==='button')o.push(k);w(k);})})(this);return o;}};
  n.children.forEach(function(k){k.parentElement=n});return AUTH(n);}
function btn(t){return node('button',t)}
"""


def _run(setup, allow):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = (open(os.path.join(ROOT, "tests", "authority_shim.js")).read().replace("process.env.CGC_FIXTURE_DOM", json.dumps(os.path.join(ROOT, "skill", "scripts", "fixture_dom.js"))) + _DOM + setup + "\nvar document={querySelectorAll:function(){return ROOT.querySelectorAll()}};"
           "var r=" + CDP._approval_js(allow) + ";"
           "console.log(JSON.stringify({r:r,clicks:ROOT.querySelectorAll().map(function(b){return b.clicked})}));")
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True,
                                     check=True).stdout)


CARD = ("var deny=btn('Deny Esc'),once=btn('Allow once ↩'),menu=btn('');"
        "var card=node('div','',[node('div','WebCodex Demo'),node('div','Allow ChatGPT to use WebCodex Demo?')].concat("
        "[node('div','',[deny,once,menu])]));"
        "var ROOT=node('body','',[node('div','',[btn('Send')]),card]);")


def test_allowlisted_card_gets_allow_once_clicked():
    out = _run(CARD, ["WebCodex Demo"])
    assert out["r"]["app"] == "WebCodex Demo"
    assert out["clicks"] == [0, 0, 1, 0], "exactly the Allow-once button, nothing else"


def test_unlisted_app_is_reported_not_clicked():
    out = _run(CARD, ["Canva"])
    assert out["r"]["app"] is None and "WebCodex Demo" in out["r"]["text"]
    assert sum(out["clicks"]) == 0


def test_no_card_means_null():
    out = _run("var ROOT=node('body','',[btn('Allow once')]);", ["WebCodex Demo"])
    assert out["r"] is None, "an Allow button with no Deny beside it is not a card"
    out = _run(CARD.replace("var ROOT", "once.hidden=true;var ROOT"), ["WebCodex Demo"])
    assert out["r"] is None, "invisible buttons do not count"


def test_off_disables(monkeypatch):
    monkeypatch.setattr(CDP, "CGC_AUTO_APPROVE", "off")
    assert CDP._approve_apps() == []
    monkeypatch.setattr(CDP, "CGC_AUTO_APPROVE", " @WebCodex Demo, Canva ")
    assert CDP._approve_apps() == ["WebCodex Demo", "Canva"]


def test_foreign_card_cannot_borrow_the_app_name_from_the_conversation():
    setup = ("var deny=btn('Deny'),once=btn('Allow once');"
             "var card=node('div','',[node('div','Canva'),node('div','Allow ChatGPT to use Canva?'),node('div','',[deny,once])]);"
             "var ROOT=node('main','" + "@WebCodex Demo please review this. " * 20 + "',[card]);")
    out = _run(setup, ["WebCodex Demo"])
    assert out["r"]["app"] is None and "Canva" in out["r"]["text"]
    assert sum(out["clicks"]) == 0


def test_a_lookalike_app_name_is_not_approved():
    setup = ("var deny=btn('Deny'),once=btn('Allow once');"
             "var card=node('div','',[node('div','WebCodex Demo Clone'),node('div','Allow ChatGPT to use WebCodex Demo Clone?'),"
             "node('div','',[deny,once])]);var ROOT=node('body','',[card]);")
    out = _run(setup, ["WebCodex Demo"])
    assert out["r"]["app"] is None and sum(out["clicks"]) == 0
    assert _run(setup, ["webcodex demo clone"])["r"]["app"] == "webcodex demo clone"



def test_two_stacked_cards_never_lend_one_card_the_other_apps_name():
    """Review S4: a Canva card above a WebCodex Demo card under one small parent. Canva's own button
    must not be clicked just because the shared parent mentions WebCodex Demo."""
    setup = ("var d1=btn('Deny'),o1=btn('Allow once'),d2=btn('Deny'),o2=btn('Allow once');"
             "var c1=node('div','',[node('div','Canva'),node('div','Allow ChatGPT to use Canva?'),node('div','',[d1,o1])]);"
             "var c2=node('div','',[node('div','WebCodex Demo'),node('div','Allow ChatGPT to use WebCodex Demo?'),node('div','',[d2,o2])]);"
             "var ROOT=node('body','',[node('div','',[c1,c2])]);")
    out = _run(setup, ["WebCodex Demo"])
    assert out["clicks"][1] == 0, "Canva's Allow once must never be clicked"


def test_no_contract_edit_can_make_an_approval_click_hit_send(tmp_path):
    """Review 2026-10-14: allow labels in the editable contract set to ['send'] made the approval
    click hit the composer's send button. The labels are code now, and the approval search skips
    anything that is not a card WIDGET (no form, editor, send or stop) — see test_click_authority."""
    assert not any(k.startswith("approval_") for k in CDP.UI)
    js = CDP._approval_js(["WebCodex Demo"])
    assert "'send'" not in js.lower() and "__cgcMayClick(ok[i],'approve',card)" in js


def test_a_read_only_approval_check_never_clicks(monkeypatch):
    """Recovery waits read the card but never grant access (the backend's approve=False)."""
    orig = CDP._approval_js
    monkeypatch.setattr(CDP, "_approval_js", lambda allow, click=True: orig(allow, False))
    out = _run(CARD, ["WebCodex Demo"])
    assert out["r"] is not None and sum(out["clicks"]) == 0
