# When ChatGPT marks a round's turn failed ("Thinking failed"), the worker sends ONE "continue" in the
# same conversation, carrying the round's own rid, and the same round then reads its answer from the
# newer turn. At most one continue per round, only from the wait of the worker that sent the round
# (`may_continue`) — never from a retrieve or any recovery, which have no send authority — and a second
# failure ends the round FAILED rather than uncertain. The CDP layer is a scripted fake.
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)
CONV = "6ab58360-59cc-83e8-bcbe-3df6cba4a4fc"
RID = "REQ-20260925-120000-00000a"


def _load(name, db):
    os.environ["CGC_STORE_DB"] = str(db)
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path))
    store_mod = _load("cgc_store", tmp_path / "control.db")
    spool = _load("cgc_spool", tmp_path / "control.db")
    backend = _load("cgc_backend", tmp_path / "control.db")
    monkeypatch.setattr(spool, "acquire_conversation_lease", lambda conv: object())
    s = store_mod.Store(str(tmp_path / "control.db"))
    s.create_round(RID, "submit", prompt="p", spec_json=json.dumps({"model": "Pro"}))
    s.set_state(RID, store_mod.READY)
    aid = s.begin_send(RID, "p", store_mod.sha256("p"), daemon_instance_id="d1")
    s.mark_accepted(aid, CONV)
    s.mark_waiting(RID)
    yield store_mod, backend, s
    s.close()


FAILED_WAIT = {"code": 4, "stderr": "CGC_ERROR turn_failed: ChatGPT marked it 'Thinking failed'\n"}


def _script(*steps):
    """A fake run_cdp that returns `steps` in order and records every call."""
    calls, it = [], iter(steps)

    def cdp(kind, **kw):
        calls.append((kind, kw))
        step = next(it)
        if kind == "wait" and step.get("answer"):
            with open(kw["out"], "w") as f:
                f.write(step["answer"])
            return {"code": 0, "out": kw["out"], "stderr": ""}
        return dict(step)
    cdp.calls = calls
    return cdp


def test_a_failed_turn_gets_one_continue_and_the_round_completes(env):
    store_mod, backend, s = env
    cdp = _script(FAILED_WAIT, {"code": 0}, {"answer": "the real answer"})
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True)
    assert final == store_mod.COMPLETED_VERIFIED
    assert s.get_round(RID)["result_text"] == "the real answer"
    kinds = [k for k, _ in cdp.calls]
    assert kinds == ["wait", "followup", "wait"]
    sent = cdp.calls[1][1]
    assert sent["rid"] == RID and sent["conversation"] == CONV
    assert f"BEGIN_RESPONSE:{RID}" in sent["prompt"] and f"END_RESPONSE:{RID}" in sent["prompt"]


def test_a_second_failure_ends_failed_not_uncertain(env):
    store_mod, backend, s = env
    cdp = _script(FAILED_WAIT, {"code": 0}, FAILED_WAIT)
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True)
    assert final == store_mod.FAILED
    r = s.get_round(RID)
    assert "turn_failed" in r["error_code"] and "NEW --request-key" in r["error_code"]
    assert [k for k, _ in cdp.calls].count("followup") == 1


def test_at_most_one_continue_per_round_even_across_waits(env):
    """A claim with no recorded outcome = a worker died after claiming, maybe after the click. The
    next wait must neither send again nor call it failed: uncertain, for the read-only retrieve."""
    store_mod, backend, s = env
    assert s.claim_auto_continue(RID, "x")
    cdp = _script(FAILED_WAIT)
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True)
    assert final == store_mod.POSSIBLY_ACCEPTED
    assert [k for k, _ in cdp.calls] == ["wait"], "the continue was already spent — nothing sent"


def test_a_recorded_not_sent_continue_ends_failed_on_the_next_wait(env):
    store_mod, backend, s = env
    assert s.claim_auto_continue(RID, "x")
    s.record_auto_continue(RID, "not_sent")
    cdp = _script(FAILED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True) == store_mod.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"]


def test_the_continue_goes_through_the_egress_gate(env, monkeypatch):
    store_mod, backend, s = env
    import cgc_spool as sp
    monkeypatch.setattr(sp, "validate_prompt", lambda p: (False, "refused: test"))
    cdp = _script(FAILED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True) == store_mod.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"]
    assert s.auto_continue_state(RID) is None, "refused before the claim — nothing spent"


