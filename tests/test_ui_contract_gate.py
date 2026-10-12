# The UI-contract gate: ChatGPT's web UI changed four times in three weeks, and each change was found
# one paid round at a time. A no-send probe now checks the composer, the @mention picker and the
# reader, records a verdict per capability in `meta`, and the daemon claims no send that needs a
# capability recorded broken. These pin the guarantees offline: fake tabs, fake DOM, no browser.
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)

RID = "REQ-20261011-120000-0000a1"
RID2 = "REQ-20261011-120000-0000a2"
CONV = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CGC_SPOOL_DIR", str(tmp_path / "spool"))
    store_mod = _load("cgc_store")
    spool = _load("cgc_spool")
    backend = _load("cgc_backend")
    cdp = _load("cdp_consult")
    probe = _load("cgc_probe")
    s = store_mod.Store()
    yield argparse.Namespace(store_mod=store_mod, spool=spool, backend=backend, cdp=cdp, probe=probe,
                             s=s, tmp=tmp_path)
    s.close()


def _queue(e, rid, *, kind="submit", mentions=(), prompt="p"):
    e.s.create_round(rid, kind, prompt=prompt,
                     spec_json=json.dumps({"model": "Pro", "model_family": "GPT-6",
                                           "mentions": list(mentions)}))


# ---- the probe never sends ------------------------------------------------------------------

class _Composer:
    """A fresh project tab, from the probe's side: answers the JS the composer + mention probes
    evaluate. Records every key and every Input.* call, so a send attempt is visible."""

    def __init__(self, app="WebCodex Demo", popup=True):
        self.text, self.mentions, self.pending = "", [], []
        self.app, self.popup = app.lower(), popup
        self.keys, self.calls, self.closed = [], [], 0

    def eval(self, js, timeout=None):
        if "loginWall" in js:
            return json.dumps({"composer": True, "onChatGPT": True, "readyState": "complete"})
        if "readyState" in js:
            return True
        if "selectAll" in js:
            self.text, self.mentions = "", []
            return 0
        if "app-mention-name" in js:
            self.mentions, self.pending = self.mentions + self.pending, []
            return list(self.mentions)
        if "var want=" in js:
            want = json.loads(js.split("var want=", 1)[1].split(";", 1)[0])
            if self.popup and want == self.app:
                self.pending.append(want)
                return {"hit": want, "open": True, "seen": [], "unconnected": [], "unverified": []}
            return {"hit": None, "open": self.popup, "loading": False, "seen": [], "unconnected": [],
                    "unverified": []}
        if "execCommand('insertText'" in js:
            if "return true" in js:
                return True
            self.text += json.loads(js.split("execCommand('insertText',false,", 1)[1].rsplit(");", 1)[0])
            return len(self.text)
        if "return d?(d.innerText" in js:
            return self.text
        raise AssertionError("unexpected js: " + js[:80])

    def key(self, *a):
        self.keys.append(a)

    def call(self, method, params=None, timeout=None):
        self.calls.append(method)
        return {}

    def close_tab(self):
        self.closed += 1

    def close(self):
        pass


def _rig_no_send(monkeypatch, cdp):
    """Every way the driver sends, rigged to fail the test if it is ever reached."""
    def boom(*a, **k):
        raise AssertionError("the probe reached the send path")
    for name in ("_send_click_js", "_send_verdict", "_await_contract", "cmd_submit", "cmd_followup",
                 "_poll_conversation", "_write_state"):
        monkeypatch.setattr(cdp, name, boom)


