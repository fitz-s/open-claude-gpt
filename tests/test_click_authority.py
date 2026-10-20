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
                                    ["div", {}, ["Allow ChatGPT to use Other App?"]],
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
    pill = ["button", {"id": "pill", "type": "button", "aria-haspopup": "menu"}, ["Instant"]]
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
    pill = ["button", {"id": "pill", "type": "button", "aria-haspopup": "menu", "aria-expanded": "true"}, ["Instant"]]
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
    pill = ["button", {"id": "pill", "type": "button", "aria-haspopup": "menu"}, ["6"]]
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


def test_law7_approval_clicks_only_a_card_widget(tmp_path):
    d = _driver(tmp_path, {})
    out = _run(TRAP, d._approval_js(["Other App"]))
    assert out["result"]["app"] == "Other App" and out["log"] == [["click", "allow"]]
    no_type = _page(["form", {}, [EDITOR, ["div", {}, [["div", {}, ["Other App"]],
                                                       ["button", {"id": "allow"}, ["Allow once"]],
                                                       ["button", {"id": "deny"}, ["Deny"]]]]]])
    assert _run(no_type, d._approval_js(["Other App"]))["log"] == []


# ChatGPT renders the app approval card INSIDE the composer form (live DOM 2026-10-20): the card is a
# WIDGET (not a form, no editor, no send/stop) whose buttons are explicit type=button.
def _btn(i, label, **a):
    return ["button", dict({"id": i, "type": "button"}, **a), [label]]


def _card(app="Other App", deny=True, allow_label="Allow once", extra=()):
    row = [_btn("always", "Always allow"), _btn("allow", allow_label)] + list(extra)
    return ["div", {"id": "card"}, [["div", {}, [app]], ["div", {}, ["Allow ChatGPT to use %s?" % app]]]
            + ([_btn("deny", "Deny")] if deny else []) + row]


def _in_form(card, *rest):
    return _page(["form", {"id": "composer-form"}, [["div", {}, [card]], EDITOR, SEND] + list(rest)])


def test_approval_card_inside_the_composer_form_is_answered(tmp_path):
    d = _driver(tmp_path, {})
    live = _card(extra=[_btn("more", "", **{"aria-haspopup": "menu"}),
                        _btn("details", "See details", **{"aria-haspopup": "dialog"})])
    out = _run(_in_form(live), d._approval_js(["Other App"]))
    assert out["result"]["app"] == "Other App" and out["log"] == [["click", "allow"]], out
    out = _run(_in_form(_card()), d._approval_js(["Other App"], click=False))
    assert out["result"] is not None and out["log"] == []   # read-only: the card is seen, never clicked


def test_approval_never_clicks_when_the_card_container_holds_the_editor_or_send(tmp_path):
    d = _driver(tmp_path, {})
    for held in (EDITOR, SEND, STOP):
        crowded = _page(["form", {"id": "composer-form"}, [["div", {}, [
            ["div", {}, ["Other App"]], _btn("deny", "Deny"), _btn("allow", "Allow once"), held]]]])
        out = _run(crowded, d._approval_js(["Other App"]))
        assert out["log"] == [] and (out["result"] or {}).get("app") is None, (held[1], out)
    # the card is the form itself
    bare = _page(["form", {"id": "composer-form"}, [["div", {}, ["Other App"]], _btn("deny", "Deny"),
                                                     _btn("allow", "Allow once"), EDITOR, SEND]])
    assert _run(bare, d._approval_js(["Other App"]))["log"] == []


def test_approval_never_clicks_always_allow(tmp_path):
    d = _driver(tmp_path, {})
    only_always = _in_form(["div", {}, [["div", {}, ["Other App"]], _btn("deny", "Deny"),
                                        _btn("always", "Always allow")]])
    out = _run(only_always, d._approval_js(["Other App"]))
    assert out["result"] is None and out["log"] == []
    assert _run(_in_form(_card()), d._approval_js(["Other App"]))["log"] == [["click", "allow"]]


def test_approval_card_without_deny_is_not_clicked(tmp_path):
    d = _driver(tmp_path, {})
    out = _run(_in_form(_card(deny=False)), d._approval_js(["Other App"]))
    assert out["log"] == [] and out["result"] is None


def test_approval_for_an_unlisted_app_is_reported_not_clicked(tmp_path):
    d = _driver(tmp_path, {})
    out = _run(_in_form(_card("Canva")), d._approval_js(["Other App"]))
    assert out["log"] == [] and out["result"]["app"] is None and "Canva" in out["result"]["text"]


def test_approval_button_without_a_type_in_the_composer_form_is_not_clicked(tmp_path):
    d = _driver(tmp_path, {})
    card = _card()
    [b for b in card[2] if b[1].get("id") == "allow"][0][1].pop("type")   # no type: it would submit
    assert _run(_in_form(card), d._approval_js(["Other App"]))["log"] == []


