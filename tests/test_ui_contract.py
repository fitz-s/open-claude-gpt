# The UI contract: every ChatGPT DOM fact the driver reads lives in skill/scripts/chatgpt_contract.json,
# and a UI change is repaired by editing that file. These pin (1) the file is the only place those facts
# are written, (2) a drift leaves a bundle that names the contract id to edit and a candidate for it,
# and (3) replaying the bundle offline against an edited contract copy passes.
import importlib.util
import json
import os
import re
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
CONTRACT = os.path.join(SCRIPTS, "chatgpt_contract.json")
sys.path.insert(0, SCRIPTS)

RID = "REQ-20261014-120000-0000c1"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mods(tmp_path, monkeypatch):
    monkeypatch.setenv("CGC_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("CGC_SPOOL_DIR", str(tmp_path / "spool"))
    for n in ("cgc_store", "cgc_spool", "cgc_backend"):
        _load(n)
    return _load("cdp_consult"), _load("cgc_probe"), _load("cgc_drift")


def _facts():
    with open(CONTRACT, encoding="utf-8") as f:
        return json.load(f)["facts"]


def _src(name):
    with open(os.path.join(SCRIPTS, name), encoding="utf-8") as f:
        return f.read()


# ---- the contract is the one place --------------------------------------------------------------

def test_every_fact_has_a_value_and_a_dated_why():
    for k, v in _facts().items():
        assert set(v) == {"value", "why"}, k
        assert re.search(r"20\d\d-\d\d", v["why"]), f"{k}: say when it was observed"


def test_every_contract_id_is_read_by_the_driver():
    src = _src("cdp_consult.py") + _src("cgc_drift.py")
    unused = [k for k in _facts() if f'"{k}"' not in src and f"'{k}'" not in src]
    assert unused == [], f"contract ids nothing reads: {unused}"


# The facts the contract owns, as the literals they used to be written as. None may reappear in the
# driver or the probe: a second copy is the copy a UI fix misses.
_OWNED = ("data-message-author-role", "data-chatgpt-search-unit-key", "data-turn=", "[data-turn]",
          "data-turn-key", "data-markdown-text-style", "assistant-message", "stop-button", "send-button",
          "prompt-textarea", "contenteditable=", "app-mention", "trailingBreak", "data-mention-list",
          "data-list-navigation-item", "data-menu-row-content", "rounded-2xs", "text-codex-description",
          "composer-intelligence-picker-content", "composer-plus-btn", "menuitemradio", 'role="slider"',
          'role=\\"slider', "radiogroup", "aria-haspopup=\\\"menu", 'aria-expanded=\\"true',
          "data-message-model-slug", "Thinking failed", "Message stream error", "captcha\\\" i",
          'type=\\"password', "too many requests", "allow once", "u5141", "u505c", "u53d1",
          "querySelector('main')", "'plugins'")


@pytest.mark.parametrize("name", ["cdp_consult.py", "cgc_probe.py"])
def test_no_contract_owned_literal_is_written_twice(name):
    # Only code is checked: comments and docstrings may describe the page.
    import io
    import tokenize
    code = [t.string for t in tokenize.generate_tokens(io.StringIO(_src(name)).readline)
            if t.type == tokenize.STRING and not t.string.startswith(('"""', "'''", 'r"""', 'f"""'))]
    hits = sorted({lit for lit in _OWNED for s in code if lit in s})
    assert hits == [], f"{name} re-spells contract facts: {hits}"


def test_the_probe_imports_the_drivers_reads_and_defines_no_selector():
    src = _src("cgc_probe.py")
    for redefined in ("_JS_U =", "_JS_A =", "_SEL_", "_MENTION_POPUP_SEL =", "querySelector"):
        assert redefined not in src, redefined


def test_a_contract_value_that_would_break_the_js_is_refused(tmp_path, mods):
    cdp = mods[0]
    facts = _facts()
    facts["composer"]["value"] = "div[data-x='y']"
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"facts": facts}))
    with pytest.raises(SystemExit, match="contract_invalid"):
        cdp._load_contract(str(p))


def test_a_demonstrated_relabel_is_the_family_and_nothing_else_is(mods):
    cdp = mods[0]
    assert cdp._is_family("Latest", "GPT-6") and cdp._is_family("gpt-6", "GPT-6")
    assert not cdp._is_family("Latest", "GPT-5.5") and not cdp._is_family("GPT-6 Mini", "GPT-6")


# ---- drift -> bundle -> edit -> replay ----------------------------------------------------------

