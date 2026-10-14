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

    def __init__(self, app="WebCodex Demo", popup=True, blocker=None):
        self.text, self.mentions, self.pending = "", [], []
        self.app, self.popup, self.blocker = app.lower(), popup, blocker
        self.keys, self.calls, self.closed = [], [], 0

    def eval(self, js, timeout=None):
        if "__cgcPlain" in js:                       # the typed-text read (chips excluded)
            return self.text
        if "loginWall" in js:
            return json.dumps({"composer": True, "form": True, "onChatGPT": True,
                               "readyState": "complete", "blocker": self.blocker})
        if "return __cgcBlocker();" in js:
            return self.blocker
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
    """A read-only tab whose eval runs the driver's real JS in node against a page. `pages` is a
    list: each read of the turn list advances one page, so a test can model a thread hydrating."""

    def __init__(self, *pages):
        if not shutil.which("node"):
            pytest.skip("node not installed")
        self.pages, self.calls = list(pages), []

    def eval(self, js, timeout=None):
        if "scrollTop" in js:
            return 1
        if "Array.prototype.map.call(u" in js and len(self.pages) > 1:
            self.pages.pop(0)                # one poll's worth of hydration
        src = _load_dom() + self.pages[0] + "\nconsole.log(JSON.stringify(" + js + "));"
        out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    def call(self, method, params=None, timeout=None):
        self.calls.append(method)
        return {}


# The shared mock DOM matches `=` and `$=` attribute selectors; the blocker rule also uses `*=`
# (with the ` i` flag), so the pages here add it, and answer `main` by hand.
_MAIN = ("var _m=match;match=function(el,sel){return sel.split(',').some(function(s){s=s.trim();"
         "var m=s.match(/^([a-z]*)\\[([\\w-]+)\\*=\"([^\"]*)\"(?: i)?\\]$/i);if(!m)return _m(el,s);"
         "if(m[1]&&el.tagName.toLowerCase()!==m[1].toLowerCase())return false;var v=el.getAttribute(m[2]);"
         "return v!=null&&v.toLowerCase().indexOf(m[3].toLowerCase())>=0;});};"
         "var q=ROOT.querySelector;ROOT.querySelector=function(s){return s==='main'?ROOT:q.call(ROOT,s)};")


def _collapsed_page(rid, *, answered=True, head_marker=True, extra=""):
    """A finished long turn ChatGPT collapsed: the DOM keeps the head (the CGC_RID marker) and a
    Show more button, never the tail pair; the answer below it is wrapped. (The mock DOM matches
    attribute selectors only, so `main` is answered by hand.)"""
    head = ("P('CGC_RID:%s')," % rid if head_marker else "") + "P('# Review'),P('the head…')"
    answer = (",unit('t1:2:assistant',[md(['BEGIN_RESPONSE:%s','the verdict','END_RESPONSE:%s'])])"
              % (rid, rid)) if answered else ""
    return ("var U=unit('t1:0:user',[%s,E('BUTTON',{'aria-expanded':'false'},[],'Show more')]);"
            "var ROOT=E('MAIN',{},[E('DIV',{'data-turn-key':'k1'},[U%s])%s]);" % (head, answer, extra)
            + _MAIN + "var document=ROOT;document.body=ROOT;var location={pathname:'/c/x'};")


def _fast(monkeypatch, cdp, settled=True):
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cdp, "STALE_RELOAD_SETTLE_S", 0)
    monkeypatch.setattr(cdp, "_await_thread_rendered", lambda c, seconds=0: True)
    # The waiter's settle signal (the switcher labels): settled, or never settling (bound reached).
    if settled:
        monkeypatch.setattr(cdp, "_settled_labels", lambda c, hold=4.0, bound=20.0: ["Pro"])
    else:
        def never(c, hold=4.0, bound=20.0):
            cdp.time.sleep(bound)
            return ["Pro"]
        monkeypatch.setattr(cdp, "_settled_labels", never)


def test_the_reader_probe_reads_a_collapsed_marker_turn(env, monkeypatch):
    _fast(monkeypatch, env.cdp)
    tab = _NodeTab(_collapsed_page(RID))
    n = env.probe.probe_reader(env.probe._NoSend(tab), RID, settle=0)
    assert n == len("the verdict")
    assert tab.calls == [], "read-only: no reload was needed and nothing was dispatched"