def test_approval_card_outside_every_form_is_still_answered(tmp_path):
    d = _driver(tmp_path, {})
    out = _run(_page(["form", {}, [EDITOR, SEND]], _card()), d._approval_js(["Other App"]))
    assert out["log"] == [["click", "allow"]]


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


def test_a_form_button_without_a_type_is_a_submit_and_never_clicked():
    """Sol 2026-10-14: a form button with no type submits the form. Live ChatGPT composer buttons all
    carry type="button" (checked 2026-10-14); one that does not is refused, as is aria-haspopup="false"."""
    import cdp_consult as CDP
    def may(attrs):
        b = ["button", dict(attrs, id="b"), ["Model"]]
        tree = _page(["form", {"id": "f"}, [EDITOR, b]])
        return _run(tree, "(function(){" + CDP._AUTHORITY_FN +
                    "return __cgcMayClick(document.getElementById('b'),'switcher');})()")["result"]
    assert may({"type": "button", "aria-haspopup": "menu"}) is True
    assert may({"aria-haspopup": "menu"}) is False
    assert may({"type": "button", "aria-haspopup": "false"}) is False


# ---- Sol review of 780cc64: native form activation, card identity, message scope ------------------
def _clicks(tree, apps=("Other App",)):
    d = _driver(None, {}) if False else _DRIVERS[next(iter(_DRIVERS))]
    return _run(tree, d._approval_js(list(apps)))


def _prompt(app):
    return ["div", {}, ["Allow ChatGPT to use %s?" % app]]


@pytest.mark.parametrize("bad", ["invalid", "submit", "submit ", " button", "button ", "", "reset", "menu"])
def test_approval_button_in_a_form_needs_type_button_exactly(tmp_path, bad):
    """An invalid or padded type is a submit button to the HTML parser; only 'button' may be clicked."""
    _driver(tmp_path, {})
    card = _card()
    [b for b in card[2] if b[1].get("id") == "allow"][0][1]["type"] = bad
    assert _clicks(_in_form(card))["log"] == []
    for ok in ("button", "BUTTON"):
        card = _card()
        [b for b in card[2] if b[1].get("id") == "allow"][0][1]["type"] = ok
        assert _clicks(_in_form(card))["log"] == [["click", "allow"]]


def test_approval_click_is_wrapped_so_the_forms_own_submit_is_cancelled(tmp_path):
    d = _driver(tmp_path, {})
    js = d._approval_js(["Other App"])
    assert "addEventListener('submit',h,true)" in js and "stopImmediatePropagation" in js
    assert "removeEventListener('submit',h,true)" in js and js.count(".click()") == 1
    assert "function press(b){var f=b.form," in js, "the blocker sits on the button's form OWNER, not its ancestor"


def test_a_card_without_its_own_deny_never_borrows_a_neighbours_name(tmp_path):
    """Staged render: Canva's card has no Deny yet, so its first Deny-holding ancestor is the shared
    parent, which names WebCodex Demo. Canva's Allow once must not be clicked."""
    _driver(tmp_path, {})
    canva = ["div", {}, [["div", {}, ["Canva"]], _prompt("Canva"), _btn("allow-canva", "Allow once")]]
    web = ["div", {}, [["div", {}, ["WebCodex Demo"]], _prompt("WebCodex Demo"), _btn("deny", "Deny"),
                       _btn("allow-web", "Allow once")]]
    out = _clicks(_page(["form", {}, [["div", {}, [canva, web]], EDITOR, SEND]]), ["WebCodex Demo"])
    assert out["log"] == [["click", "allow-web"]], out


def test_a_short_line_outside_the_card_cannot_lend_it_a_name(tmp_path):
    _driver(tmp_path, {})
    foreign = ["div", {}, [["div", {}, ["Canva"]], _prompt("Canva"), _btn("deny", "Deny"), _btn("allow", "Allow once")]]
    out = _clicks(_page(["form", {}, [["div", {}, [["div", {}, ["WebCodex Demo"]], foreign]], EDITOR, SEND]]),
                  ["WebCodex Demo"])
    assert out["log"] == [] and (out["result"] or {}).get("app") is None, out