def test_a_newer_round_that_never_reached_begin_send_does_not_block_the_continue(env):
    """Copilot: a follow-up blocked at the gate never sent; it must not count as the thread moving on."""
    store_mod, backend, s = env
    later = "REQ-20260925-121000-00000e"
    s.create_round(later, "followup", prompt="q", thread_id=CONV, parent_rid=RID,
                   spec_json=json.dumps({"model": "Pro"}))
    s.set_state(later, store_mod.READY)
    s.set_state(later, store_mod.BLOCKED)
    cdp = _script(FAILED_WAIT, {"code": 0}, {"answer": "a"})
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True) == store_mod.COMPLETED_VERIFIED


def test_a_continue_provably_not_sent_ends_failed_and_is_not_retried(env):
    store_mod, backend, s = env
    cdp = _script(FAILED_WAIT, {"code": 2, "stderr": "CGC_ERROR model_not_selectable"})
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True)
    assert final == store_mod.FAILED
    assert not s.claim_auto_continue(RID, "x"), "the marker was written before the send"


@pytest.mark.parametrize("outcome", [
    {"code": 124, "stderr": "timed out"},                                 # killed mid-send
    {"code": 2, "stderr": "CGC_ERROR rid_echo_mismatch: sent follow-up"},  # post-click, unconfirmed
    "raise",
])
def test_a_continue_that_may_have_landed_leaves_the_round_uncertain(env, outcome):
    """Review finding: a continue that timed out or crashed AFTER its click may be generating now.
    Closing the round FAILED would abandon it; it must stay uncertain for the read-only retrieve."""
    store_mod, backend, s = env
    if outcome == "raise":
        calls = []

        def cdp(kind, **kw):
            calls.append(kind)
            if kind == "followup":
                raise ConnectionResetError("socket closed after click")
            return dict(FAILED_WAIT)
    else:
        cdp = _script(FAILED_WAIT, outcome)
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True)
    assert final == store_mod.POSSIBLY_ACCEPTED
    assert "retrieve, don't resend" in s.get_round(RID)["error_code"]
    assert not s.claim_auto_continue(RID, "x"), "still at most one continue"


def test_the_retrieve_after_an_unsure_continue_reads_the_continue_or_closes_failed(env):
    """The recovery the unsure path relies on: resume re-waits the same rid. Landed → its answer;
    not landed → the old failed turn again → FAILED, with no second continue."""
    store_mod, backend, s = env
    assert s.claim_auto_continue(RID, "x")              # the continue was spent (unsure outcome)
    s.set_state(RID, store_mod.POSSIBLY_ACCEPTED)
    assert s.claim_auto_retrieve(RID)                   # the daemon's one-shot read-only reattach
    cdp = _script(FAILED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp) == store_mod.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"]


def test_a_retrieve_never_sends_a_continue(env):
    store_mod, backend, s = env
    rr = "REQ-20260925-120500-00000b"
    s.create_round(rr, "retrieve", parent_rid=RID, thread_id=CONV,
                   spec_json=json.dumps({"conversation": CONV, "parent_rid": RID}))
    s.set_state(rr, store_mod.READY)
    s.set_state(rr, store_mod.WAITING)
    cdp = _script(FAILED_WAIT)
    final = backend._wait_phase(s, rr, CONV, {}, cdp, wait_rid=RID, is_retrieve=True)
    assert final == store_mod.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"]


def test_a_plain_no_answer_timeout_is_still_uncertain_not_continued(env):
    """Only ChatGPT's own failure marker triggers a continue; an ordinary timeout keeps the existing
    retrieve-don't-resend path."""
    store_mod, backend, s = env
    cdp = _script({"code": 4, "stderr": "CGC_ERROR timeout_no_answer: nothing\n"})
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True)
    assert final == store_mod.POSSIBLY_ACCEPTED
    assert [k for k, _ in cdp.calls] == ["wait"]


def test_the_continue_prompt_passes_the_egress_gate_and_resolves_to_its_rid(env):
    _store_mod, backend, _s = env
    import cdp_consult as c
    import cgc_spool as sp
    p = backend._continue_prompt(RID)
    assert sp.validate_prompt(p)[0] is True
    assert c._turn_canonical_rid(p) == RID


