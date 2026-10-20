# Channel-reliability regressions (field incidents 2026-10-14..18, the zeus session that wrote "consult is
# optional and currently stuck"). Each test fails if its fix is reverted.
#   B  ChatGPT's safety filter replaced a reply with "This content can't be shown ..." (258 chars, no
#      wrapper): the waiter waited 90 minutes for an answer that could never come. It is a terminal typed
#      outcome, `remote_refused`, on the first poll that reads it.
#   C  A sent round whose page shows no generation and no reply is dead, not slow: REQ-20261018-101759-06b1be
#      read gen=False node=none ac=0 for the whole 5400 s (the thread was parked on a tool-approval prompt).
#      After the one stale-tab reload the wait ends `no_generation` (post-send: retrieve, never resend); a
#      thinking round, which keeps the stop button or a reply node up, is never cut.
#   D  (no code change: the daemon's own restart path recovered the browser; see the report.)
#   A  The duplicate user turn is not our send (one click, Enter only when no send button exists).
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)
CONV = "6acf3714-2b74-83ea-9432-9c8d4447b667"
RID = "REQ-20261014-192330-a959d4"
OTHER = "REQ-20261014-200000-b0b0b0"


def _load(name, path, db=None):
    if db is not None:
        os.environ["CGC_STORE_DB"] = str(db)
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


CDP = _load("cdp_consult_channel", os.path.join(SCRIPTS, "cdp_consult.py"))
_SU = _load("test_search_unit_schema_channel", os.path.join(ROOT, "tests", "test_search_unit_schema.py"))
_DOM = _SU._DOM

# The refusal as the live page renders it (captured read-only from the debug Chrome, 2026-10-20, conversation
# 6acf3714, REQ-20261014-192330-a959d4): NOT a markdown answer but a notice -- a leaf <p class="font-semibold">
# heading, a <p> with the filter's stated reason, a Learn more link, all outside any .markdown container, inside
# a fallback-turn assistant unit. The page had no [data-message-author-role] (historical render).
HEADING = "This content can\u2019t be shown"
REASON = ("This content was flagged for possible cybersecurity risk. You may be eligible for Daybreak Blue, "
          "which reduces refusals for authorized defensive work on supported models. Other safeguards still apply.")
STOP_BUTTON = "E('BUTTON',{'data-testid':'stop-button','aria-label':'Stop streaming'},[],'Stop')"


def _node(js, setup):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = _DOM + setup + "\nconsole.log(JSON.stringify(" + js + "));"
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True,
                                     check=True).stdout)


def _user(i, rid):
    return "unit(%s,[%s])" % (json.dumps("t%d:0:user" % i), ",".join(
        "P(%s)" % json.dumps(x) for x in ("CGC_RID:" + rid, "# round", "BEGIN_RESPONSE:" + rid,
                                          "<full answer>", "END_RESPONSE:" + rid)))


def _turn(i, rid, *assistant_texts, extra=""):
    kids = [_user(i, rid)] + ["unit(%s,[md([%s])])" % (json.dumps("t%d:%d:assistant" % (i, 2 + n)),
                                                      json.dumps(t)) for n, t in enumerate(assistant_texts)]
    return "E('DIV',{'data-turn-key':%s},[%s%s])" % (json.dumps("k%d" % i), ",".join(kids), extra)


def _notice(i, extra_class="font-semibold"):
    """The captured notice as an assistant unit: heading leaf + reason leaf + link, no markdown container."""
    return ("unit(%s,[E('DIV',{},[E('DIV',{},[E('DIV',{'data-talvt-turn-state':'cancelled'},["
            "E('DIV',{'class':'mt-3 flex flex-col gap-2'},[E('DIV',{'class':'flex min-w-0 grow items-start'},["
            "E('DIV',{'class':'min-w-0 grow pt-[2px]'},[E('P',{'class':%s},[],%s),E('P',{},[],%s),"
            "E('A',{'href':'https://chatgpt.com/cyber'},[],'Learn more')])])])])])])])"
            % (json.dumps("t%d:2:assistant" % i), json.dumps(extra_class), json.dumps(HEADING), json.dumps(REASON)))


def _refused_turn(i, rid):
    return "E('DIV',{'data-turn-key':%s},[%s,%s])" % (json.dumps("k%d" % i), _user(i, rid), _notice(i))


def _answer_turn(i, rid, text):
    """A real answer: the words render inside the markdown container (assistant_body), unwrapped or wrapped."""
    return _turn(i, rid, text)


def _wrapped(rid, body):
    return "BEGIN_RESPONSE:%s\n%s\nEND_RESPONSE:%s" % (rid, body, rid)


def _page(*kids):
    return ("var ROOT=E('MAIN',{},[" + ",".join(kids) + "]);var document=ROOT;document.body=ROOT;"
            "var location={pathname:'/c/x'};")


def _detect(page, rid=RID):
    return json.loads(_node(CDP._detect_js(rid, scoped=True), page))


# ---- B: the safety block is read, in the reader JS ---------------------------------------------------

def test_the_captured_refusal_notice_is_read_as_a_refusal():
    """The real DOM: heading leaf outside any markdown, in a fallback-turn assistant unit."""
    st = _detect(_page(_refused_turn(3, RID)))
    assert st["refused"] == HEADING, st
    assert st["refused_note"].startswith("This content was flagged for possible cybersecurity risk"), st
    assert st["done"] is False and st["failed"] is None and st["generating"] is False


