# @mention of a ChatGPT app/plugin (e.g. "WebCodex Demo"): an agent asks for it with one flag,
# `fire --mention NAME`, and the rest is mechanical — spec -> daemon argv -> composer popup pick
# before the paste. These pin each hop offline (fake CDP, no browser).
import argparse
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skill", "scripts")
sys.path.insert(0, SCRIPTS)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SCRIPTS, name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CDP = _load("cdp_consult")


class _FakeComposer:
    """Answers the JS the paste path evaluates. `offers` = app labels the @ popup shows."""

    def __init__(self, offers, popup_open=True, unconnected=(), non_app=False, loading=False,
                 late_by=0, drop_on_paste=False):
        self.offers = [o.lower() for o in offers]
        self.popup_open, self.unconnected, self.non_app = popup_open, list(unconnected), non_app
        self.loading = loading
        self.late_by, self.drop_on_paste = late_by, drop_on_paste  # polls before the node shows
        self._pending = 0
        self.html = ""
        self.text = ""
        self.mentions = 0

    def eval(self, js, timeout=None):
        if "readyState" in js:
            return True
        if "selectAll" in js:
            self.html = self.text = ""
            self.mentions = 0
            return 0
        if "app-mention-name" in js:
            if self._pending:                       # a click landed: the node shows after late_by reads
                if self.late_by <= 0:
                    self.mentions, self._pending = self.mentions + self._pending, 0
                else:
                    self.late_by -= 1
            return self.mentions
        if "var want=" in js and "data-cgc-pre" in js:
            want = json.loads(js.split("var want=", 1)[1].split(";", 1)[0])
            if want in self.offers:
                if not self.non_app:  # a row that is not an app leaves the composer without a mention
                    self.html = self.html.replace("@" + want, "") + f'<span data-mention>{want}</span>'
                    self.text = self.text.lower().replace("@" + want, want)
                    self._pending += 1
                return {"hit": want, "open": True, "seen": [], "unconnected": []}
            return {"hit": None, "open": self.popup_open, "loading": self.loading,
                    "seen": self.offers, "unconnected": self.unconnected}
        if "execCommand('insertText'" in js:
            chunk = json.loads(js.split("execCommand('insertText',false,", 1)[1].rsplit(");", 1)[0])
            if self.drop_on_paste and len(chunk) > 1:
                self.mentions = 0          # a select-and-replace accident eats the mention node
            self.text += chunk
            self.html += chunk.lower()
            return True if "return true" in js else len(self.text)
        if "return d?(d.innerText" in js:
            return self.text
        raise AssertionError("unexpected js: " + js[:80])


