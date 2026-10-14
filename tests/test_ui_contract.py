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
          "data-turn-key", "data-markdown-text-style", "assistant-message",
          "prompt-textarea", "contenteditable=", "app-mention", "trailingBreak", "data-mention-list",
          "data-list-navigation-item", "data-menu-row-content", "rounded-2xs", "text-codex-description",
          "composer-intelligence-picker-content", "composer-plus-btn", "menuitemradio", 'role="slider"',
          'role=\\"slider', "radiogroup", "aria-haspopup=\\\"menu", 'aria-expanded=\\"true',
          "data-message-model-slug", "Thinking failed", "Message stream error", "captcha\\\" i",
          'type="password', "too many requests",
          "querySelector('main')", "'plugins'", "Extra High", "upgrade|")


# Send authority restates what it needs (LAW 7): these code constants deliberately repeat page
# facts — the message/turn attributes a click may never land in, the editor shape, the attach
# button, the app-row icon, the non-switcher words — because what may be clicked is decided by code
# that no contract edit can move. They are the ONLY place a contract-shaped literal may appear twice.
_AUTHORITY_CONSTANTS = {"_MESSAGE_SCOPE_SEL", "_EDITOR_SHAPE_SEL", "_NOT_SWITCHER_SRC", "_PLUS_BUTTON_ID",
                        "_MENTION_APP_SECTION", "_MENTION_APP_ICON", "_MENU_WIDGET_SEL"}


def _code_strings(name):
    """Every string constant in the file's code: docstrings (the first statement of a module, class
    or function) are prose about the page and are left out, as are the send-authority constants
    (_AUTHORITY_CONSTANTS); every other string, triple-quoted JS included, is checked."""
    import ast
    tree = ast.parse(_src(name))
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
            skip.add(id(n.body[0].value))
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in _AUTHORITY_CONSTANTS
                                             for t in n.targets):
            skip.update(id(c) for c in ast.walk(n.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


# The schema's own enumeration words (role_from, level), which the loader's type check must name.
_SCHEMA_WORDS = {"last_colon_segment"}


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
    hits += sorted({x for x in vals if len(x) >= 12 and x not in _SCHEMA_WORDS and any(x in s for s in code)})
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


def _write(tmp_path, facts):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"facts": facts}))
    return str(p)


REPRO = ("/private/tmp/claude-501/-Users-leofitz-chatgpt-consult/67c829ec-c5f0-415e-8ed9-5f9b7a6a8b0b/"
         "scratchpad/contract-review-repro")
# The reviewer's payload: a LIST where an attribute name belongs, which a repr() splice turned into
# executable JS inside a single-quoted string.
_INJECTION = [");};globalThis.contractReviewInjected=1;function __review(){return void("]


def test_law1_the_reviewers_list_payload_is_refused(tmp_path, mods):
    cdp = mods[0]
    facts = _facts()
    facts["turn_schemas"]["value"][0]["attr"] = _INJECTION
    with pytest.raises(SystemExit, match="contract_invalid"):
        cdp._load_contract(_write(tmp_path, facts))


@pytest.mark.parametrize("fid", sorted(_facts()))
@pytest.mark.parametrize("wrong", [None, 1, True, ["x"], {"a": "b"}, "", "a\nb"])
def test_law1_every_id_refuses_every_other_type(tmp_path, mods, fid, wrong):
    cdp = mods[0]
    facts = _facts()
    v = facts[fid]["value"]
    if type(wrong) is type(v) and wrong != "" and wrong != "a\nb":
        return                                          # same type: not a mismatch to test here
    facts[fid]["value"] = wrong
    with pytest.raises(SystemExit, match="contract_invalid"):
        cdp._load_contract(_write(tmp_path, facts))


def test_law1_an_unknown_or_missing_id_is_refused(tmp_path, mods):
    cdp = mods[0]
    facts = _facts()
    facts["extra"] = {"value": "x", "why": "2026-10"}
    with pytest.raises(SystemExit, match="contract_invalid"):
        cdp._load_contract(_write(tmp_path, facts))
    facts = _facts()
    del facts["composer"]
    with pytest.raises(SystemExit, match="contract_invalid"):
        cdp._load_contract(_write(tmp_path, facts))


