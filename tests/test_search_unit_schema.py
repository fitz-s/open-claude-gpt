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
 Object.defineProperty(e,'innerText',{get:function(){return this.childNodes.map(function(c){return c.nodeType===3?c.nodeValue:c.innerText}).filter(function(x){return x!==''}).join('\n')}});
 e.querySelectorAll=function(sel){var out=[];(function w(n){n.children.forEach(function(c){if(match(c,sel))out.push(c);w(c);})})(this);return out;};
 e.querySelector=function(sel){return this.querySelectorAll(sel)[0]||null};
 e.getBoundingClientRect=function(){return {width:1,height:1}};
 e.closest=function(sel){for(var n=this;n;n=n.parentElement)if(match(n,sel))return n;return null};
 return e;}
function match(el,sel){return sel.split(',').some(function(s){s=s.trim();if(s==='*')return true;var m=s.match(/^([a-z]*)\[([\w-]+)(?:([$]?=)"?([^"\]]*)"?)?\]$/i);if(!m)return false;
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
    got = json.loads(_eval(CDP._detect_js(R1, scoped=True), _page()))
    assert got["done"] is True and got["ac"] == 1


def test_an_unanswered_turn_is_not_done():
    got = json.loads(_eval(CDP._detect_js(R2, scoped=True), _page()))
    assert got["done"] is False


def test_extract_returns_the_body_without_the_cards():
    body = _eval(CDP._extract_js(R1, scoped=True), _page())
    assert body.strip() == "the verdict"


def test_the_no_sentinel_salvage_read_skips_the_attachment_cards():
    setup = _page().replace("md(['BEGIN_RESPONSE:%s','the verdict','END_RESPONSE:%s'])" % (R1, R1),
                            "md(['an unwrapped answer'])")
    raw = _eval(CDP._last_assistant_js(R1, scoped=True), setup)
    assert raw.strip() == "an unwrapped answer" and "Open file" not in raw


def test_the_mcp_fallback_readers_see_the_new_schema():
    """poll_js (consult.py) and extractAnswer (retrieval_window.js) are the MCP backend's copies of
    the reader; run the REAL code of each (pulled out exactly as test_parity does) on this DOM."""
    import importlib.util as ilu
    ps = ilu.spec_from_file_location("test_parity", os.path.join(ROOT, "tests", "test_parity.py"))
    par = ilu.module_from_spec(ps)
    ps.loader.exec_module(par)
    poll_js, rid = par._poll_js_parse_source()
    page = _page().replace(R1, rid)
    got = json.loads(_eval(poll_js, page))
    assert got["assistantCount"] == 1 and got["done"] is True

    src = open(par.RETRIEVAL_WINDOW_JS, encoding="utf-8").read()
    fns = []
    for name in ("isFenceToggle", "cgcText", "extractAnswer"):
        m = __import__("re").search(r"function " + name + r"\(", src)
        depth, j = 0, src.index("{", m.start())
        for k in range(j, len(src)):
            depth += {"{": 1, "}": -1}.get(src[k], 0)
            if depth == 0:
                fns.append(src[m.start():k + 1])
                break
    tags = __import__("re").search(r"var BLOCK_TAGS = [^;]+;", src).group(0)
    setup = (page + tags + "var BEGIN=" + json.dumps("BEGIN_RESPONSE:" + rid) + ",END="
             + json.dumps("END_RESPONSE:" + rid) + ";" + "\n".join(fns))
    assert _eval("extractAnswer()", setup).strip() == "the verdict"



def _turned(extra_r2=""):
    """Wrap each turn's units in a [data-turn-key] container, as the live page does, so the failure
    probe can read the turn's own text. extra_r2 is appended to R2's turn (e.g. ChatGPT's marker)."""
    t1 = ("E('DIV',{'data-turn-key':'k1'},[unit('t1:0:user',[P('# Round 5'),P('BEGIN_RESPONSE:%s'),"
          "P('<full answer>'),P('END_RESPONSE:%s')]),unit('t1:2:assistant',[md(['BEGIN_RESPONSE:%s',"
          "'the verdict','END_RESPONSE:%s']),card('results.json')])])" % (R1, R1, R1, R1))
    t2 = ("E('DIV',{'data-turn-key':'k2'},[unit('t2:0:user',[P('# Round 6 (quotes BEGIN_RESPONSE:%s)'),"
          "P('BEGIN_RESPONSE:%s'),P('<full answer>'),P('END_RESPONSE:%s')])%s])" % (R1, R2, R2, extra_r2))
    return ("var ROOT=E('MAIN',{},[" + t1 + "," + t2 + "]);var document=ROOT;document.body=ROOT;"
            "var location={pathname:'/c/x'};")


def test_chatgpts_failure_marker_on_the_source_turn_is_read():
    got = json.loads(_eval(CDP._detect_js(R2, scoped=True), _turned(",E('SPAN',{},[],'Thinking failed')")))
    assert got["failed"] == "Thinking failed" and got["done"] is False


def test_a_failure_on_a_later_turn_is_not_attributed_to_an_earlier_one():
    """R2's prompt quotes R1's BEGIN token and R2 failed. Scoped to its own turn, R1 is unaffected."""
    setup = _turned(",E('SPAN',{},[],'Thinking failed')")
    assert json.loads(_eval(CDP._detect_js(R1, scoped=True), setup))["failed"] is None
    assert json.loads(_eval(CDP._detect_js(R2, scoped=True), setup))["failed"] == "Thinking failed"


def test_no_marker_means_no_failure():
    assert json.loads(_eval(CDP._detect_js(R2, scoped=True), _turned()))["failed"] is None



def test_an_answer_line_that_starts_like_a_failure_is_not_a_failure():
    """Review S1: the marker is a lone status element outside any message body. An unwrapped answer
    whose last line is "Network error handling is untested" must not read as a failed turn."""
    setup = _turned(",unit('t2:2:assistant',[md(['Network error handling is untested in cgc_spool.'])])")
    assert json.loads(_eval(CDP._detect_js(R2, scoped=True), setup))["failed"] is None


# 2026-10-08: ChatGPT collapses a long user message to its head plus "…Show more" (a 20.7 KB prompt
# kept 10098 chars in the DOM), and the template's sentinel pair is the LAST thing in the render, so
# the collapsed turn never carried the rid. The send had landed and the answer finished, but the
# contract saw {search-unit-v1: users 1, assistants 0, ridLanded false} -> unknown_send, and the
# waiter and the retrieve read `rid_absent` beside a complete answer. The fix is in the part that is
# never collapsed: every prompt now OPENS with a plain `CGC_RID:<rid>` line, and a turn's own rid is
# that line (cdp_consult._turn_canonical_rid). The turn's React state is not consulted — which fiber
# or ancestor is current cannot be known from a DOM node (a reused node updated R1 -> R2 kept R1 on
# its fiber), so a stale item attached below must change nothing.
def _collapsed(rid, *, marker=True, collapsed=True, answered=False, stale_item=None, tail=None):
    """One user turn. marker: the head opens with CGC_RID:<rid>. collapsed: a Show more button.
    tail: a rid whose BEGIN/END pair still shows in the DOM (a collapsed turn has none).
    stale_item: a rid whose full message a React fiber carries on this node (it must be ignored)."""
    head = ("P('CGC_RID:%s')," % rid if marker else "") + "P('# Round 7'),P('the head that survives the collapse…')"
    button = ",E('BUTTON',{'aria-expanded':'false'},[],'Show more')" if collapsed else ""
    pair = (",P('BEGIN_RESPONSE:%s'),P('<full answer>'),P('END_RESPONSE:%s')" % (tail, tail)) if tail else ""
    item = json.dumps({"message": "CGC_RID:%s\n# Round 7\nthe hidden tail\nBEGIN_RESPONSE:%s\n<full answer>\n"
                                  "END_RESPONSE:%s" % ((stale_item,) * 3)}) if stale_item else None
    fiber = ("U['__reactFiber$x']={memoizedProps:{},return:{memoizedProps:{},return:"
             "{memoizedProps:{item:%s}}}};" % item) if item else ""
    answer = (",unit('t7:2:assistant',[md(['BEGIN_RESPONSE:%s','the verdict','END_RESPONSE:%s'])])" % (rid, rid)
              if answered else "")
    return ("var U=unit('t7:0:user',[%s%s%s]);%s"
            "var ROOT=E('MAIN',{},[E('DIV',{'data-turn-key':'k7'},[U%s])]);"
            "var document=ROOT;document.body=ROOT;var location={pathname:'/c/x'};"
            % (head, pair, button, fiber, answer))


def _landed(rid, setup):
    got = json.loads(_eval(CDP._contract_js(rid), setup))
    return {f["adapter"]: f["ridLanded"] for f in got["families"]}["search-unit-v1"]


def _texts(setup):
    return _eval(CDP._all_user_texts_js(), setup)


def test_a_collapsed_user_turn_still_lands_the_send_contract():
    got = json.loads(_eval(CDP._contract_js(R2), _collapsed(R2)))
    fam = {f["adapter"]: f for f in got["families"]}
    assert fam["search-unit-v1"]["ridLanded"] is True and fam["search-unit-v1"]["users"] == 1
    assert _landed(R1, _collapsed(R2)) is False


def test_a_legacy_collapsed_turn_without_the_marker_stays_unproven():
    """No marker and the tail pair collapsed away: the DOM head is all there is, no rid is in it,
    and the send stays unknown_send (a human looks) rather than being guessed landed."""
    setup = _collapsed(R2, marker=False)
    assert _landed(R2, setup) is False
    assert CDP._locate_source_turn(_texts(setup), R2)["present"] is False


def test_an_expanded_legacy_turn_is_still_read_by_its_tail_pair():
    assert _landed(R2, _collapsed(R2, marker=False, collapsed=False, tail=R2)) is True


def test_the_waiter_finds_a_collapsed_source_turn_and_its_answer():
    setup = _collapsed(R2, answered=True)
    assert CDP._locate_source_turn(_texts(setup), R2) == {"present": True, "is_last": True, "observed_rid": R2}
    assert CDP._turn_canonical_rid(_eval(CDP._last_user_text_js(), setup)) == R2
    got = json.loads(_eval(CDP._detect_js(R2, scoped=True), setup))
    assert got["done"] is True and got["node"] == "found"


def test_a_stale_react_item_on_the_node_changes_nothing():
    """The reproduced defect: a reused node updated R1 -> R2 shows R2 in the DOM while its
    __reactFiber$ still carries R1's message. React state is ignored; the head's rid is the turn's."""
    setup = _collapsed(R2, answered=True, stale_item=R1)
    assert _landed(R2, setup) is True and _landed(R1, setup) is False
    assert CDP._locate_source_turn(_texts(setup), R1)["present"] is False
    assert CDP._turn_canonical_rid(_eval(CDP._last_user_text_js(), setup)) == R2
    assert _eval(CDP._last_assistant_js(R1, scoped=True), setup) == ""     # R2's answer is not R1's


def test_a_rid_echo_followup_check_reads_the_marker():
    """The follow-up's rid_echo check parses the last user turn's text, collapsed or not."""
    for setup in (_collapsed(R2), _collapsed(R2, collapsed=False, tail=R2)):
        assert CDP._turn_canonical_rid(_eval(CDP._last_user_text_js(), setup)) == R2


def test_two_refire_shaped_turns_resolve_to_distinct_rids():
    """Identical heads except the marker (a refire of the same prompt under a new rid)."""
    def turn(n, rid):
        return ("unit('t%d:0:user',[P('CGC_RID:%s'),P('# Round 7'),P('the same head…'),"
                "E('BUTTON',{'aria-expanded':'false'},[],'Show more')])" % (n, rid))
    setup = ("var ROOT=E('MAIN',{},[%s,%s]);var document=ROOT;document.body=ROOT;"
             "var location={pathname:'/c/x'};" % (turn(1, R1), turn(2, R2)))
    texts = _texts(setup)
    assert [CDP._turn_canonical_rid(t) for t in texts] == [R1, R2]
    assert CDP._locate_source_turn(texts, R1) == {"present": True, "is_last": False, "observed_rid": R2}
    assert CDP._locate_source_turn(texts, R2) == {"present": True, "is_last": True, "observed_rid": R2}


def test_a_marker_that_disagrees_with_the_tail_pair_names_no_rid():
    setup = _collapsed(R2, collapsed=False, tail=R1)
    assert _landed(R1, setup) is False and _landed(R2, setup) is False