def _page(attr="data-chatgpt-search-unit-key"):
    """A finished conversation in the fixture's own form: a sidebar (must not reach the fixture), a
    long private prompt (must be cut), and the rid's wrapped answer. `attr` is the message units'
    attribute; another name is ChatGPT shipping a new schema generation."""
    private = "private prompt body " * 40
    user = ["div", {attr: "t1:0:user"}, [["p", {}, ["CGC_RID:" + RID]], ["p", {}, [private]]]]
    asst = ["div", {attr: "t1:2:assistant"},
            [["div", {"data-markdown-text-style": "assistant-message"},
              [["p", {}, ["BEGIN_RESPONSE:" + RID]], ["p", {}, ["the verdict"]],
               ["p", {}, ["END_RESPONSE:" + RID]]]]]]
    nav = ["nav", {}, [["a", {"href": "/c/other"}, ["Other private thread title"]]]]
    main = ["main", {}, [["div", {"data-turn-key": "k1"}, [user, asst]]]]
    return ["html", {}, [["body", {}, [nav, main]]]]


class _Tab:
    """A finished thread's tab whose page is `tree`: the driver's JS runs in node on fixture_dom.js."""

    def __init__(self, drift, tree):
        if not shutil.which("node"):
            pytest.skip("node not installed")
        self.t = drift.FixtureTab("", "/c/x")
        self.t.tree = [tree]

    def eval(self, js, timeout=None):
        return self.t.eval(js, timeout)

    def call(self, method, params=None, timeout=None):
        return {}


def _fast(monkeypatch, cdp):
    monkeypatch.setattr(cdp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cdp, "STALE_RELOAD_SETTLE_S", 0)
    monkeypatch.setattr(cdp, "_await_thread_rendered", lambda c, seconds=0: True)
    monkeypatch.setattr(cdp, "_settled_labels", lambda c, hold=4.0, bound=20.0: ["Pro"])


def test_the_reader_reads_the_page_through_the_fixture_dom(mods, monkeypatch):
    cdp, probe, drift = mods
    _fast(monkeypatch, cdp)
    tab = _Tab(drift, _page())
    assert probe.probe_reader(probe._NoSend(tab), RID, settle=0) == len("the verdict")


def test_a_renamed_unit_attribute_leaves_a_bundle_and_an_edited_contract_replays(mods, monkeypatch,
                                                                                 tmp_path):
    cdp, probe, drift = mods
    _fast(monkeypatch, cdp)
    tab = _Tab(drift, _page("data-message-unit"))
    with pytest.raises(probe.Drift) as e:
        probe.probe_reader(probe._NoSend(tab), RID, settle=0, diagnose=True)
    m = re.match(r"diagnosis (\S+)/diagnosis\.json; ", e.value.cause)
    assert m, "the recorded cause leads with the bundle's path: " + e.value.cause
    inc = m.group(1)
    assert inc.startswith(os.path.join(os.environ["CGC_DATA_DIR"], "incidents", "I-"))
    raw = open(os.path.join(inc, "diagnosis.json"), encoding="utf-8").read()
    d = json.loads(raw)
    assert len(raw.encode()) <= drift.JSON_CAP
    assert d["schema"] == "cgc.drift.v1" and d["capability"] == "reader" and d["rid"] == RID
    assert d["edit"]["ids"] == ["turn_schemas"], "the one fact the page contradicts"
    assert d["edit"]["file"] == cdp.CONTRACT_PATH
    assert any(c["selector"] == '[data-message-unit$=":assistant"]' for c in d["observed"]["candidates"]), \
        d["observed"]["candidates"]
    seen = {x["id"]: x["matches"] for x in d["observed"]["matches"]}
    assert seen["turn_schemas/search-unit-v1/user"] == 0
    assert seen["turn_schemas/search-unit-v1/assistant"] == 0
    assert d["check"]["replay_command"][-1] == inc and d["check"]["probe_command"][-1] == "reader"
    fixture = open(os.path.join(inc, "fixture.html"), encoding="utf-8").read()
    assert "Other private thread title" not in fixture and "href" not in fixture, "no sidebar, no URLs"
    assert fixture.count("private prompt body") <= 3, "page text is bounded"
    assert "CGC_RID:" + RID in fixture and "END_RESPONSE:" + RID in fixture, "the rid markers survive"

    # Replay with the contract as it is: the drift reproduces offline.
    r = probe.replay(inc)
    assert r["ok"] is False and r["contradicted"] == ["turn_schemas"]
    # The agent's edit, in a copy: the candidate's attribute becomes the next schema generation.
    facts = _facts()
    facts["turn_schemas"]["value"].append({"adapter": "message-unit-v1", "attr": "data-message-unit",
                                           "level": "message", "role_from": "last_colon_segment",
                                           "send_rank": 3})
    edited = tmp_path / "chatgpt_contract.json"
    edited.write_text(json.dumps({"facts": facts}))
    r = probe.replay(inc, str(edited))
    assert r["ok"] is True, r
    assert r["detail"] == f"{len('the verdict')}-char answer read for {RID}"
    assert cdp.UI["turn_schemas"] == _facts()["turn_schemas"]["value"], "the running driver is untouched"