def test_law1_a_quote_in_a_selector_is_encoded_not_executed(tmp_path, mods):
    """With the JSON encoding the character filter is only control characters: a quote or a
    backslash in a selector is data, and the JS it lands in still parses to that exact string."""
    cdp = mods[0]
    if not shutil.which("node"):
        pytest.skip("node not installed")
    import subprocess
    for s in ("div[data-x='y']", 'a[b="c"]', "x\\y", "停止", "\U0001f600", "');alert(1);('"):
        js = "console.log(JSON.stringify(" + cdp._js_str(s) + "))"
        assert json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                         check=True).stdout) == s
    facts = _facts()
    facts["composer"]["value"] = "div[data-x='y']"
    assert cdp._load_contract(_write(tmp_path, facts))["composer"] == "div[data-x='y']"
    with pytest.raises(TypeError):
        cdp._js_str(["not", "a", "string"])


def test_law4_an_invalid_contract_blocks_the_round_as_proven_not_sent(tmp_path, mods, monkeypatch):
    """The driver dies at import on an invalid contract, before any browser call. Through
    process_round, with the driver's own real stderr: blocked, not_sent_proven, the error kept, and
    refire eligible once the contract is fixed."""
    import subprocess
    import cgc_backend
    import cgc_store
    facts = _facts()
    facts["turn_schemas"]["value"][0]["attr"] = _INJECTION
    monkeypatch.setenv("CGC_CONTRACT", _write(tmp_path, facts))
    drv = subprocess.run([sys.executable, os.path.join(SCRIPTS, "cdp_consult.py"), "submit",
                          "--prompt-file", "-"], input="p", capture_output=True, text=True)
    assert drv.returncode != 0 and drv.stderr.startswith("CGC_ERROR contract_invalid")
    rid = "REQ-20261014-120000-0000d1"
    with cgc_store.Store() as s:
        s.create_round(rid, "submit", prompt="p", spec_json=json.dumps({"model": "Pro"}))
        s.set_state(rid, cgc_store.READY)
        res = {"code": drv.returncode, "conversation": "", "stderr": drv.stderr, "outcome": {}}
        final = cgc_backend.process_round(s, s.get_round(rid), lambda kind, **kw: res,
                                          daemon_instance_id="d", validate=lambda p: (True, "ok"))
        r = s.get_round(rid)
        assert final == cgc_store.BLOCKED and r["state"] == cgc_store.BLOCKED
        assert r["send_disposition"] == cgc_store.NOT_SENT_PROVEN
        assert r["error_code"].startswith("contract_invalid") and "turn_schemas" in r["error_code"]
        assert cgc_backend._release_eligible(s, r), "refire may resend it once the contract is fixed"


# Updated 2026-10-14: approval labels moved to code, approval search excludes the composer form.
_GOLDEN_JS = "bfbf3b842976be524962828e4de926d0063b74c6d02d132fbfbbe33674bc6315"


def _generated_js(cdp):
    """Every JS string the driver generates from the shipped contract, keyed by name (the
    sample-argument dump the refactor was proven EXACT_EQUAL against)."""
    import inspect
    sample = {"rid": "REQ-20261011-120000-0000a1", "target": "Pro", "name": "WebCodex Demo",
              "token": "tok1", "chunk": "a\nb", "label": "6", "allow": ["WebCodex Demo"], "i": 1}
    out = {}
    for k in sorted(dir(cdp)):
        v = getattr(cdp, k)
        if k in ("_SEND_BUTTON_SEL", "_STOP_BUTTON_SEL"):
            continue                                    # names the reviewed dump did not have
        if isinstance(v, str) and ("function" in v or "querySelector" in v or "[" in v):
            out[k] = v
        elif isinstance(v, tuple) and k.startswith("_ADAPT"):
            out[k] = repr(v)
        elif callable(v) and k.endswith("_js") and inspect.isfunction(v):
            sig = inspect.signature(v)
            args, ok = {}, True
            for p in sig.parameters.values():
                if p.kind == p.KEYWORD_ONLY:
                    continue
                if p.name in sample:
                    args[p.name] = sample[p.name]
                elif p.default is p.empty:
                    ok = False
            if not ok:
                out[k] = "SKIP " + str(sig)
                continue
            out[k] = v(**args)
            if "scoped" in sig.parameters:
                out[k + "[scoped]"] = v(**args, scoped=True)
    return out


