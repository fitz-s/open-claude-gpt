# App approval cards ("WebCodex Demo — Allow file materialization? [Deny] [Allow once]") stall a round
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
  n.children.forEach(function(k){k.parentElement=n});return n;}
function btn(t){return node('button',t)}
"""


def _run(setup, allow):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = (_DOM + setup + "\nvar document={querySelectorAll:function(){return ROOT.querySelectorAll()}};"
           "var r=" + CDP._approval_js(allow) + ";"
           "console.log(JSON.stringify({r:r,clicks:ROOT.querySelectorAll().map(function(b){return b.clicked})}));")
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True,
                                     check=True).stdout)


CARD = ("var deny=btn('Deny Esc'),once=btn('Allow once ↩'),menu=btn('');"
        "var card=node('div','',[node('div','WebCodex Demo'),node('div','Allow file materialization?')].concat("
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
             "var card=node('div','',[node('div','Canva'),node('div','Allow file materialization?'),node('div','',[deny,once])]);"
             "var ROOT=node('main','" + "@WebCodex Demo please review this. " * 20 + "',[card]);")
    out = _run(setup, ["WebCodex Demo"])
    assert out["r"]["app"] is None and "Canva" in out["r"]["text"]
    assert sum(out["clicks"]) == 0


def test_a_lookalike_app_name_is_not_approved():
    setup = ("var deny=btn('Deny'),once=btn('Allow once');"
             "var card=node('div','',[node('div','WebCodex Demo Clone'),node('div','Allow file materialization?'),"
             "node('div','',[deny,once])]);var ROOT=node('body','',[card]);")
    out = _run(setup, ["WebCodex Demo"])
    assert out["r"]["app"] is None and sum(out["clicks"]) == 0
    assert _run(setup, ["webcodex demo clone"])["r"]["app"] == "webcodex demo clone"