class _FollowupTab:
    """Just enough of a live tab for cmd_followup's send + landed-confirmation path."""

    def __init__(self, turns, lands, send="clicked"):
        self.turns, self.lands, self.clicked = list(turns), lands, False
        self.send, self.keys, self.tab_closed = send, [], 0

    def eval(self, expr, timeout=None):
        if ".length" in expr and "querySelectorAll" in expr and "function" not in expr:
            return len(self.turns)
        if "return 'clicked'" in expr:                      # the send eval
            if self.send == "clicked":
                self.clicked = True
                if self.lands:
                    self.turns.append(self.lands)
            return self.send
        if "location.pathname" in expr:
            return "/c/" + CONV
        return None

    def call(self, *a, **k):
        return {}

    def key(self, *a):
        self.keys.append(a)

    def conversation_id(self):
        return CONV

    def close(self):
        pass

    def close_tab(self):
        self.tab_closed += 1


def _run_followup(monkeypatch, tab, rid, blank_last=False):
    import types
    import cdp_consult as c
    monkeypatch.setattr(c, "CDP", lambda *a, **k: tab)
    monkeypatch.setattr(c, "_await_composer", lambda *a, **k: (True, {}))
    monkeypatch.setattr(c, "_paste_prompt", lambda *a, **k: None)
    monkeypatch.setattr(c, "_egress_gate", lambda p: (True, "ok"))
    monkeypatch.setattr(c, "_last_user_text_js", lambda: "LAST")
    monkeypatch.setattr(c, "_write_state", lambda **k: None)
    orig = tab.eval
    tab.eval = lambda e, timeout=None: ((("" if blank_last else tab.turns[-1])) if e == "LAST"
                                        else orig(e, timeout))
    t = {"now": 0.0}
    monkeypatch.setattr(c.time, "time", lambda: t.__setitem__("now", t["now"] + 1) or t["now"])
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    pf = os.path.join(os.environ["CGC_STATE_DIR"], "p.md")
    open(pf, "w").write("Continuing this consult.\nBEGIN_RESPONSE:%s\nx\nEND_RESPONSE:%s\n" % (rid, rid))
    ns = types.SimpleNamespace(no_gate=True, conversation=CONV, prompt_file=pf, task=None, rid=rid,
                               model="skip", model_family="skip", allow_model_mismatch=False,
                               port=9333, mention=[], watch=False, out=None)
    return c.cmd_followup(ns)


def _turn(rid):
    return "ask\nBEGIN_RESPONSE:%s\n<full answer>\nEND_RESPONSE:%s" % (rid, rid)


def test_a_same_rid_resend_is_confirmed_only_by_a_new_turn(env, monkeypatch):
    """The continue re-uses the round's rid, so the thread's last turn ALREADY echoes it. Before the
    click lands, that old echo must not count as confirmation."""
    tab = _FollowupTab([_turn(RID)], lands=None)
    assert _run_followup(monkeypatch, tab, RID) != 0, "no new turn landed — must not report ok"
    tab = _FollowupTab([_turn(RID)], lands=_turn(RID) + "\n")
    assert _run_followup(monkeypatch, tab, RID) == 0, "a new turn with the same rid is the resend"


def test_a_normal_followup_still_confirms_on_its_own_new_rid(env, monkeypatch):
    other = "REQ-20260925-110000-0000ff"
    tab = _FollowupTab([_turn(other)], lands=_turn(RID))
    assert _run_followup(monkeypatch, tab, RID) == 0


def test_a_clean_answer_after_a_continue_is_verified_despite_a_stub_raw_from_the_failed_wait(env):
    """Review S2: the failed wait can leave a stub .raw; the second wait must start clean."""
    store_mod, backend, s = env

    def cdp(kind, **kw):
        cdp.calls.append(kind)
        if kind == "wait" and cdp.calls.count("wait") == 1:
            open(kw["out"] + ".raw", "w").write("stub")
            return dict(FAILED_WAIT)
        if kind == "followup":
            return {"code": 0}
        open(kw["out"], "w").write("clean answer")
        return {"code": 0, "out": kw["out"], "stderr": ""}
    cdp.calls = []
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True) == store_mod.COMPLETED_VERIFIED


def test_no_continue_when_another_round_was_sent_into_the_thread_after(env):
    """Review S3: 'answer that same request' would refer to the newer round's prompt."""
    store_mod, backend, s = env
    later = "REQ-20260925-121000-00000c"
    s.create_round(later, "followup", prompt="q", thread_id=CONV, parent_rid=RID,
                   spec_json=json.dumps({"model": "Pro"}))
    s.set_state(later, store_mod.READY)
    s.begin_send(later, "q", store_mod.sha256("q"), daemon_instance_id="d1")
    cdp = _script(FAILED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True) == store_mod.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"]
    assert "no automatic continue went out" in s.get_round(RID)["error_code"]


