# A follow-up into a long thread re-opens it; the page renders every turn, then unmounts the older
# ones while the answer streams (the heartbeat's assistant count fell 4 -> 2 and 6 -> 3). The waiter
# had pinned its source turn by ORDINAL at wait start ("the 5th user turn"), so once the older turns
# unmounted that ordinal named no turn: the scoped read returned no node, the heartbeat printed
# len=0 for the whole 5400 s while a complete wrapped answer sat in the tab, and the timeout salvage
# (which re-derived a fresh ordinal) took it as an UNWRAPPED answer — so the round was reported
# unverified. These run the waiter's real reader JS in node against that DOM, as it changes.
import importlib.util
import json
import os
import shutil
import subprocess
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CDP = _load("cdp_consult", os.path.join(ROOT, "skill", "scripts", "cdp_consult.py"))
_DOM = _load("test_search_unit_schema", os.path.join(ROOT, "tests", "test_search_unit_schema.py"))._DOM

CONV = "00000000-0000-4000-8000-000000000001"
R = ["REQ-20990101-0%d0000-00000%d" % (i, i) for i in range(6)]
BODY = ["the verdict, in full detail " * 90]          # ~2.5 KB: above --min-unwrapped, like the incident


def _node(js, setup):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = _DOM + setup + "\nconsole.log(JSON.stringify(" + js + "));"
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True,
                                     check=True).stdout)


def _wrapped(rid, body=BODY):
    return ["BEGIN_RESPONSE:" + rid] + list(body) + ["END_RESPONSE:" + rid]


def _turn(i, rid, answer=None, extra=""):
    """One [data-turn-key] turn: the user unit carries the template's own sentinel pair last (what
    _turn_canonical_rid reads); `answer` (lines) adds the assistant unit."""
    user = "unit(%s,[%s])" % (json.dumps("t%d:0:user" % i), ",".join(
        "P(%s)" % json.dumps(x) for x in ("# Follow-up", "BEGIN_RESPONSE:" + rid, "<full answer>",
                                          "END_RESPONSE:" + rid)))
    kids = [user] + (["unit(%s,[md(%s)])" % (json.dumps("t%d:2:assistant" % i), json.dumps(answer))]
                     if answer else [])
    return "E('DIV',{'data-turn-key':%s},[%s%s])" % (json.dumps("k%d" % i), ",".join(kids), extra)


def _page(*turns):
    return ("var ROOT=E('MAIN',{},[" + ",".join(turns) + "]);var document=ROOT;document.body=ROOT;"
            "var location={pathname:'/c/x'};")


# ---- the DOM reads ---------------------------------------------------------------------------

def test_the_source_turn_is_found_by_identity_after_older_turns_unmount():
    src = R[4]
    page = _page(_turn(2, R[2], _wrapped(R[2])), _turn(3, R[3], _wrapped(R[3])),
                 _turn(4, src, _wrapped(src)))
    st = json.loads(_node(CDP._detect_js(src, scoped=True), page))
    assert st["done"] is True and st["node"] == "found"
    assert _node(CDP._extract_js(src, scoped=True), page).strip() == BODY[0].strip()


def test_a_later_rounds_echo_of_the_wrapper_is_never_the_source_turns_answer():
    """[S3]: R1's own turn has no answer; R2's answer reproduces R1's whole wrapper. An unscoped read
    takes it (so the fixture is adversarial); the scoped read must not, however the turns shift."""
    page = _page(_turn(1, R[1]), _turn(2, R[2], _wrapped(R[1], ["echoed, not R1's answer"])))
    assert json.loads(_node(CDP._detect_js(R[1]), page))["done"] is True
    st = json.loads(_node(CDP._detect_js(R[1], scoped=True), page))
    assert st["done"] is False and st["node"] == "none"
    assert _node(CDP._extract_js(R[1], scoped=True), page) == ""
    assert _node(CDP._last_assistant_js(R[1], scoped=True), page) == ""


def test_a_resent_rid_reads_the_later_turn_and_its_marker_not_the_failed_one():
    """Auto-continue re-sends a failed round with the SAME rid. The later turn owns the answer, and
    the first turn's 'Thinking failed' must not end the wait on the continue."""
    rid = R[3]
    failed = ",E('SPAN',{},[],'Thinking failed')"
    pending = _page(_turn(3, rid, extra=failed), _turn(4, rid))
    st = json.loads(_node(CDP._detect_js(rid, scoped=True), pending))
    assert st["failed"] is None and st["done"] is False
    answered = _page(_turn(3, rid, extra=failed), _turn(4, rid, _wrapped(rid)))
    assert json.loads(_node(CDP._detect_js(rid, scoped=True), answered))["done"] is True