def test_the_captured_page_has_no_author_role_attribute_and_the_reader_still_resolves_its_interval():
    """The fallback render has zero [data-message-author-role]: the reader runs on the search-unit schema alone.
    The source turn resolves, the assistant unit is `found`, and the interval ends at the next round."""
    page = _page(_refused_turn(3, RID), _turn(4, OTHER, _wrapped(OTHER, "a later round's answer")))
    assert "data-message-author-role" not in page
    st = _detect(page)
    assert st["node"] == "found" and st["ac"] == 2 and st["refused"] == HEADING, st
    assert _detect(page, OTHER)["done"] is True and _detect(page, OTHER)["refused"] is None


def test_either_apostrophe_is_the_heading():
    straight = _notice(3).replace(json.dumps(HEADING), json.dumps("This content can't be shown"))
    page = _page("E('DIV',{'data-turn-key':'k3'},[%s,%s])" % (_user(3, RID), straight))
    assert _detect(page)["refused"] == "This content can't be shown"


def test_a_heading_inside_the_answers_markdown_is_the_models_words_not_a_refusal():
    """The same words as a leaf INSIDE the answer's markdown container (assistant_body) are prose."""
    nested = ("E('DIV',{'data-turn-key':'k1'},[%s,unit('t1:2:assistant',[E('DIV',"
              "{'data-markdown-text-style':'assistant-message'},[E('P',{},[],%s)])])])"
              % (_user(1, RID), json.dumps(HEADING)))
    assert _detect(_page(nested))["refused"] is None, "the assistant_body attribute is enough"


# ---- the captured DOM, on the replay engine that knows classes (skill/scripts/fixture_dom.js) -----------

def _fx(tag, attrs, *kids):
    return [tag, attrs, list(kids)]


def _captured_page(rid=RID, *, heading_wrapper=None, stop=False, later=None):
    """The live page, structure as captured: a fallback-turn user unit carrying the rid marker, then the
    assistant unit holding the notice (ancestors as listed, classes as listed). No data-message-author-role
    anywhere. `heading_wrapper` wraps the heading <p> in an element with that class (an answer's prose);
    `later` appends a following turn."""
    heading = _fx("p", {"class": "font-semibold"}, HEADING)
    if heading_wrapper:
        heading = _fx("div", {"class": heading_wrapper}, heading)
    notice = _fx("div", {"data-chatgpt-selection-conversation-id": "c", "data-chatgpt-selection-message-id": "m"},
                 _fx("div", {},
                     _fx("div", {"data-chatgpt-search-unit-key": "fallback-turn-3:2:assistant",
                                 "data-content-search-unit-key": "fallback-turn-3:2:assistant"},
                         _fx("div", {}, _fx("div", {}, _fx("div", {"data-talvt-turn-state": "cancelled"},
                             _fx("div", {}, _fx("div", {"data-content-search-turn-key": "fallback-turn-3"},
                                 _fx("div", {"class": "mt-3 flex flex-col gap-2"},
                                     _fx("div", {"class": "flex items-center gap-6 self-start"},
                                         _fx("div", {"class": "flex min-w-0 grow items-start"},
                                             _fx("div", {"class": "min-w-0 grow pt-[2px]"}, heading,
                                                 _fx("p", {}, REASON),
                                                 _fx("a", {"href": "https://chatgpt.com/cyber"}, "Learn more")
                                                 ))))))))))))
    user = _fx("div", {"data-chatgpt-search-unit-key": "fallback-turn-3:0:user"},
               _fx("p", {}, "CGC_RID:" + rid), _fx("p", {}, "BEGIN_RESPONSE:" + rid),
               _fx("p", {}, "<full answer>"), _fx("p", {}, "END_RESPONSE:" + rid))
    turns = [_fx("div", {"data-turn-key": "88281a9c"}, user, notice)] + (later or [])
    body = [_fx("main", {}, *turns)]
    if stop:
        body.append(_fx("button", {"data-testid": "stop-button", "aria-label": "Stop streaming"}, "Stop"))
    return [_fx("html", {}, _fx("body", {}, *body))]


def _fx_detect(tree, rid=RID):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    r = subprocess.run(["node", os.path.join(SCRIPTS, "fixture_dom.js")], capture_output=True, text=True,
                       input=json.dumps({"tree": tree, "path": "/c/x", "js": CDP._detect_js(rid, scoped=True)}))
    assert r.returncode == 0, r.stderr[-400:]
    return json.loads(json.loads(r.stdout))


def test_the_captured_live_refusal_dom_is_a_refusal():
    st = _fx_detect(_captured_page())
    assert st["refused"] == HEADING, st
    assert st["refused_note"].startswith("This content was flagged for possible cybersecurity risk"), st
    assert st["node"] == "found" and st["done"] is False and st["generating"] is False