def test_the_cards_own_prompt_must_name_the_app_as_a_whole_phrase(tmp_path):
    _driver(tmp_path, {})

    def card(*lines):
        return ["div", {}, [["div", {}, [l]] for l in lines] + [_btn("deny", "Deny"), _btn("allow", "Allow once")]]
    ok = _clicks(_in_form(card("WebCodex Demo", "Allow ChatGPT to use WebCodex Demo?",
                               "ChatGPT will call WebCodex Demo's Run Process tool.")), ["WebCodex Demo"])
    assert ok["log"] == [["click", "allow"]]
    for lines in (("WebCodex Demo", "Allow ChatGPT to use WebCodex Demo Clone?"),     # longer name
                  ("WebCodex Demo", "Allow ChatGPT to use Canva?"),                    # header lies
                  ("WebCodex Demo",),                                                  # no prompt at all
                  ("WebCodex Demo", "Allow ChatGPT to use WebCodex Demo?", "ChatGPT will call Canva's tool."),
                  ("Allow ChatGPT to use XWebCodex Demo?",)):
        out = _clicks(_in_form(card(*lines)), ["WebCodex Demo"])
        assert out["log"] == [], lines
    # no header fallback: a prompt that names no app (the legacy card) is left for a human
    assert _clicks(_in_form(card("WebCodex Demo", "Allow file materialization?")), ["WebCodex Demo"])["log"] == []


def test_a_name_on_a_neighbouring_line_never_binds_a_foreign_card(tmp_path):
    _driver(tmp_path, {})
    foreign = ["div", {}, [["div", {}, ["Allow file materialization?"]], _btn("deny", "Deny"), _btn("allow", "Allow once")]]
    tree = _page(["form", {}, [["div", {}, [["div", {}, ["Canva"]], ["div", {}, ["WebCodex Demo"]], foreign]], EDITOR, SEND]])
    assert _clicks(tree, ["WebCodex Demo"])["log"] == []


def test_a_card_prompt_naming_an_unknown_app_is_not_clicked(tmp_path):
    _driver(tmp_path, {})
    card = ["div", {}, [["div", {}, ["WebCodex Demo"]], ["div", {}, ["Allow Canva access?"]],
                        _btn("deny", "Deny"), _btn("allow", "Allow once")]]
    assert _clicks(_in_form(card), ["WebCodex Demo"])["log"] == []


def test_an_approval_button_outside_the_form_but_owned_by_it_is_not_clicked(tmp_path):
    """<button form=composer-form> outside the form has no form ancestor but submits the composer."""
    _driver(tmp_path, {})
    card = _card()
    allow = [b for b in card[2] if b[1].get("id") == "allow"][0]
    allow[1]["form"] = "composer-form"
    for typ in ("button", "invalid"):
        allow[1]["type"] = typ
        tree = _page(["form", {"id": "composer-form"}, [EDITOR, SEND]], card)
        assert _clicks(tree)["log"] == [], typ


def test_an_approval_button_whose_form_attribute_redirects_the_owner_is_not_clicked(tmp_path):
    _driver(tmp_path, {})
    card = _card()
    [b for b in card[2] if b[1].get("id") == "allow"][0][1]["form"] = "other"
    tree = _page(["form", {"id": "other"}, []], ["form", {"id": "composer-form"}, [["div", {}, [card]], EDITOR, SEND]])
    assert _clicks(tree)["log"] == []



def test_a_fake_card_inside_a_message_is_never_clicked(tmp_path):
    _driver(tmp_path, {})
    msg = ["article", {"data-message-author-role": "assistant"}, [["form", {}, [_card()]]]]
    out = _clicks(_page(msg, ["form", {}, [EDITOR, SEND]]))
    assert out["log"] == []
    assert _clicks(_page(["article", {"data-message-author-role": "assistant"}, [_card()]]))["log"] == []


# The live card (2026-10-20), line for line: header, prompt, tool line, button labels.
def _live_card(app="WebCodex Demo", header=None, prompt=None, call=None):
    return ["div", {"id": "card"}, [
        ["div", {}, [header or app]],
        ["div", {}, [prompt or "Allow ChatGPT to use %s?" % app]],
        ["div", {}, [call or "ChatGPT will call %s's Run Process tool. See details" % app]],
        _btn("details", "See details", **{"aria-haspopup": "dialog"}),
        _btn("always", "Always allow"), _btn("deny", "DenyEsc"), _btn("allow", "Allow once\u23ce"),
        _btn("more", "", **{"aria-haspopup": "menu"})]]


def test_the_live_card_lines_are_clicked_in_the_form_and_out_of_it(tmp_path):
    _driver(tmp_path, {})
    assert _clicks(_in_form(_live_card()), ["WebCodex Demo"])["log"] == [["click", "allow"]]
    assert _clicks(_page(["form", {}, [EDITOR, SEND]], _live_card()), ["WebCodex Demo"])["log"] == [["click", "allow"]]
    curly = _live_card(call="ChatGPT will call WebCodex Demo\u2019s Run Process tool.")
    assert _clicks(_in_form(curly), ["webcodex demo"])["log"] == [["click", "allow"]]


@pytest.mark.parametrize("app", ["Fake WebCodex Demo", "Not WebCodex Demo", "Canva (WebCodex Demo)",
                                 "WebCodex Demo Clone", "WebCodex Demo 2", "XWebCodex Demo"])