def test_a_source_turn_that_is_not_rendered_is_named_not_read_as_an_empty_answer():
    page = _page(_turn(2, R[2], _wrapped(R[2])))
    st = json.loads(_node(CDP._detect_js(R[4], scoped=True), page))
    assert st["node"] == "source_unrendered" and st["done"] is False
    pending = json.loads(_node(CDP._detect_js(R[2]), _page(_turn(2, R[2]))))
    assert pending["node"] == "none", "an unscoped read with no assistant node yet"


def test_the_js_turn_rid_parser_matches_the_python_one():
    """__cgcRid is the page-side mirror of _turn_canonical_rid (SHARED CONTRACT #3): the waiter now
    anchors its interval with it, so the two must agree on every shape the Python tests pin."""
    a, b = R[1], R[2]
    corpus = [
        f"review\nBEGIN_RESPONSE:{a}\nthe answer\nEND_RESPONSE:{a}\n",
        f"Earlier you wrote BEGIN_RESPONSE:{a} inline.\nBEGIN_RESPONSE:{b}\nask\nEND_RESPONSE:{b}\n",
        f"```\nBEGIN_RESPONSE:{a}\n```\nBEGIN_RESPONSE:{b}\nask\nEND_RESPONSE:{b}\n",
        f"BEGIN_RESPONSE:{a}\nold\nEND_RESPONSE:{a}\nnow:\nBEGIN_RESPONSE:{b}\nask\nEND_RESPONSE:{b}\n",
        f"BEGIN_RESPONSE:{b}\nask\nEND_RESPONSE:{b}\nquoted after:\nBEGIN_RESPONSE:{a}\nEND_RESPONSE:{a}",
        f"just discussing BEGIN_RESPONSE:{a} with no end\n",
        f"BEGIN_RESPONSE:{a}\nx\nEND_RESPONSE:{b}\n",
        f"END_RESPONSE:{a}\nBEGIN_RESPONSE:{a}\n",
        f"  BEGIN_RESPONSE:{a}  \r\nx\r\n\tEND_RESPONSE:{a}\t\r\n",
        f"BEGIN_RESPONSE:{a}x\nEND_RESPONSE:{a}x\n",
        "plain question, no sentinels\n",
        "",
    ]
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = CDP._RID_FN + "console.log(JSON.stringify(" + json.dumps(corpus) + ".map(__cgcRid)));"
    got = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                    check=True).stdout)
    assert got == [CDP._turn_canonical_rid(t) for t in corpus]


# ---- the waiter, end to end, on a DOM that changes under it -----------------------------------

class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def time(self):
        return self.t

    def sleep(self, seconds=0.0):
        self.t += max(float(seconds or 0.0), 0.001)


class NodeTab:
    """A tab whose reads run the waiter's real JS in node. `stages` is [(detect_reads, page), ...]:
    each page is served for that many heartbeat reads (None = from then on)."""

    def __init__(self, stages):
        self.stages, self.detects, self.closed = stages, 0, False

    def _page(self):
        left = self.detects
        for n, page in self.stages:
            if n is None or left <= n:
                return page
            left -= n
        return self.stages[-1][1]

    def eval(self, expr, timeout=None):
        if "var allow=" in expr or expr.startswith("(function(){try{var sc="):
            return None                                   # no approval card; force-render is a no-op
        if "generating:stop,done:done" in expr:
            self.detects += 1
        return _node(expr, self._page())

    def call(self, method, params=None, timeout=None):
        if method == "Page.reload":
            return {}
        raise AssertionError(f"the waiter is read-only; it called {method}")

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
    clock = _Clock()
    monkeypatch.setattr(CDP, "time", clock)

    def run(tab, rid, timeout=600):
        monkeypatch.setattr(CDP, "CDP", lambda *a, **kw: tab)
        ns = types.SimpleNamespace(rid=rid, conversation=CONV, port=9333,
                                   out=str(tmp_path / "answer.txt"), poll=20, timeout=timeout,
                                   settle_seconds=300, min_unwrapped=2000, keep_tab=False)
        start = clock.time()
        code = CDP.cmd_wait(ns)
        return code, ns.out, clock.time() - start
    return run