def test_the_captured_dom_has_no_author_role_and_the_reader_resolves_its_interval_anyway():
    """Reported, not widened: on the fallback render (zero [data-message-author-role]) the source turn, the
    assistant unit and the interval end all resolve through the search-unit schema alone."""
    later = [_fx("div", {"data-turn-key": "next"},
                 _fx("div", {"data-chatgpt-search-unit-key": "fallback-turn-4:0:user"},
                     _fx("p", {}, "CGC_RID:" + OTHER), _fx("p", {}, "BEGIN_RESPONSE:" + OTHER),
                     _fx("p", {}, "END_RESPONSE:" + OTHER)),
                 _fx("div", {"data-chatgpt-search-unit-key": "fallback-turn-4:2:assistant"},
                     _fx("div", {"data-markdown-text-style": "assistant-message"},
                         _fx("p", {}, "BEGIN_RESPONSE:" + OTHER), _fx("p", {}, "later answer"),
                         _fx("p", {}, "END_RESPONSE:" + OTHER))))]
    page = _captured_page(later=later)
    assert "data-message-author-role" not in json.dumps(page)
    st = _fx_detect(page)
    assert st["node"] == "found" and st["refused"] == HEADING and st["ac"] == 2, st
    nxt = _fx_detect(page, OTHER)
    assert nxt["done"] is True and nxt["refused"] is None, "the later round is its own interval"


@pytest.mark.parametrize("wrapper", ["markdown prose dark:prose-invert", "x-markdown-y"])
def test_the_same_heading_inside_a_markdown_container_is_prose_not_a_notice(wrapper):
    """`.markdown` and `[class*=markdown]` both mark an answer's own words (contract prose_container)."""
    assert _fx_detect(_captured_page(heading_wrapper=wrapper))["refused"] is None


def test_the_captured_notice_is_not_read_while_the_stop_button_is_up():
    st = _fx_detect(_captured_page(stop=True))
    assert st["generating"] is True and st["refused"] is None


# Sol's counterexamples, UNWRAPPED, inside a markdown answer: never a refusal.
SHORT_ANSWER = ("This content was flagged for possible risk is just the UI string; the code does not verify a "
                "moderation status.")


def test_a_short_unwrapped_answer_that_opens_with_the_phrase_is_not_a_refusal():
    assert len(SHORT_ANSWER) == 103 or 100 < len(SHORT_ANSWER) < 120
    st = _detect(_page(_turn(1, RID, SHORT_ANSWER)))
    assert st["refused"] is None, st
    st = _detect(_page(_turn(1, RID, "This content can\u2019t be shown is the literal UI heading; the code does "
                                    "not handle it.")))
    assert st["refused"] is None, "a whole-message heading prefix is not a heading leaf"


def test_the_heading_alone_as_an_answers_whole_message_inside_markdown_is_not_a_refusal():
    st = _detect(_page(_turn(1, RID, HEADING)))
    assert st["refused"] is None


def test_the_notice_is_not_read_while_the_stop_button_is_up():
    stop = "E('DIV',{}," + "[" + STOP_BUTTON + "])"
    st = _detect(_page(_refused_turn(3, RID), stop))
    assert st["generating"] is True and st["refused"] is None, st


def test_a_refusal_on_a_later_round_is_not_this_rounds():
    page = _page(_turn(1, RID, _wrapped(RID, "fine answer")), _refused_turn(2, OTHER))
    assert _detect(page, RID)["refused"] is None
    assert _detect(page, OTHER)["refused"] == HEADING


def test_the_refusal_headings_and_prose_container_live_in_the_contract():
    facts = json.load(open(os.path.join(SCRIPTS, "chatgpt_contract.json"), encoding="utf-8"))["facts"]
    assert CDP._CONTRACT_SCHEMA["refusal_headings"] == "words" and CDP._CONTRACT_SCHEMA["prose_container"] == "sel"
    assert HEADING in facts["refusal_headings"]["value"] and "refusal_markers" not in facts
    assert "markdown" in facts["prose_container"]["value"] and "refusal_headings" in CDP.UI


# ---- C: what the reader reports for a LIVE thinking round (the real detect JS, in node) -----------------

def test_the_reader_reports_a_thinking_round_as_live_by_stop_button_or_by_node():
    """The search-unit build renders no assistant unit until the answer finishes, so a thinking round is
    `node=none` with the stop button up (generating:true); once the thinking-summary stub renders it is a
    `node=found`. Either one is life to the waiter. A dead round is neither: no stop button, no node."""
    user_only = _turn(1, RID)
    thinking = _detect(_page(user_only, "E('DIV',{},[" + STOP_BUTTON + "])"))
    assert thinking["generating"] is True and thinking["node"] == "none" and thinking["ac"] == 0
    stub = _detect(_page(_turn(1, RID, "Thinking about the request")))
    assert stub["generating"] is False and stub["node"] == "found" and stub["ac"] == 1
    dead = _detect(_page(user_only))
    assert dead["generating"] is False and dead["node"] == "none" and dead["ac"] == 0 and dead["refused"] is None


# ---- the waiter: both new verdicts end the wait, never touching the composer --------------------------

class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, seconds=0.0):
        self.t += max(float(seconds or 0.0), 0.001)


def _dead(**over):
    st = {"generating": False, "done": False, "blocker": None, "failed": None, "refused": None,
          "node": "none", "superseded": False, "len": 0, "begin": False, "end": False, "ac": 0}
    st.update(over)
    return st


def _user_text():
    return "CGC_RID:%s\n# round\nBEGIN_RESPONSE:%s\n<full answer>\nEND_RESPONSE:%s" % (RID, RID, RID)


