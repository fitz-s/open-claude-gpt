# LAW 7: a click is authorized by code, never located by data alone. The contract LOCATES widgets;
# cdp_consult's page-side __cgcMayClick / __cgcMayType decide whether an element may be clicked,
# focused or typed into, and every action helper asks them first. These run every helper, under
# contracts edited to point anywhere, on a fixture DOM that holds a send button, a stop button, an
# approval card and an edit-message form, and record every click, focus and keystroke: none may
# land on any of them. The reviewer's cases (stable_contract_authority_repro.py) come first.
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
CONTRACT = os.path.join(SCRIPTS, "chatgpt_contract.json")
FIXTURE_JS = os.path.join(SCRIPTS, "fixture_dom.js")
sys.path.insert(0, SCRIPTS)

if not shutil.which("node"):
    pytest.skip("node not installed", allow_module_level=True)


def _facts():
    with open(CONTRACT, encoding="utf-8") as f:
        return json.load(f)["facts"]


_DRIVERS = {}


def _driver(tmp_path, edits):
    """A fresh cdp_consult loaded on the shipped contract with `edits` applied."""
    key = json.dumps(edits, sort_keys=True)
    if key in _DRIVERS:
        return _DRIVERS[key]
    facts = _facts()
    for k, v in edits.items():
        facts[k]["value"] = v
    p = tmp_path / ("c%d.json" % len(_DRIVERS))
    p.write_text(json.dumps({"facts": facts}))
    os.environ["CGC_CONTRACT"] = str(p)
    try:
        spec = importlib.util.spec_from_file_location("_authority_driver_%d" % len(_DRIVERS),
                                                      os.path.join(SCRIPTS, "cdp_consult.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        os.environ.pop("CGC_CONTRACT", None)
    _DRIVERS[key] = mod
    return mod


# Every element's click(), dispatched click event, focus() and the editor's typing are recorded.
_SPY = """(function(){var log=[];var E=document.documentElement.constructor.prototype;
E.click=function(){log.push(['click',this.id||this.tagName])};
E.dispatchEvent=function(e){if(/^(click|pointerdown|mousedown|pointerup|mouseup|keydown)$/.test(e.type))
 log.push([e.type,this.id||this.tagName]);return true};
E.focus=function(){document.activeElement=this;log.push(['focus',this.id||this.tagName])};
document.execCommand=function(c,u,t){if(c==='insertText'||c==='delete')log.push([c,(document.activeElement||{}).id]);return true};
var r;try{r=EXPR}catch(x){r='threw:'+x.message}return {result:r,log:log};})()"""


def _run(tree, expr, active=None):
    js = _SPY.replace("EXPR", expr)
    if active:
        js = "(function(){document.activeElement=document.getElementById(%s);return %s})()" % (
            json.dumps(active), js)
    p = subprocess.run(["node", FIXTURE_JS], input=json.dumps({"tree": tree, "js": js, "path": "/c/x"}),
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _page(*children):
    return [["html", {}, [["body", {}, list(children)]]]]


EDITOR = ["div", {"id": "editor", "role": "textbox", "contenteditable": "true"}, []]
SEND = ["button", {"id": "send", "data-testid": "send-button", "aria-label": "Send"}, ["Send"]]
STOP = ["button", {"id": "stop", "data-testid": "stop-button", "aria-label": "Stop"}, ["Stop"]]
COMPOSER = ["form", {"id": "composer-form"}, [EDITOR, SEND, STOP]]
APPROVAL = ["div", {"id": "card"}, [["div", {}, ["Other App"]],
                                    ["button", {"id": "allow"}, ["Allow once"]],
                                    ["button", {"id": "deny"}, ["Deny"]]]]
EDIT_FORM = ["article", {"data-message-author-role": "user"}, [
    ["form", {"id": "edit-form"}, [["div", {"id": "old-editor", "role": "textbox", "contenteditable": "true"},
                                     ["earlier message"]],
                                    ["button", {"id": "edit-send", "aria-label": "Send"}, ["Send"]]]]]]
PROTECTED = {"send", "stop", "allow", "deny", "edit-send", "old-editor", "edit-form", "composer-form",
             "editor", "card"}
TRAP = _page(COMPOSER, APPROVAL, EDIT_FORM)


def _helpers(d):
    """Every page-side action helper, as (name, js)."""
    return [
        ("open_switcher", d._open_cand_by_label_js("Send")),
        ("open_switcher_stop", d._open_cand_by_label_js("Stop")),
        ("flat_item", d._click_item_js("Send")),
        ("flat_item_pro", d._click_item_js("Pro")),
        ("submenu0", d._open_submenu_js(0)),
        ("submenu1", d._open_submenu_js(1)),
        ("family", d._click_family_js("Send")),
        ("family_allow", d._click_family_js("Allow once")),
        ("slider_focus", d._SLIDER_FOCUS_JS),
        ("clear", d._clear_composer_js()),
        ("paste", d._paste_chunk_js("private prompt")),
        ("mention_type", d._mention_type_js("Send", "tok")),
        ("mention_pick", d._mention_pick_js("Send", "tok")),
        ("approve", d._approval_js(["Other App"])),
    ]


def _landed(out):
    """The protected elements a helper's run touched."""
    return sorted({t for _a, t in out["log"] if t in PROTECTED})


# ---- the reviewer's cases, first ---------------------------------------------------------------

def test_sol_minimal_one_id_submenu_trigger_never_clicks_send(tmp_path, monkeypatch):
    """The minimal repro: picker_submenu_trigger set to the send button's own selector."""
    d = _driver(tmp_path, {"picker_submenu_trigger": 'button[data-testid="send-button"]'})
    out = _run(_page(["form", {}, [EDITOR, SEND]]), d._open_submenu_js(0))
    assert out["result"] is False and _landed(out) == []


def test_sol_minimal_case_through_the_whole_model_selection_reports_dirty_honestly(tmp_path, monkeypatch):
    """Whole _select_model under the one-id edit: nothing protected is clicked; and if anything at all
    was clicked before a failure, the pick reports dirty=true."""
    d = _driver(tmp_path, {"picker_submenu_trigger": 'button[data-testid="send-button"]'})
    pill = ["button", {"id": "pill", "aria-haspopup": "menu"}, ["Instant"]]
    tree = _page(["form", {}, [EDITOR, pill, SEND]])

    class Tab:
        log = []

        def eval(self, js, timeout=None):
            out = _run(tree, js)
            self.log.extend(out["log"])
            return out["result"]

        def key(self, *a):
            self.log.append(["key", a[0]])

    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    monkeypatch.setattr(d, "_picker_snap", lambda c: None)
    t = Tab()
    pick = d._select_model(t, "Pro", "GPT-6")
    assert pick.confirmed is False
    assert not [x for x in t.log if x[1] in PROTECTED], t.log
    # The only thing clicked is the composer's own menu trigger, whose menu the run then proved
    # closed: an undone mutation, so dirty=False is the truth (dirty = mutated and NOT proven undone).
    assert {x[1] for x in t.log if x[0] in ("click", "pointerdown")} <= {"pill"}
    assert pick.dirty is False


def test_a_menu_the_run_cannot_prove_closed_is_reported_dirty(tmp_path, monkeypatch):
    """Clicked something, then failed, and could not prove it undone: dirty=true, never false."""
    d = _driver(tmp_path, {})
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    monkeypatch.setattr(d, "_picker_snap", lambda c: None)
    pill = ["button", {"id": "pill", "aria-haspopup": "menu", "aria-expanded": "true"}, ["Instant"]]
    tree = _page(["form", {}, [EDITOR, pill, SEND]], ["div", {"role": "menu"}, [
        ["div", {"data-testid": "composer-intelligence-picker-content"}, [
            ["div", {"role": "menuitemradio", "aria-checked": "true"}, ["GPT-6"]],
            ["div", {"role": "menuitemradio", "aria-checked": "true"}, ["GPT-5.5"]]]]]])

    class Tab:
        def eval(self, js, timeout=None):
            return _run(tree, js)["result"]

        def key(self, *a):
            pass

    pick = d._select_model(Tab(), "Pro", "GPT-6")
    assert pick.confirmed is False and pick.dirty is True


@pytest.mark.parametrize("case,edits,tree,expr", [
    ("switcher-Send", {"switcher_labels": ["Send"]}, _page(["form", {}, [EDITOR, SEND]]),
     lambda d: d._open_cand_by_label_js("Send")),
    ("submenu-button-Send", {"picker_submenu_trigger": "button"}, _page(["form", {}, [EDITOR, SEND]]),
     lambda d: d._open_submenu_js(0)),
    ("submenu-button-Stop", {"picker_submenu_trigger": "button"}, _page(["form", {}, [EDITOR, STOP]]),
     lambda d: d._open_submenu_js(0)),
    ("submenu-button-Allow", {"picker_submenu_trigger": "button"},
     _page(["form", {}, [EDITOR, ["button", {"id": "allow"}, ["Allow once"]]]]), lambda d: d._open_submenu_js(0)),
    ("family-form-Send", {"picker_owner": "form", "picker_family_radio": "button",
                          "family_aliases": {"Send": "GPT-6"}}, _page(["form", {}, [EDITOR, SEND]]),
     lambda d: d._click_family_js("Send")),
    ("slider-focus-Send", {"picker_menu": "form", "picker_slider": "button", "picker_menuitem": "button"},
     _page(["form", {}, [EDITOR, SEND]]), lambda d: d._SLIDER_FOCUS_JS),
    ("comma-menuitem-slider", {"picker_menuitem": "form,body"},
     _page(["form", {}, [EDITOR, SEND, ["span", {"role": "slider"}, []]]]), lambda d: d._SLIDER_FOCUS_JS),
    ("flat-items-outside-menu", {"picker_flat_items": "button"},
     _page(["button", {"id": "send", "aria-label": "Upgrade"}, ["Pro"]]), lambda d: d._click_item_js("Pro")),
])
def test_sol_repro_cases_click_nothing_protected(tmp_path, case, edits, tree, expr):
    d = _driver(tmp_path, edits)
    out = _run(tree, expr(d))
    assert _landed(out) == [], (case, out)


def test_sol_composer_retargets_cannot_type_into_or_send_from_another_form(tmp_path):
    other = ["form", {"id": "edit-form"}, [["div", {"id": "old-editor", "contenteditable": "true"}, []],
                                            ["button", {"id": "edit-send", "aria-label": "Send"}, ["Send"]]]]
    d = _driver(tmp_path, {"composer": "#old-editor"})
    for js in (d._paste_chunk_js("private prompt"), d._clear_composer_js(), d._send_click_js()):
        out = _run(_page(["article", {}, [other]], ["form", {}, [EDITOR, SEND]]), js)
        assert _landed(out) == [], out
    d = _driver(tmp_path, {"composer_exclude_ancestor": "nav"})
    for js in (d._paste_chunk_js("replacement private prompt"), d._send_click_js()):
        out = _run(TRAP, js, active="old-editor")
        assert "old-editor" not in [t for _a, t in out["log"]] and "edit-send" not in [t for _a, t in out["log"]], out


def test_sol_a_second_visible_composer_form_makes_typing_and_sending_ambiguous(tmp_path):
    """composer='#other-editor' aims at an unrelated form's editor that sits in no message: two
    visible forms each holding a visible editor is ambiguous, so nothing is typed and nothing sent.
    A hidden second form (the live re-opened-thread shape) does not count."""
    other = ["form", {"id": "unrelated-form"}, [["div", {"id": "other-editor", "contenteditable": "true"}, []],
                                                 ["button", {"id": "other-send", "aria-label": "Send"}, ["Send"]]]]
    d = _driver(tmp_path, {"composer": "#other-editor"})
    tree = _page(other, ["form", {}, [EDITOR, SEND]])
    for js in (d._paste_chunk_js("private prompt"), d._send_click_js()):
        out = _run(tree, js)
        assert not [x for x in out["log"] if x[1] in ("other-editor", "other-send")], out
    hidden = ["form", {"id": "h", "hidden": ""}, [["div", {"id": "h-ed", "contenteditable": "true"}, []]]]
    shipped = _driver(tmp_path, {})
    out = _run(_page(hidden, ["form", {}, [EDITOR, SEND]]), shipped._send_click_js(), active="editor")
    assert out["log"] == [["click", "send"]]


def test_sol_a_file_row_is_never_picked_as_an_app_whatever_the_contract_says(tmp_path):
    """mention_section_header / mention_app_icon_class are no longer contract facts: an app row is
    identified by code."""
    assert "mention_section_header" not in _facts() and "mention_app_icon_class" not in _facts()
    row = ["button", {"id": "file-row", "data-list-navigation-item": "true"}, [
        ["div", {"data-menu-row-content": "true", "class": "flex"}, [
            ["span", {}, [["span", {"class": "text-file-document"}, []]]],
            ["span", {"class": "flex"}, [["span", {}, ["WebCodex Demo"]],
                                         ["span", {"class": "text-codex-description"}, ["A private file"]]]]]]]]
    tree = _page(["div", {"data-mention-list-scroll-area": ""}, [["div", {}, [["div", {}, ["Files"]], row]]]])
    d = _driver(tmp_path, {})
    out = _run(tree, d._mention_pick_js("WebCodex Demo", "tok"))
    assert out["result"]["hit"] is None and not [x for x in out["log"] if x[1] == "file-row"]


def test_sol_the_probes_no_send_handle_uses_the_drivers_own_send_selector(tmp_path):
    import cgc_probe
    assert cgc_probe._SEND_JS.search(cgc_probe.cdp._send_click_js())


# ---- the fuzz -----------------------------------------------------------------------------------
_KINDS = {"sel", "one", "words", "word", "alias", "class", "attr"}
_VALUES = {"sel": ["button", "*", "form", "body", "button,form"], "one": ["button", "*", "form", "body"],
           "words": [["Send"], ["Stop"], ["Allow once"], ["Deny"]],
           "word": ["Send", "Allow once"],
           "alias": [{"Send": "GPT-6"}, {"Allow once": "GPT-6"}],
           "class": ["flex"], "attr": ["id", "aria-label", "data-testid"]}


def _fuzz_cases():
    import cdp_consult
    out = []
    for fid, kind in sorted(cdp_consult._CONTRACT_SCHEMA.items()):
        for v in _VALUES.get(kind, []):
            out.append((fid, v))
    return out


@pytest.mark.parametrize("fid,value", _fuzz_cases())
def test_law7_no_contract_edit_lands_a_click_on_send_stop_approve_or_a_message(tmp_path, fid, value):
    d = _driver(tmp_path, {fid: value})
    for name, js in _helpers(d):
        out = _run(TRAP, js, active="editor")
        landed = _landed(out)
        # The one legitimate target of each purpose: the composer's own editor for typing, and the
        # named app's Allow once (outside every form) for 'approve'.
        if name in ("clear", "paste", "mention_type"):
            landed = [t for t in landed if t != "editor"]
        if name == "approve":
            landed = [t for t in landed if t != "allow"]
        assert landed == [], (fid, value, name, out["log"])


def test_law7_the_send_click_needs_exactly_one_send_button_in_a_form_outside_every_message(tmp_path):
    d = _driver(tmp_path, {})
    assert _run(TRAP, d._send_click_js(), active="editor")["log"] == [["click", "send"]]
    two = _page(["form", {}, [EDITOR, SEND, ["button", {"id": "send2", "data-testid": "send-button"}, []]]])
    assert _run(two, d._send_click_js(), active="editor")["result"] == "ambiguous"
    only_edit = _page(EDIT_FORM)
    out = _run(only_edit, d._send_click_js(), active="old-editor")
    assert out["result"] == "no-composer" and out["log"] == []


# ---- the shipped contract still drives the real fixtures ----------------------------------------

def test_law7_the_shipped_contract_still_picks_the_recorded_app_row(tmp_path):
    fx = json.load(open(os.path.join(ROOT, "tests", "fixtures", "mention_popup.json")))
    import cgc_drift
    d = _driver(tmp_path, {})
    for case, app in (("webcodex_demo", "WebCodex Demo"), ("all_plugins", "GitHub"), ("atlas", "Atlas")):
        tree = [["html", {}, [["body", {}, cgc_drift.parse_fixture(fx["cases"][case]["popup_html"])]]]]
        out = _run(tree, d._mention_pick_js(app, "tok"))
        assert out["result"]["hit"] == app.lower(), (case, out["result"])
        assert [a for a, _t in out["log"]] == ["click"], out["log"]


def test_law7_the_shipped_contract_still_drives_the_model_picker(tmp_path):
    """The GPT-6 picker as read live 2026-09-06: a composer pill opens a menu with the family radios
    and the tier slider. Opening, picking a family and focusing the slider all still act."""
    d = _driver(tmp_path, {})
    pill = ["button", {"id": "pill", "aria-haspopup": "menu"}, ["6"]]
    radios = [["div", {"id": "r6", "role": "menuitemradio", "aria-checked": "true"}, ["GPT-6"]],
              ["div", {"id": "r55", "role": "menuitemradio", "aria-checked": "false"}, ["GPT-5.5"]]]
    slider = ["div", {"id": "power", "role": "menuitem"}, [["span", {"id": "sl", "role": "slider"}, []]]]
    menu = ["div", {"role": "menu"}, [["div", {"data-testid": "composer-intelligence-picker-content"},
                                       radios + [slider]]]]
    tree = _page(["form", {}, [EDITOR, pill, SEND]], menu)
    assert _run(tree, d._open_cand_by_label_js("6"))["log"][0] == ["pointerdown", "pill"]
    assert _run(tree, d._click_family_js("GPT-5.5"))["log"][0] == ["pointerdown", "r55"]
    assert _run(tree, d._SLIDER_FOCUS_JS)["log"] == [["focus", "power"]]
    flat = _page(["form", {}, [EDITOR, SEND]], ["div", {"role": "menu"}, [
        ["div", {"id": "pro", "role": "menuitem"}, ["Pro Extended"]]]])
    assert _run(flat, d._click_item_js("Pro"))["log"][0] == ["pointerdown", "pro"]


def test_law7_approval_clicks_only_a_card_outside_every_form(tmp_path):
    d = _driver(tmp_path, {})
    out = _run(TRAP, d._approval_js(["Other App"]))
    assert out["result"]["app"] == "Other App" and out["log"] == [["click", "allow"]]
    in_form = _page(["form", {}, [EDITOR, ["div", {}, [["div", {}, ["Other App"]],
                                                       ["button", {"id": "allow"}, ["Allow once"]],
                                                       ["button", {"id": "deny"}, ["Deny"]]]]]])
    assert _run(in_form, d._approval_js(["Other App"]))["log"] == []


def test_a_selector_list_where_a_scope_is_joined_is_refused_at_load(tmp_path):
    """picker_menu / picker_slider / mention_loading are joined into a descendant scope
    ('<menu> <slider>'): a comma list there would make its first branches document-wide (the
    reviewer's picker_menu='form,body'). The loader refuses one."""
    import cdp_consult
    for fid in ("picker_menu", "picker_slider", "mention_loading"):
        facts = _facts()
        facts[fid]["value"] = "form,body"
        p = tmp_path / ("%s.json" % fid)
        p.write_text(json.dumps({"facts": facts}))
        with pytest.raises(SystemExit, match="contract_invalid"):
            cdp_consult._load_contract(str(p))


def test_a_clear_break_on_the_confirming_pass_leaves_a_bundle(tmp_path, monkeypatch):
    """Code-review candidate, real: the composer clear's drift was confirmed without a diagnosis."""
    import cgc_probe
    calls = []
    monkeypatch.setattr(cgc_probe.cdp, "_blocker", lambda t: None)
    monkeypatch.setattr(cgc_probe.cdp, "_diagnose", lambda *a, **k: calls.append(a[1]) or "/x/I-1")

    class T:
        def eval(self, js, timeout=None):
            return 5

    with pytest.raises(cgc_probe.Drift) as e:
        cgc_probe._cleared(T(), diagnose=True)
    assert calls == ["composer"] and e.value.cause.startswith("diagnosis /x/I-1/diagnosis.json")


def test_replay_reads_the_contract_it_is_given_not_the_installed_one(tmp_path):
    """Code-review candidate: replay --contract ignored because the driver module loads the
    installed contract at import. It does not: _driver loads a fresh module with CGC_CONTRACT set."""
    import cgc_probe
    facts = _facts()
    facts["turn_container"]["value"] = "[data-edited-turn-key]"
    p = tmp_path / "edited.json"
    p.write_text(json.dumps({"facts": facts}))
    assert cgc_probe._driver(str(p)).UI["turn_container"] == "[data-edited-turn-key]"
    assert cgc_probe.cdp.UI["turn_container"] == "[data-turn-key]"