def test_the_composer_and_mention_probes_never_send(env, monkeypatch):
    cdp = env.cdp
    _rig_no_send(monkeypatch, cdp)
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    picked = []
    monkeypatch.setattr(cdp, "_select_model", lambda c, t, f: picked.append((t, f)) or
                        cdp._ModelPick(True, t, "6", None, False))
    tab = _Composer()
    got = env.probe.probe_composer(env.probe._NoSend(tab), model="Pro", family="GPT-6",
                                   app="WebCodex Demo")
    assert got["composer"][0] == "pass" and got["mention"][0] == "pass"
    assert picked == [("Pro", "GPT-6")], "the configured family and tier are selected"
    assert len(env.probe.DUMMY) >= 15_000
    assert tab.keys == [] and tab.calls == [], "no key and no Input.* event reached the tab"
    assert tab.text == "" and tab.mentions == [], "the composer is left empty"


def test_the_no_send_handle_refuses_every_send_shape(env):
    t = env.probe._NoSend(_Composer())
    for js in (env.cdp._send_click_js(), "document.querySelector('form').requestSubmit()"):
        with pytest.raises(env.probe.SendRefused):
            t.eval(js)
    with pytest.raises(env.probe.SendRefused):
        t.key("Enter", "Enter", 13)
    with pytest.raises(env.probe.SendRefused):
        t.call("Input.dispatchKeyEvent", {"type": "keyDown"})
    t.key("Escape", "Escape", 27)        # the picker's own keys still work


def test_the_probe_module_never_names_the_send_path():
    src = open(os.path.join(SCRIPTS, "cgc_probe.py"), encoding="utf-8").read()
    for name in ("_send_click_js", "_send_verdict", "cmd_submit", "cmd_followup", "_await_contract"):
        assert name not in src, name


def test_a_run_opens_and_closes_only_its_own_tab(env, monkeypatch):
    cdp, probe = env.cdp, env.probe
    _rig_no_send(monkeypatch, cdp)
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cdp, "_select_model", lambda c, t, f: cdp._ModelPick(True, t, "6", None, False))
    tabs = []

    def fake_cdp(port, match=None, create_url=None):
        assert create_url and match is None, "a probe only ever opens its own new tab"
        tabs.append(_Composer())
        return tabs[-1]
    monkeypatch.setattr(cdp, "CDP", fake_cdp)
    monkeypatch.setenv("CGC_APPS", "")
    rep = probe.run_probe("composer")
    assert rep["composer"]["verdict"] == "pass" and rep["composer"]["recorded"]
    assert [t.closed for t in tabs] == [1]


# ---- the reader probe uses the waiter's reads ---------------------------------------------------

def _dom():
    return _load_dom()