class FakeTab:
    """A tab whose heartbeat state is a function of virtual time and of whether it was reloaded."""

    def __init__(self, clock, state, after_reload=None, answer="the answer"):
        self.clock, self.state, self.after_reload, self.answer = clock, state, after_reload, answer
        self.t0, self.reloads, self.keys, self.closed = clock.time(), 0, [], False

    def now(self):
        return self.clock.time() - self.t0

    def eval(self, expr, timeout=None):
        if "var allow=" in expr or expr.startswith("(function(){try{var sc="):
            return None
        if "Array.prototype.map.call(u" in expr:
            return [_user_text()]
        if "generating:stop,done:done" in expr:
            fn = self.after_reload if (self.reloads and self.after_reload) else self.state
            return json.dumps(fn(self.now()))
        if "res.done?res.body" in expr:
            healed = self.reloads and self.after_reload
            return self.answer if healed else ""
        if "closest?" in expr:
            return "gpt-6-pro"
        if "__cgcText(__cgcNode(" in expr:
            return ""
        raise AssertionError("unexpected eval: " + expr[:100])

    def call(self, method, params=None, timeout=None):
        if method == "Page.reload":
            self.reloads += 1
            return {}
        raise AssertionError("the waiter is read-only; it called " + method)

    def key(self, *a):
        self.keys.append(a)
        raise AssertionError("a wait may never press a key")

    def conversation_id(self):
        return CONV

    def close_tab(self):
        self.closed = True

    def close(self):
        pass


