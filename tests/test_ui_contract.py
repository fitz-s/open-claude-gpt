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
          'type="password', "too many requests", "allow once", "允许", "停止", "发送", "拒绝",
          "querySelector('main')", "'plugins'", "Extra High", "upgrade|")


def _code_strings(name):
    """Every string constant in the file's code: docstrings (the first statement of a module, class
    or function) are prose about the page and are left out; every other string, triple-quoted JS
    included, is checked."""
    import ast
    tree = ast.parse(_src(name))
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
            docs.add(id(n.body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs]


@pytest.mark.parametrize("name", ["cdp_consult.py", "cgc_probe.py"])
def test_no_contract_owned_literal_is_written_twice(name):
    code = _code_strings(name)
    hits = sorted({lit for lit in _OWNED for s in code if lit in s})
    # Every selector, attribute and label the contract holds, too: none may be spelled in code
    # (values short enough to be ordinary words are covered by the list above instead).
    vals = []
    for v in _facts().values():
        stack = [v["value"]]
        while stack:
            x = stack.pop()
            if isinstance(x, str):
                vals.append(x)
            elif isinstance(x, (list, dict)):
                stack.extend(x.values() if isinstance(x, dict) else x)
    hits += sorted({x for x in vals if len(x) >= 12 and any(x in s for s in code)})
    assert hits == [], f"{name} re-spells contract facts: {hits}"


def test_the_literal_check_sees_triple_quoted_js():
    import ast
    src = 'X = """var b=document.querySelector(\'[data-testid="send-button"]\')"""\n'
    strings = [n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant)]
    assert any("send-button" in s for s in strings)


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


def _capture(drift, cdp, tree, cap="reader"):
    t = drift.FixtureTab("", "/c/abc")
    t.tree = [tree]
    return t.eval(drift.capture_js(cdp.UI, cap))


def test_sol_attribute_values_share_the_text_budget_and_lose_urls_and_paths(mods):
    cdp, _probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    tree = ["html", {}, [["body", {}, [["main", {}, [
        ["div", {"data-turn": "user", "data-preview": "private " * 100,
                 "data-href": "https://private.example/secret", "app-src": "/" + "Users/private/file",
                 "data-testid": "conversation-turn-3", "aria-label": "Edit message"}, ["hi"]]]]]]]]
    page = json.dumps(_capture(drift, cdp, tree)["tree"])
    assert page.count("private") <= 10, "an attribute's prose is cut to the shared budget"
    assert "private.example" not in page and "/Users/" not in page
    assert '"data-turn": "user"' in page and '"data-testid": "conversation-turn-3"' in page


def test_sol_identifier_shapes_and_candidates_cannot_carry_content(mods):
    cdp, _probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    leak = "privateconversationcontent"
    nodes = [["div", {"id": leak * 2, "class": leak, "aria-describedby": leak * 3,
                      "data-k": leak + ":user", "data-x": "constructor"}, []] for _ in range(10)]
    nodes.append(["div", {"data-message-id": "1", "aria-label": "https://private.example/secret"},
                  ["see /" + "Users/private/file now"]])
    tree = ["html", {}, [["body", {}, [["main", {}, nodes]]]]]
    snap = _capture(drift, cdp, tree)
    out = json.dumps(snap)
    assert out.count(leak) <= 12, out.count(leak)
    assert "private.example" not in out and "Users/private" not in out
    d, fx = drift.bundle(cdp.UI, snap, cap="reader", code="x", contract_path=cdp.CONTRACT_PATH,
                         detail="the popup offered 'https://private.example/x' and /" + "Users/me/f")
    assert "private.example" not in d["detail"] and "Users/me" not in d["detail"]


def test_sol_a_one_node_wrapper_still_reads_after_capture(mods, monkeypatch):
    """BEGIN, body and END in ONE text node: the fixture keeps the markers and a stand-in body."""
    cdp, probe, drift = mods
    _fast(monkeypatch, cdp)
    tree = _page()
    md = tree[2][0][2][1][2][0][2][1][2][0]                       # the markdown body
    md[2] = [["p", {}, ["BEGIN_RESPONSE:%s\nthe verdict\nEND_RESPONSE:%s" % (RID, RID)]]]
    snap = _capture(drift, cdp, tree)
    tab = _Tab(drift, ["html", {}, [snap["tree"]]])
    ans, miss = probe._read(probe._NoSend(tab), RID)
    assert miss is None and ans.strip()


def _picker_tree(families, announced=True):
    """The GPT-6 picker as read live 2026-09-06: family radios, and a slider whose wrapper's
    aria-describedby names the tier announcement."""
    radios = [["div", {"role": "menuitemradio", "aria-checked": "true" if i == 0 else "false"}, [f]]
              for i, f in enumerate(families)]
    slider = ["div", {"role": "menuitem", "aria-label": "Power", "aria-describedby": "_r_cn_"},
              [["span", {"role": "slider", "aria-valuenow": "4", "aria-valuemin": "0",
                         "aria-valuemax": "4"}, []]]]
    desc = ["span", {"id": "_r_cn_"}, ["Pro, 5 of 5." if announced else ""]]
    owner = ["div", {"data-testid": "composer-intelligence-picker-content"}, radios + [slider, desc]]
    return ["html", {}, [["body", {}, [["div", {"role": "menu"}, [owner]]]]]]


def _bundle_of(drift, cdp, cap, tree, inputs, tmp_path):
    t = drift.FixtureTab("", "/g/x")
    t.tree = [tree]
    snap = t.eval(drift.capture_js(cdp.UI, cap))
    return drift.record(snap, cdp.UI, cap=cap, code="x", detail="y", contract_path=cdp.CONTRACT_PATH,
                        inputs=inputs)