def test_the_second_wait_gets_what_is_left_of_the_budget(env):
    store_mod, backend, s = env
    cdp = _script(FAILED_WAIT, {"code": 0}, {"answer": "a"})
    backend._wait_phase(s, RID, CONV, {"model": "Pro", "timeout": 5400}, cdp, may_continue=True)
    first, second = [kw["timeout"] for k, kw in cdp.calls if k == "wait"]
    assert 0 < second <= first <= 5400, "the remainder of ONE budget — no floor, no fresh budget"


@pytest.mark.parametrize("res,want", [
    ({"code": 124, "stderr": "subprocess timeout: Command '[.., '--mention', 'Usage Tracker']'"}, None),
    ({"code": 2, "stderr": "CGC_ERROR rid_echo_mismatch: the captcha usage note"}, None),
    ({"code": 2, "stderr": "Traceback (most recent call last):\nRuntimeError: rate_limit"}, None),
    ({"code": 2, "stderr": "CGC_ERROR model_not_selectable: wanted 'Pro'"}, "model_not_selectable"),
    ({"code": 1, "stderr": "CGC_ERROR cdp_attach_failed: opened a tab"}, "attach_failed"),
    ({"code": 2, "stderr": "CGC_ERROR new_tab_no_ws"}, "new_tab"),
    ({"code": 3, "stderr": "CGC_LOGIN needed\nCGC_ERROR login_needed: log in"}, "login_needed"),
    ({"code": 2, "stderr": "usage: cdp_consult.py [-h] --port PORT"}, "usage"),
])
def test_only_a_driver_written_line_proves_not_sent(env, res, want):
    """Re-review R1: a timeout's text (it embeds argv, so a mention named "Usage Tracker" appears)
    or a traceback must never prove a send did not happen."""
    _store_mod, backend, _s = env
    assert backend._not_sent_marker(res, backend._NOT_SENT_BLOCK + backend._NOT_SENT_RETRY) == want


def test_a_timed_out_continue_with_a_marker_word_in_its_argv_stays_uncertain(env):
    store_mod, backend, s = env
    cdp = _script(FAILED_WAIT, {"code": 124,
                                "stderr": "subprocess timeout: '--mention', 'Usage Tracker'"})
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro", "mentions": ["Usage Tracker"]}, cdp, may_continue=True)
    assert final == store_mod.POSSIBLY_ACCEPTED


def test_a_sibling_that_sends_while_the_continue_waits_for_the_lease_cancels_it(env, monkeypatch):
    """Re-review R2: the newer-round check is repeated once the lease is held."""
    store_mod, backend, s = env
    import cgc_spool as sp
    later = "REQ-20260925-121000-00000d"
    s.create_round(later, "followup", prompt="q", thread_id=CONV, parent_rid=RID,
                   spec_json=json.dumps({"model": "Pro"}))
    s.set_state(later, store_mod.READY)

    def lease(conv):          # the sibling takes the lease first and sends
        if s.get_round(later)["state"] == store_mod.READY:
            s.begin_send(later, "q", store_mod.sha256("q"), daemon_instance_id="d2")
            return None
        return object()
    monkeypatch.setattr(sp, "acquire_conversation_lease", lease)
    monkeypatch.setattr(backend.time, "sleep", lambda x: None)
    cdp = _script(FAILED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp, may_continue=True) == store_mod.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"]


def test_a_same_rid_resend_without_a_baseline_refuses_before_clicking(env, monkeypatch):
    """Copilot: if the last turn never renders, no baseline exists and a later hydration of the OLD
    same-rid turn would read as the new echo. Refuse pre-click instead."""
    import cdp_consult as c
    tab = _FollowupTab([_turn(RID)], lands=_turn(RID) + "\n")
    assert _run_followup(monkeypatch, tab, RID, blank_last=True) == c.EXIT_NOT_SENT_PRECLICK
    assert not tab.clicked


# ---- a follow-up's mention refusal must not leave its own tab behind, and must wait for the thread ----