def test_finding1_a_thread_still_hydrating_is_read_not_judged(env, monkeypatch):
    """The user turn renders first, the assistant turn seconds later. An early read sees no
    assistant turn; the probe keeps reading until the settle window, and passes."""
    _fast(monkeypatch, env.cdp)
    clock = {"t": 0.0}
    monkeypatch.setattr(env.probe.time, "time", lambda: clock["t"])
    monkeypatch.setattr(env.probe.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    early = _collapsed_page(RID, answered=False)
    tab = _NodeTab(early, early, _collapsed_page(RID))
    assert env.probe.probe_reader(env.probe._NoSend(tab), RID, settle=60) == len("the verdict")


def test_finding1_a_thread_that_never_hydrates_is_inconclusive(env, monkeypatch):
    """No assistant turn by the end of the window, and the rid is NOT rendered in <main>: the page
    never showed the conversation, so nothing is proven and nothing is marked."""
    _fast(monkeypatch, env.cdp)
    page = ("var ROOT=E('MAIN',{},[]);" + _MAIN +
            "var document=ROOT;document.body=ROOT;var location={pathname:'/c/x'};")
    with pytest.raises(env.probe.Transient) as e:
        env.probe.probe_reader(env.probe._NoSend(_NodeTab(page)), RID, settle=0)
    assert not isinstance(e.value, env.probe.Blocked)


def test_a_page_that_hides_its_turns_from_the_reader_is_drift(env, monkeypatch):
    _fast(monkeypatch, env.cdp)
    # The rid is visibly on the page, but in a user turn no turn selector reads any more.
    page = ("var ROOT=E('MAIN',{},[E('DIV',{'data-new-turn':'user'},[P('CGC_RID:%s'),P('ask')])]);"
            % RID + _MAIN + "var document=ROOT;document.body=ROOT;var location={pathname:'/c/x'};")
    with pytest.raises(env.probe.Drift) as d:
        env.probe.probe_reader(env.probe._NoSend(_NodeTab(page)), RID, settle=0)
    assert d.value.cap == "reader" and d.value.phase == "rid"


@pytest.mark.parametrize("overlay,blocker", [
    (",E('DIV',{'id':'cf-challenge-running'},[],'Verify you are human')", "captcha"),
    (",E('DIV',{'role':'alert'},[],'Too many requests. Try again later.')", "rate_limit"),
])
def test_finding2_a_challenge_or_rate_limit_overlay_never_breaks_the_reader(env, monkeypatch,
                                                                            overlay, blocker):
    _fast(monkeypatch, env.cdp)
    page = _collapsed_page(RID, answered=False, extra=overlay)
    with pytest.raises(env.probe.Blocked) as e:
        env.probe.probe_reader(env.probe._NoSend(_NodeTab(page)), RID, settle=0)
    assert e.value.blocker == blocker


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
    assert rep["composer"]["verdict"] == "transient"
    assert rep["reader"]["verdict"] == "unprovable", "no fixture in this store (a no-op: reader is not broken)"
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
    assert rep["model"]["verdict"] == "broken" and len(tabs) == 2
    assert rep["composer"]["verdict"] == "pass", "the paste is its own capability"
    cap = env.s.capabilities()["model"]
    assert cap["ok"] is False and cap["phase"] == "probe:done" and "GPT-6" in cap["cause"]
    assert [t.closed for t in tabs] == [1, 1], "each attempt closed its own tab"


@pytest.mark.parametrize("blocker", ["login", "captcha"])
def test_a_challenge_or_login_page_leaves_every_record_untouched(env, monkeypatch, blocker):
    """The composer probe on a tab showing a login wall or a challenge: inconclusive, the human
    condition reported, every capability record exactly as it was — even with a picker that would
    otherwise read as broken."""
    cdp, probe, s = env.cdp, env.probe, env.s
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    s.mark_broken("reader", phase="probe:rid", cause="an earlier break")
    before = s.capabilities()
    monkeypatch.setattr(cdp, "_select_model", lambda c, t, f: cdp._ModelPick(False, None, None,
                                                                            "no picker", False))
    monkeypatch.setattr(cdp, "CDP", lambda port, match=None, create_url=None: _Composer(blocker=blocker))
    monkeypatch.setenv("CGC_APPS", "")
    rep = probe.run_probe("composer")
    assert rep["model"]["verdict"] == "transient" and rep["model"]["phase"] == "human:" + blocker
    assert s.capabilities() == before
    assert probe.print_states(rep) == 3       # reader is still broken; nothing new was written


def test_finding3_composer_drift_needs_the_apps_own_structure(env, monkeypatch):
    """No editor on a page whose app never rendered its form (readyState alone is 'complete' early)
    is inconclusive; no editor inside a rendered form is drift; a challenge is a blocker."""
    cdp, probe = env.cdp, env.probe

    class Page(_Composer):
        def __init__(self, st):
            super().__init__()
            self.st = st

        def eval(self, js, timeout=None):
            if "loginWall" in js:
                return json.dumps(self.st)
            return super().eval(js, timeout)
    monkeypatch.setattr(cdp, "_await_composer", lambda c, seconds=60: (False, json.loads(c.eval("loginWall"))))
    base = {"composer": False, "onChatGPT": True, "readyState": "complete", "blocker": None}
    with pytest.raises(probe.Transient):
        probe.probe_composer(probe._NoSend(Page({**base, "form": False})), model="skip", family="skip", app=None)
    with pytest.raises(probe.Drift):
        probe.probe_composer(probe._NoSend(Page({**base, "form": True})), model="skip", family="skip", app=None)
    with pytest.raises(probe.Blocked):
        probe.probe_composer(probe._NoSend(Page({**base, "form": True, "blocker": "captcha",
                                                 "captcha": True})), model="skip", family="skip", app=None)
    # The shared rule: the composer state reads the same blocker function the waiter does.
    assert cdp._BLOCKER_FN in cdp._composer_state_js() and cdp._BLOCKER_FN in cdp._detect_js(RID)
    assert '[id*="challenge"]' in cdp._BLOCKER_FN


def test_finding4_a_mention_break_is_rechecked_on_its_own_app(env, monkeypatch):
    probe, s = env.probe, env.s
    monkeypatch.setenv("CGC_APPS", "")
    assert probe.probe_app(s) is None
    s.mark_broken("mention", phase="probe:mention", cause="popup never opened", ref="app:Notion")
    assert probe.probe_app(s) == "Notion", "the app the break was recorded on"
    assert probe.probe_app(s, "Canva") == "Canva", "--mention wins"
    monkeypatch.setenv("CGC_APPS", "WebCodex Demo")
    s.mark_passed("mention", started_epoch=s.capability_epoch(), phase="probe", ref="app:Notion")
    assert probe.probe_app(s) == "WebCodex Demo"


def test_finding4_with_no_app_the_probe_says_how_to_name_one(env, monkeypatch, capsys):
    cdp, probe = env.cdp, env.probe
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cdp, "_select_model", lambda c, t, f: cdp._ModelPick(True, t, "6", None, False))
    monkeypatch.setattr(cdp, "CDP", lambda port, match=None, create_url=None: _Composer())
    monkeypatch.setenv("CGC_APPS", "")
    rep = probe.run_probe("mention")
    assert rep["mention"]["verdict"] == "unestablished"
    probe.print_states(rep)
    assert 'cgc probe --mention "<app>"' in capsys.readouterr().err
    rep = probe.run_probe("mention", "WebCodex Demo")
    assert rep["mention"]["verdict"] == "pass" and rep["mention"]["recorded"]
    assert env.s.capabilities()["mention"]["rid_or_conversation"] == "app:WebCodex Demo"


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


