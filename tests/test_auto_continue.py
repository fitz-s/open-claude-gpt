# When ChatGPT marks a round's turn failed ("Thinking failed"), the worker sends ONE "continue" in the
# same conversation, carrying the round's own rid, and the same round then reads its answer from the
# newer turn. At most one continue per round, never for a retrieve (which never sends), and a second
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
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp)
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
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp)
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
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp)
    assert final == store_mod.POSSIBLY_ACCEPTED
    assert [k for k, _ in cdp.calls] == ["wait"], "the continue was already spent — nothing sent"


def test_a_recorded_not_sent_continue_ends_failed_on_the_next_wait(env):
    store_mod, backend, s = env
    assert s.claim_auto_continue(RID, "x")
    s.record_auto_continue(RID, "not_sent")
    cdp = _script(FAILED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp) == store_mod.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"]


def test_the_continue_goes_through_the_egress_gate(env, monkeypatch):
    store_mod, backend, s = env
    import cgc_spool as sp
    monkeypatch.setattr(sp, "validate_prompt", lambda p: (False, "refused: test"))
    cdp = _script(FAILED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp) == store_mod.FAILED
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
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp) == store_mod.COMPLETED_VERIFIED


def test_a_continue_provably_not_sent_ends_failed_and_is_not_retried(env):
    store_mod, backend, s = env
    cdp = _script(FAILED_WAIT, {"code": 2, "stderr": "CGC_ERROR model_not_selectable"})
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp)
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
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp)
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
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp)
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

    def __init__(self, turns, lands):
        self.turns, self.lands, self.clicked = list(turns), lands, False

    def eval(self, expr, timeout=None):
        if ".length" in expr and "querySelectorAll" in expr and "function" not in expr:
            return len(self.turns)
        if "b.click()" in expr:
            self.clicked = True
            if self.lands:
                self.turns.append(self.lands)
            return True
        if "location.pathname" in expr:
            return "/c/" + CONV
        return None

    def call(self, *a, **k):
        return {}

    def key(self, *a):
        pass

    def conversation_id(self):
        return CONV

    def close(self):
        pass


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
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp) == store_mod.COMPLETED_VERIFIED


def test_no_continue_when_another_round_was_sent_into_the_thread_after(env):
    """Review S3: 'answer that same request' would refer to the newer round's prompt."""
    store_mod, backend, s = env
    later = "REQ-20260925-121000-00000c"
    s.create_round(later, "followup", prompt="q", thread_id=CONV, parent_rid=RID,
                   spec_json=json.dumps({"model": "Pro"}))
    s.set_state(later, store_mod.READY)
    s.begin_send(later, "q", store_mod.sha256("q"), daemon_instance_id="d1")
    cdp = _script(FAILED_WAIT)
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp) == store_mod.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"]
    assert "no automatic continue went out" in s.get_round(RID)["error_code"]


def test_the_second_wait_gets_what_is_left_of_the_budget(env):
    store_mod, backend, s = env
    cdp = _script(FAILED_WAIT, {"code": 0}, {"answer": "a"})
    backend._wait_phase(s, RID, CONV, {"model": "Pro", "timeout": 5400}, cdp)
    second = [kw for k, kw in cdp.calls if k == "wait"][1]
    assert 600 <= second["timeout"] <= 5400


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
    final = backend._wait_phase(s, RID, CONV, {"model": "Pro", "mentions": ["Usage Tracker"]}, cdp)
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
    assert backend._wait_phase(s, RID, CONV, {"model": "Pro"}, cdp) == store_mod.FAILED
    assert [k for k, _ in cdp.calls] == ["wait"]


def test_a_same_rid_resend_without_a_baseline_refuses_before_clicking(env, monkeypatch):
    """Copilot: if the last turn never renders, no baseline exists and a later hydration of the OLD
    same-rid turn would read as the new echo. Refuse pre-click instead."""
    import cdp_consult as c
    tab = _FollowupTab([_turn(RID)], lands=_turn(RID) + "\n")
    assert _run_followup(monkeypatch, tab, RID, blank_last=True) == c.EXIT_NOT_SENT_PRECLICK
    assert not tab.clicked