def _run_followup_refusing(monkeypatch, tab, reopened, exc):
    """cmd_followup with _paste_prompt raising `exc`; `reopened` decides whether CDP() first says the
    tab is gone (so the command opens it itself)."""
    import types
    import cdp_consult as c

    def fake_cdp(port, match=None, create_url=None):
        if match is not None and reopened:
            raise SystemExit("CGC_ERROR conversation_not_found: no tab")
        return tab

    monkeypatch.setattr(c, "CDP", fake_cdp)
    monkeypatch.setattr(c, "_await_composer", lambda *a, **k: (True, {}))
    monkeypatch.setattr(c, "_await_thread_rendered", lambda *a, **k: True)
    monkeypatch.setattr(c, "_egress_gate", lambda p: (True, "ok"))
    monkeypatch.setattr(c, "_last_user_text_js", lambda: "LAST")
    monkeypatch.setattr(c, "_write_state", lambda **k: None)

    def refuse(*a, **k):
        raise exc
    monkeypatch.setattr(c, "_paste_prompt", refuse)
    orig = tab.eval
    tab.eval = lambda e, timeout=None: tab.turns[-1] if e == "LAST" else orig(e, timeout)
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    pf = os.path.join(os.environ["CGC_STATE_DIR"], "p2.md")
    open(pf, "w").write("Continuing.\nBEGIN_RESPONSE:%s\nx\nEND_RESPONSE:%s\n" % (RID, RID))
    ns = types.SimpleNamespace(no_gate=True, conversation=CONV, prompt_file=pf, task=None, rid=RID,
                               model="skip", model_family="skip", allow_model_mismatch=False,
                               port=9333, mention=["WebCodex Demo"], watch=False, out=None)
    return c.cmd_followup(ns)


def test_a_mention_refusal_closes_the_tab_the_followup_reopened(env, monkeypatch):
    import cdp_consult as c
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=None)
    closed = []
    tab.close_tab = lambda: closed.append(1)
    rc = _run_followup_refusing(monkeypatch, tab, reopened=True,
                                exc=c._MentionFailure("mention_not_found: not an app"))
    assert rc == 3 and closed == [1], "a tab this command opened must not outlive a refusal"


def test_a_mention_refusal_leaves_a_live_tab_it_did_not_open_alone(env, monkeypatch):
    import cdp_consult as c
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=None)
    closed = []
    tab.close_tab = lambda: closed.append(1)
    rc = _run_followup_refusing(monkeypatch, tab, reopened=False,
                                exc=c._MentionFailure("mention_not_found: not an app"))
    assert rc == 3 and closed == [], "a tab the user or another round holds is not ours to close"


def test_a_preclick_failure_closes_the_reopened_tab_too(env, monkeypatch):
    import cdp_consult as c
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=None)
    closed = []
    tab.close_tab = lambda: closed.append(1)
    rc = _run_followup_refusing(monkeypatch, tab, reopened=True, exc=c._PreClickFailure("paste truncated"))
    assert rc == c.EXIT_NOT_SENT_PRECLICK and closed == [1]


# ---- hydration is a precondition; tab ownership on EVERY exit ---------------------------------------

def _followup_env(monkeypatch, tab, reopened, hydrated, paste=None, select_raises=None,
                  mention=("WebCodex Demo",), select=None):
    """cmd_followup with the page's hydration forced, recording whether it pasted or clicked."""
    import types
    import cdp_consult as c
    rec = {"pasted": 0, "clicked": 0, "closed": 0, "mentions": []}

    def fake_cdp(port, match=None, create_url=None):
        if match is not None and reopened:
            raise SystemExit("CGC_ERROR conversation_not_found: no tab")
        return tab

    def do_paste(*a, **k):
        rec["pasted"] += 1
        rec["mentions"].append(list(a[2]) if len(a) > 2 else None)
        if paste is not None:
            raise paste

    monkeypatch.setattr(c, "CDP", fake_cdp)
    monkeypatch.setattr(c, "_await_composer", lambda *a, **k: (True, {}))
    monkeypatch.setattr(c, "_await_thread_rendered", lambda *a, **k: hydrated)
    monkeypatch.setattr(c, "_egress_gate", lambda p: (True, "ok"))
    monkeypatch.setattr(c, "_last_user_text_js", lambda: "LAST")
    monkeypatch.setattr(c, "_write_state", lambda **k: None)
    monkeypatch.setattr(c, "_paste_prompt", do_paste)
    if select_raises is not None:
        def boom(*a, **k):
            raise select_raises
        monkeypatch.setattr(c, "_select_model", boom)
    if select is not None:
        monkeypatch.setattr(c, "_select_model", select)
    orig = tab.eval

    def ev(e, timeout=None):
        if "return 'clicked'" in e:
            rec["clicked"] += 1
        return tab.turns[-1] if e == "LAST" else orig(e, timeout)
    tab.eval = ev
    tab.close_tab = lambda: rec.__setitem__("closed", rec["closed"] + 1)
    tab.rec = rec
    clock = {"now": 0.0}
    monkeypatch.setattr(c.time, "time", lambda: clock.__setitem__("now", clock["now"] + 1) or clock["now"])
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    pf = os.path.join(os.environ["CGC_STATE_DIR"], "p3.md")
    open(pf, "w").write("Continuing.\nBEGIN_RESPONSE:%s\nx\nEND_RESPONSE:%s\n" % (RID, RID))
    ns = types.SimpleNamespace(no_gate=True, conversation=CONV, prompt_file=pf, task=None, rid=RID,
                               model="Pro" if (select_raises or select) else "skip", model_family="skip",
                               allow_model_mismatch=False, port=9333, mention=list(mention),
                               watch=False, out=None)
    return c.cmd_followup(ns), rec