def _load_dom():
    spec = importlib.util.spec_from_file_location(
        "test_search_unit_schema", os.path.join(ROOT, "tests", "test_search_unit_schema.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m._DOM


class _NodeTab:
    """A read-only tab whose eval runs the driver's real JS in node against a fixed page."""

    def __init__(self, page):
        if not shutil.which("node"):
            pytest.skip("node not installed")
        self.page, self.calls = page, []

    def eval(self, js, timeout=None):
        if "scrollTop" in js:
            return 1
        src = _load_dom() + self.page + "\nconsole.log(JSON.stringify(" + js + "));"
        out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def call(self, method, params=None, timeout=None):
        self.calls.append(method)
        return {}


def _collapsed_page(rid, *, answered=True, head_marker=True):
    """A finished long turn ChatGPT collapsed: the DOM keeps the head (the CGC_RID marker) and a
    Show more button, never the tail pair; the answer below it is wrapped."""
    head = ("P('CGC_RID:%s')," % rid if head_marker else "") + "P('# Review'),P('the head…')"
    answer = (",unit('t1:2:assistant',[md(['BEGIN_RESPONSE:%s','the verdict','END_RESPONSE:%s'])])"
              % (rid, rid)) if answered else ""
    return ("var U=unit('t1:0:user',[%s,E('BUTTON',{'aria-expanded':'false'},[],'Show more')]);"
            "var ROOT=E('MAIN',{},[E('DIV',{'data-turn-key':'k1'},[U%s])]);"
            "var document=ROOT;document.body=ROOT;var location={pathname:'/c/x'};" % (head, answer))


def test_the_reader_probe_reads_a_collapsed_marker_turn(env, monkeypatch):
    monkeypatch.setattr(env.cdp.time, "sleep", lambda s: None)
    tab = _NodeTab(_collapsed_page(RID))
    n = env.probe.probe_reader(env.probe._NoSend(tab), RID)
    assert n == len("the verdict")
    assert tab.calls == [], "read-only: no reload was needed and nothing was dispatched"


def test_a_page_that_hides_its_turns_from_the_reader_is_drift(env, monkeypatch):
    monkeypatch.setattr(env.cdp.time, "sleep", lambda s: None)
    monkeypatch.setattr(env.cdp, "STALE_RELOAD_SETTLE_S", 0)
    # The rid is visibly on the page, but in a user turn no turn selector reads any more.
    # (The mock DOM matches attribute selectors only, so `main` is answered by hand.)
    page = ("var ROOT=E('MAIN',{},[E('DIV',{'data-new-turn':'user'},[P('CGC_RID:%s'),P('ask')])]);"
            "var q=ROOT.querySelector;ROOT.querySelector=function(s){return s==='main'?ROOT:q.call(ROOT,s)};"
            "var document=ROOT;document.body=ROOT;var location={pathname:'/c/x'};" % RID)
    tab = _NodeTab(page)
    monkeypatch.setattr(env.cdp, "_await_thread_rendered", lambda c, seconds=0: True)
    with pytest.raises(env.probe.Drift) as d:
        env.probe.probe_reader(env.probe._NoSend(tab), RID)
    assert d.value.cap == "reader" and d.value.phase == "rid"


def test_a_page_without_the_conversation_is_inconclusive_not_drift(env, monkeypatch):
    monkeypatch.setattr(env.cdp.time, "sleep", lambda s: None)
    monkeypatch.setattr(env.cdp, "_await_thread_rendered", lambda c, seconds=0: False)
    tab = _NodeTab("var ROOT=E('MAIN',{},[]);var document=ROOT;document.body=ROOT;"
                   "var location={pathname:'/c/x'};")
    with pytest.raises(env.probe.Transient):
        env.probe.probe_reader(env.probe._NoSend(tab), RID)


def test_the_reader_round_prefers_a_long_marker_prompt_and_takes_a_short_legacy_one(env):
    store_mod, s = env.store_mod, env.s

    def done(rid, prompt, conv):
        s.create_round(rid, "submit", prompt=prompt)
        s.set_state(rid, store_mod.READY)
        aid = s.begin_send(rid, prompt, store_mod.sha256(prompt), daemon_instance_id="d")
        s.mark_accepted(aid, conv)
        s.mark_waiting(rid)
        s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="a")

    pair = lambda r: "\nBEGIN_RESPONSE:%s\n<a>\nEND_RESPONSE:%s\n" % (r, r)  # noqa: E731
    legacy = "REQ-20261011-120000-0000b1"
    done(legacy, "# old\nshort" + pair(legacy), "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
    assert env.probe.pick_reader_round(s)["kind"] == "legacy", "no marker round yet: a short legacy one"
    long_legacy = "REQ-20261011-120000-0000b2"
    done(long_legacy, "# old\n" + "x" * 20_000 + pair(long_legacy), "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    marker = "REQ-20261011-120000-0000b3"
    done(marker, "CGC_RID:%s\n\n# new\n%s" % (marker, "y" * 13_000) + pair(marker),
         "dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    got = env.probe.pick_reader_round(s)
    assert got["rid"] == marker and got["kind"] == "marker"


# ---- capability state -----------------------------------------------------------------------

def test_a_stale_pass_cannot_clear_a_newer_failure(env):
    s = env.s
    s.mark_broken("reader", phase="probe:rid", cause="old break")
    started = s.capability_epoch()                       # a probe starts AFTER that break …
    s.mark_broken("reader", phase="send:contract", cause="a real round failed mid-probe")
    assert s.mark_passed("reader", started_epoch=started, phase="probe") is False
    assert s.capabilities()["reader"]["ok"] is False, "the newer failure stands"
    later = s.capability_epoch()                         # … a probe started after it may clear it
    assert s.mark_passed("reader", started_epoch=later, phase="probe") is True
    assert s.capabilities()["reader"]["ok"] is True


def test_capability_records_carry_the_named_fields(env):
    rec = env.s.mark_broken("composer", phase="probe:model", cause="Latest not offered",
                            ref=RID, driver_rev="abc")
    assert set(rec) == {"ok", "epoch", "phase", "cause", "rid_or_conversation", "checked_at",
                        "driver_rev"}
    assert env.s.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'ui.%'").fetchone()[0] == 2, \
        "one key per capability plus the epoch counter, all in meta"


def test_transient_errors_leave_state_unchanged(env, monkeypatch):
    cdp, probe, s = env.cdp, env.probe, env.s
    s.mark_broken("composer", phase="probe:paste", cause="earlier break")
    before = s.capabilities()
    opened = []

    def dead(port, match=None, create_url=None):
        opened.append(create_url)
        raise SystemExit("CGC_ERROR cdp_attach_failed: opened a tab but it never answered")
    monkeypatch.setattr(cdp, "CDP", dead)
    monkeypatch.setenv("CGC_APPS", "")
    rep = probe.run_probe()
    assert {v["verdict"] for v in rep.values()} <= {"transient", "unestablished"}
    assert s.capabilities() == before, "an inconclusive probe writes nothing"
    assert len(opened) == 2, "retried once, then reported inconclusive"


def test_a_confirmed_break_is_recorded_after_its_retry(env, monkeypatch):
    cdp, probe = env.cdp, env.probe
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cdp, "_select_model", lambda c, t, f: cdp._ModelPick(
        False, "Pro", "6", "model family 'GPT-6' is not offered (the picker lists: 'GPT-7')", False))
    tabs = []
    monkeypatch.setattr(cdp, "CDP", lambda port, match=None, create_url=None: tabs.append(_Composer())
                        or tabs[-1])
    monkeypatch.setenv("CGC_APPS", "")
    rep = probe.run_probe("composer")
    assert rep["composer"]["verdict"] == "broken" and len(tabs) == 2
    cap = env.s.capabilities()["composer"]
    assert cap["ok"] is False and cap["phase"] == "probe:model" and "GPT-6" in cap["cause"]


# ---- the gate ---------------------------------------------------------------------------------

def test_a_broken_mention_gates_only_mention_rounds(env):
    s = env.s
    _queue(env, RID, mentions=["WebCodex Demo"])
    _queue(env, RID2)
    s.mark_broken("mention", phase="probe:mention", cause="the @ popup never opened")
    got = s.claim_ready("d1")
    assert got["rid"] == RID2, "the plain round is claimed past the gated one"
    assert s.claim_ready("d1") is None, "the mention round stays queued"
    assert s.get_round(RID)["state"] == env.store_mod.QUEUED
    assert s.gated_by(s.get_round(RID)).keys() == {"mention"}


def test_a_broken_reader_holds_every_send_and_a_pass_releases_it(env):
    s = env.s
    _queue(env, RID)
    s.mark_broken("reader", phase="probe:rid", cause="no turn selector reads the page")
    assert s.claim_ready("d1") is None
    assert s.mark_passed("reader", started_epoch=s.capability_epoch(), phase="probe")
    assert s.claim_ready("d1")["rid"] == RID


def test_retrieve_is_never_gated(env):
    s = env.s
    for cap in env.store_mod.CAPABILITIES:
        s.mark_broken(cap, phase="probe", cause="everything broke")
    s.create_round(RID, "retrieve", spec_json=json.dumps({"conversation": CONV, "parent_rid": RID2}))
    assert env.store_mod.round_needs("retrieve", None) == ()
    assert s.claim_ready("d1")["rid"] == RID


def test_a_gated_rounds_await_names_the_pause(env, monkeypatch, capsys):
    backend, s = env.backend, env.s
    _queue(env, RID, mentions=["WebCodex Demo"])
    s.mark_broken("mention", phase="probe:mention", cause="the @ popup never opened")
    monkeypatch.setattr(env.spool, "daemon_alive", lambda: True)
    monkeypatch.setattr(backend, "GATE_GRACE_S", 0)
    code = backend.await_round(argparse.Namespace(rid=RID, out=str(env.tmp / "a.txt"), poll=0.01,
                                                  timeout=5))
    env_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 3 and env_json["state"] == "queued" and env_json["retryable"] is True
    assert env_json["human_action"].startswith("ChatGPT UI changed (mention: the @ popup never opened)")
    assert "queued rounds are intact" in env_json["human_action"]
    assert s.get_round(RID)["state"] == env.store_mod.QUEUED, "await never moves the round"


# ---- real rounds: typed outcomes, marking, and no resend ----------------------------------------

def _ready(e, rid, **kw):
    _queue(e, rid, **kw)
    e.s.set_state(rid, e.store_mod.READY)
    return e.s.get_round(rid)


def test_a_real_rounds_deterministic_preclick_drift_marks_the_capability(env):
    r = _ready(env, RID, mentions=["WebCodex Demo"])
    res = {"code": 3, "conversation": "", "outcome": {"ok": False, "submitted": False,
           "reason": "mention_not_found", "phase": "mention", "drift": "mention"},
           "stderr": "CGC_ERROR mention_not_found: The @ popup never opened — NOT submitted.\n"}
    final = env.backend.process_round(env.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                                      validate=lambda p: (True, "ok"))
    assert final == env.store_mod.BLOCKED
    cap = env.s.capabilities()["mention"]
    assert cap["ok"] is False and cap["phase"] == "send:mention" and cap["rid_or_conversation"] == RID


@pytest.mark.parametrize("marker", ["login_needed", "captcha", "rate_limit"])
def test_login_captcha_and_rate_limit_never_mark_a_capability(env, marker):
    r = _ready(env, RID)
    res = {"code": 3, "conversation": "", "outcome": {"ok": False, "submitted": False,
           "reason": marker, "phase": "composer"},
           "stderr": f"CGC_ERROR {marker}: a human must act\n"}
    env.backend.process_round(env.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                              validate=lambda p: (True, "ok"))
    assert env.s.capabilities() == {}


def test_a_model_refusal_for_a_stale_frozen_family_marks_nothing(env, monkeypatch):
    monkeypatch.setenv("CGC_MODEL_FAMILY", "GPT-6")
    env.s.create_round(RID, "submit", prompt="p",
                       spec_json=json.dumps({"model": "Pro", "model_family": "Latest"}))
    env.s.set_state(RID, env.store_mod.READY)
    res = {"code": 3, "conversation": "", "outcome": {"submitted": False, "reason": "model_not_selectable",
           "phase": "model", "drift": "composer"},
           "stderr": "CGC_ERROR model_not_selectable: model family 'Latest' is not offered\n"}
    env.backend.process_round(env.s, env.s.get_round(RID), lambda kind, **kw: res,
                              daemon_instance_id="d", validate=lambda p: (True, "ok"))
    assert env.s.capabilities() == {}, "the round's own stale spec is not a UI change"


def test_composer_drift_needs_a_loaded_fresh_tab(env):
    out = env.cdp._composer_outcome
    loaded = {"onChatGPT": True, "readyState": "complete"}
    assert out(loaded, fresh=True)["drift"] == "composer"
    assert "drift" not in out(loaded, fresh=False), "a re-opened thread may simply be gone"
    assert "drift" not in out({"onChatGPT": True, "readyState": "loading"}, fresh=True)
    assert "drift" not in out({"loginWall": True, "readyState": "complete"}, fresh=True)


def test_an_uncertain_send_never_auto_resends_and_keeps_its_typed_outcome(env, monkeypatch, capsys):
    backend, s, store_mod = env.backend, env.s, env.store_mod
    r = _ready(env, RID)
    calls = []
    contract = {"families": [{"adapter": "search-unit-v1", "ridLanded": True, "users": 1,
                              "assistants": 0}], "generating": False}
    res = {"code": 1, "conversation": CONV,
           "outcome": {"ok": False, "submitted": True, "rid": RID, "phase": "contract",
                       "drift": "reader", "reason": "selector_drift", "adapter": "search-unit-v1",
                       "contract": contract, "conversation_id": CONV},
           "stderr": "CGC_ERROR selector_drift: the prompt WAS sent\n"}
    final = backend.process_round(s, r, lambda kind, **kw: calls.append(kind) or res,
                                  daemon_instance_id="d", validate=lambda p: (True, "ok"))
    assert final == store_mod.POSSIBLY_ACCEPTED and calls == ["submit"]
    rr = s.get_round(RID)
    assert "selector_drift: submitted=true" in rr["error_code"]
    assert s.conversation_of(RID) == CONV, "the conversation id is kept for a read-only retrieve"
    assert s.capabilities()["reader"]["ok"] is False
    # It is never claimable again: recovery only offers it as a read-only retrieve.
    assert s.claim_ready("d") is None
    assert RID not in s.recover()["dispatchable"]
    monkeypatch.setattr(env.spool, "daemon_alive", lambda: True)
    backend.await_round(argparse.Namespace(rid=RID, out=str(env.tmp / "a.txt"), poll=0.01, timeout=2))
    env_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env_json["state"] == "possibly_accepted" and env_json["retryable"] is False
    assert env_json["send"]["submitted"] is True and env_json["send"]["reason"] == "selector_drift"
    assert env_json["send"]["phase"] == "contract" and env_json["send"]["contract"] == contract
    assert "enqueue --kind retrieve" in env_json["next_command"], "retrieve, never resend"


def test_the_daemon_carries_the_drivers_typed_line(env, monkeypatch):
    spool = env.spool
    sys.modules["cgc_spool"] = spool
    daemon = _load("cgc_daemon")
    line = {"ok": False, "submitted": None, "reason": "unknown_send", "phase": "contract",
            "conversation_id": CONV, "contract": {"families": []}}
    monkeypatch.setattr(daemon, "_run", lambda *a, **k: (3, json.dumps(line) + "\n", "CGC_ERROR unknown_send"))
    got = daemon._make_run_cdp()("submit", rid=RID, prompt="p", project_url="u", model="Pro",
                                 model_family="GPT-6", mentions=[])
    assert got["outcome"] == line and got["conversation"] == CONV


# ---- scheduling -------------------------------------------------------------------------------

def test_probe_schedule(env):
    sys.modules["cgc_spool"] = env.spool
    d = _load("cgc_daemon")
    due = d._probe_due
    assert due(0, None, False, False), "at daemon start"
    assert not due(3600, 0, False, True), "fresh enough"
    assert due(6 * 3600, 0, False, True), "stale before queued paid work"
    assert not due(6 * 3600, 0, False, False), "stale but nothing to send"
    assert not due(10 * 60, 0, True, False), "a break re-probes at most once per 30 min"
    assert due(30 * 60, 0, True, False)


def test_no_new_send_is_claimed_while_a_probe_runs(env, monkeypatch):
    sys.modules["cgc_spool"] = env.spool
    d = _load("cgc_daemon")
    _queue(env, RID)
    spawned = []
    monkeypatch.setattr(d.subprocess, "Popen", lambda argv, **k: spawned.append(argv) or
                        type("P", (), {"pid": 1})())
    d._dispatch_store({}, 3, "d1", claim=False)
    assert spawned == [] and env.s.get_round(RID)["state"] == env.store_mod.QUEUED
    d._dispatch_store({}, 3, "d1")
    assert spawned and env.s.get_round(RID)["state"] == env.store_mod.READY