def test_replay_is_a_command_and_needs_no_browser(mods, monkeypatch, tmp_path):
    cdp, probe, drift = mods
    _fast(monkeypatch, cdp)
    tab = _Tab(drift, _page("data-message-unit"))
    with pytest.raises(probe.Drift) as e:
        probe.probe_reader(probe._NoSend(tab), RID, settle=0, diagnose=True)
    inc = re.match(r"diagnosis (\S+)/diagnosis\.json", e.value.cause).group(1)
    monkeypatch.setattr(cdp, "CDP", lambda *a, **k: pytest.fail("replay opened a tab"))
    monkeypatch.setattr(sys, "argv", ["cgc_probe.py", "--replay", inc])
    assert probe.main() == 3


def test_no_bundle_on_the_first_pass_or_behind_a_blocker(mods, monkeypatch):
    cdp, probe, drift = mods
    _fast(monkeypatch, cdp)
    tab = _Tab(drift, _page("data-message-unit"))
    with pytest.raises(probe.Drift) as e:
        probe.probe_reader(probe._NoSend(tab), RID, settle=0)
    assert not e.value.cause.startswith("diagnosis ")
    monkeypatch.setattr(cdp, "_blocker", lambda c: "login")
    assert cdp._diagnose(tab, "reader", "x", "y") is None
    assert not os.path.isdir(os.path.join(os.environ["CGC_DATA_DIR"], "incidents"))


def test_the_bundle_is_capped_and_its_page_text_bounded(mods):
    cdp, _probe, drift = mods
    big = ["main", {}, [["div", {"data-turn": "user", "class": "x" * 500}, ["word " * 2000]]
                        for _ in range(50)]]
    snap = {"counts": [], "candidates": [{"selector": f"[data-x{i}]", "matches": 1, "visible": 1}
                                          for i in range(40)],
            "tree": ["body", {}, [big]], "path": "/c/x"}
    d, _fixture = drift.bundle(cdp.UI, snap, cap="reader", code="UI_CONTRACT_BROKEN", detail="d" * 900,
                               contract_path=cdp.CONTRACT_PATH)
    assert len(json.dumps(d, indent=1).encode()) <= drift.JSON_CAP
    assert len(d["detail"]) == 300


def test_the_capture_sanitizes_on_the_page(mods):
    """The page-side walk itself keeps allowlisted attributes and <= 200 chars of text."""
    cdp, _probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    tree = ["html", {}, [["body", {}, [
        ["script", {}, ["secret()"]],
        ["main", {}, [["div", {"data-turn": "user", "style": "x", "onclick": "steal()", "href": "/x"},
                       ["A" * 500]],
                      ["input", {"type": "password", "value": "hunter2"}, []]]]]]]]
    t = drift.FixtureTab("", "/c/abc-123")
    t.tree = [tree]
    snap = t.eval(drift.capture_js(cdp.UI, "reader"))
    page = json.dumps(snap["tree"])
    assert "secret()" not in page and "steal()" not in page and "hunter2" not in page
    assert '"data-turn": "user"' in page and '"type": "password"' in page
    assert page.count("A") <= 201 and snap["path"] == "/c/x"


def test_a_send_refusal_carries_its_bundle_into_the_attempt_and_the_daemon_log(mods, capsys):
    import cgc_backend
    path = "/state/cgc/incidents/I-1-model-abc/diagnosis.json"
    ev = cgc_backend._send_evidence({
        "code": 3, "outcome": {"submitted": False, "reason": "model_not_selectable"},
        "stderr": f"CGC_ERROR model_not_selectable: x\nCGC_DRIFT_BUNDLE model: {path} — edit ...\n"})
    assert ev["diagnosis"] == path
    assert f"CGC_UI_DIAGNOSIS model_not_selectable: {path}" in capsys.readouterr().err
