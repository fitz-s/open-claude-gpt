#!/usr/bin/env python3
# Created: 2026-09-06
# Authority basis: production log /tmp/cgc/spool/logs/REQ-20260906-025243-c975c2.log (2026-09-06).
# The first waiter on that round spent its entire 5400s timeout printing
#   CGC_WAIT alive: gen=False len=539 done=False begin=False end=False ac=3
# — three assistant turns for a conversation that had four — and exited 4 with
# timeout_no_answer. A second waiter started moments later re-opened the conversation, logged
# `resolving source turn… (not yet visible)` three times, then read a 36446-char completed answer
# in 17 seconds. The answer was there the whole time; the first waiter's tab was stale.
"""
Offline tests for cmd_wait's stale-tab recovery in cdp_consult.py: no browser, no network. The
fake CDP below answers Runtime.evaluate by dispatching on substrings of the generated JS (the same
technique tests/test_model_picker.py uses) and refuses, loudly, any call that could reach the
composer — the recovery is READ-side and must stay structurally incapable of re-sending.
Run: python3 -m pytest tests/test_wait_stale_tab.py -q
"""
import importlib.util as _ilu
import json as _json
import os as _os
import types

import pytest

_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_spec = _ilu.spec_from_file_location(
    "cdp_consult", _os.path.join(_REPO, "skill", "scripts", "cdp_consult.py"))
_CDP = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_CDP)

RID = "REQ-20260906-025243-c975c2"
CONV = "6a9d16b7-bb9c-83ea-8665-400615e369bd"
ANSWER = "the answer that existed the whole time, " * 40


def _user_turn(rid):
    """A user turn as the DOM flattens it: the template's own sentinel pair is the last thing in
    the render, which is what _turn_canonical_rid matches on."""
    return f"# Follow-up round\nsome ask\nBEGIN_RESPONSE:{rid}\n<full answer>\nEND_RESPONSE:{rid}"


class FakeTab:
    """A ChatGPT tab whose DOM may lag the server-side conversation until it is reloaded."""

    def __init__(self, *, turns_before_reload, turns_after_reload, answer=ANSWER):
        self._before, self._after = turns_before_reload, turns_after_reload
        self._answer = answer
        self.reloads = 0
        self.sends = []          # anything that could reach the composer lands here — must stay []
        self.closed_tab = False

    # -- the DOM the waiter is allowed to read ---------------------------------------------
    @property
    def _turns(self):
        return self._after if self.reloads else self._before

    def _answer_visible(self):
        return RID in self._turns

    def eval(self, expr, timeout=None):
        if "Array.prototype.map.call(u" in expr:
            return [_user_turn(r) for r in self._turns]
        if expr.startswith("(function(){try{var sc="):        # _FORCE_RENDER_JS
            return None
        if "generating:stop,done:done" in expr:               # _detect_js
            vis = self._answer_visible()
            return _json.dumps({"generating": False, "done": vis, "blocker": None,
                                "len": len(self._answer) if vis else 539,
                                "begin": vis, "end": vis, "ac": len(self._turns)})
        if "res.done?res.body" in expr:                       # _extract_js
            return self._answer if self._answer_visible() else ""
        if "closest?" in expr:                                # _model_slug_js
            return "gpt-6-pro"
        if "__cgcText(__cgcNode(" in expr:                    # _last_assistant_js (raw salvage)
            return "" if self._answer_visible() else "a stale 562-char stub" * 20
        raise AssertionError(f"fake got an unexpected eval: {expr[:120]}")

    # -- everything below is either navigation (allowed) or a send (never) ------------------
    def call(self, method, params=None, timeout=None):
        if method == "Page.reload":
            self.reloads += 1
            return {}
        if method in ("Page.navigate", "Runtime.enable", "Page.enable"):
            return {}
        raise AssertionError(f"fake got an unexpected CDP call: {method}")

    def key(self, *a, **kw):
        self.sends.append(("key", a))
        raise AssertionError("the read-side recovery must never dispatch a key event")

    def conversation_id(self):
        return CONV

    def close_tab(self):
        self.closed_tab = True

    def close(self):
        pass


class _Clock:
    """A virtual clock, installed as cdp_consult's own `time` (the real module is untouched):
    sleeping advances it instead of blocking. That is what lets a test cross REAL 300s settle
    windows and a REAL 5400s timeout in microseconds while exercising the waiter's actual
    arithmetic — a stale-tab window is defined in wall-clock, so faking the wall clock is the only
    way to test it honestly."""

    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, seconds=0.0):
        self.t += max(float(seconds or 0.0), 0.001)