def test_finding10_the_unbroken_claim_takes_the_one_row_fast_path(env, monkeypatch):
    s = env.s
    for i in range(3):
        _queue(env, "REQ-20261011-120000-0000c%d" % i)
    seen = []
    s.db.set_trace_callback(seen.append)
    assert s.claim_ready("d1")["rid"] == "REQ-20261011-120000-0000c0"
    s.db.set_trace_callback(None)
    assert any("ORDER BY r.created_at LIMIT 1" in q for q in seen)


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


# ---- finding 6: the reader fixture is never deadlocked by the gate ------------------------------

def _completed(e, rid, prompt, conv):
    s, store_mod = e.s, e.store_mod
    s.create_round(rid, "submit", prompt=prompt)
    s.set_state(rid, store_mod.READY)
    aid = s.begin_send(rid, prompt, store_mod.sha256(prompt), daemon_instance_id="d")
    s.mark_accepted(aid, conv)
    s.mark_waiting(rid)
    s.finish(rid, store_mod.COMPLETED_VERIFIED, result_text="the verdict")


def test_finding6_a_gated_newer_followup_does_not_block_the_only_fixture(env, monkeypatch):
    """reader broken; the only completed thread has a NEWER follow-up the gate holds queued. The
    probe still picks that thread, passes, clears reader, and the follow-up then claims."""
    cdp, probe, s, store_mod = env.cdp, env.probe, env.s, env.store_mod
    _completed(env, RID, "CGC_RID:%s\n\n# q\nask\nBEGIN_RESPONSE:%s\n<a>\nEND_RESPONSE:%s\n" % (RID, RID, RID),
               CONV)
    s.create_round(RID2, "followup", thread_id=CONV, prompt="continuing this consult",
                   spec_json=json.dumps({"conversation": CONV, "model": "Pro"}))
    s.mark_broken("reader", phase="send:contract", cause="selector_drift")
    assert s.claim_ready("d1") is None, "the follow-up is held by the gate"
    pick = probe.pick_reader_round(s)
    assert pick and pick["rid"] == RID, "a queued (unsent) newer round never disqualifies the fixture"
    _fast(monkeypatch, cdp)
    monkeypatch.setattr(env.spool, "acquire_conversation_lease", lambda conv: type("L", (), {
        "close": lambda self: None})())
    tabs = []

    def open_tab(port, match=None, create_url=None):
        assert create_url == "https://chatgpt.com/c/" + CONV
        tabs.append(_NodeTab(_collapsed_page(RID)))
        tabs[-1].close_tab = lambda: None
        tabs[-1].close = lambda: None
        return tabs[-1]
    monkeypatch.setattr(cdp, "CDP", open_tab)
    rep = probe.run_probe("reader")
    assert rep["reader"]["verdict"] == "pass" and rep["reader"]["recorded"]
    assert s.capabilities()["reader"]["ok"] is True
    assert s.claim_ready("d1")["rid"] == RID2