def test_a_reopened_thread_that_unmounts_older_turns_completes_on_its_wrapped_answer(wait, capsys):
    """The incident: 5 user turns at wait start (source is the 5th), then the two oldest unmount,
    then the source's wrapped answer lands. It must complete as soon as the answer exists — not
    at the timeout, and never as an unwrapped salvage of a wrapped answer."""
    src = R[4]
    older = [_turn(i, R[i], _wrapped(R[i], ["answer %d" % i])) for i in range(4)]
    tab = NodeTab([(2, _page(*older, _turn(4, src))),
                   (2, _page(*older[2:], _turn(4, src))),
                   (None, _page(*older[2:], _turn(4, src, _wrapped(src))))])
    code, out, spent = wait(tab, src)
    err = capsys.readouterr().err
    assert code == 0, err
    assert open(out).read().strip() == BODY[0].strip()
    assert "CGC_DONE wrote" in err and "rescued at timeout" not in err and "CGC_UNWRAPPED" not in err
    assert spent < 200, "done on the first read that sees the answer, not at the 600 s timeout"


def test_an_unmount_never_shifts_the_wait_onto_a_later_rounds_interval(wait, capsys):
    """[S3] under the same unmount: R1 is recovered (a later R2 exists) and R1 never answered. When
    R0 unmounts, "user turn #2" becomes R2 — whose answer echoes R1's whole wrapper. The wait must
    not take it."""
    r2_turn = _turn(2, R[2], _wrapped(R[1], ["echoed, not R1's answer"]))
    tab = NodeTab([(2, _page(_turn(0, R[0], _wrapped(R[0], ["a0"])), _turn(1, R[1]), r2_turn)),
                   (None, _page(_turn(1, R[1]), r2_turn))])
    code, out, _ = wait(tab, R[1], timeout=200)
    assert code != 0
    assert not os.path.exists(out) or "echoed" not in open(out).read()
    assert "CGC_DONE" not in capsys.readouterr().err


def test_the_heartbeat_names_an_unrendered_source_turn(wait, capsys):
    """If the source turn itself leaves the DOM, the heartbeat says so instead of a bare len=0."""
    src = R[4]
    tab = NodeTab([(1, _page(_turn(3, R[3], _wrapped(R[3])), _turn(4, src))),
                   (None, _page(_turn(3, R[3], _wrapped(R[3]))))])
    code, _, _ = wait(tab, src, timeout=120)
    err = capsys.readouterr().err
    assert code == 4
    beats = [ln for ln in err.splitlines() if "CGC_WAIT alive:" in ln]
    assert "node=none" in beats[0] and "node=source_unrendered" in beats[-1], beats


# ---- a wrapped answer is graded verified, whichever exit of the wait finds it -----------------
# The daemon grades a round completed_unverified by the mere presence of the `<out>.raw` sidecar
# (cgc_backend._wait_phase). Before this, a wrapped answer could reach the caller as unverified two
# ways: the timeout salvage read it and wrote it as CGC_UNWRAPPED (the extract and the salvage read
# are separate evals, and on the incident's tab only the salvage one found the node), or an earlier
# stub-stable dump left a .raw behind that the verified exit never removed.

class ForceRenderTab(NodeTab):
    """A virtualized tab: the answer node is only in the DOM after `renders` scroll-to-bottom
    force-renders — the timeout path's own scroll is the one that materializes it."""

    def __init__(self, before, after, renders):
        super().__init__([(None, before)])
        self.after, self.renders = after, renders

    def _page(self):
        return self.after if self.renders <= 0 else self.stages[0][1]

    def eval(self, expr, timeout=None):
        if expr.startswith("(function(){try{var sc="):
            self.renders -= 1
            return None
        return super().eval(expr, timeout)


def _wait_then_await(wait, tab, rid, tmp_path, monkeypatch, capsys, **kw):
    """Drive the real waiter through the daemon's own verdict (_wait_phase) and then `await`: the
    code, the stored state and the envelope the caller sees."""
    import sys as _sys
    scripts = os.path.join(ROOT, "skill", "scripts")
    monkeypatch.syspath_prepend(scripts)
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path))
    for m in ("cgc_store", "cgc_spool", "cgc_backend"):
        _sys.modules.pop(m, None)
    import cgc_backend as backend
    import cgc_spool as spool
    import cgc_store as store_mod
    monkeypatch.setattr(spool, "daemon_alive", lambda: True)
    s = store_mod.Store()
    s.create_round(rid, "submit", prompt="p")
    s.set_state(rid, store_mod.READY)
    s.mark_accepted(s.begin_send(rid, "p", "h" * 64, daemon_instance_id="d1"), CONV)
    s.mark_waiting(rid)

    def run_cdp(kind, **k):
        assert kind == "wait"
        monkeypatch.setattr(CDP, "CDP", lambda *a, **kw_: tab)
        ns = types.SimpleNamespace(rid=k["rid"], conversation=k["conversation"], port=9333,
                                   out=k["out"], poll=20, timeout=kw.get("timeout", 600),
                                   settle_seconds=300, min_unwrapped=2000, keep_tab=False)
        return {"code": CDP.cmd_wait(ns), "out": k["out"], "model_slug": None, "stderr": ""}
    state = backend._wait_phase(s, rid, CONV, {}, run_cdp)
    s.close()
    capsys.readouterr()
    code = backend.await_round(types.SimpleNamespace(rid=rid, out=str(tmp_path / "got.txt"),
                                                     timeout=5, poll=1))
    cap = capsys.readouterr()
    return state, code, json.loads(cap.out.strip().splitlines()[-1]), cap.err


