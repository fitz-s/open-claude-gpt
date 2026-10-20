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

# The refusal as the live page rendered it: one assistant message, 258 chars, no wrapper.
REFUSAL = ("This content can’t be shown This content was flagged for possible biological risk. "
           "If you think this is a mistake, please submit feedback. " + "x" * 100)[:258]
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


def _wrapped(rid, body):
    return "BEGIN_RESPONSE:%s\n%s\nEND_RESPONSE:%s" % (rid, body, rid)


def _page(*kids):
    return ("var ROOT=E('MAIN',{},[" + ",".join(kids) + "]);var document=ROOT;document.body=ROOT;"
            "var location={pathname:'/c/x'};")


def _detect(page, rid=RID):
    return json.loads(_node(CDP._detect_js(rid, scoped=True), page))


# ---- B: the safety block is read, in the reader JS ---------------------------------------------------

def test_the_safety_block_is_read_as_a_refusal():
    st = _detect(_page(_turn(1, RID, REFUSAL)))
    assert st["refused"] and st["refused"].startswith("This content can’t be shown"), st
    assert st["done"] is False and st["failed"] is None


def test_the_flagged_phrase_alone_is_read_too():
    st = _detect(_page(_turn(1, RID, "This content was flagged for possible cyber risk.")))
    assert st["refused"], st


def test_an_answer_that_merely_quotes_the_phrase_is_not_a_refusal():
    """A real, wrapped answer (and a long one that starts with the phrase) must never read as a block."""
    body = "This content was flagged for possible risk, the filter said. " + "analysis " * 120
    st = _detect(_page(_turn(1, RID, _wrapped(RID, body))))
    assert st["refused"] is None and st["done"] is True
    st = _detect(_page(_turn(1, RID, "The reply said: This content can’t be shown (quoted)")))
    assert st["refused"] is None, "anchored at the start of the message, not a substring"


def test_a_refusal_on_a_later_round_is_not_this_rounds():
    page = _page(_turn(1, RID, _wrapped(RID, "fine answer")), _turn(2, OTHER, REFUSAL))
    assert _detect(page, RID)["refused"] is None
    assert _detect(page, OTHER)["refused"]


def test_the_refusal_phrases_live_in_the_contract():
    facts = json.load(open(os.path.join(SCRIPTS, "chatgpt_contract.json"), encoding="utf-8"))["facts"]
    assert CDP._CONTRACT_SCHEMA["refusal_markers"] == "words"
    assert any(w.startswith("This content can") for w in facts["refusal_markers"]["value"])
    assert "refusal_markers" in CDP.UI


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
    code, tab, spent, _ = wait(lambda c: FakeTab(c, lambda t: _dead(refused=REFUSAL[:160], node="found",
                                                                    len=258, ac=1)))
    err = capsys.readouterr().err
    assert code == 4 and spent < 60, (code, spent)
    assert "CGC_ERROR remote_refused" in err and "timeout_no_answer" not in err
    assert "NEW --request-key" in err and "code review" in err
    assert tab.closed and tab.reloads == 0 and tab.keys == []


def test_a_wrapped_answer_outranks_a_refusal_read(wait, capsys):
    """done beats refused: a finished, verified answer is never discarded for a phrase in its interval."""
    both = lambda t: _dead(done=True, refused=REFUSAL[:160], node="found", len=500, ac=2)

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
    return lambda t: _dead(refused=REFUSAL[:160], node="found", len=258, ac=1, **over)


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
    both = lambda t: _dead(done=True, failed="Thinking failed", refused=REFUSAL[:160], node="found", len=500, ac=2)

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

def test_a_streaming_prefix_with_the_stop_button_up_is_not_a_refusal():
    """The first characters of any reply can be the marker while the answer is still streaming."""
    stop = "E('DIV',{}," + "[" + STOP_BUTTON + "])"
    st = _detect(_page(_turn(1, RID, "This content can’t be shown"), stop))
    assert st["generating"] is True and st["refused"] is None, st
    # the same text with the stop button gone is the refusal
    assert _detect(_page(_turn(1, RID, "This content can’t be shown")))["refused"]


def test_a_short_legitimate_answer_that_opens_with_the_phrase_is_not_a_refusal():
    """A real answer carries the wrapper the prompt demands. 111 chars of it, finished or still streaming
    (BEGIN seen, no END yet, stop button momentarily absent), is never read as the filter's replacement."""
    body = "This content was flagged for possible risk is just the UI string; the verdict follows below."
    finished = _detect(_page(_turn(1, RID, _wrapped(RID, body))))
    assert finished["done"] is True and finished["refused"] is None
    partial = _detect(_page(_turn(1, RID, "BEGIN_RESPONSE:%s\n%s" % (RID, body))))
    assert partial["done"] is False and partial["refused"] is None, partial


def test_a_refusal_must_repeat_on_two_polls_before_it_ends_anything(wait, capsys):
    """A one-poll read (a message caught mid-render) ends nothing: the refusal flickers every other poll
    and the wait runs to its own timeout."""
    flicker = lambda t: _dead(refused=REFUSAL[:160] if int(t / 20) % 2 else None, node="found", len=258, ac=1)
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
    note = ("E('DIV',{'data-turn-key':'h'},[unit('h:0:user',[P('and what about speed?')]),"
            "unit('h:2:assistant',[md([%s])])])" % json.dumps(REFUSAL))
    st = _detect(_page(answered, note))
    assert st["done"] is True and st["refused"] is None, st


def test_a_steer_before_any_reply_does_not_end_the_refusal_interval():
    """The exception __cgcNode keeps: a rid-less steer typed while the round was still thinking folds into
    it, and the one reply after it (here a refusal) is this round's."""
    steer = "E('DIV',{'data-turn-key':'h'},[unit('h:0:user',[P('additional focus on speed')])])"
    reply = "E('DIV',{'data-turn-key':'r'},[unit('r:2:assistant',[md([%s])])])" % json.dumps(REFUSAL)
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


def test_a_marker_led_message_carrying_a_cgc_sentinel_is_not_a_refusal():
    """The wrapper is the proof an answer is the model's own words. A short message that opens with the marker
    but carries BEGIN_RESPONSE (a model quoting the UI string inside its wrapper, mid-stream, with the stop
    button momentarily absent) is never the filter's replacement."""
    text = "This content was flagged for possible risk. BEGIN_RESPONSE:%s the UI shows this string" % RID
    assert len(text) < CDP._REFUSAL_MAX_CHARS
    assert _detect(_page(_turn(1, RID, text)))["refused"] is None