def test_finding6_a_thread_with_a_sent_newer_round_or_active_work_is_not_a_fixture(env):
    probe, s, store_mod = env.probe, env.s, env.store_mod
    _completed(env, RID, "CGC_RID:%s\n\nask" % RID, CONV)
    s.create_round(RID2, "followup", thread_id=CONV, prompt="p")
    s.set_state(RID2, store_mod.READY)
    s.begin_send(RID2, "p", store_mod.sha256("p"), daemon_instance_id="d")   # it touched the thread
    assert probe.pick_reader_round(s) is None


def test_finding6_a_queued_retrieve_flows_while_a_probe_runs(env, monkeypatch):
    sys.modules["cgc_spool"] = env.spool
    d = _load("cgc_daemon")
    _queue(env, RID)
    env.s.create_round(RID2, "retrieve", spec_json=json.dumps({"conversation": CONV, "parent_rid": RID}))
    spawned = []
    monkeypatch.setattr(d.subprocess, "Popen", lambda argv, **k: spawned.append(argv[-1]) or
                        type("P", (), {"pid": 1})())
    d._dispatch_store({}, 3, "d1", sends=False)
    assert spawned == [RID2], "the retrieve is claimed; the send waits for the probe"
    assert env.s.get_round(RID)["state"] == env.store_mod.QUEUED
    d._dispatch_store({}, 3, "d1")
    assert env.s.get_round(RID)["state"] == env.store_mod.READY


# ---- real rounds: typed outcomes, marking, and no resend ----------------------------------------

def _ready(e, rid, **kw):
    _queue(e, rid, **kw)
    e.s.set_state(rid, e.store_mod.READY)
    return e.s.get_round(rid)


@pytest.mark.parametrize("outcome", [
    {"reason": "mention_not_found", "phase": "mention"},
    {"reason": "model_not_selectable", "phase": "model"},
    {"reason": "composer_not_ready", "phase": "composer", "form": True},
])
def test_finding5_a_preclick_failure_never_marks_a_capability(env, outcome):
    """A pre-click failure spends nothing and, from one round, cannot tell a UI change from a
    deleted project, a stale frozen family or an unconnected app — so only the probe (on the
    configured project, model and app) records composer/mention. The round blocks or requeues."""
    r = _ready(env, RID, mentions=["WebCodex Demo"])
    res = {"code": 3, "conversation": "", "outcome": {"ok": False, "submitted": False, **outcome},
           "stderr": f"CGC_ERROR {outcome['reason']}: refused — NOT submitted.\n"}
    env.backend.process_round(env.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                              validate=lambda p: (True, "ok"))
    assert env.s.capabilities() == {}
    assert env.s.send_evidence(RID)["reason"] == outcome["reason"], "the typed outcome is kept"


@pytest.mark.parametrize("marker", ["login_needed", "captcha", "rate_limit"])
def test_login_captcha_and_rate_limit_never_mark_a_capability(env, marker):
    r = _ready(env, RID)
    res = {"code": 3, "conversation": "", "outcome": {"ok": False, "submitted": False,
           "reason": marker, "phase": "composer", "blocker": marker.split("_")[0]},
           "stderr": f"CGC_ERROR {marker}: a human must act\n"}
    env.backend.process_round(env.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                              validate=lambda p: (True, "ok"))
    assert env.s.capabilities() == {}