def test_mention_is_picked_before_the_prompt(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    c = _FakeComposer(["WebCodex Demo"])
    CDP._paste_prompt(c, "review this", ["WebCodex Demo"])
    assert "data-mention" in c.html
    assert c.text.startswith("webcodex demo") and c.text.endswith("review this")


def test_unknown_mention_fails_closed_before_the_click(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    monkeypatch.setattr(CDP, "_MENTION_WAIT_S", 0.05)
    c = _FakeComposer(["canva"])
    with pytest.raises(CDP._MentionFailure) as e:
        CDP._paste_prompt(c, "review this", ["Nope App"])
    assert "mention_not_found" in str(e.value)
    assert "'canva'" in str(e.value), "the error names what the popup did offer"
    assert isinstance(e.value, CDP._PreClickFailure), "never an uncertain post-click failure"
    assert c.text == "", "no half-typed @name left in the composer"


def test_no_mention_is_the_old_paste():
    c = _FakeComposer([])
    CDP._paste_prompt(c, "plain")
    assert c.text == "plain"


def test_fire_freezes_mentions_into_the_request(tmp_path, monkeypatch):
    consult = _load("consult")
    monkeypatch.setattr(consult, "CGC_STATE_DIR", str(tmp_path))
    import cgc_spool as spool
    seen = {}
    monkeypatch.setattr(spool, "cmd_enqueue", lambda a: seen.update(vars(a)) or 0)
    ns = argparse.Namespace(
        repo_dir=ROOT, repo=None, pr=None, ref=None, compare=None, pulls=False, blobs=False,
        files=None, issues=None, base=None, verify=False, allow_nonpublic=False, backend="cdp",
        window_template=None, task="probe", title="t", role=None, followup=False, no_code=True,
        context_file=None, refs_file=None, output_file=None, output_replace=False,
        target_chars=32000, hard_chars=40000, expect_minutes=25, model="Pro", out=None,
        project_url="https://chatgpt.com/", conversation=None, mention=["WebCodex Demo"])
    assert isinstance(consult.cmd_fire(ns), dict)
    assert seen["mentions"] == ["WebCodex Demo"]


def test_mention_flag_strips_the_at_sign():
    consult = _load("consult")
    assert consult._mention_name(" @WebCodex  Demo ") == "WebCodex Demo"
    with pytest.raises(argparse.ArgumentTypeError):
        consult._mention_name("@")


def test_mentions_route_the_request_but_leave_old_fingerprints_alone():
    backend = _load("cgc_backend")
    base = dict(kind="submit", logical_sha="x", project_url="u", model="Pro", model_family="Latest",
                parent=None, conversation=None)
    plain = backend._request_fingerprint(argparse.Namespace(**base), "p")
    empty = backend._request_fingerprint(argparse.Namespace(**base, mentions=[]), "p")
    tagged = backend._request_fingerprint(argparse.Namespace(**base, mentions=["WebCodex Demo"]), "p")
    assert plain == empty, "a request with no mention keeps its pre-existing fingerprint"
    assert tagged != plain, "same key + a different app is a different request"
    assert "mention_not_found" in backend._NOT_SENT_BLOCK, "a missing app needs a human, not a retry"


def test_daemon_passes_mentions_on_the_argv(monkeypatch):
    daemon = _load("cgc_daemon")
    got = {}

    class _R:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr(daemon.subprocess, "run", lambda cmd, **kw: got.setdefault("cmd", cmd) and _R())
    daemon._make_run_cdp()("submit", rid="REQ-20260924-000000-abcdef", prompt="p",
                           project_url="https://chatgpt.com/", model="Pro", model_family="Latest",
                           mentions=["WebCodex Demo"])
    i = got["cmd"].index("--mention")
    assert got["cmd"][i + 1] == "WebCodex Demo"


# The picker JS runs in node over a minimal DOM built from a tree spec: {t: tag, a: attrs, c: children,
# x: innerText}. Its querySelectorAll knows only descendant chains of `[attr]` / `[attr="v"]` — the popup selector's
# whole vocabulary — and throws on anything else, so a selector that grows fails here loudly.
_NODE_DOM = r"""
function build(spec, parent){
  if(typeof spec==='string'){return {nodeType:3,textContent:spec,parentElement:parent,innerText:spec};}
  var n={nodeType:1,tagName:String(spec.t).toUpperCase(),_a:spec.a||{},parentElement:parent||null,
    children:[],childNodes:[],clicked:0,previousElementSibling:null,
    getAttribute:function(k){return k in this._a?this._a[k]:null},
    hasAttribute:function(k){return k in this._a},
    getBoundingClientRect:function(){return {width:1,height:1}},
    click:function(){this.clicked++}};
  (spec.c||[]).forEach(function(c){var k=build(c,n);n.childNodes.push(k);
    if(k.nodeType===1){k.previousElementSibling=n.children.length?n.children[n.children.length-1]:null;n.children.push(k);}});
  // innerText: block-level children (a row's label and description spans sit on separate lines in the
  // browser, headers and rows too) are separated by a newline; inline text runs together.
  Object.defineProperty(n,'innerText',{get:function(){if(spec.x!=null)return spec.x;
    var parts=n.childNodes.map(function(k){return k.innerText}).filter(function(x){return x!==''});
    var block=n.children.length>1&&n.children.every(function(k){return k.tagName!=='SVG'});
    var rowBody=n.hasAttribute&&n.hasAttribute('data-menu-row-content');
    return parts.join((block&&(rowBody||n._a['data-list-navigation-item']===undefined))?'\n':'')}});
  Object.defineProperty(n,'textContent',{get:function(){return n.innerText}});
  Object.defineProperty(n,'firstElementChild',{get:function(){return n.children[0]||null}});
  n.querySelector=function(sel){return qin(n,sel)[0]||null};
  return n;
}
function all(r){var o=[];(function w(n){o.push(n);(n.children||[]).forEach(w);})(r);return o;}
function attrs(sel){return sel.split(' ').map(function(p){var m=/^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(p)||/^\.([\w-]+)$/.exec(p);
  if(!m)throw new Error('unsupported selector: '+sel);return m[0][0]==='.'?['class~',m[1]]:[m[1],m[2]];});}
function has(n,q){if(q[0]==='class~')return (n.getAttribute('class')||'').split(/\s+/).indexOf(q[1])>=0;
  return q[1]===undefined?n.hasAttribute(q[0]):n.getAttribute(q[0])===q[1];}
function qin(root,sel){var a=attrs(sel);return all(root).filter(function(n){
  if(n===root||!has(n,a[a.length-1]))return false;
  var i=a.length-2,p=n.parentElement;
  while(i>=0&&p){if(has(p,a[i]))i--;p=p.parentElement;}
  return i<0;});}
function qsa(sel){return qin({children:[ROOT],parentElement:null,hasAttribute:function(){return false},getAttribute:function(){return null}},sel);}
var document={querySelectorAll:qsa,querySelector:function(s){return qsa(s)[0]||null;}};
"""


def _row(label, desc="Does things", icon="rounded-2xs icon-xs", extra=None, **attrs):
    """One popup row in the shape the live DOM has: button > content block > [icon span > icon, label
    span (+ description span)] plus, on an unconnected app, a bare text node inside the content block."""
    body = [{"t": "span", "a": {"class": "leadingIcon-x"}, "c": [{"t": "svg", "a": {"class": icon}}]},
            {"t": "span", "c": [{"t": "span", "c": [{"t": "span", "c": [label]}] + (
                [{"t": "span", "a": {"class": "text-codex-description"}, "c": [desc]}] if desc else [])}]}]
    if extra:
        body.append(extra)
    return {"t": "button", "a": {"data-list-navigation-item": "true", **attrs},
            "c": [{"t": "div", "a": {"data-menu-row-content": "true"}, "c": body}]}


def _app(label, **kw):
    return _row(label, **kw)


def _unconnected(label, word="Connect", **kw):
    return _row(label, extra=word, **kw)


def _file(label):
    return _row(label, desc=None, icon="text-file-document size-5")


def _action(label, icon="icon-leading shrink-0"):
    return _row(label, icon=icon)


def _popup_tree(*items):
    """Real popup shape: scroll area > wrapper > [section header | row ...]. A str is a header, a dict
    is a row built by _app/_unconnected/_file/_action/_row."""
    kids = [{"t": "div", "c": [it]} if isinstance(it, str) else it for it in items]
    return {"t": "body", "c": [{"t": "div", "a": {"data-mention-list-scroll-area": ""},
                                "c": [{"t": "div", "c": kids}]}]}


def _tree_from_html(html, row_lines=None):
    """Parse a captured popup outerHTML into the tree spec, keeping every element and text node as
    captured: the picker's structural tests run on the real markup, not on innerText alone."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        VOID = {"br", "img", "input", "hr", "meta", "link"}

        def __init__(self):
            super().__init__()
            self.root = {"t": "body", "c": []}
            self.st = [self.root]

        def _open(self, tag, attrs):
            n = {"t": tag, "a": {k: (v or "") for k, v in attrs}, "c": []}
            self.st[-1]["c"].append(n)
            return n

        def handle_starttag(self, tag, attrs):
            n = self._open(tag, attrs)
            if tag not in self.VOID:
                self.st.append(n)

        def handle_startendtag(self, tag, attrs):
            self._open(tag, attrs)

        def handle_endtag(self, tag):
            for i in range(len(self.st) - 1, 0, -1):
                if self.st[i]["t"] == tag:
                    del self.st[i:]
                    return

        def handle_data(self, d):
            if d.strip():
                self.st[-1]["c"].append(d)

    p = P()
    p.feed(html)
    return p.root


def _run_pick_tree(tree, name, token="t0"):
    """Run the real picker JS in node against `tree`; returns (result, labels of clicked rows)."""
    import shutil, subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = (_NODE_DOM + "var ROOT=build(" + json.dumps(tree) + ",null);\n"
           "var r=" + CDP._mention_pick_js(name, token) + ";"
           "console.log(JSON.stringify([r,all(ROOT).filter(function(n){return n.clicked;})"
           ".map(function(n){return n.innerText.split('\\n')[0];})]));")
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout
    r, clicked = json.loads(out)
    return r, clicked


def _run_pick(tree, name, token="t0"):
    r, clicked = _run_pick_tree(tree, name, token)
    return [r["hit"], clicked]


def test_picker_exact_match_beats_prefix():
    tree = _popup_tree("Plugins", _app("WebCodex Demo Pro"), _app("WebCodex Demo"))
    assert _run_pick(tree, "WebCodex Demo") == ["webcodex demo", ["WebCodex Demo"]]


def test_picker_takes_a_unique_prefix():
    tree = _popup_tree("Plugins", _app("WebCodex Demo - sandbox"), _app("Canva"))
    assert _run_pick(tree, "WebCodex Demo") == ["webcodex demo - sandbox", ["WebCodex Demo - sandbox"]]


def test_picker_refuses_an_ambiguous_prefix():
    tree = _popup_tree("Plugins", _app("WebCodex Demo Pro"), _app("WebCodex Demo Lite"))
    assert _run_pick(tree, "WebCodex Demo") == [None, []]


def test_picker_ignores_controls_that_were_on_screen_before_the_typing():
    # a same-label control that pre-dates the "@name" keystrokes is not the popup
    assert _run_pick(_popup_tree("Plugins", _app("WebCodex Demo", **{"data-cgc-pre": "t0"})),
                     "WebCodex Demo") == [None, []]
    tree = _popup_tree("Plugins", _app("WebCodex Demo", **{"data-cgc-pre": "t0"}), _app("WebCodex Demo"))
    assert _run_pick(tree, "WebCodex Demo") == ["webcodex demo", ["WebCodex Demo"]]


def test_picker_only_picks_plugin_rows_never_files():
    # a query also lists Files; a file named like the app must not be clicked in its place
    tree = _popup_tree("Plugins", _app("Canva"), "Files", _file("webcodex demo notes.md"))
    r, clicked = _run_pick_tree(tree, "WebCodex Demo")
    assert r["hit"] is None and clicked == [] and r["seen"] == ["canva"]


def test_picker_never_clicks_an_unconnected_app():
    # a row with a Connect control starts an authorization flow: reported, never clicked
    tree = _popup_tree("Plugins", _unconnected("Notion"), _app("Canva"))
    r, clicked = _run_pick_tree(tree, "Notion")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["notion"]
    assert r["open"] is True and r["seen"] == ["canva"]


def test_picker_never_substitutes_a_connected_prefix_sibling_for_an_unconnected_exact_app():
    # 'Notion' is unconnected; 'Notion Calendar' is connected and starts with the same text. The
    # exact app is refused, and the sibling is NOT clicked in its place (it would be the wrong app).
    tree = _popup_tree("Plugins", _unconnected("Notion"), _app("Notion Calendar"))
    r, clicked = _run_pick_tree(tree, "Notion")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["notion"]
    # asked for by its own name, the connected sibling is still pickable
    assert _run_pick(tree, "Notion Calendar") == ["notion calendar", ["Notion Calendar"]]


def test_picker_recognizes_an_unconnected_row_in_any_ui_language():
    # the extra control's text is localized; the row's SHAPE (a stray text node in the content block) is not
    r, clicked = _run_pick_tree(_popup_tree(_unconnected("Notion", word="\u8fde\u63a5")), "Notion")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["notion"]


def test_picker_refuses_a_two_line_authorization_row_with_no_description():
    # label + Connect and NO description: two innerText lines, so a line count would call it connected.
    # The content block's stray text node says otherwise.
    tree = _popup_tree(_row("Notion", desc=None, extra="Connect"))
    r, clicked = _run_pick_tree(tree, "Notion")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["notion"]


def test_picker_never_clicks_a_headerless_exact_file_row():
    # "headerless" is not evidence of an app: a lone file result can render bare. Its leading icon is a
    # file icon and it has no description, so it is not app-shaped and is never clicked.
    for tree in (_popup_tree(_file("atlas_notes.md")), _popup_tree(_file("Atlas"))):
        r, clicked = _run_pick_tree(tree, "atlas_notes.md" if "notes" in json.dumps(tree) else "Atlas")
        assert r["hit"] is None and clicked == []
    r, clicked = _run_pick_tree(_popup_tree(_file("Atlas")), "Atlas")
    assert r["unverified"] == ["atlas"], "reported as unverified, never guessed"


def test_picker_never_clicks_an_add_action_row():
    r, clicked = _run_pick_tree(_popup_tree(_action("Create image")), "Create image")
    assert r["hit"] is None and clicked == []
    r, clicked = _run_pick_tree(_popup_tree("Add", _action("Create image")), "Create image")
    assert r["hit"] is None and clicked == []


def test_picker_takes_a_headerless_exact_app_row():
    assert _run_pick(_popup_tree(_app("Atlas")), "Atlas") == ["atlas", ["Atlas"]]
    # ...but not by prefix: without a Plugins header only an exact label counts
    assert _run_pick(_popup_tree(_app("Atlas Pro")), "Atlas") == [None, []]


def test_picker_takes_no_row_after_an_empty_spacer_and_no_headerless_row_among_headers():
    # an empty-text spacer between the Files header and its rows must not hide the header
    tree = _popup_tree("Plugins", _app("Canva"), "Files", "", _file("atlas_notes.md"))
    r, clicked = _run_pick_tree(tree, "Atlas")
    assert r["hit"] is None and clicked == []
    # a header-less row in a popup that HAS headers belongs to no section: not a plugin
    r, clicked = _run_pick_tree(_popup_tree(_app("Atlas"), "Files", _file("a.md")), "Atlas")
    assert r["hit"] is None and clicked == []


def test_popup_offer_is_reported_when_nothing_matches():
    r, clicked = _run_pick_tree(_popup_tree("Plugins", _app("Canva")), "WebCodex Demo")
    assert r == {"hit": None, "open": True, "loading": False, "seen": ["canva"],
                 "unconnected": [], "unverified": []} and clicked == []


def test_popup_never_opened_is_reported_as_such():
    r, _ = _run_pick_tree({"t": "body", "c": []}, "WebCodex Demo")
    assert r["hit"] is None and r["open"] is False and r["seen"] == []


# ---- the live DOM, captured 2026-09-29 -----------------------------------------------------------
# The composer's @ popup carries no ARIA roles at all, which is why the role-based selector matched
# nothing and every mention failed with "The popup offered: nothing". These pin the picker against
# the real markup (elements, classes and text nodes), not a hand-written imitation of it.
FIXTURE = json.load(open(os.path.join(ROOT, "tests", "fixtures", "mention_popup.json")))


def _live(case):
    return _tree_from_html(FIXTURE["cases"][case]["popup_html"])


def _texts(tree):
    """A row's label the way the picker reads it: the first line of its innerText."""
    return tree


def test_live_popup_rows_carry_no_aria_role_so_the_selector_cannot_be_role_based():
    for c in FIXTURE["cases"].values():
        for row in __import__("re").findall(r"<button [^>]*>", c["popup_html"]):
            assert "role=" not in row
    assert "role=" not in CDP._MENTION_OPT_SEL


def test_live_popup_picks_the_named_app_and_nothing_else():
    assert _run_pick(_live("webcodex_demo"), "WebCodex Demo") == ["webcodex demo", ["WebCodex Demo"]]
    assert _run_pick(_live("all_plugins"), "WebCodex Demo") == ["webcodex demo", ["WebCodex Demo"]]
    # 'Atlas' is the exact label of one row, the tail of 'Demo Atlas' and the stem of Files rows
    assert _run_pick(_live("atlas"), "Atlas") == ["atlas", ["Atlas"]]
    assert _run_pick(_live("all_plugins"), "atlas") == ["atlas", ["Atlas"]], "case-insensitive"
    assert _run_pick(_live("all_plugins"), "Demo Atlas") == ["demo atlas", ["Demo Atlas"]]
    # connector icons come as an <img> (hosted) or an <svg> (inline); both are app-shaped
    assert _run_pick(_live("all_plugins"), "GitHub") == ["github", ["GitHub"]]


def test_live_popup_never_picks_a_file_row():
    for name in ("atlas_notes_a.md", "atlas_notes_b.md", "Atlas notes"):
        r, clicked = _run_pick_tree(_live("atlas"), name)
        assert r["hit"] is None and clicked == []
    # a query that returns ONLY a file: the popup carries the Files header, and the row is not app-shaped
    r, clicked = _run_pick_tree(_live("lone_file"), "atlas_notes_c.md")
    assert r["hit"] is None and clicked == []


def test_live_popup_never_picks_an_add_action():
    for name in ("Create image", "Sketch", "Add photos & files"):
        r, clicked = _run_pick_tree(_live("all_plugins"), name)
        assert r["hit"] is None and clicked == [], name
    r, clicked = _run_pick_tree(_live("create_image"), "Create image")
    assert r["hit"] is None and clicked == [], "a bare single-result Add action is not an app"


def test_live_popup_refuses_an_unconnected_app():
    for case in ("all_plugins", "notion"):
        r, clicked = _run_pick_tree(_live(case), "Notion")
        assert r["hit"] is None and clicked == [] and r["unconnected"] == ["notion"], case
    r, clicked = _run_pick_tree(_live("all_plugins"), "Slack")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["slack"]
    # the headerless two-result query: the unconnected calendar is refused, its connected sibling is not
    r, clicked = _run_pick_tree(_live("outlook"), "Outlook Calendar")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["outlook calendar"]
    assert _run_pick(_live("outlook"), "Outlook Email") == ["outlook email", ["Outlook Email"]]


def test_live_popup_with_no_match_is_open_and_empty():
    # settled: ChatGPT renders an explicit "No results" line and no rows
    r, clicked = _run_pick_tree(_live("no_match"), "Qzxvkw")
    assert r["hit"] is None and r["open"] is True and r["seen"] == [] and clicked == []
    assert r["loading"] is False
    assert _run_pick_tree(_live("atlas"), "Atlas")[0]["loading"] is False


def test_a_popup_still_on_its_loading_skeleton_reads_as_loading():
    # not yet settled: a role=status "Loading suggestions" node and no rows (seen on a slow query)
    tree = {"t": "body", "c": [{"t": "div", "a": {"data-mention-list-scroll-area": ""}, "c": [
        {"t": "div", "a": {"role": "status"}, "c": ["Loading suggestions"]}]}]}
    r, clicked = _run_pick_tree(tree, "Qzxvkw")
    assert r["hit"] is None and r["open"] is True and r["loading"] is True and clicked == []


def test_live_picked_app_is_counted_by_the_composer_mention_probe():
    attr = __import__("re").search(r"querySelectorAll\('\[([\w-]+)\]'\)", CDP._composer_mentions_js())
    assert attr and (attr.group(1) + "=") in FIXTURE["composer_after_pick"]


def test_error_says_which_way_the_popup_failed(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    monkeypatch.setattr(CDP, "_MENTION_WAIT_S", 0.05)

    def fail(**kw):
        c = _FakeComposer(**kw)
        with pytest.raises(CDP._MentionFailure) as e:
            CDP._paste_prompt(c, "p", ["WebCodex Demo"])
        assert c.text == "", "nothing half-typed is left behind"
        return str(e.value)

    never = fail(offers=[], popup_open=False)
    empty = fail(offers=[])
    loading = fail(offers=[], loading=True)
    others = fail(offers=["canva"])
    unconnected = fail(offers=[], unconnected=["webcodex demo"])
    assert "never opened" in never and "code fix" in never
    assert "opened but nothing matched" in empty and "enable" in empty
    assert "loading skeleton" in loading and "opened but nothing matched" not in loading
    assert "'canva'" in others and "never opened" not in others
    assert "NOT connected" in unconnected and "never authorizes" in unconnected
    assert len({never, empty, loading, others, unconnected}) == 5


def test_a_mention_node_that_shows_late_is_still_a_success(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    c = _FakeComposer(["WebCodex Demo"], late_by=CDP._MENTION_SETTLE_POLLS - 1)
    CDP._paste_prompt(c, "review this", ["WebCodex Demo"])
    assert c.mentions == 1 and c.text.endswith("review this")


def test_a_mention_node_that_never_shows_fails_closed_after_one_click(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    clicks = []
    c = _FakeComposer(["WebCodex Demo"], late_by=10 ** 6)
    real = c.eval
    c.eval = lambda js, timeout=None: (clicks.append(1) if "var want=" in js and c.offers else None) or real(js, timeout)
    with pytest.raises(CDP._MentionFailure):
        CDP._paste_prompt(c, "review this", ["WebCodex Demo"])
    assert len(clicks) == 1, "one click, ever: a slow node is polled for, never re-clicked"


def test_a_prompt_paste_that_eats_the_mention_is_not_sent(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    c = _FakeComposer(["WebCodex Demo"], drop_on_paste=True)
    with pytest.raises(CDP._MentionFailure) as e:
        CDP._paste_prompt(c, "review this", ["WebCodex Demo"])
    assert "0 of the 1 requested app mentions" in str(e.value)
    assert isinstance(e.value, CDP._PreClickFailure)


def test_a_click_that_yields_no_app_mention_fails_closed(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    c = _FakeComposer(["WebCodex Demo"], non_app=True)
    with pytest.raises(CDP._MentionFailure) as e:
        CDP._paste_prompt(c, "review this", ["WebCodex Demo"])
    assert "is not an app" in str(e.value) and c.text == ""


def test_known_apps_default_and_snapping(monkeypatch):
    consult = _load("consult")
    monkeypatch.delenv("CGC_APPS", raising=False)
    assert consult.known_apps() == ["WebCodex Demo"]
    assert consult._mention_name("webcodex") == "WebCodex Demo", "unique prefix snaps"
    assert consult._mention_name("@WEBCODEX DEMO") == "WebCodex Demo"
    assert consult._mention_name("Canva") == "Canva", "unlisted names pass through"
    monkeypatch.setenv("CGC_APPS", "WebCodex Demo, WebCodex Pro ,@Canva")
    assert consult.known_apps() == ["WebCodex Demo", "WebCodex Pro", "Canva"]
    assert consult._mention_name("webcodex") == "webcodex", "ambiguous prefix is left for the popup"


def test_apps_command_prints_the_list(monkeypatch, capsys):
    consult = _load("consult")
    monkeypatch.setenv("CGC_APPS", "WebCodex Demo")
    assert consult.cmd_apps(None) == 0
    assert json.loads(capsys.readouterr().out)["apps"] == ["WebCodex Demo"]


def test_blocked_round_keeps_the_offered_app_names():
    backend = _load("cgc_backend")
    raw = ("noise\nCGC_ERROR mention_not_found: the composer offered no app named 'X' for '@X' "
           "within 8s. The popup offered: 'webcodex demo' — re-fire ... — NOT submitted.\n")
    code = backend._block_code(raw)
    assert code.startswith("mention_not_found") and "'webcodex demo'" in code
    assert backend._block_code("CGC_ERROR login_needed: log in") == "login_needed"


# ---- a re-opened conversation carries two editors ----------------------------------------------
# Probed live 2026-09-29: a conversation opened by URL holds a HIDDEN 0x0 editor BEFORE the visible one,
# and document.querySelector took it. The typing and the picked app went to the visible editor while
# every read (mention count, text, clear) looked at the hidden one, so a mention that had landed read as
# "the composer took no app mention from it", and the follow-up that carried it was reported "already
# sent" although nothing was sent. The composer is the visible editor; focus breaks a tie.

_NODE_COMPOSER = r"""
function ed(o){o=o||{};return {tagName:'DIV',_a:{role:'textbox',contenteditable:'true'},
  getBoundingClientRect:function(){return o.hidden?{width:0,height:0}:{width:547,height:36}},
  contains:function(n){return n===this||(o.hasFocus&&n===ACTIVE)},id:o.id||''};}
var ACTIVE=null;
var document={querySelectorAll:function(sel){return EDS},activeElement:null};
"""


def _pick_composer(eds_js, active_idx=None):
    import shutil, subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = (_NODE_COMPOSER + "var EDS=" + eds_js + ";"
           + ("ACTIVE=EDS[%d];document.activeElement=ACTIVE;" % active_idx if active_idx is not None else "")
           + "var d=" + CDP._composer_get_js() + ";"
           "console.log(JSON.stringify(d?EDS.indexOf(d):null));")
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True,
                                     check=True).stdout)


def test_composer_is_the_visible_editor_not_the_first_match():
    # the live shape: a hidden editor first, the visible one second
    assert _pick_composer("[ed({hidden:true,id:'a'}),ed({id:'b'})]") == 1
    # a single visible editor (the project page) is unchanged
    assert _pick_composer("[ed({id:'a'})]") == 0


def test_composer_prefers_the_focused_editor_when_several_are_visible():
    assert _pick_composer("[ed({id:'a'}),ed({id:'b',hasFocus:true})]", active_idx=1) == 1


def test_composer_falls_back_to_the_first_match_while_nothing_is_visible():
    # still hydrating: the old behaviour, so the wait loops that poll for a composer keep working
    assert _pick_composer("[ed({hidden:true,id:'a'}),ed({hidden:true,id:'b'})]") == 0
    assert _pick_composer("[]") is None


def test_every_composer_probe_reads_the_one_visible_editor():
    # the mention count, text read, clear and paste all resolve the composer through this one
    # expression, so they can never disagree with each other about WHICH editor they look at
    for js in (CDP._composer_mentions_js(), CDP._composer_text_js(), CDP._clear_composer_js(),
               CDP._paste_chunk_js("x"), CDP._mention_type_js("A", "t")):
        assert CDP._composer_get_js() in js


def test_followup_waits_for_the_thread_to_render_before_reading_its_baseline(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    seen = iter([0, 0, 0, 2])

    class C:
        def eval(self, js, timeout=None):
            return next(seen)

    assert CDP._await_thread_rendered(C()) is True


def test_followup_thread_that_never_renders_is_reported_not_assumed(monkeypatch):
    clock = iter(range(0, 1000, 10))
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    monkeypatch.setattr(CDP.time, "time", lambda: next(clock))

    class C:
        def eval(self, js, timeout=None):
            return 0

    assert CDP._await_thread_rendered(C(), seconds=30) is False