def test_law1_the_shipped_contract_generates_exactly_the_reviewed_js(mods):
    """Updated once for LAW 7 (the click-authority guard): the golden hash is the guarded output;
    test_law7_the_generated_js_is_mains_except_the_guard proves what changed against main."""
    import hashlib
    js = _generated_js(mods[0])
    assert hashlib.sha256(json.dumps(js, sort_keys=True).encode()).hexdigest() == _GOLDEN_JS


# main (fdfaa5d)'s generated JS, dumped with _generated_js's sample arguments.
_MAIN_JS = os.path.join(ROOT, "tests", "fixtures", "main_generated_js.json")
# The deliberate non-guard changes since main, each reviewed: the send contract's stop selector
# gained the waiter's Chinese label; the approval regexes write their CJK as \u escapes, and the
# approval search skips any Allow once inside a form (46ed0bc).
_MAIN_DELTAS = {"_contract_js": [('button[aria-label*="Stop"]\')', 'button[aria-label*="Stop"],'
                                  'button[aria-label*="\\u505c\\u6b62"]\')')],
                "_approval_js": [("/^(deny|拒绝)/", "/^(deny|\\u62d2\\u7edd)/"),
                                 ("/^(allow once|允许一次)/", "/^(allow once|\\u5141\\u8bb8\\u4e00\\u6b21)/"),
                                 # 46ed0bc: the approval search skips any Allow once inside a form
                                 ("return vis(b)&&/^(allow once|\\u5141", "return vis(b)&&!(b.closest&&b.closest('form'))&&/^(allow once|\\u5141")]}


def test_law7_the_generated_js_is_mains_except_the_guard(mods):
    """Every generated JS string equals main's once the click-authority guard is taken out: the
    _AUTHORITY_FN prelude and its call sites. Nothing else about the page JS changed."""
    import re as _re
    cdp = mods[0]
    main = json.load(open(_MAIN_JS))
    now = _generated_js(cdp)
    assert set(main) <= set(now)
    subs = [(cdp._AUTHORITY_FN, ""),
            ("if(!EL||!__cgcMayClick(EL,'switcher'))return null;", "if(!EL)return null;"),
            (".filter(function(x){return __cgcMayClick(x,'picker');});", ";"),
            ("if(!mi||!__cgcMayClick(mi,'picker')||!__cgcMayClick(s,'picker'))return false;", "if(!mi)return false;"),
            ("if(!EL||!__cgcMayClick(EL,'picker'))return false;", "if(!EL)return false;"),
            ("if(!__cgcMayType(d))return -1;", "if(!d)return -1;"),
            ("if(!__cgcMayType(d))return null;", "if(!d)return null;"),
            ("if(!__cgcMayType(d))return 'no-composer';", "if(!d)return 'no-composer';"),
            ("if(app&&__cgcMayClick(ok[i],'approve'))ok[i].click();else if(app)app=null;", "if(app)ok[i].click();"),
            (cdp._SEND_MARK, "")]
    mention = _re.compile(r"var root=document\.querySelector\([^;]*\);if\(!__cgcMayClick\(hit,'mention',root\)\)"
                          r"return \{[^}]*\};")
    for k, want in main.items():
        got = now[k]
        for a, b in subs:
            got = got.replace(a, b)
        got = mention.sub("", got)
        for a, b in _MAIN_DELTAS.get(k, []):
            want = want.replace(a, b, 1)
        assert got == want, k


def test_law2_the_send_button_is_not_in_the_contract(tmp_path, mods):
    """Send authority is code: the contract has no id for it, and adding one is refused."""
    cdp = mods[0]
    assert "send_button" not in _facts() and "stop_button" not in _facts()
    for bad in ("widened-contract.json", "invalid-contract.json"):
        path = os.path.join(REPRO, bad)
        if not os.path.exists(path):
            pytest.skip("reviewer repro not present")
        with pytest.raises(SystemExit, match="contract_invalid"):
            cdp._load_contract(path)