def test_a_selector_drift_on_a_blocked_page_marks_nothing(env):
    """The driver read a rate limit / challenge on the tab where its rid landed and no reply showed:
    it reports `blocker`, not `drift`, and the round stays uncertain without touching reader."""
    r = _ready(env, RID)
    res = {"code": 1, "conversation": CONV, "stderr": "CGC_ERROR selector_drift: the prompt WAS sent\n",
           "outcome": {"submitted": True, "reason": "selector_drift", "phase": "contract",
                       "blocker": "rate_limit", "conversation_id": CONV}}
    final = env.backend.process_round(env.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                                      validate=lambda p: (True, "ok"))
    assert final == env.store_mod.POSSIBLY_ACCEPTED and env.s.capabilities() == {}
    assert env.s.conversation_of(RID) == CONV


def test_an_uncertain_send_never_auto_resends_and_keeps_its_typed_outcome(env, monkeypatch, capsys):
    backend, s, store_mod = env.backend, env.s, env.store_mod
    fixture = "REQ-20261011-120000-0000f1"           # a finished round a probe could re-prove on
    _completed(env, fixture, "CGC_RID:%s\n\nask" % fixture, "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
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


@pytest.mark.parametrize("broken", ["record_attempt_evidence", "mark_broken"])
def test_finding5b_a_diagnostics_failure_never_moves_the_round_or_loses_its_address(env, monkeypatch,
                                                                                    broken):
    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(env.s, broken, boom)
    r = _ready(env, RID)
    res = {"code": 1, "conversation": CONV, "stderr": "CGC_ERROR selector_drift: the prompt WAS sent\n",
           "outcome": {"submitted": True, "reason": "selector_drift", "phase": "contract",
                       "drift": "reader", "conversation_id": CONV}}
    final = env.backend.process_round(env.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                                      validate=lambda p: (True, "ok"))
    assert final == env.store_mod.POSSIBLY_ACCEPTED
    assert env.s.get_round(RID)["state"] == env.store_mod.POSSIBLY_ACCEPTED
    assert env.s.conversation_of(RID) == CONV


def test_finding9_every_envelope_carries_the_typed_outcome_and_a_null_submitted(env, monkeypatch, capsys):
    r = _ready(env, RID)
    res = {"code": 3, "conversation": "", "stderr": "CGC_ERROR unknown_send: clicked send\n",
           "outcome": {"submitted": None, "reason": "unknown_send", "phase": "contract"}}
    env.backend.process_round(env.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                              validate=lambda p: (True, "ok"))
    ev = env.s.send_evidence(RID)
    assert "submitted" in ev and ev["submitted"] is None, "unknown stays unknown, never dropped"
    # A BLOCKED round's envelope carries it too.
    r2 = _ready(env, RID2)
    blk = {"code": 3, "conversation": "", "stderr": "CGC_ERROR login_needed: log in\n",
           "outcome": {"submitted": False, "reason": "login_needed", "phase": "composer", "blocker": "login"}}
    env.backend.process_round(env.s, r2, lambda kind, **kw: blk, daemon_instance_id="d",
                              validate=lambda p: (True, "ok"))
    monkeypatch.setattr(env.spool, "daemon_alive", lambda: True)
    env.backend.await_round(argparse.Namespace(rid=RID2, out=str(env.tmp / "b.txt"), poll=0.01, timeout=2))
    env_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env_json["state"] == "blocked" and env_json["send"]["reason"] == "login_needed"


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


# ---- scheduling and the daemon's ownership of the probe ----------------------------------------

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


def test_finding7_shutdown_terminates_and_reaps_the_probe(env, monkeypatch):
    sys.modules["cgc_spool"] = env.spool
    d = _load("cgc_daemon")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    d._stop_probe(child)
    assert child.poll() is not None, "the probe child is ended and reaped"
    src = open(os.path.join(SCRIPTS, "cgc_daemon.py"), encoding="utf-8").read()
    assert "_stop_probe(probe)" in src.split("def run_loop", 1)[1], "run_loop's shutdown ends it"
    psrc = open(os.path.join(SCRIPTS, "cgc_probe.py"), encoding="utf-8").read()
    assert "signal.SIGTERM" in psrc, "SIGTERM unwinds the probe's finally blocks (its tab closes)"


def test_finding8_once_ends_when_only_gated_rounds_remain(env):
    sys.modules["cgc_spool"] = env.spool
    d = _load("cgc_daemon")
    _queue(env, RID, mentions=["WebCodex Demo"])
    assert d._claimable()
    env.s.mark_broken("mention", phase="probe", cause="x")
    assert not d._claimable(), "a gated round is not claimable work: --once may exit"
    assert env.s.get_round(RID)["state"] == env.store_mod.QUEUED


# ---- pre-release review: the ledger, the reader self-lock, use-by --------------------------------

_MARKER_ERR = "CGC_ERROR model_not_selectable: wanted 'Pro', switcher shows 'Medium' — NOT submitting.\n"


def _spent(e):
    return [m[0] for m in e.store_mod.spent_messages(e.s.db)]


def _preclick_requeue(e, rid):
    r = _ready(e, rid)
    res = {"code": 3, "conversation": "", "stderr": _MARKER_ERR,
           "outcome": {"submitted": False, "reason": "model_not_selectable", "phase": "model"}}
    assert e.backend.process_round(e.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                                   validate=lambda p: (True, "ok")) == e.store_mod.QUEUED


def test_ledger_a_preclick_requeue_then_cancel_spends_nothing(env, capsys):
    _preclick_requeue(env, RID)
    assert _spent(env) == [], "the requeued attempt provably never clicked"
    assert env.backend.cancel_round(RID) == 0
    assert _spent(env) == []
    assert env.backend._release_eligible(env.s, env.s.get_round(RID)), "its request-key is free"


def test_ledger_a_preclick_requeue_then_a_real_send_spends_one(env):
    _preclick_requeue(env, RID)
    env.s.set_state(RID, env.store_mod.READY)
    aid = env.s.begin_send(RID, "p", env.store_mod.sha256("p"), daemon_instance_id="d")
    assert env.s.get_round(RID)["send_disposition"] is None, "this attempt may click: the proof is gone"
    env.s.mark_accepted(aid, CONV)
    assert _spent(env) == [RID]


def test_ledger_an_uncertain_send_spends_one(env):
    r = _ready(env, RID)
    res = {"code": 1, "conversation": "", "stderr": "CGC_ERROR unknown_send: clicked send\n",
           "outcome": {"submitted": None, "reason": "unknown_send", "phase": "contract"}}
    assert env.backend.process_round(env.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                                     validate=lambda p: (True, "ok")) == env.store_mod.POSSIBLY_ACCEPTED
    assert _spent(env) == [RID]


def _drift(e, rid):
    r = _ready(e, rid)
    res = {"code": 1, "conversation": CONV, "stderr": "CGC_ERROR selector_drift: the prompt WAS sent\n",
           "outcome": {"submitted": True, "reason": "selector_drift", "phase": "contract",
                       "drift": "reader", "conversation_id": CONV}}
    e.backend.process_round(e.s, r, lambda kind, **kw: res, daemon_instance_id="d",
                            validate=lambda p: (True, "ok"))


def test_reader_drift_with_no_fixture_is_recorded_not_gated(env):
    _drift(env, RID)
    assert env.s.capabilities() == {}, "nothing could re-prove it, so it must not gate"
    assert any(e["kind"] == "ui_drift_ungated" for e in env.s.events(RID))
    _queue(env, RID2)
    assert env.s.claim_ready("d1")["rid"] == RID2, "sends keep flowing"


def test_a_reader_break_whose_fixture_is_gone_is_cleared_as_unprovable(env, monkeypatch):
    fixture = "REQ-20261011-120000-0000f2"
    _completed(env, fixture, "CGC_RID:%s\n\nask" % fixture, "ffffffff-ffff-4fff-8fff-ffffffffffff")
    _drift(env, RID)
    assert env.s.capabilities()["reader"]["ok"] is False, "a fixture existed: the drift gates"
    _queue(env, RID2)
    assert env.s.claim_ready("d1") is None
    # The fixture's thread then takes a new send, so it no longer serves.
    env.s.create_round("REQ-20261011-120000-0000f3", "followup",
                       thread_id="ffffffff-ffff-4fff-8fff-ffffffffffff", prompt="p")
    env.s.set_state("REQ-20261011-120000-0000f3", env.store_mod.READY)
    env.s.begin_send("REQ-20261011-120000-0000f3", "p", env.store_mod.sha256("p"), daemon_instance_id="d")
    assert env.probe.pick_reader_round(env.s) is None
    monkeypatch.setattr(env.cdp, "CDP", lambda *a, **k: pytest.fail("no tab: nothing to read"))
    rep = env.probe.run_probe("reader")
    assert rep["reader"]["verdict"] == "unprovable" and rep["reader"]["recorded"]
    cap = env.s.capabilities()["reader"]
    assert cap["ok"] is True and cap["phase"] == "unprovable" and "re-proven" in cap["cause"]
    assert env.s.claim_ready("d1")["rid"] == RID2, "no deadlock: sends resume"


def _use_by_round(e, rid, use_by, mentions=(), key=None):
    e.s.create_round(rid, "submit", prompt="p", request_key=key,
                     spec_json=json.dumps({"model": "Pro", "mentions": list(mentions),
                                           "consult_meta": {"use_by": use_by}}))


def test_a_gated_round_past_use_by_is_expired_by_the_next_claim(env):
    _use_by_round(env, RID, "2020-01-01T00:00:00+00:00", mentions=["WebCodex Demo"], key="k-exp")
    env.s.mark_broken("mention", phase="probe", cause="popup never opened")
    assert env.s.claim_ready("d1") is None
    r = env.s.get_round(RID)
    assert r["state"] == env.store_mod.FAILED and r["error_code"].startswith("expired: use-by 2020")
    assert r["send_disposition"] == env.store_mod.NOT_SENT_PROVEN
    assert env.backend._release_eligible(env.s, r), "its request-key is released"


def test_a_future_use_by_is_left_alone(env):
    _use_by_round(env, RID, "2999-01-01T00:00:00+00:00")
    assert env.s.claim_ready("d1")["rid"] == RID


@pytest.mark.parametrize("cmd", ["submit", "followup"])
def test_use_by_is_rechecked_at_the_click(env, monkeypatch, cmd):
    """Model selection and the paste run after the worker's own use-by check; the driver re-checks
    immediately before the click and, past it, clicks nothing (exit 6, provably not sent)."""
    import types
    cdp = env.cdp
    clicked = []
    monkeypatch.setattr(cdp, "_send_click_js", lambda: clicked.append(1) or "CLICK")
    monkeypatch.setattr(cdp, "_egress_gate", lambda p: (True, "ok"))
    monkeypatch.setattr(cdp, "_has_sendable_source", lambda p: True)
    monkeypatch.setattr(cdp, "_await_composer", lambda *a, **k: (True, {}))
    monkeypatch.setattr(cdp, "_paste_prompt", lambda *a, **k: None)
    monkeypatch.setattr(cdp, "_await_thread_rendered", lambda c, seconds=0: True)
    monkeypatch.setattr(cdp, "_last_user_text_js", lambda: "LAST")
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)

    class Tab:
        def eval(self, js, timeout=None):
            return 1 if "length" in js else ("x" if js == "LAST" else None)

        def conversation_id(self):
            return CONV

        def call(self, *a, **k):
            return {}

        def close_tab(self):
            pass

        def close(self):
            pass
    monkeypatch.setattr(cdp, "CDP", lambda *a, **k: Tab())
    pf = env.tmp / "p.md"
    pf.write_text("CGC_RID:%s\n\nask\nBEGIN_RESPONSE:%s\n<a>\nEND_RESPONSE:%s\n" % (RID, RID, RID))
    ns = types.SimpleNamespace(rid=RID, prompt_file=str(pf), project_url="https://chatgpt.com/",
                               model="skip", model_family="skip", allow_model_mismatch=False,
                               reuse_tab=False, no_gate=True, port=9333, mention=[], watch=False,
                               out=None, conversation=CONV, task=None,
                               not_after="2020-01-01T00:00:00+00:00")
    fn = cdp.cmd_submit if cmd == "submit" else cdp.cmd_followup
    assert fn(ns) == cdp.EXIT_NOT_SENT_PRECLICK
    assert clicked == [], "past use-by, the send button is never evaluated"