def test_a_thread_that_never_renders_is_refused_before_any_paste_or_click(env, monkeypatch, capsys):
    """The page shows no user turn on a thread that has at least one: not hydrated. A stale same-rid
    turn appearing after the click would read as a fresh send, so nothing is pasted or clicked."""
    import cdp_consult as c
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=_turn(RID))
    rc, rec = _followup_env(monkeypatch, tab, reopened=True, hydrated=False)
    assert rc == c.EXIT_NOT_SENT_PRECLICK
    assert rec["pasted"] == 0 and rec["clicked"] == 0, "not one keystroke or click before hydration"
    assert rec["closed"] == 1, "the tab this command re-opened must not be leaked"
    assert "never rendered" in capsys.readouterr().err


def test_an_unhydrated_live_tab_is_refused_but_never_closed(env, monkeypatch):
    import cdp_consult as c
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=None)
    rc, rec = _followup_env(monkeypatch, tab, reopened=False, hydrated=False)
    assert rc == c.EXIT_NOT_SENT_PRECLICK and rec["pasted"] == 0 and rec["clicked"] == 0
    assert rec["closed"] == 0, "a tab we attached to is the user's or another round's, never ours to close"


def test_a_reopened_tab_is_closed_when_a_pre_click_step_raises(env, monkeypatch):
    """The cleanup owner is the finally block, so an exception (not just a return) cannot leak it."""
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=None)
    with pytest.raises(RuntimeError):
        _followup_env(monkeypatch, tab, reopened=True, hydrated=True, select_raises=RuntimeError("boom"))
    assert tab.rec["closed"] == 1 and tab.rec["clicked"] == 0, \
        "an exception before the click must still close the tab this command re-opened"


def _labelled_tab(reads):
    """A follow-up tab whose switcher labels (_cand_labels_js) come from `reads`, the last repeating."""
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=_turn(RID))
    orig, now = tab.eval, {"v": []}

    def ev(e, timeout=None):
        if ".map(function(b){" in e:
            now["v"] = next(reads, now["v"])
            return now["v"]
        return orig(e, timeout)
    tab.eval, tab.labels = ev, now
    return tab


def test_a_reopened_followup_picks_the_model_only_once_the_switchers_settle(env, monkeypatch):
    """Live 2026-09-30: a re-opened thread renders the mode toggle ['Explore'] first and the tier
    switcher ~2.5 s later. Releasing on any label handed _select_model a composer with no tier picker
    to open; it must see the full label set, unchanged across two reads."""
    import cdp_consult as c
    tab = _labelled_tab(iter([[], ["Explore"], ["Explore"], ["Explore", "Pro"], ["Explore", "Pro"]]))
    seen = []

    def pick(_c, target, family=None):
        seen.append(list(tab.labels["v"]))
        return c._ModelPick(True, "Pro", "Pro", None, False)
    rc, rec = _followup_env(monkeypatch, tab, reopened=True, hydrated=True, mention=[], select=pick)
    assert seen == [["Explore", "Pro"]], seen
    assert rc == 0 and rec["clicked"] == 1


def test_a_reopened_followup_whose_switchers_never_settle_still_reaches_the_model_gate(env, monkeypatch):
    """The settle is bounded: a composer that never shows two stable labels still gets one
    _select_model verdict, and a refusal there still sends nothing."""
    import cdp_consult as c
    tab = _labelled_tab(iter([["Explore"]]))
    calls = []

    def pick(_c, target, family=None):
        calls.append(target)
        return c._ModelPick(False, "Explore", None, "no tier picker", False)
    rc, rec = _followup_env(monkeypatch, tab, reopened=True, hydrated=True, mention=[], select=pick)
    assert calls == ["Pro"] and rc == 2
    assert rec["pasted"] == 0 and rec["clicked"] == 0