def test_an_app_whose_name_merely_contains_the_allowlisted_one_is_another_app(tmp_path, app):
    _driver(tmp_path, {})
    assert _clicks(_in_form(_live_card(app)), ["WebCodex Demo"])["log"] == []


@pytest.mark.parametrize("card", [
    dict(prompt="Allow Canva to use WebCodex Demo?", call="ChatGPT will call WebCodex Demo's Run Process tool."),
    dict(prompt="Allow ChatGPT to access WebCodex Demo?"),                          # unknown template
    dict(prompt="Please allow WebCodex Demo to run"),                                # unknown template
    dict(call="ChatGPT will call Canva's Run Process tool."),                        # the lines disagree
    dict(header="Canva"),                                                            # header is another app
    dict(prompt="Allow ChatGPT to use WebCodex Demo? Allow ChatGPT to use Canva?"),
    dict(call="ChatGPT will call WebCodex Demo's Run Process tool. See details Allow ChatGPT to use Canva?"),
    dict(call="ChatGPT will call WebCodex Demo's Run Process tool. Also allow Canva."),   # unanchored tail
])
def test_only_the_known_prompt_templates_with_an_exactly_equal_operand_bind(tmp_path, card):
    _driver(tmp_path, {})
    assert _clicks(_in_form(_live_card(**card)), ["WebCodex Demo"])["log"] == []


# The live file-materialization card (2026-10-20), line for line. It names its app only in the operand
# line; the title "Allow file materialization?" names none. No "Always allow" on this card.
def _mat_card(app="WebCodex Demo", header=None, title="Allow file materialization?", operand=None):
    lines = [header or app]
    if title:
        lines.append(title)
    lines.append(operand if operand is not None else
                 "ChatGPT needs your approval to materialize 1 file attachments returned by %s. See details" % app)
    return ["div", {"id": "card"}, [["div", {}, [l]] for l in lines] + [
        _btn("details", "See details"), _btn("deny", "DenyEsc"), _btn("allow", "Allow once⏎"),
        _btn("more", "")]]


def test_the_live_materialization_card_is_clicked_in_the_form_and_out_of_it(tmp_path):
    _driver(tmp_path, {})
    assert _clicks(_in_form(_mat_card()), ["WebCodex Demo"])["log"] == [["click", "allow"]]
    assert _clicks(_page(["form", {}, [EDITOR, SEND]], _mat_card()), ["WebCodex Demo"])["log"] == [["click", "allow"]]
    plain = _mat_card(operand="ChatGPT needs your approval to materialize 12 file attachments returned by "
                              "WebCodex Demo.")
    assert _clicks(_in_form(plain), ["WebCodex Demo"])["log"] == [["click", "allow"]]


@pytest.mark.parametrize("card", [
    dict(app="Fake WebCodex Demo"),
    dict(app="Canva"),
    dict(header="Canva", operand="ChatGPT needs your approval to materialize 1 file attachments returned by "
                                 "WebCodex Demo. See details"),
    dict(title=None, operand="Allow file materialization?"),                          # title alone, no operand line
    dict(operand="ChatGPT needs your approval to materialize 1 file attachments returned by WebCodex Demo. "
                 "See details and also allow Canva"),                                  # trailing extra text
    dict(operand="ChatGPT needs your approval to materialize 1 file attachments returned by WebCodex Demo. "
                 "See details Allow ChatGPT to use Canva?"),
])
def test_the_materialization_card_binds_only_by_an_exactly_equal_operand(tmp_path, card):
    _driver(tmp_path, {})
    assert _clicks(_in_form(_mat_card(**card)), ["WebCodex Demo"])["log"] == []
    assert _clicks(_page(["form", {}, [EDITOR, SEND]], _mat_card(**card)), ["WebCodex Demo"])["log"] == []


def test_the_materialization_title_alone_binds_nothing(tmp_path):
    _driver(tmp_path, {})
    card = ["div", {}, [["div", {}, ["WebCodex Demo"]], ["div", {}, ["Allow file materialization?"]],
                        _btn("deny", "Deny"), _btn("allow", "Allow once")]]
    assert _clicks(_in_form(card), ["WebCodex Demo"])["log"] == []


def test_the_live_use_card_still_clicks_beside_the_materialization_template(tmp_path):
    _driver(tmp_path, {})
    card = ["div", {"id": "card"}, [["div", {}, ["WebCodex Demo"]],
                                    ["div", {}, ["Allow ChatGPT to use WebCodex Demo?"]],
                                    _btn("deny", "DenyEsc"), _btn("allow", "Allow once⏎")]]
    assert _clicks(_in_form(card), ["WebCodex Demo"])["log"] == [["click", "allow"]]