def test_a_completed_rounds_envelope_carries_its_typed_send(env, monkeypatch, capsys):
    r = _ready(env, RID)
    landed = {"ok": True, "submitted": True, "reason": "landed", "phase": "contract",
              "conversation_id": CONV, "adapter": "search-unit-v1"}

    def cdp(kind, **kw):
        if kind == "submit":
            return {"code": 0, "conversation": CONV, "stderr": "", "outcome": landed}
        with open(kw["out"], "w") as f:
            f.write("the verdict")
        return {"code": 0, "out": kw["out"], "stderr": ""}
    final = env.backend.process_round(env.s, r, cdp, daemon_instance_id="d",
                                      validate=lambda p: (True, "ok"))
    assert final == env.store_mod.COMPLETED_VERIFIED
    monkeypatch.setattr(env.spool, "daemon_alive", lambda: True)
    assert env.backend.await_round(argparse.Namespace(rid=RID, out=str(env.tmp / "a.txt"), poll=0.01,
                                                      timeout=2)) == 0
    env_json = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert env_json["state"] == "completed_verified"
    assert env_json["send"]["submitted"] is True and env_json["send"]["reason"] == "landed"
    assert env_json["send"]["conversation_id"] == CONV


def test_a_broken_model_picker_holds_only_rounds_that_select_that_model(env, monkeypatch):
    monkeypatch.setenv("CGC_MODEL_FAMILY", "GPT-6")
    s = env.s
    s.create_round(RID, "submit", prompt="p", spec_json=json.dumps({"model": "skip", "model_family": "skip"}))
    s.create_round(RID2, "submit", prompt="p", spec_json=json.dumps({"model": "Pro", "model_family": "GPT-6"}))
    s.create_round("REQ-20261011-120000-0000d3", "submit", prompt="p",
                   spec_json=json.dumps({"model": "Pro", "model_family": "GPT-5.5"}))
    s.mark_broken("model", phase="probe:done", cause="'GPT-6' is not offered",
                  ref=env.store_mod.model_ref("Pro", "GPT-6"))
    got = [s.claim_ready("d1")["rid"], s.claim_ready("d1")["rid"]]
    assert got == [RID, "REQ-20261011-120000-0000d3"], "a skip round and another family still claim"
    assert s.claim_ready("d1") is None and s.get_round(RID2)["state"] == env.store_mod.QUEUED