def test_law2_no_contract_edit_can_widen_what_the_send_click_clicks(tmp_path, mods, monkeypatch):
    """The send click's selector is the code constant whatever contract is loaded."""
    _cdp, probe, _drift = mods
    facts = _facts()
    for k, v in facts.items():                         # every selector fact widened to `button`
        if isinstance(v["value"], str) and "[" in v["value"]:
            v["value"] = "button"
    drv = probe._driver(_write(tmp_path, facts))
    js = drv._send_click_js()
    # The only button query inside the composer's form is the code constant.
    queries = re.findall(r"f\.querySelectorAll\(([^)]*)\)", js)
    assert set(queries) == {drv._js_str(drv._SEND_BUTTON_SEL), drv._js_str(drv._EDITOR_SHAPE_SEL)}, \
        "every form query is a code constant: the send button and the editor shape"
    if not shutil.which("node"):
        pytest.skip("node not installed")
    # The reviewer's case, in a real DOM: a form holding the editor and a Cancel button only.
    t = mods[2].FixtureTab("", "/c/x")
    t.tree = [["html", {}, [["body", {}, [["form", {}, [
        ["div", {"role": "textbox", "contenteditable": "true"}, []],
        ["button", {"aria-label": "Cancel"}, ["Cancel"]]]]]]]]]
    assert t.eval(drv._send_click_js()) != "clicked", "Cancel is never clicked"
    assert t.eval(mods[0]._send_click_js()) in ("no-send", "no-focus"), "nor under the shipped contract"


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
    d, _fixture = drift.bundle(cdp.UI, snap, cap="reader", code="UI_CONTRACT_BROKEN", detail="word " * 180,
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


def test_sol_candidate_selectors_carry_no_page_prose(mods):
    cdp, _probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    nodes = [["div", {"data-model-label": f"private-model-description-{i}abcdefghi"}, []]
             for i in range(8)]
    nodes.append(["div", {"data-message-unit": "t1:2:assistant"}, []])
    snap = _capture(drift, cdp, ["html", {}, [["body", {}, [["main", {}, nodes]]]]], cap="model")
    sels = [c["selector"] for c in snap["candidates"]]
    assert not any("private-model-description" in s for s in sels), sels
    assert not any("data-model-label" in s for s in sels), "a name off the allowlist is not kept"
    snap = _capture(drift, cdp, ["html", {}, [["body", {}, [["main", {}, nodes]]]]])
    assert '[data-message-unit$=":assistant"]' in [c["selector"] for c in snap["candidates"]]


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
    facts = _facts()
    facts["composer"]["value"] = "x\ny"
    with pytest.raises(SystemExit, match="contract_invalid"):
        cdp._load_contract(_write(tmp_path, facts))


def test_sol_replay_leaves_cgc_contract_as_it_found_it(mods, monkeypatch):
    _cdp, probe, _drift = mods
    monkeypatch.setenv("CGC_CONTRACT", CONTRACT)
    probe._driver(CONTRACT)
    assert os.environ["CGC_CONTRACT"] == CONTRACT


# ---- law 3: privacy by allowlist ------------------------------------------------------------------

def test_law3_the_reviewers_cases_keep_nothing_private(mods, tmp_path):
    cdp, _probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    long_name = "data-" + "x" * 360                          # a 365-character attribute name
    nodes = [
        ["div", {"data-message-author-role": "user", "data-cookie": "sid=abc123",
                 "data-session-id": "s3cr3t", long_name: "v", "data-preview": "private text",
                 "data-testid": "https://private.example/secret", "aria-label": "../../etc/passwd",
                 "class": "flex hiddenvalue=1 " + "a" * 40}, ["see ./notes/plan.md and https://x.io/a"]],
        ["div", {"data-message-author-role": "assistant", "data-src": "/" + "Users/me/f"}, ["ok"]]]
    tree = ["html", {}, [["body", {}, [["main", {}, nodes]]]]]
    snap = _capture(drift, cdp, tree)
    d, fixture = drift.bundle(cdp.UI, snap, cap="reader", code="x", contract_path=cdp.CONTRACT_PATH,
                              detail="the popup offered https://private.example/x and ../a/b")
    out = fixture + json.dumps(d)
    for leak in ("private.example", "x.io", "etc/passwd", "notes/plan", "Users/me", "sid=", "abc123",
                 "s3cr3t", "x" * 41, "private text", "hiddenvalue", "a" * 33):
        assert leak not in out, leak
    assert "data-cookie" not in out and "data-session-id" not in out and "data-preview" not in out
    assert 'data-message-author-role="user"' in fixture and 'class="flex"' in fixture


def test_law3_page_text_shares_one_200_character_budget(mods):
    cdp, _probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    nodes = [["p", {"data-message-author-role": "user", "aria-label": "label words here"},
              ["secret words " * 10]] for _ in range(30)]
    snap = _capture(drift, cdp, ["html", {}, [["body", {}, [["main", {}, nodes]]]]])
    d, fixture = drift.bundle(cdp.UI, snap, cap="reader", code="x", detail="",
                              contract_path=cdp.CONTRACT_PATH)
    text = re.sub(r"<[^>]*>", "", fixture) + "".join(
        re.findall(r'aria-label="([^"]*)"', fixture))
    assert len(re.sub(r"[\s…]", "", text)) <= 200


# ---- law 5: replay fidelity ------------------------------------------------------------------------

def test_law5_a_hidden_composer_does_not_replay_as_present(mods, tmp_path):
    cdp, probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    hidden = ["html", {}, [["body", {}, [["form", {}, [
        ["div", {"role": "textbox", "contenteditable": "true", "hidden": ""}, ["draft"]]]]]]]]
    inc = _bundle_of(drift, cdp, "composer", hidden, {}, tmp_path)
    assert 'data-cgc-invisible=""' in open(os.path.join(inc, "fixture.html")).read()
    assert probe.replay(inc)["ok"] is False
    old = os.path.join(REPRO, "incidents", "I-20261014-161205-composer-21cbe0")
    if os.path.isdir(old):
        assert probe.replay(old)["ok"] is False, "the reviewer's hidden-composer capture"


def test_law5_the_recorded_mention_popup_replays_as_a_hit(mods, tmp_path):
    """tests/fixtures/mention_popup.json webcodex_demo, captured and replayed with the shipped
    contract: the label and description spans (flex items) read as two lines, not one."""
    cdp, probe, drift = mods
    if not shutil.which("node"):
        pytest.skip("node not installed")
    fx = json.load(open(os.path.join(ROOT, "tests", "fixtures", "mention_popup.json")))
    popup = drift.parse_fixture(fx["cases"]["webcodex_demo"]["popup_html"])
    tree = ["html", {}, [["body", {}, popup]]]
    inc = _bundle_of(drift, cdp, "mention", tree, {"app": "WebCodex Demo"}, tmp_path)
    r = probe.replay(inc)
    assert r["ok"] is True, r


def test_a_send_refusal_carries_its_bundle_into_the_attempt_and_the_daemon_log(mods, capsys):
    import cgc_backend
    path = "/state/cgc/incidents/I-1-model-abc/diagnosis.json"
    ev = cgc_backend._send_evidence({
        "code": 3, "outcome": {"submitted": False, "reason": "model_not_selectable"},
        "stderr": f"CGC_ERROR model_not_selectable: x\nCGC_DRIFT_BUNDLE model: {path} — edit ...\n"})
    assert ev["diagnosis"] == path
    assert f"CGC_UI_DIAGNOSIS model_not_selectable: {path}" in capsys.readouterr().err


def test_a_free_text_attribute_name_never_rides_a_role_value(mods):
    """Review 2026-10-14: any attribute whose value was `user`/`assistant` was kept by name, so an
    email-shaped name reached the fixture. Only identifier-shaped names qualify."""
    cdp, _probe, drift = mods
    t = drift.FixtureTab("", "/c/x")
    t.tree = [["html", {}, [["body", {}, [["main", {}, [
        ["div", {"john.smith@gmail.com": "user", "Weird_Name": "assistant"}, ["q"]]]]]]]]]
    snap = t.eval(drift.capture_js(cdp.UI, "reader"))
    assert "john.smith@gmail.com" not in json.dumps(snap) and "Weird_Name" not in json.dumps(snap)


def test_a_class_heavy_page_never_starves_the_unit_attributes(mods):
    """Review 2026-10-14: class tokens shared the attribute budget, so a Tailwind-heavy answer
    dropped every unit attribute and replay could read no turn."""
    cdp, _probe, drift = mods
    tw = "markdown prose w-full break-words dark:prose-invert"
    kids = [["p", {"class": tw}, [["span", {"class": "hljs-keyword hljs-string"}, ["x"]]]] for _ in range(60)]
    t = drift.FixtureTab("", "/c/x")
    t.tree = [["html", {}, [["body", {}, [["main", {}, [
        ["div", {"data-message-author-role": "user"}, ["q"]],
        ["div", {"data-message-author-role": "assistant"}, kids]]]]]]]]
    snap = t.eval(drift.capture_js(cdp.UI, "reader"))
    units = snap["tree"][2][0][2]
    assert [u[1].get("data-message-author-role") for u in units] == ["user", "assistant"]