@pytest.fixture
def wait_env(tmp_path, monkeypatch):
    """Pin every durable path into tmp and put the waiter on a virtual clock."""
    monkeypatch.setattr(_CDP, "CGC_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(_CDP, "STATE_PATH", str(tmp_path / "active.json"))
    monkeypatch.setattr(_CDP, "STATE_LOCK_PATH", str(tmp_path / "active.json.lock"))
    monkeypatch.setattr(_CDP, "STALE_TURN_AFTER_S", 0)
    monkeypatch.setattr(_CDP, "STALE_RELOAD_SETTLE_S", 0)
    clock = _Clock()
    monkeypatch.setattr(_CDP, "time", clock)

    def run(tab, *, timeout=2.0, poll=0.001, settle_seconds=0.5):
        monkeypatch.setattr(_CDP, "CDP", lambda *a, **kw: tab)
        ns = types.SimpleNamespace(rid=RID, conversation=CONV, port=9333,
                                   out=str(tmp_path / "answer.txt"), poll=poll,
                                   timeout=timeout, settle_seconds=settle_seconds,
                                   min_unwrapped=2000, keep_tab=False)
        return _CDP.cmd_wait(ns), ns
    run.clock = clock
    return run


def test_a_stale_tab_is_reloaded_once_and_then_succeeds(wait_env, capsys):
    """The incident's shape: the tab never shows the turn this round sent, so the waiter must stop
    trusting it. One reload, then the normal watch on a DOM that now has the answer."""
    tab = FakeTab(turns_before_reload=["REQ-20260906-020000-aaaaaa"],
                  turns_after_reload=["REQ-20260906-020000-aaaaaa", RID])
    code, ns = wait_env(tab)
    assert code == 0
    assert tab.reloads == 1, "exactly one recovery — not zero, not a reload loop"
    assert open(ns.out).read() == ANSWER
    assert "stale-tab" in capsys.readouterr().err


def test_a_genuinely_absent_turn_reloads_once_and_still_gives_up(wait_env, capsys):
    """The bound matters as much as the recovery: a wrong --conversation, or a round that was
    never sent to this thread, must still reach the existing refusal instead of reloading forever."""
    tab = FakeTab(turns_before_reload=["REQ-20260906-020000-aaaaaa"],
                  turns_after_reload=["REQ-20260906-020000-aaaaaa"])
    code, _ = wait_env(tab, timeout=0.5)
    assert code == 2, "rid_absent — the round's turn is nowhere in this conversation"
    assert tab.reloads == 1, "at most one recovery per wait, however long the window is"
    assert "rid_absent" in capsys.readouterr().err


def test_the_recovery_can_never_re_send(wait_env):
    """The at-most-once send invariant is not defended by care here, it is defended by reach: the
    only non-read CDP verb the whole wait path can issue is Page.reload. Any composer keystroke or
    unexpected CDP method makes the fake fail the test outright."""
    tab = FakeTab(turns_before_reload=[], turns_after_reload=[RID])
    code, _ = wait_env(tab)
    assert code == 0 and tab.reloads == 1
    assert tab.sends == [], "nothing on the read-side recovery path may touch the composer"


# ---------------------------------------------------------------------------------------------
# The SECOND trigger: the source turn resolves fine, and the tab dies afterwards. This is the
# incident's actual shape — the missing-turn trigger above can never fire for it, because the
# user turn was in the DOM at t+0. What the waiter had instead was its own 'stub-stable' verdict,
# emitted ~18 times per dead wait and, until now, meaning nothing.
# ---------------------------------------------------------------------------------------------

FROZEN_STUB = "x" * 562  # the log's dead last-assistant read, well under --min-unwrapped
# The failing wait's own command line, verbatim: `poll 20s, timeout 5400s, settle 300s`. The
# virtual clock makes running the real cadence free, so these tests trip the trigger at the real
# 3 x 300s = 900s and prove the remaining 4500s is no longer spent.
PROD = {"poll": 20, "timeout": 5400, "settle_seconds": 300}


def _st(**over):
    """The frozen heartbeat the log actually printed: gen=False len=539 done=False ac=3."""
    st = {"generating": False, "done": False, "blocker": None, "len": 539,
          "begin": False, "end": False, "ac": 3}
    st.update(over)
    return st


def _incident_states():
    """Generation starts normally (gen=True, ac=4, bytes moving), then the tab loses an assistant
    turn mid-stream and holds 539/ac=3 forever."""
    live = iter([_st(generating=True, len=25, ac=4), _st(generating=True, len=455, ac=4)])
    return lambda: next(live, _st())


class FrozenTab:
    """A tab whose source turn IS visible, so only the frozen-stub trigger can reach it."""

    def __init__(self, *, states, turns_before_reload=(RID,), turns_after_reload=(RID,),
                 recovers=True, answer=ANSWER, clock=None):
        self._states = states
        self._before, self._after = list(turns_before_reload), list(turns_after_reload)
        self._recovers = recovers
        self._answer = answer
        self._clock = clock
        self.reloads = 0
        self.reload_at = None     # virtual wall-clock of the recovery, so a test can price it
        self.sends = []
        self.closed_tab = False

    @property
    def _turns(self):
        return self._after if self.reloads else self._before

    def _healed(self):
        return bool(self.reloads) and self._recovers

    def eval(self, expr, timeout=None):
        if "Array.prototype.map.call(u" in expr:
            return [_user_turn(r) for r in self._turns]
        if expr.startswith("(function(){try{var sc="):
            return None
        if "generating:stop,done:done" in expr:
            if self._healed():
                return _json.dumps(_st(done=True, len=len(self._answer),
                                       begin=True, end=True, ac=4))
            return _json.dumps(self._states())
        if "res.done?res.body" in expr:
            return self._answer if self._healed() else ""
        if "closest?" in expr:
            return "gpt-6-pro"
        if "__cgcText(__cgcNode(" in expr:
            return "" if self._healed() else FROZEN_STUB
        raise AssertionError(f"fake got an unexpected eval: {expr[:120]}")

    def call(self, method, params=None, timeout=None):
        if method == "Page.reload":
            self.reloads += 1
            if self._clock is not None:
                self.reload_at = self._clock.time()
            return {}
        if method in ("Page.navigate", "Runtime.enable", "Page.enable"):
            return {}
        raise AssertionError(f"fake got an unexpected CDP call: {method}")

    def key(self, *a, **kw):
        self.sends.append(("key", a))
        raise AssertionError("the read-side recovery must never dispatch a key event")

    def conversation_id(self):
        return CONV

    def close_tab(self):
        self.closed_tab = True

    def close(self):
        pass


def test_a_frozen_stub_is_reloaded_once_and_then_succeeds(wait_env, capsys):
    """REQ-20260906-025243-c975c2 replayed: generation begins, the assistant turn count drops
    4->3, the byte count freezes at 539 and never moves again. After STALE_STUB_CYCLES settle
    windows of that, the waiter stops believing the tab, reloads once, and reads the answer that
    had been finished the whole time — instead of spending 5400s to report it missing."""
    tab = FrozenTab(states=_incident_states(), clock=wait_env.clock)
    t0 = wait_env.clock.time()
    code, ns = wait_env(tab, **PROD)
    assert code == 0
    assert tab.reloads == 1, "exactly one recovery — not zero, not a reload loop"
    # The price of the trigger, in the units the operator sees: three 300s settle windows plus the
    # polls that established the freeze — vs the 5400s the real wait spent reaching exit 4.
    assert 900 <= tab.reload_at - t0 <= 1100, "STALE_STUB_CYCLES x settle, not a poll-scale hair trigger"
    assert open(ns.out).read() == ANSWER
    err = capsys.readouterr().err
    assert "stale-tab" in err and "consecutive stub-stable" in err
    assert tab.sends == [], "the frozen-stub recovery may not touch the composer either"


def test_growing_bytes_are_never_treated_as_a_stale_tab(wait_env, capsys):
    """A byte count that keeps moving is a live round, however slowly it renders. It never even
    reaches the stub-stable verdict, so it can never spend the reload — and it still ends at the
    existing timeout with the existing exit code."""
    n = iter(range(600, 100000, 37))
    tab = FrozenTab(states=lambda: _st(len=next(n)), recovers=False)
    code, _ = wait_env(tab, **PROD)
    assert code == 4, "unchanged verdict: no wrapped answer, no substantial unwrapped message"
    assert tab.reloads == 0, "movement is life — nothing here is stale"
    assert "timeout_no_answer" in capsys.readouterr().err


def test_a_generating_tab_is_never_treated_as_a_stale_tab(wait_env):
    """gen=True is the legitimate thinking/streaming gap the stub-stable message names. Astra can
    pause streaming for seconds on a classifier check and think for minutes; neither may cost the
    round its recovery, no matter how long the waiter sits there."""
    tab = FrozenTab(states=lambda: _st(generating=True), recovers=False)
    code, _ = wait_env(tab, **PROD)
    assert code == 4
    assert tab.reloads == 0, "a generating round is never stale"


def test_the_two_triggers_share_one_reload_budget(wait_env, capsys):
    """One wait, one reload, whichever trigger claims it. Here the missing-turn trigger fires
    first (the turn is absent at t+0) and the tab then comes back with the turn but a dead answer
    — the frozen-stub trigger must find the budget already spent rather than reload again."""
    tab = FrozenTab(states=_incident_states(),
                    turns_before_reload=["REQ-20260906-020000-aaaaaa"],
                    turns_after_reload=["REQ-20260906-020000-aaaaaa", RID],
                    recovers=False)
    code, _ = wait_env(tab, **PROD)
    assert code == 4, "the second trigger must not rescue what the first already failed to"
    assert tab.reloads == 1, "one budget, two triggers — never two reloads in one wait"
    assert tab.sends == []
    assert "timeout_no_answer" in capsys.readouterr().err


def test_a_genuinely_dead_round_still_reaches_the_existing_timeout(wait_env, capsys):
    """The recovery shortens a dead WATCH; it must not change the verdict on a dead ROUND. A tab
    that reloads and still has nothing spends its one reload and then ends exactly where it did
    before: exit 4, timeout_no_answer, the raw stub preserved beside --out."""
    tab = FrozenTab(states=_incident_states(), recovers=False)
    code, ns = wait_env(tab, **PROD)
    assert code == 4
    assert tab.reloads == 1
    assert open(ns.out + ".raw").read() == FROZEN_STUB
    assert "timeout_no_answer" in capsys.readouterr().err