def test_a_model_break_binds_the_pair_it_checked_not_todays_config(env, monkeypatch):
    """GPT-6/Pro recorded broken, then the config moves to GPT-5.5: the verdict still names GPT-6, so
    a GPT-5.5 round claims and the frozen GPT-6 round stays held — today's env decides neither."""
    s = env.s
    s.create_round(RID, "submit", prompt="p", spec_json=json.dumps({"model": "Pro", "model_family": "GPT-6"}))
    s.create_round(RID2, "submit", prompt="p", spec_json=json.dumps({"model": "Pro", "model_family": "GPT-5.5"}))
    s.mark_broken("model", phase="probe:done", cause="'GPT-6' is not offered",
                  ref=env.store_mod.model_ref("Pro", "GPT-6"))
    monkeypatch.setenv("CGC_MODEL_FAMILY", "GPT-5.5")
    assert s.claim_ready("d1")["rid"] == RID2
    assert s.claim_ready("d1") is None and s.get_round(RID)["state"] == env.store_mod.QUEUED
    assert set(s.gated_by(s.get_round(RID))) == {"model"} and not s.gated_by(s.get_round(RID2))


def test_a_legacy_model_break_without_a_pair_gates_nothing(env):
    s = env.s
    s.create_round(RID, "submit", prompt="p", spec_json=json.dumps({"model": "Pro", "model_family": "GPT-6"}))
    s.mark_broken("model", phase="probe:done", cause="old record")
    assert s.claim_ready("d1")["rid"] == RID


