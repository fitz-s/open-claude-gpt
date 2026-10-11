# A collapsed long user turn reads its full text from the React item (see _USER_TEXT_FN) — but only
# when that text is provably THIS turn's CURRENT text. `__reactFiber$*` is set when the DOM node mounts
# and React double-buffers: after an update the committed fiber may be that fiber's `.alternate`, and
# the one on the node still holds the previous message. Real React 18.3.1 in headless Chrome: a reused
# user node updated from R1 to R2 showed R2 in the DOM and R1 on the node's fiber, so the send contract
# claimed R1 landed, the waiter found R1's source turn, and the scoped assistant read returned R2's
# unwrapped answer under R1. These run the real reader JS in node against fake fibers shaped like every
# buffer combination; whatever is not provable falls back to the DOM text (fail closed).
import json

import pytest

from test_search_unit_schema import CDP, R1, R2, _eval

HEAD = "# Round 7\nvisible head of the prompt"        # 28 letters/digits: past the 20 an identity proof needs
ANSWER = "Answer belonging to R2 only. " * 100
NO_SRC = {"present": False, "is_last": False, "observed_rid": None}


def _msg(rid, head=HEAD):
    return "%s\nhidden tail\nBEGIN_RESPONSE:%s\n<full answer>\nEND_RESPONSE:%s" % (head, rid, rid)


def _page(host, alt, *, dom_head=HEAD, dom_kids=None, link_host=True, button=True):
    """One collapsed user turn (DOM = dom_head + "Show more") followed by an unwrapped answer, with
    fibers shaped like React's: host -> mid -> item fiber, the item two fibers above the unit.
    host / alt: the message on the item fiber of the fiber the node points at, and of its alternate
    (alt None: no alternate anywhere; host None: no item anywhere). link_host=False: the alternate
    pair exists on the item fiber only, the host fiber has none. dom_kids: the head's element
    constructors as JS (a bubble rendered as markdown), instead of one <p> per dom_head line."""
    kids = dom_kids or ",".join("P(%s)" % json.dumps(x) for x in dom_head.split("\n"))
    if button:
        kids += ",E('BUTTON',{'aria-expanded':'false'},[],'Show more')"
    js = ("function chain(m){var c={memoizedProps:m===null?{}:{item:{message:m}},return:null,alternate:null},"
          "d={memoizedProps:{},return:c,alternate:null},h={memoizedProps:{},return:d,alternate:null};"
          "return {h:h,d:d,c:c};}function pair(a,b){a.alternate=b;b.alternate=a;}"
          "var A=chain(%s);U['__reactFiber$x']=A.h;" % json.dumps(host))
    if alt is not None:
        js += "var B=chain(%s);pair(A.c,B.c);pair(A.d,B.d);%s" % (json.dumps(alt), "pair(A.h,B.h);" if link_host else "")
    return ("var U=unit('t7:0:user',[%s]);%s"
            "var ROOT=E('MAIN',{},[E('DIV',{'data-turn-key':'k7'},[U,unit('t7:2:assistant',[md([%s])])])]);"
            "var document=ROOT;document.body=ROOT;var location={pathname:'/c/x'};" % (kids, js, json.dumps(ANSWER)))


def _landed(rid, setup):
    got = json.loads(_eval(CDP._contract_js(rid), setup))
    return {f["adapter"]: f["ridLanded"] for f in got["families"]}["search-unit-v1"]


def _read(setup):
    return _eval(CDP._last_user_text_js(), setup)


def _src(rid, setup):
    return CDP._locate_source_turn(_eval(CDP._all_user_texts_js(), setup), rid)


def _raw(rid, setup):
    return _eval(CDP._last_assistant_js(rid, scoped=True), setup)


def _read_as_dom(setup):
    """The turn reads as its DOM head: no rid in it, and no consumer can locate it by any rid."""
    assert "BEGIN_RESPONSE" not in _read(setup)
    for rid in (R1, R2):
        assert _landed(rid, setup) is False and _src(rid, setup) == NO_SRC and _raw(rid, setup) == ""


# ---- a stale buffer: the DOM shows R2, one fiber still holds R1 ----------------------------------

@pytest.mark.parametrize("host,alt,why", [
    (_msg(R1, HEAD + " " + R1), _msg(R2, HEAD + " " + R2), "stale host fiber, current alternate"),
    (_msg(R2, HEAD + " " + R2), _msg(R1, HEAD + " " + R1), "current host fiber, stale alternate")])
def test_a_reused_node_with_disagreeing_buffers_is_never_attributed_to_r1(host, alt, why):
    setup = _page(host, alt, dom_head=HEAD + " " + R2)
    assert _landed(R1, setup) is False                              # R1 not claimed landed
    assert _src(R1, setup) == NO_SRC                                # R1's source turn not located (__cgcSrc)
    assert "Answer belonging to R2" not in _raw(R1, setup)          # R2's unwrapped answer not salvaged as R1's


@pytest.mark.parametrize("host,alt", [(_msg(R1), _msg(R2)), (_msg(R2), _msg(R1))])
def test_a_refire_shaped_pair_cannot_pass_on_its_head(host, alt):
    """The two messages share the head the DOM shows and differ only in the trailing rid the collapse
    hides, so the prefix check alone cannot tell them apart; the buffers disagreeing does."""
    _read_as_dom(_page(host, alt))