@pytest.fixture
def wait(tmp_path, monkeypatch):
    monkeypatch.setattr(CDP, "CGC_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(CDP, "STATE_PATH", str(tmp_path / "active.json"))
    monkeypatch.setattr(CDP, "STATE_LOCK_PATH", str(tmp_path / "active.json.lock"))
    monkeypatch.setattr(CDP, "STALE_TURN_AFTER_S", 10 ** 9)   # only the verdict under test may reload
    monkeypatch.setattr(CDP, "STALE_RELOAD_SETTLE_S", 0)
    clock = _Clock()
    monkeypatch.setattr(CDP, "time", clock)

    def run(make_tab, *, timeout=5400, poll=20, settle=300):
        tab = make_tab(clock)
        monkeypatch.setattr(CDP, "CDP", lambda *a, **kw: tab)
        ns = types.SimpleNamespace(rid=RID, conversation=CONV, port=9333, out=str(tmp_path / "answer.txt"),
                                   poll=poll, timeout=timeout, settle_seconds=settle, min_unwrapped=2000,
                                   keep_tab=False)
        t0 = clock.time()
        code = CDP.cmd_wait(ns)
        return code, tab, clock.time() - t0, ns
    return run


# ---- B: end to end -----------------------------------------------------------------------------------

def test_a_refused_round_ends_remote_refused_on_the_second_poll(wait, capsys):
    """The 2026-10-14 round waited 5400 s for a reply the filter had already replaced. The text must repeat
    on two consecutive polls (see the flicker test), so it ends one poll after the first read."""
    code, tab, spent, _ = wait(lambda c: FakeTab(c, lambda t: _dead(refused=HEADING, node="found",
                                                                    len=258, ac=1)))
    err = capsys.readouterr().err
    assert code == 4 and spent < 60, (code, spent)
    assert "CGC_ERROR remote_refused" in err and "timeout_no_answer" not in err
    assert "NEW --request-key" in err and "code review" in err
    assert tab.closed and tab.reloads == 0 and tab.keys == []


def test_a_wrapped_answer_outranks_a_refusal_read(wait, capsys):
    """done beats refused: a finished, verified answer is never discarded for a phrase in its interval."""
    both = lambda t: _dead(done=True, refused=HEADING, node="found", len=500, ac=2)

    def mk(c):
        tab = FakeTab(c, both)
        tab.eval_orig = tab.eval
        tab.eval = lambda expr, timeout=None: ("the verified answer" if "res.done?res.body" in expr
                                               else tab.eval_orig(expr, timeout))
        return tab
    code, tab, _, ns = wait(mk)
    assert code == 0 and "remote_refused" not in capsys.readouterr().err
    assert open(ns.out).read() == "the verified answer"


# ---- C: the dead-round verdict ------------------------------------------------------------------------

def test_a_round_with_no_generation_and_no_reply_ends_no_generation_after_one_reload(wait, capsys):
    """06b1be replayed at its real cadence (poll 20 s, budget 5400 s): the page is dead from the first poll.
    One reload, then the verdict ~NO_GEN_RECHECK_S later — not the 5400 s the real wait spent."""
    code, tab, spent, _ = wait(lambda c: FakeTab(c, lambda t: _dead()))
    err = capsys.readouterr().err
    assert code == 4
    assert CDP.NO_GENERATION_S <= spent <= CDP.NO_GENERATION_S + CDP.NO_GEN_RECHECK_S + 100, spent
    assert tab.reloads == 1, "the wait's one reload is spent before the verdict"
    assert "CGC_ERROR no_generation" in err and "timeout_no_answer" not in err
    assert "approval" in err and "never re-send" in err
    assert tab.closed and tab.keys == []


def test_the_threshold_is_the_env_knob_and_zero_disables_it(wait, monkeypatch, capsys):
    monkeypatch.setattr(CDP, "NO_GENERATION_S", 0)
    code, tab, spent, _ = wait(lambda c: FakeTab(c, lambda t: _dead()), timeout=2000)
    assert code == 4 and "no_generation" not in capsys.readouterr().err and tab.reloads == 0
    for raw, want in ((None, 900), ("120", 120), ("junk", 900), ("-5", 0)):
        if raw is None:
            monkeypatch.delenv("CGC_NO_GENERATION_S", raising=False)
        else:
            monkeypatch.setenv("CGC_NO_GENERATION_S", raw)
        fresh = _load("cdp_consult_knob", os.path.join(SCRIPTS, "cdp_consult.py"))
        assert fresh.NO_GENERATION_S == want, (raw, fresh.NO_GENERATION_S)


def test_a_thinking_round_with_the_stop_button_up_is_never_cut(wait, capsys):
    """A live Pro round keeps the stop button up through every thinking phase and has no reply node yet
    (the search-unit build renders none until the answer finishes): gen=True node=none for the whole budget."""
    code, tab, spent, _ = wait(lambda c: FakeTab(c, lambda t: _dead(generating=True)))
    err = capsys.readouterr().err
    assert code == 4 and spent >= 5400, "ran the full budget"
    assert "no_generation" not in err and tab.reloads == 0


def test_a_thinking_round_showing_only_its_summary_node_is_never_cut(wait, capsys):
    """The other shape of a live thinking round: no stop button read, but the thinking-summary stub is an
    assistant node in the interval (node=found)."""
    code, tab, spent, _ = wait(lambda c: FakeTab(c, lambda t: _dead(node="found", len=210, ac=1)))
    assert code == 4 and spent >= 5400
    assert "no_generation" not in capsys.readouterr().err


def test_the_dead_clock_is_continuous_a_sign_of_life_restarts_it(wait, capsys):
    """Dead 800 s, one generating beat, dead again: the clock restarts at the beat, so the verdict comes
    NO_GENERATION_S after IT, never at 900 s of cumulative deadness."""
    def state(t):
        return _dead(generating=True) if 800 <= t < 840 else _dead()
    code, tab, spent, _ = wait(lambda c: FakeTab(c, state))
    assert code == 4 and "CGC_ERROR no_generation" in capsys.readouterr().err
    assert spent >= 840 + CDP.NO_GENERATION_S, spent


def test_life_signs_inside_every_window_keep_a_slow_round_alive(wait, capsys):
    """A stop button blinking up every 600 s, for the whole budget, is a live round."""
    def state(t):
        return _dead(generating=True) if int(t) % 600 < 40 else _dead()
    code, tab, spent, _ = wait(lambda c: FakeTab(c, state))
    assert code == 4 and spent >= 5400 and "no_generation" not in capsys.readouterr().err


def test_a_reply_that_appears_after_the_reload_is_delivered(wait, capsys):
    """The tab was stale: the reload shows the finished answer, and the wait delivers it instead of ending."""
    done = lambda t: _dead(done=True, node="found", len=11, ac=1)
    code, tab, spent, ns = wait(lambda c: FakeTab(c, lambda t: _dead(), after_reload=done))
    assert code == 0 and tab.reloads == 1
    assert open(ns.out).read() == "the answer"
    assert "no_generation" not in capsys.readouterr().err


# ---- backend: both verdicts are typed, neither can resend --------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path))
    store_mod = _load("cgc_store", os.path.join(SCRIPTS, "cgc_store.py"), tmp_path / "control.db")
    spool = _load("cgc_spool", os.path.join(SCRIPTS, "cgc_spool.py"), tmp_path / "control.db")
    backend = _load("cgc_backend", os.path.join(SCRIPTS, "cgc_backend.py"), tmp_path / "control.db")
    monkeypatch.setattr(spool, "acquire_conversation_lease", lambda conv: object())
    s = store_mod.Store(str(tmp_path / "control.db"))
    s.create_round(RID, "submit", prompt="p", spec_json=json.dumps({"model": "Pro"}))
    s.set_state(RID, store_mod.READY)
    aid = s.begin_send(RID, "p", store_mod.sha256("p"), daemon_instance_id="d1")
    s.mark_accepted(aid, CONV)
    s.mark_waiting(RID)
    yield store_mod, backend, s
    s.close()


REFUSED_WAIT = {"code": 4, "stderr": "CGC_WAIT alive: gen=False\nCGC_ERROR remote_refused: ChatGPT's safety "
                                     "filter replaced the reply with 'This content can’t be shown'\n"}
NOGEN_WAIT = {"code": 4, "stderr": "CGC_ERROR no_generation: the page has shown no generation and no "
                                   "reply for 1020s after the send\n"}


def _script(*steps):
    calls, it = [], iter(steps)

    def cdp(kind, **kw):
        calls.append(kind)
        return dict(next(it))
    cdp.calls = calls
    return cdp


def test_a_refusal_is_a_failed_round_with_the_rephrase_instruction_and_no_continue(env):
    store_mod, backend, s = env
    cdp = _script(REFUSED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True) == store_mod.FAILED
    assert cdp.calls == ["wait"], "a continue is a second paid send into a thread the filter just refused"
    r = s.get_round(RID)
    assert r["error_code"].startswith("CGC_ERROR remote_refused")
    d = backend.decide(s, r, daemon_up=True)
    assert d["code"] == "remote_failed" and d["next_command"] is None and d["retryable"] is False
    assert d["human_action"] == ("ChatGPT's safety filter blocked this round: rephrase the security framing "
                                 "(e.g. describe it as code review) and re-fire with a new --request-key")
    assert backend.ERROR_CODE["remote_refused"] == "remote_failed"
    assert backend.code_of(store_mod.FAILED, r["error_code"]) == "remote_failed"


def test_a_refusal_found_by_the_one_shot_auto_retrieve_is_failed_too(env):
    """resume_round re-waits the SOURCE round (not a retrieve kind): a refusal there ends it the same way."""
    store_mod, backend, s = env
    s.set_state(RID, store_mod.POSSIBLY_ACCEPTED, error_code="wait produced no answer (exit 4)")
    cdp = _script(REFUSED_WAIT)
    final = backend.resume_round(s, s.get_round(RID), cdp)
    assert final == store_mod.FAILED and cdp.calls == ["wait"]
    assert backend.decide(s, s.get_round(RID))["human_action"].startswith("ChatGPT's safety filter blocked")


def test_a_no_generation_wait_is_uncertain_retrieve_never_resend(env):
    store_mod, backend, s = env
    cdp = _script(NOGEN_WAIT)
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True)
    assert final == store_mod.POSSIBLY_ACCEPTED and cdp.calls == ["wait"], "post-send: nothing is re-sent"
    r = s.get_round(RID)
    assert r["error_code"].startswith("CGC_ERROR no_generation")
    d = backend.decide(s, r, daemon_up=True)
    assert d["code"] == "send_uncertain" and "enqueue --kind retrieve" in d["next_command"]
    assert "approval" in d["human_action"] and "never re-send" in d["human_action"]
    assert backend.ERROR_CODE["no_generation"] == "send_uncertain"