def test_once_does_not_wait_for_a_future_not_before(env):
    sys.modules["cgc_spool"] = env.spool
    d = _load("cgc_daemon")
    tomorrow = (__import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                + __import__("datetime").timedelta(days=1)).isoformat()
    env.s.create_round(RID, "submit", prompt="p", not_before=tomorrow)
    assert not d._claimable(), "--once exits now; the round runs on schedule later"
    assert env.s.get_round(RID)["state"] == env.store_mod.QUEUED


def test_the_reader_never_breaks_on_a_page_that_did_not_settle(env, monkeypatch):
    """A miss with the rid on the page, but switcher labels that never stopped changing: the page
    was still hydrating, so the probe reports inconclusive, not a break."""
    _fast(monkeypatch, env.cdp, settled=False)
    clock = {"t": 0.0}
    monkeypatch.setattr(env.probe.time, "time", lambda: clock["t"])
    monkeypatch.setattr(env.probe.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(env.cdp.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    page = ("var ROOT=E('MAIN',{},[E('DIV',{'data-new-turn':'user'},[P('CGC_RID:%s'),P('ask')])]);"
            % RID + _MAIN + "var document=ROOT;document.body=ROOT;var location={pathname:'/c/x'};")
    with pytest.raises(env.probe.Transient) as e:
        env.probe.probe_reader(env.probe._NoSend(_NodeTab(page)), RID, settle=0)
    assert not isinstance(e.value, (env.probe.Drift, env.probe.Blocked)) and "never settled" in str(e.value)


def test_a_skip_family_still_needs_its_tier(env):
    """`family='skip'` only skips the family check; the tier is still selected, so a broken
    `model:Pro@skip` holds that round. Only `tier='skip'` sends without touching the picker."""
    s = env.s
    s.create_round(RID, "submit", prompt="p", spec_json=json.dumps({"model": "Pro", "model_family": "skip"}))
    s.create_round(RID2, "submit", prompt="p", spec_json=json.dumps({"model": "skip", "model_family": "skip"}))
    s.mark_broken("model", phase="probe:done", cause="tier not selectable",
                  ref=env.store_mod.model_ref("Pro", "skip"))
    assert s.claim_ready("d1")["rid"] == RID2
    assert s.claim_ready("d1") is None and s.get_round(RID)["state"] == env.store_mod.QUEUED
