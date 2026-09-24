# 2026-09-24: ChatGPT dropped both turn attributes the reader knew (data-turn, data-message-author-role).
# Each message is now a unit tagged data-chatgpt-search-unit-key="<turn>:<n>:user|assistant", and a
# finished answer's unit also holds attachment cards ("x.py Code Open file") AFTER the markdown body.
# Live effect before this fix: the send landed and the answer finished, but every read saw 0 turns —
# followup reported ok:false, the waiter reported rid_absent. These run the real reader JS in node
# against a mock of that exact DOM shape.
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

R1, R2 = "REQ-20260924-103001-127adc", "REQ-20260924-161402-ddaec7"

# A tiny DOM: elements with attributes, children, text; querySelectorAll supports the attribute
# selectors the reader uses ([a], [a="v"], [a$="v"], comma lists, descendant-free).
_DOM = r"""
function E(tag,attrs,kids,text){var e={tagName:tag,nodeType:1,attrs:attrs||{},childNodes:[],children:[],parentElement:null};
 (kids||[]).forEach(function(k){k.parentElement=e;e.childNodes.push(k);if(k.nodeType===1)e.children.push(k);});
 if(text!=null){var t={nodeType:3,nodeValue:text};e.childNodes.unshift(t);}
 e.getAttribute=function(a){return a in this.attrs?this.attrs[a]:null};
 e.hasAttribute=function(a){return a in this.attrs};
 Object.defineProperty(e,'textContent',{get:function(){return this.childNodes.map(function(c){return c.nodeType===3?c.nodeValue:c.textContent}).join('')}});
 Object.defineProperty(e,'innerText',{get:function(){return this.textContent}});
 e.querySelectorAll=function(sel){var out=[];(function w(n){n.children.forEach(function(c){if(match(c,sel))out.push(c);w(c);})})(this);return out;};
 e.querySelector=function(sel){return this.querySelectorAll(sel)[0]||null};
 e.getBoundingClientRect=function(){return {width:1,height:1}};
 e.closest=function(sel){for(var n=this;n;n=n.parentElement)if(match(n,sel))return n;return null};
 return e;}
function match(el,sel){return sel.split(',').some(function(s){s=s.trim();var m=s.match(/^([a-z]*)\[([\w-]+)(?:([$]?=)"?([^"\]]*)"?)?\]$/i);if(!m)return false;
 if(m[1]&&el.tagName.toLowerCase()!==m[1].toLowerCase())return false;var v=el.getAttribute(m[2]);if(v==null)return false;
 if(!m[3])return true;return m[3]==='='?v===m[4]:v.slice(-m[4].length)===m[4];});}
function P(t){return E('P',{},[],t)}
function unit(key,kids){return E('DIV',{'data-chatgpt-search-unit-key':key},kids)}
function md(lines){return E('DIV',{'data-markdown-text-style':'assistant-message'},lines.map(P))}
function card(name){return E('DIV',{},[E('SPAN',{'data-file-reference':'true'},[],name),E('SPAN',{},[],'Code'),E('SPAN',{},[],'Open file')])}
"""


def _page(r1_answer=True):
    turn1 = [
        "unit('t1:0:user',[P('# Round 5'),P('BEGIN_RESPONSE:%s'),P('<full answer>'),P('END_RESPONSE:%s')])" % (R1, R1),
        "unit('t1:2:assistant',[md(['BEGIN_RESPONSE:%s','the verdict','END_RESPONSE:%s']),card('results.json')])" % (R1, R1),
    ]
    turn2 = ["unit('t2:0:user',[P('# Round 6'),P('BEGIN_RESPONSE:%s'),P('<full answer>'),P('END_RESPONSE:%s')])" % (R2, R2)]
    return "var ROOT=E('MAIN',{},[" + ",".join(turn1 + turn2) + "]);var document=ROOT;document.body=ROOT;var location={pathname:'/c/x'};"


def _eval(js, setup):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = _DOM + setup + "\nconsole.log(JSON.stringify(" + js + "));"
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)


def test_the_sent_turn_is_visible_to_the_send_contract():
    got = json.loads(_eval(CDP._contract_js(R2), _page()))
    fam = {f["adapter"]: f for f in got["families"]}
    assert fam["search-unit-v1"]["ridLanded"] is True
    assert fam["search-unit-v1"]["users"] == 2 and fam["search-unit-v1"]["assistants"] == 1


def test_a_finished_answer_is_done_despite_attachment_cards_after_it():
    got = json.loads(_eval(CDP._detect_js(R1, 0), _page()))
    assert got["done"] is True and got["ac"] == 1


def test_an_unanswered_turn_is_not_done():
    got = json.loads(_eval(CDP._detect_js(R2, 1), _page()))
    assert got["done"] is False


def test_extract_returns_the_body_without_the_cards():
    body = _eval(CDP._extract_js(R1, 0), _page())
    assert body.strip() == "the verdict"