def test_a_wrapped_answer_rendered_only_by_the_timeout_scroll_is_verified(
        wait, tmp_path, monkeypatch, capsys):
    """The incident's end state: the wait began on a fully rendered thread, the older turns have
    since unmounted, and the answer node exists only after the timeout path's force-render. The
    wrapper is there, so this is a sentinel-verified answer — completed_verified, await exit 0 —
    not the CGC_UNWRAPPED salvage the incident recorded."""
    src = R[4]
    older = [_turn(i, R[i], _wrapped(R[i], ["answer %d" % i])) for i in range(4)]
    tab = ForceRenderTab(_page(*older, _turn(4, src)),
                         _page(*older[2:], _turn(4, src, _wrapped(src))),
                         renders=31)   # 30 polls in a 600 s wait, then the timeout path's own scroll
    state, code, env, _ = _wait_then_await(wait, tab, src, tmp_path, monkeypatch, capsys)
    assert state == "completed_verified"
    assert code == 0 and env["state"] == "completed_verified" and env["confidence"] == "verified"
    assert open(env["answer_path"]).read().strip() == BODY[0].strip()


def test_a_wrapped_answer_is_verified_even_after_a_stub_dump_left_a_raw_file(
        wait, tmp_path, monkeypatch, capsys):
    """A thinking stub settles (stub-stable writes <out>.raw), then the wrapped answer lands. The
    verified exit must not leave that dump behind for the daemon to read as a salvage."""
    src = R[4]
    stub = _turn(4, src, ["Thinking about the review"])
    tab = NodeTab([(20, _page(stub)), (None, _page(_turn(4, src, _wrapped(src))))])
    state, code, env, _ = _wait_then_await(wait, tab, src, tmp_path, monkeypatch, capsys,
                                           timeout=1200)
    assert state == "completed_verified" and code == 0 and env["confidence"] == "verified"


def test_the_timeout_salvage_grades_text_it_finds_wrapped_as_verified(wait, tmp_path, monkeypatch,
                                                                       capsys):
    """The timeout's extract and its salvage read are separate evals of the node; if only the salvage
    read sees the finished wrapper, the canonical parser on that text decides, not the eval order."""
    src = R[4]
    tab = NodeTab([(None, _page(_turn(4, src)))])
    answered = _page(_turn(4, src, _wrapped(src)))
    orig = tab.eval
    tab.eval = lambda e, timeout=None: (_node(e, answered) if "return __cgcText(__cgcNode(" in e
                                        else orig(e, timeout))
    state, code, env, _ = _wait_then_await(wait, tab, src, tmp_path, monkeypatch, capsys,
                                           timeout=120)
    assert state == "completed_verified" and code == 0 and env["confidence"] == "verified"
    assert open(env["answer_path"]).read().strip() == BODY[0].strip()


def test_an_unwrapped_salvage_is_still_graded_unverified(wait, tmp_path, monkeypatch, capsys):
    """The verified grade is earned by the wrapper, not by reaching the timeout path."""
    src = R[4]
    tab = NodeTab([(None, _page(_turn(4, src, BODY)))])
    state, code, env, _ = _wait_then_await(wait, tab, src, tmp_path, monkeypatch, capsys,
                                           timeout=120)
    assert state == "completed_unverified" and code == 3 and env["confidence"] == "unverified"


def test_a_later_rounds_wrapper_is_never_salvaged_into_the_source_round(wait, tmp_path, monkeypatch,
                                                                         capsys):
    """[S3] at the timeout: R1's own turn has no answer; R2's answer reproduces R1's whole wrapper.
    Neither the rescue nor the salvage parse may take it."""
    r2_turn = _turn(2, R[2], _wrapped(R[1], ["echoed, not R1's answer"] * 90))
    tab = NodeTab([(None, _page(_turn(1, R[1]), r2_turn))])
    state, code, env, _ = _wait_then_await(wait, tab, R[1], tmp_path, monkeypatch, capsys,
                                           timeout=120)
    assert state not in ("completed_verified", "completed_unverified") and code != 0