def test_a_no_generation_after_the_one_continue_is_typed_too(env):
    """The auto-continue's re-wait can end on either verdict as well: it is classified, not filed as an
    anonymous 'wait produced no answer'."""
    store_mod, backend, s = env
    failed = {"code": 4, "stderr": "CGC_ERROR turn_failed: ChatGPT marked it 'Thinking failed'\n"}
    cdp = _script(failed, {"code": 0}, REFUSED_WAIT)
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True)
    assert final == store_mod.FAILED and cdp.calls == ["wait", "followup", "wait"]
    assert s.get_round(RID)["error_code"].startswith("CGC_ERROR remote_refused")


# ---- A: one click is one send -----------------------------------------------------------------------

def test_one_click_is_one_send_eval_and_no_enter(monkeypatch, tmp_path, capfd):
    """The duplicate user turn on 6ad4e397 is not ours: the server transcript holds ONE user message, the
    first attempt died in tab creation before any composer existed, and the Enter fallback is reachable only
    for a reply of 'no-send' (no button was rendered, so no click happened). A click reply never earns Enter."""
    safety = _load("test_v06_safety_channel", os.path.join(ROOT, "tests", "test_v06_safety.py"))
    for cmd in ("submit", "followup"):
        rc, tab = safety._drive(monkeypatch, tmp_path, cmd, "clicked")
        assert tab.send_evals == 1 and tab.keys == [], (cmd, tab.send_evals, tab.keys)
    rc, tab = safety._drive(monkeypatch, tmp_path, "submit", "no-send")
    assert tab.send_evals == 1 and tab.keys == [("Enter", "Enter", 13)]


# ===== review round 1 (Sol): precedence, evidence, interval, dead clock =====================================

class ScriptedTab(FakeTab):
    """FakeTab plus: an approval card on screen during `card(t)` and a failed detect read during `gap(t)`."""

    def __init__(self, clock, state, card=lambda t: False, gap=lambda t: False, **kw):
        super().__init__(clock, state, **kw)
        self.card, self.gap, self.cards_answered = card, gap, 0

    def eval(self, expr, timeout=None):
        if "var allow=" in expr and self.card(self.now()):
            self.cards_answered += 1
            return {"app": "WebCodex Demo", "text": "WebCodex Demo\nAllow file materialization?"}
        if "generating:stop,done:done" in expr and self.gap(self.now()):
            raise RuntimeError("CDP Runtime.evaluate timeout")
        return super().eval(expr, timeout)


def _refused_state(**over):
    return lambda t: _dead(refused=HEADING, node="found", len=258, ac=1, **over)


# ---- 1: a refusal outranks a continuable failure; a finished answer outranks both ----------------------

def test_a_refusal_beside_a_failed_marker_is_remote_refused_and_nothing_is_continued(wait, env, capsys):
    """Both markers on the page: the old order exited turn_failed and the backend then sent a continue into
    the refused thread (wait, followup, wait). The waiter now reports the refusal, and fed through the
    backend that is FAILED with no followup issued."""
    code, tab, spent, _ = wait(lambda c: FakeTab(c, _refused_state(failed="Thinking failed")))
    err = capsys.readouterr().err
    assert code == 4 and "CGC_ERROR remote_refused" in err and "turn_failed" not in err, err
    store_mod, backend, s = env
    cdp = _script({"code": code, "stderr": err})
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True) == store_mod.FAILED
    assert cdp.calls == ["wait"], "no continue goes into a refused thread"
    assert s.get_round(RID)["error_code"].startswith("CGC_ERROR remote_refused")


def test_a_failed_marker_alone_is_still_a_continuable_turn_failure(wait, capsys):
    code, tab, spent, _ = wait(lambda c: FakeTab(c, lambda t: _dead(failed="Thinking failed", node="found",
                                                                   len=40, ac=1)))
    err = capsys.readouterr().err
    assert code == 4 and "CGC_ERROR turn_failed" in err and "remote_refused" not in err