def test_sol_model_replay_runs_the_family_decision(mods, tmp_path):
    cdp, probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    inc = _bundle_of(drift, cdp, "model", _picker_tree(["Alien", "Other"]),
                     {"model": "Pro", "family": "GPT-6"}, tmp_path)
    r = probe.replay(inc)
    assert r["ok"] is False and "GPT-6" in r["detail"], r
    inc = _bundle_of(drift, cdp, "model", _picker_tree(["Latest", "GPT-5.5"]),
                     {"model": "Pro", "family": "GPT-6"}, tmp_path)
    r = probe.replay(inc)
    assert r["ok"] is True, r
    # Sol: the family offered but the tier unreadable (no announcement) is not a pass.
    inc = _bundle_of(drift, cdp, "model", _picker_tree(["GPT-6"], announced=False),
                     {"model": "Pro", "family": "GPT-6"}, tmp_path)
    assert probe.replay(inc)["ok"] is False


def test_sol_model_replay_accepts_the_flat_menu_fallback(mods, tmp_path):
    cdp, probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    tree = ["html", {}, [["body", {}, [["div", {"role": "menu"}, [
        ["div", {"role": "menuitem"}, ["Instant"]], ["div", {"role": "menuitem"}, ["Pro Extended"]]]]]]]]
    inc = _bundle_of(drift, cdp, "model", tree, {"model": "Pro", "family": "skip"}, tmp_path)
    assert probe.replay(inc)["ok"] is True


def test_sol_model_replay_follows_the_drivers_legacy_rules(mods, tmp_path):
    """A flat Pro menu with no picker (the pre-picker composer) settles the family; a bare 'Pro'
    slider (legacy, no announcement) reads its tier the way _slider_state_label does."""
    cdp, probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    flat = ["html", {}, [["body", {}, [["div", {"role": "menu"}, [
        ["div", {"role": "menuitem"}, ["Pro"]]]]]]]]
    assert probe.replay(_bundle_of(drift, cdp, "model", flat, {"model": "Pro", "family": "GPT-6"},
                                   tmp_path))["ok"] is True
    bare = ["html", {}, [["body", {}, [["div", {"role": "menu"}, [
        ["div", {"data-testid": "composer-intelligence-picker-content"}, [
            ["div", {}, ["Pro"]],
            ["div", {"role": "menuitem"}, [["span", {"role": "slider", "aria-valuenow": "4"}, []]]]]]]]]]]]
    assert probe.replay(_bundle_of(drift, cdp, "model", bare, {"model": "Pro", "family": "skip"},
                                   tmp_path))["ok"] is True


def test_sol_backticked_paths_and_unstructured_data_attrs_are_budgeted(mods):
    cdp, _probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    nodes = [["div", {"data-preview": "privatecontent" + str(i)}, []] for i in range(40)]
    nodes.append(["p", {"data-message-author-role": "user"}, ["see `/" + "Users/private/file` now"]])
    snap = _capture(drift, cdp, ["html", {}, [["body", {}, [["main", {}, nodes]]]]])
    out = json.dumps(snap["tree"])
    assert "Users/private" not in out and out.count("privatecontent") <= 15
    assert '"data-message-author-role": "user"' in out


def test_sol_composer_replay_runs_the_resolver(mods, tmp_path):
    cdp, probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    ed = ["div", {"role": "textbox", "contenteditable": "true"}, []]
    in_article = ["html", {}, [["body", {}, [["form", {}, [["article", {}, [ed]]]]]]]]
    assert probe.replay(_bundle_of(drift, cdp, "composer", in_article, {}, tmp_path))["ok"] is False
    in_form = ["html", {}, [["body", {}, [["form", {}, [ed]]]]]]
    assert probe.replay(_bundle_of(drift, cdp, "composer", in_form, {}, tmp_path))["ok"] is True


def test_sol_family_names_compare_alike_on_both_sides(mods):
    cdp = mods[0]
    assert cdp._is_family("Latest", "Latest") and cdp._is_family("latest", "GPT-6")
    assert cdp._is_family("GPT-6", "latest")


def test_sol_contract_values_splice_as_js(mods, tmp_path):
    cdp = mods[0]
    assert cdp._js_str("停止") == "'\\u505c\\u6b62'"
    for bad in ("x\ny", "\U0001f600"):
        facts = _facts()
        facts["composer"]["value"] = bad
        p = tmp_path / "c.json"
        p.write_text(json.dumps({"facts": facts}))
        with pytest.raises(SystemExit, match="contract_invalid"):
            cdp._load_contract(str(p))


def test_sol_replay_leaves_cgc_contract_as_it_found_it(mods, monkeypatch):
    _cdp, probe, _drift = mods
    monkeypatch.setenv("CGC_CONTRACT", CONTRACT)
    probe._driver(CONTRACT)
    assert os.environ["CGC_CONTRACT"] == CONTRACT


def test_a_send_refusal_carries_its_bundle_into_the_attempt_and_the_daemon_log(mods, capsys):
    import cgc_backend
    path = "/state/cgc/incidents/I-1-model-abc/diagnosis.json"
    ev = cgc_backend._send_evidence({
        "code": 3, "outcome": {"submitted": False, "reason": "model_not_selectable"},
        "stderr": f"CGC_ERROR model_not_selectable: x\nCGC_DRIFT_BUNDLE model: {path} — edit ...\n"})
    assert ev["diagnosis"] == path
    assert f"CGC_UI_DIAGNOSIS model_not_selectable: {path}" in capsys.readouterr().err