class _SleepClock:
    """Only sleep moves time, so a settle window is measured in the seconds it really waits."""

    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _settle_on(monkeypatch, timeline):
    """Run _settled_labels against labels that change at given virtual seconds:
    timeline = [(from_second, labels), ...]. Returns (labels returned, seconds spent)."""
    import types
    import cdp_consult as c
    clock = _SleepClock()
    monkeypatch.setattr(c, "time", clock)

    def ev(_e):
        return [lab for t0, lab in timeline if clock.t >= t0][-1]
    got = c._settled_labels(types.SimpleNamespace(eval=ev))
    return got, clock.t


def test_a_single_stable_switcher_settles_at_the_hold_not_the_bound(env, monkeypatch):
    """A build with one switcher (the ['6'] badge) must not pay the full 20 s on every re-open."""
    got, spent = _settle_on(monkeypatch, [(0, ["6"])])
    assert got == ["6"] and 4 <= spent < 5


def test_a_tier_switcher_that_hydrates_late_is_waited_for(env, monkeypatch):
    """The live shape: ['Explore'] alone, then 'Pro' ~2.5 s later. Two equal reads 1 s apart would
    release on ['Explore']; the hold outlasts the gap."""
    got, spent = _settle_on(monkeypatch, [(0, []), (0.5, ["Explore"]), (3.0, ["Explore", "Pro"])])
    assert got == ["Explore", "Pro"] and spent < 8


def test_labels_that_never_settle_stop_at_the_bound(env, monkeypatch):
    got, spent = _settle_on(monkeypatch, [(t, ["Pro"] if int(t) % 2 else ["6"]) for t in range(40)])
    assert 20 <= spent < 21


def test_a_composer_that_never_shows_a_switcher_stops_at_the_bound(env, monkeypatch):
    got, spent = _settle_on(monkeypatch, [(0, [])])
    assert got == [] and 20 <= spent < 21


def test_a_reopened_tab_stays_open_once_the_click_was_attempted(env, monkeypatch):
    """After the click nothing is provably unsent: the tab is the evidence, so it is not closed even
    though the landed check then fails."""
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=None)   # click lands nothing
    rc, rec = _followup_env(monkeypatch, tab, reopened=True, hydrated=True)
    assert rec["clicked"] == 1 and rc != 0, "the click was made and the send never confirmed"
    assert rec["closed"] == 0, "an uncertain post-click tab is kept, not closed"


def test_a_reopened_tab_is_closed_on_a_pre_click_mention_refusal(env, monkeypatch):
    import cdp_consult as c
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=None)
    rc, rec = _followup_env(monkeypatch, tab, reopened=True, hydrated=True,
                            paste=c._MentionFailure("mention_not_found: not an app"))
    assert rc == 3 and rec["clicked"] == 0 and rec["closed"] == 1


def test_a_followup_without_a_mention_is_a_first_class_path(env, monkeypatch):
    """The first round of a thread carries the app; a follow-up needs no mention and must not touch the
    popup. cmd_followup hands the paste an EMPTY mention list, so _insert_mention is never reached."""
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=_turn(RID))
    rc, rec = _followup_env(monkeypatch, tab, reopened=False, hydrated=True, mention=[])
    assert rec["pasted"] == 1 and rec["mentions"] == [[]], "pasted with no mention to insert"
    assert rec["clicked"] == 1 and rc == 0, "and it sends normally"


# ---- the send verdict at BOTH click sites: nothing but clicked/no-send may go on, Enter only for no-send ----
# (an unknown reply is not a refusal: tests/test_v06_safety.py pins that it is never exit 6)

@pytest.mark.parametrize("verdict", ["no-composer", "ambiguous", "disabled", "no-focus", "error-preclick:x"])
def test_followup_never_presses_enter_or_sends_on_a_refused_send_target(env, monkeypatch, verdict):
    import cdp_consult as c
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=_turn(RID), send=verdict)
    rc, rec = _followup_env(monkeypatch, tab, reopened=True, hydrated=True)
    assert rc == c.EXIT_NOT_SENT_PRECLICK, verdict
    assert tab.keys == [] and tab.clicked is False, "no Enter, no click"
    assert rec["closed"] == 1, "the tab this command re-opened is closed: nothing was sent"


def test_followup_presses_enter_only_when_the_composer_has_no_send_button_and_holds_focus(env, monkeypatch):
    tab = _FollowupTab([_turn("REQ-20260925-110000-0000ff")], lands=None, send="no-send")
    _followup_env(monkeypatch, tab, reopened=False, hydrated=True)
    assert tab.keys == [("Enter", "Enter", 13)], "the pre-existing fallback, for 'no-send' only"