def test_a_finished_wrapped_answer_outranks_both_markers(wait, capsys):
    both = lambda t: _dead(done=True, failed="Thinking failed", refused=HEADING, node="found", len=500, ac=2)

    def mk(c):
        tab = FakeTab(c, both)
        orig = tab.eval
        tab.eval = lambda expr, timeout=None: ("the verified answer" if "res.done?res.body" in expr
                                               else orig(expr, timeout))
        return tab
    code, tab, _, ns = wait(mk)
    assert code == 0 and open(ns.out).read() == "the verified answer"
    assert "remote_refused" not in capsys.readouterr().err


# ---- 2: a prefix is not evidence ---------------------------------------------------------------------

def test_a_streaming_37_char_prefix_with_the_stop_button_up_is_not_a_refusal():
    """Sol's counterexample: the first characters of a long answer still streaming (stop button up) are the
    marker. Unwrapped, inside the markdown container, and the page is generating: never a refusal."""
    prefix = "This content was flagged for possible"
    assert len(prefix) == 37
    stop = "E('DIV',{}," + "[" + STOP_BUTTON + "])"
    st = _detect(_page(_turn(1, RID, prefix), stop))
    assert st["generating"] is True and st["refused"] is None, st


def test_a_streaming_prefix_between_phases_with_the_stop_button_gone_is_still_not_a_refusal():
    """Sol's counterexample: a pause between generation phases (the stop button gone for ~40 s) with the
    answer's text unchanged. Structure, not stability, is the evidence: the words sit inside the markdown
    container, so the read is None however many polls repeat it."""
    for text in ("This content can\u2019t be shown", "This content was flagged for possible"):
        assert _detect(_page(_turn(1, RID, text)))["refused"] is None, text


def test_a_refusal_must_repeat_on_two_polls_before_it_ends_anything(wait, capsys):
    """A one-poll read (a message caught mid-render) ends nothing: the refusal flickers every other poll
    and the wait runs to its own timeout."""
    flicker = lambda t: _dead(refused=HEADING if int(t / 20) % 2 else None, node="found", len=258, ac=1)
    code, tab, spent, _ = wait(lambda c: FakeTab(c, flicker))
    err = capsys.readouterr().err
    assert code == 4 and spent >= 5400 and "remote_refused" not in err


def test_a_refusal_read_while_generating_ends_nothing(wait, capsys):
    """Defence in depth: even a reader that reports `refused` while the stop button is up cannot end the wait."""
    code, tab, spent, _ = wait(lambda c: FakeTab(c, _refused_state(generating=True)))
    assert code == 4 and spent >= 5400 and "remote_refused" not in capsys.readouterr().err


# ---- 3: the refusal is read in the same interval as the answer ---------------------------------------

def test_a_refusal_after_a_later_riless_exchange_is_not_the_answered_rounds():
    """RID answered, then the human types a rid-less note and the filter refuses THAT reply: the interval
    ended at the note (a reply already existed), so the earlier round is not refused."""
    answered = _turn(1, RID, _wrapped(RID, "the verdict"))
    note = ("E('DIV',{'data-turn-key':'h'},[unit('h:0:user',[P('and what about speed?')]),%s])" % _notice(9))
    st = _detect(_page(answered, note))
    assert st["done"] is True and st["refused"] is None, st


def test_a_steer_before_any_reply_does_not_end_the_refusal_interval():
    """The exception __cgcNode keeps: a rid-less steer typed while the round was still thinking folds into
    it, and the one reply after it (here a refusal) is this round's."""
    steer = "E('DIV',{'data-turn-key':'h'},[unit('h:0:user',[P('additional focus on speed')])])"
    reply = "E('DIV',{'data-turn-key':'r'},[%s])" % _notice(9)
    st = _detect(_page(_turn(1, RID), steer, reply))
    assert st["refused"], st


def test_the_refusal_and_the_answer_share_one_interval_rule():
    assert CDP._INTERVAL_END_JS in CDP._NODE_FN and CDP._INTERVAL_END_JS in CDP._detect_js(RID, scoped=True)


# ---- 4: the dead-round clock ------------------------------------------------------------------------

def test_an_approval_card_seen_mid_wait_restarts_the_dead_clock(wait, capsys):
    """A card answered at t=800 is a live prompt: the clock restarts there, so the verdict is NO_GENERATION_S
    after it, not at 900 s of cumulative deadness (which reloaded at 902 and exited at 1022)."""
    code, tab, spent, _ = wait(lambda c: ScriptedTab(c, lambda t: _dead(), card=lambda t: 800 <= t < 805))
    assert tab.cards_answered >= 1
    assert code == 4 and "CGC_ERROR no_generation" in capsys.readouterr().err
    assert spent >= 800 + CDP.NO_GENERATION_S, spent


def test_a_failed_read_restarts_the_dead_clock(wait, capsys):
    """A CDP read gap is not evidence of inactivity: the clock restarts at the first good read after it."""
    code, tab, spent, _ = wait(lambda c: ScriptedTab(c, lambda t: _dead(), gap=lambda t: 800 <= t < 860))
    err = capsys.readouterr().err
    assert code == 4 and "CGC_WAIT eval-retry" in err and "CGC_ERROR no_generation" in err
    assert spent >= 860 + CDP.NO_GENERATION_S, spent