def test_buffers_disagreeing_only_on_the_item_fiber_still_read_the_dom():
    _read_as_dom(_page(_msg(R1), _msg(R2), link_host=False))


def test_an_alternate_that_carries_no_item_is_not_agreement():
    setup = _page(_msg(R2), None).replace("U['__reactFiber$x']=A.h;",
                                          "U['__reactFiber$x']=A.h;var B=chain(null);pair(A.h,B.h);")
    _read_as_dom(setup)


# ---- the buffers agree: the message is the turn's current text ------------------------------------

def test_agreeing_buffers_read_the_message():
    """Updated R1 -> R2, then re-rendered with R2 unchanged: both buffers hold R2."""
    setup = _page(_msg(R2), _msg(R2))
    assert _read(setup) == _msg(R2)
    assert _landed(R2, setup) is True and _landed(R1, setup) is False
    assert _src(R2, setup) == {"present": True, "is_last": True, "observed_rid": R2}
    assert _src(R1, setup)["present"] is False
    assert _raw(R2, setup).strip() == ANSWER.strip() and _raw(R1, setup) == ""


def test_a_node_that_never_updated_has_no_alternate_and_reads_the_message():
    """Also pins that the button's own "Show more" is outside the compared DOM head: the message
    continues the head with its hidden tail, never with the button's text."""
    setup = _page(_msg(R2), None)
    assert _read(setup) == _msg(R2) and _landed(R2, setup) is True


# ---- identity against the DOM ---------------------------------------------------------------------

def test_a_message_the_dom_head_is_not_a_prefix_of_reads_the_dom():
    _read_as_dom(_page(_msg(R2, "# Round 99\nsomebody else's prompt"), None))


def test_the_prefix_check_compares_past_the_first_characters():
    shared = "x" * 190
    assert _landed(R2, _page(_msg(R2, shared + "B"), None, dom_head=shared + "A")) is False
    setup = _page(_msg(R2, shared + "A"), None, dom_head=shared + "A")
    assert _landed(R2, setup) is True


def test_whitespace_differences_between_the_dom_and_the_message_do_not_matter():
    msg = _msg(R2, "# Round 7\n\n  visible    head of the   prompt")
    setup = _page(msg, None)
    assert _read(setup) == msg


# ---- the comparison keeps letters and digits only: a rendered bubble drops markdown markers -------

# ChatGPT may render a user bubble as markdown. Every consult prompt opens with "# {title}" and holds
# "- " bullets and **bold**, so a whitespace-only comparison would reject EVERY long prompt and bring
# back the unknown_send / rid_absent this whole read exists to prevent.
MD_RAW = "# Round 7\n\n- first goal item here\n- second goal item\n\n**Important: read this carefully**"
MD_DOM = ("E('H1',{},[],'Round 7'),E('UL',{},[E('LI',{},[],'first goal item here'),E('LI',{},[],'second goal item')]),"
          "E('P',{},[E('STRONG',{},[],'Important: read this carefully')]),P('…')")


def test_a_head_rendered_as_markdown_still_matches_the_raw_message():
    msg = MD_RAW + "\nhidden tail\nBEGIN_RESPONSE:%s\n<full answer>\nEND_RESPONSE:%s" % (R2, R2)
    setup = _page(msg, msg, dom_kids=MD_DOM)
    assert _read(setup) == msg
    assert _landed(R2, setup) is True and _src(R2, setup)["present"] is True


def test_a_markdown_rendered_head_still_defers_to_the_dom_when_the_buffers_disagree():
    old = MD_RAW + "\nhidden tail\nBEGIN_RESPONSE:%s\n<full answer>\nEND_RESPONSE:%s" % (R1, R1)
    new = old.replace(R1, R2)
    setup = _page(old, new, dom_kids=MD_DOM)
    assert _landed(R1, setup) is False and _landed(R2, setup) is False


def test_punctuation_alone_never_separates_a_head_from_its_message():
    dom = "Round 7: audit the cache layer, please!"
    assert _landed(R2, _page(_msg(R2, "## Round 7 - audit the cache-layer please"), None, dom_head=dom)) is True


def test_a_different_turn_that_differs_in_words_as_well_as_punctuation_reads_the_dom():
    dom = "Round 7: audit the cache layer, please!"
    other = "## Round 8 - audit the cache-layer please"             # 8 for 7: a different turn
    setup = _page(_msg(R2, other), None, dom_head=dom)
    _read_as_dom(setup)


def test_a_head_under_twenty_letters_and_digits_proves_no_identity():
    assert _landed(R2, _page(_msg(R2, "a" * 19), None, dom_head="a" * 19)) is False
    assert _landed(R2, _page(_msg(R2, "a" * 20), None, dom_head="a" * 20)) is True
    # punctuation does not pad the count
    assert _landed(R2, _page(_msg(R2, "Fix it!!!"), None, dom_head="Fix it!!!")) is False


# ---- everything else is unchanged -----------------------------------------------------------------

def test_a_turn_without_a_show_more_button_still_reads_only_the_dom():
    _read_as_dom(_page(_msg(R2), _msg(R2), button=False))


def test_a_collapsed_turn_with_no_item_still_falls_back_to_the_dom():
    _read_as_dom(_page(None, None))