def _run_submit(monkeypatch, tab, reuse_tab=False, mention=(), paste=None):
    """cmd_submit with the browser faked: attach, composer, model and paste stubbed, the send verdict real."""
    import types
    import cdp_consult as c
    made = {}

    def fake_cdp(port, match=None, create_url=None):
        made["create_url"] = create_url
        return tab

    monkeypatch.setattr(c, "CDP", fake_cdp)
    monkeypatch.setattr(c, "_await_composer", lambda *a, **k: (True, {}))
    monkeypatch.setattr(c, "_egress_gate", lambda p: (True, "ok"))
    monkeypatch.setattr(c, "_has_sendable_source", lambda p: True)
    monkeypatch.setattr(c, "_ensure_chrome", lambda port: None)
    monkeypatch.setattr(c, "_write_state", lambda **k: None)

    def do_paste(*a, **k):
        if paste is not None:
            raise paste
    monkeypatch.setattr(c, "_paste_prompt", do_paste)
    tab.call = lambda *a, **k: {}
    clock = {"now": 0.0}     # a virtual clock: the post-click waits run out instantly on the fake tab
    monkeypatch.setattr(c.time, "time", lambda: clock.__setitem__("now", clock["now"] + 1) or clock["now"])
    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    pf = os.path.join(os.environ["CGC_STATE_DIR"], "sub.md")
    open(pf, "w").write("Review https://github.com/acme/w\nBEGIN_RESPONSE:%s\nx\nEND_RESPONSE:%s\n" % (RID, RID))
    ns = types.SimpleNamespace(rid=RID, prompt_file=pf, project_url="https://chatgpt.com/", model="skip",
                               model_family="skip", allow_model_mismatch=False, reuse_tab=reuse_tab,
                               no_gate=True, port=9333, mention=list(mention), watch=False, out=None)
    return c.cmd_submit(ns)


@pytest.mark.parametrize("verdict", ["no-composer", "ambiguous", "disabled", "no-focus", "error-preclick:x"])
def test_submit_never_presses_enter_or_sends_on_a_refused_send_target(env, monkeypatch, verdict):
    import cdp_consult as c
    tab = _FollowupTab([], lands=None, send=verdict)
    assert _run_submit(monkeypatch, tab) == c.EXIT_NOT_SENT_PRECLICK, verdict
    assert tab.keys == [] and tab.clicked is False
    assert tab.tab_closed == 1, "the tab submit opened is closed: nothing was sent"


def test_submit_presses_enter_only_for_no_send(env, monkeypatch):
    tab = _FollowupTab([], lands=None, send="no-send")
    _run_submit(monkeypatch, tab)
    assert tab.keys == [("Enter", "Enter", 13)]


def test_submit_never_closes_a_tab_it_did_not_open_on_any_pre_click_exit(env, monkeypatch):
    """--reuse-tab attaches to a tab that was already there: a refusal before the click must leave it."""
    import cdp_consult as c
    for verdict in ("no-composer", "no-focus"):
        tab = _FollowupTab([], lands=None, send=verdict)
        assert _run_submit(monkeypatch, tab, reuse_tab=True) == c.EXIT_NOT_SENT_PRECLICK
        assert tab.tab_closed == 0, verdict
    tab = _FollowupTab([], lands=None)
    assert _run_submit(monkeypatch, tab, reuse_tab=True, paste=c._MentionFailure("mention_not_found")) == 3
    assert tab.tab_closed == 0
    tab = _FollowupTab([], lands=None)
    assert _run_submit(monkeypatch, tab, reuse_tab=True, paste=c._PreClickFailure("truncated")) == c.EXIT_NOT_SENT_PRECLICK
    assert tab.tab_closed == 0


def test_submit_closes_the_tab_it_opened_on_a_pre_click_refusal_and_keeps_none_after_a_click(env, monkeypatch):
    import cdp_consult as c
    tab = _FollowupTab([], lands=None)
    assert _run_submit(monkeypatch, tab, paste=c._MentionFailure("mention_not_found")) == 3
    assert tab.tab_closed == 1
    tab = _FollowupTab([], lands=None)
    assert _run_submit(monkeypatch, tab, paste=c._PreClickFailure("truncated")) == c.EXIT_NOT_SENT_PRECLICK
    assert tab.tab_closed == 1