# ---- review round 2 (Sol): structure is the evidence, stability only a corroboration -------------------

def test_sols_111_char_counterexample_is_exactly_111_chars_and_not_a_refusal():
    assert len(SHORT_ANSWER) == 111
    st = _detect(_page(_turn(1, RID, SHORT_ANSWER)))
    assert st["refused"] is None and st["generating"] is False, st


def test_a_growing_answer_that_opens_with_the_phrase_is_never_a_refusal_at_any_length(wait, capsys):
    """Sol's 207 -> 226 char answer: still growing, unwrapped, in markdown. Structure never reads it, so no pair
    of polls -- stable or not, compared in full or by a prefix -- can turn it into a refusal."""
    for n in (207, 226):
        text = (SHORT_ANSWER + " " + "x" * 300)[:n]
        assert len(text) == n
        assert _detect(_page(_turn(1, RID, text)))["refused"] is None, n
    lens = iter(range(207, 100000, 19))
    grow = lambda t: _dead(refused=None, node="found", len=next(lens), ac=1)
    code, tab, spent, _ = wait(lambda c: FakeTab(c, grow))
    assert code == 4 and spent >= 5400 and "remote_refused" not in capsys.readouterr().err


def test_the_reader_reports_the_whole_heading_never_a_prefix_of_it():
    """The two-poll comparison sees exactly what the reader returned: the full leaf text, uncut."""
    long_heading = "This content can\u2019t be shown"
    st = _fx_detect(_captured_page())
    assert st["refused"] == long_heading and len(st["refused"]) == len(long_heading)


def test_a_second_poll_must_match_the_heading_in_full(wait, capsys):
    """The kept stability check compares the whole reported value: two reads that differ anywhere are two
    different reads, not a stable one."""
    seq = {"n": 0}

    def state(t):
        seq["n"] += 1
        return _dead(refused=HEADING + ("A" if seq["n"] % 2 else "B"), node="found", len=258, ac=1)
    code, tab, spent, _ = wait(lambda c: FakeTab(c, state))
    assert code == 4 and spent >= 5400 and "remote_refused" not in capsys.readouterr().err


# ---- the leaf must EQUAL a heading, and be a leaf (the two structural rules that need their own cases) -----

def _notice_with(heading_text, *, wrap_in_div=False, extra_sibling=None):
    """The captured notice with a different heading leaf, outside any markdown container."""
    head = "E('P',{'class':'font-semibold'},[],%s)" % json.dumps(heading_text)
    if wrap_in_div:                                   # the heading's text carried by a NON-leaf container
        head = "E('DIV',{},[E('SPAN',{},[],%s)])" % json.dumps(heading_text)
    kids = head + ("," + extra_sibling if extra_sibling else "")
    return ("E('DIV',{'data-turn-key':'k3'},[%s,unit('t3:2:assistant',[E('DIV',{},[E('DIV',{},[%s])])])])"
            % (_user(3, RID), kids))


def test_a_leaf_that_only_starts_with_the_heading_is_not_a_refusal_even_outside_markdown():
    """Whole-leaf equality, not a prefix: a notice-shaped element whose text merely BEGINS with the heading
    (here the heading and its reason fused in one leaf, outside markdown) is not the heading."""
    fused = HEADING + " This content was flagged for possible cybersecurity risk."
    assert _detect(_page(_notice_with(fused)))["refused"] is None
    assert _detect(_page(_notice_with(HEADING + ".")))["refused"] is None
    assert _detect(_page(_notice_with(HEADING)))["refused"] == HEADING


def test_the_heading_must_be_a_leaf_element_not_a_container_that_holds_it():
    """A DIV whose only content is a SPAN holding the heading: the SPAN is the leaf and is read once; a block
    that holds the heading AND other text (a container) is not a heading, however it starts."""
    holds_more = "E('DIV',{},[E('SPAN',{},[],%s),E('SPAN',{},[],' and more text')])" % json.dumps(HEADING)
    page = _page("E('DIV',{'data-turn-key':'k3'},[%s,unit('t3:2:assistant',[%s])])" % (_user(3, RID), holds_more))
    assert _detect(page)["refused"] == HEADING, "the inner SPAN leaf IS the heading; the container is not"
    both = ("E('DIV',{'data-turn-key':'k3'},[%s,unit('t3:2:assistant',[E('DIV',{},[E('SPAN',{},[],%s),"
            "E('SPAN',{},[],' and more text')])])])" % (_user(3, RID), json.dumps("Intro: " + HEADING)))
    assert _detect(_page(both))["refused"] is None


def test_a_heading_split_across_several_leaves_is_not_a_heading_leaf():
    """The leaf rule's only observable edge, kept on purpose (the contract's failure_markers use the same
    whole-leaf semantics): a block whose text equals the heading only by joining SEVERAL leaves is not a leaf
    heading. The captured notice is one <p>; a UI that splits it needs a contract/reader change, and until then
    the wait falls back to its own budget rather than guess from joined fragments."""
    a, b = HEADING[:12], HEADING[12:]
    split = ("E('DIV',{'data-turn-key':'k3'},[%s,unit('t3:2:assistant',[E('DIV',{},[E('P',{},[E('SPAN',{},[],%s),"
             "E('SPAN',{},[],%s)])])])])" % (_user(3, RID), json.dumps(a), json.dumps(b)))
    assert _detect(_page(split))["refused"] is None
