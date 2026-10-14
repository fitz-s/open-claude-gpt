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
    """Answers the JS the paste path evaluates. `offers` = app labels the @ popup shows. Mention nodes carry
    display NAMES (as the live composer does); `yields` overrides which name a click actually produces."""

    def __init__(self, offers, popup_open=True, unconnected=(), non_app=False, loading=False,
                 late_by=0, drop_on_paste=False, yields=None, unverified=()):
        self.offers = [o.lower() for o in offers]
        self.popup_open, self.unconnected, self.non_app = popup_open, list(unconnected), non_app
        self.loading, self.unverified = loading, list(unverified)
        self.late_by, self.drop_on_paste = late_by, drop_on_paste  # polls before the node shows
        self.yields = yields
        self._pending = []
        self.html = ""
        self.text = ""
        self.mentions = []
        self.chips = []          # labels in self.text that a chip node renders, not typed text

    def eval(self, js, timeout=None):
        if "__cgcPlain" in js:                      # the typed-text read: chip nodes are not text
            out = self.text
            for chip in self.chips:
                out = out.replace(chip, "", 1)
            return out
        if "readyState" in js:
            return True
        if "selectAll" in js:
            self.html = self.text = ""
            self.mentions, self.chips = [], []
            return 0
        if "app-mention-name" in js:
            if self._pending:                       # a click landed: the node shows after late_by reads
                if self.late_by <= 0:
                    self.mentions, self._pending = self.mentions + self._pending, []
                else:
                    self.late_by -= 1
            return list(self.mentions)
        if "var want=" in js and "data-cgc-pre" in js:
            want = json.loads(js.split("var want=", 1)[1].split(";", 1)[0])
            if want in self.offers:
                if not self.non_app:  # a row that is not an app leaves the composer without a mention
                    self.html = self.html.replace("@" + want, "") + f'<span data-mention>{want}</span>'
                    self.text = self.text.lower().replace("@" + want, want)
                    self.chips.append(want)
                    self._pending.append(self.yields or want)
                return {"hit": want, "open": True, "seen": [], "unconnected": [], "unverified": []}
            return {"hit": None, "open": self.popup_open, "loading": self.loading, "seen": self.offers,
                    "unconnected": self.unconnected, "unverified": self.unverified}
        if "execCommand('insertText'" in js:
            chunk = json.loads(js.split("execCommand('insertText',false,", 1)[1].rsplit(");", 1)[0])
            if self.drop_on_paste and len(chunk) > 1:
                self.mentions = []          # a select-and-replace accident eats the mention node
            self.text += chunk
            self.html += chunk.lower()
            return True if "return true" in js else len(self.text)
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
    base = dict(kind="submit", logical_sha="x", project_url="u", model="Pro", model_family="GPT-6",
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
                           project_url="https://chatgpt.com/", model="Pro", model_family="GPT-6",
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
    click:function(){this.clicked++},
    contains:function(x){while(x){if(x===n)return true;x=x.parentElement;}return false;}};
  (spec.c||[]).forEach(function(c){var k=build(c,n);n.childNodes.push(k);
    if(k.nodeType===1){k.previousElementSibling=n.children.length?n.children[n.children.length-1]:null;n.children.push(k);}});
  // innerText: block-level children (a row's label and description spans sit on separate lines in the
  // browser, headers and rows too) are separated by a newline; inline text runs together.
  Object.defineProperty(n,'innerText',{get:function(){if(spec.x!=null)return spec.x;
    var parts=n.childNodes.map(function(k){return k.innerText}).filter(function(x){return x!==''});
    var block=n.children.length>1&&n.children.every(function(k){return k.tagName!=='SVG'});
    var rowBody=n.hasAttribute&&n.hasAttribute('data-menu-row-content');
    return parts.join((block&&(rowBody||n._a['data-list-navigation-item']===undefined))?'\n':'')}});
  // textContent concatenates text nodes with NO separator (the browser does; innerText adds the block
  // newlines). A mock that returned innerText here hid a live regression: adjacent label/description
  // spans read 'WebCodex DemoLocal...' in a real page, and a strict equality on it refused every app.
  Object.defineProperty(n,'textContent',{get:function(){if(spec.x!=null)return spec.x;
    return n.childNodes.map(function(k){return k.textContent}).join('')}});
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


def test_picker_refuses_two_rows_with_the_same_exact_label_and_says_so():
    # two identically named rows may be two different apps: never pick one of them
    tree = _popup_tree("Plugins", _app("Notion"), _app("Notion"), _app("Canva"))
    r, clicked = _run_pick_tree(tree, "Notion")
    assert r["hit"] is None and clicked == [] and r["ambiguous"] == 2
    msg = CDP._mention_offer(True, r["seen"], r["unconnected"], r["loading"], r["unverified"], r["ambiguous"], "Notion")
    assert msg.startswith("ambiguous: 2 rows named 'Notion'") and "will not pick one" in msg


def test_the_failure_message_for_identical_labels_reaches_the_caller(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    monkeypatch.setattr(CDP, "_MENTION_WAIT_S", 0.05)
    c = _FakeComposer([])
    real = c.eval
    c.eval = lambda js, timeout=None: ({"hit": None, "open": True, "loading": False, "seen": ["notion", "notion"],
                                        "unconnected": [], "unverified": [], "ambiguous": 2}
                                       if "var want=" in js and "data-cgc-pre" in js else real(js, timeout))
    with pytest.raises(CDP._MentionFailure) as e:
        CDP._paste_prompt(c, "body", ["Notion"])
    assert "ambiguous: 2 rows named 'Notion'" in str(e.value) and c.text == ""


def test_picker_never_takes_a_prefix_match_it_would_send_the_wrong_app():
    # 'Atlas' asked for, only 'Atlas Pro' enabled: the ONLY prefix match. Clicking it would attach a
    # different app and every later check would pass, so the prompt would go out with the wrong app.
    tree = _popup_tree("Plugins", _app("Atlas Pro"), _app("Canva"))
    r, clicked = _run_pick_tree(tree, "Atlas")
    assert r["hit"] is None and clicked == [] and r["seen"] == ["atlas pro", "canva"]
    # the same with a single result and no header (a lone enabled app that merely starts with the name)
    r, clicked = _run_pick_tree(_popup_tree(_app("Notion Calendar")), "Notion")
    assert r["hit"] is None and clicked == []
    # asked for by its own full name it is picked
    assert _run_pick(tree, "Atlas Pro") == ["atlas pro", ["Atlas Pro"]]


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


def _shaped_row(label, desc="Does things", in_label="", block_extra=None, sibling=None):
    """A real-shaped app row with an extra control planted where an unconnected app carries it."""
    lead = {"t": "span", "a": {"class": "leadingIcon-x"}, "c": [{"t": "svg", "a": {"class": "rounded-2xs icon-xs"}}]}
    text = {"t": "span", "c": [{"t": "span", "c": [{"t": "span", "c": [label + in_label]}] + (
        [{"t": "span", "a": {"class": "text-codex-description"}, "c": [desc]}] if desc else [])}]}
    content = [lead, text] + ([block_extra] if block_extra else [])
    kids = [{"t": "div", "a": {"data-menu-row-content": "true"}, "c": content}] + ([sibling] if sibling else [])
    return {"t": "button", "a": {"data-list-navigation-item": "true"}, "c": kids}


def _desc_row(label, desc_children):
    """A real-shaped app row whose DESCRIPTION element holds `desc_children` (text and/or elements)."""
    lead = {"t": "span", "a": {"class": "leadingIcon-x"}, "c": [{"t": "svg", "a": {"class": "rounded-2xs icon-xs"}}]}
    text = {"t": "span", "c": [{"t": "span", "c": [{"t": "span", "c": [label]},
                                                   {"t": "span", "a": {"class": "text-codex-description"},
                                                    "c": desc_children}]}]}
    return {"t": "button", "a": {"data-list-navigation-item": "true"},
            "c": [{"t": "div", "a": {"data-menu-row-content": "true"}, "c": [lead, text]}]}


@pytest.mark.parametrize("shape,row", [
    ("description text plus a Connect <button> nested inside the description",
     _desc_row("Notion", ["Notion docs", {"t": "button", "c": ["Connect"]}])),
    ("description text plus a bare ' Connect' text node inside the description",
     _desc_row("Notion", ["Notion docs", " Connect"])),
    ("a Connect <span> nested inside the label element",
     _shaped_row("Notion", in_label="")  # replaced below: label element holds an element child
     if False else {"t": "button", "a": {"data-list-navigation-item": "true"}, "c": [{"t": "div", "a": {"data-menu-row-content": "true"}, "c": [
         {"t": "span", "a": {"class": "leadingIcon-x"}, "c": [{"t": "svg", "a": {"class": "rounded-2xs icon-xs"}}]},
         {"t": "span", "c": [{"t": "span", "c": [{"t": "span", "c": ["Notion", {"t": "span", "c": ["Connect"]}]},
                                                 {"t": "span", "a": {"class": "text-codex-description"}, "c": ["docs"]}]}]}]}]}),
])
def test_picker_refuses_a_connect_control_nested_inside_the_label_or_description(shape, row):
    for tree in (_popup_tree("Plugins", row), _popup_tree(row)):
        r, clicked = _run_pick_tree(tree, "Notion")
        assert r["hit"] is None and clicked == [], shape


def test_the_inherent_limit_a_description_that_really_is_or_ends_in_connect_reads_as_connected():
    """Stated, not hidden: 'Connect' that is PART of the single description text node cannot be told from a
    description that genuinely says so. The rule is structural (label + description, leaf elements holding one
    text node, nothing else in the row); it cannot read meaning. This pins the boundary so nobody assumes it
    is closed. (A SECOND text node or an element carrying it is refused, see the tests above.)"""
    row = _desc_row("Notion", ["Notion docs and Connect"])          # ONE text node
    assert _run_pick(_popup_tree("Plugins", row), "Notion") == ["notion", ["Notion"]]


@pytest.mark.parametrize("shape,row", [
    ("Connect as text inside the label span", _shaped_row("Notion", in_label=" Connect")),
    ("Connect as a <button> in the content block", _shaped_row("Notion", block_extra={"t": "button", "c": ["Connect"]})),
    ("Connect as a bare text node in the content block", _shaped_row("Notion", block_extra="Connect")),
    ("Connect as a sibling of the content block", _shaped_row("Notion", sibling={"t": "span", "c": ["Connect"]})),
    ("localized Connect beside the content block", _shaped_row("Notion", sibling={"t": "span", "c": ["\u8fde\u63a5"]})),
    ("label plus Connect, no description", _shaped_row("Notion", desc=None, block_extra="Connect")),
])
def test_picker_refuses_every_shape_an_unconnected_app_can_take(shape, row):
    # the extra control is legally placed in several spots; none of them may read as "connected".
    # (the label used for the match ignores the stray text, so this is what stops the click)
    for tree in (_popup_tree("Plugins", row), _popup_tree(row)):
        r, clicked = _run_pick_tree(tree, "Notion")
        assert clicked == [], shape
        assert r["hit"] is None, shape


def test_picker_still_clicks_a_plain_headerless_app_with_a_description():
    # the real shape of a connected app that is the ONLY result: rounded-2xs icon, label, description, nothing
    # else. This must stay clickable, or every single-result mention would fail closed.
    assert _run_pick(_popup_tree(_shaped_row("WebCodex Demo", desc="Local demo")), "WebCodex Demo") == \
        ["webcodex demo", ["WebCodex Demo"]]


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
                 "unconnected": [], "unverified": [], "ambiguous": 0} and clicked == []


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
    assert c.mentions == ["webcodex demo"] and c.text.endswith("review this")


def test_a_click_that_yields_a_different_app_is_refused_and_the_composer_cleared(monkeypatch):
    # every earlier check would pass: the row is app-shaped, exact, and a node appears. The node's NAME is
    # what proves it is the requested app, so a click that attached another one must not be sent.
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    c = _FakeComposer(["Notion"], yields="notion calendar")
    with pytest.raises(CDP._MentionFailure) as e:
        CDP._paste_prompt(c, "review this", ["Notion"])
    msg = str(e.value)
    assert "'notion calendar'" in msg and "wrong app" in msg and "'notion'" in msg
    assert isinstance(e.value, CDP._PreClickFailure) and c.text == "" and c.mentions == []


def test_two_requested_apps_each_have_to_be_the_one_that_appeared(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    c = _FakeComposer(["Alpha", "Beta"])
    CDP._paste_prompt(c, "body", ["Alpha", "Beta"])
    assert sorted(c.mentions) == ["alpha", "beta"]
    # the SECOND click producing the first app's node again is not "Beta"
    c = _FakeComposer(["Alpha", "Beta"])
    real = c.eval
    n = {"clicks": 0}

    def ev(js, timeout=None):
        if "var want=" in js and "data-cgc-pre" in js:
            n["clicks"] += 1
            c.yields = "alpha"           # every click attaches Alpha
        return real(js, timeout)
    c.eval = ev
    with pytest.raises(CDP._MentionFailure):
        CDP._paste_prompt(c, "body", ["Alpha", "Beta"])


def test_a_paste_that_swaps_the_mention_for_another_app_is_not_sent_even_though_the_count_is_right(monkeypatch):
    """After the paste the composer holds ONE app mention, as requested, but it is the WRONG app. A count
    check passes; only the names differ (kept != wanted while the lengths are equal)."""
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    c = _FakeComposer(["WebCodex Demo"])
    real = c.eval

    def ev(js, timeout=None):
        if "execCommand('insertText'" in js and "return true" not in js:      # a prompt chunk, not the space
            c.mentions = ["some other app"]                                    # swapped: same length, other name
        return real(js, timeout)
    c.eval = ev
    with pytest.raises(CDP._MentionFailure) as e:
        CDP._paste_prompt(c, "review this", ["WebCodex Demo"])
    assert len(c.mentions) == 1, "the count is exactly what was requested"
    msg = str(e.value)
    assert "'some other app'" in msg and "'webcodex demo'" in msg and "wrong apps" in msg


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
    assert "holds the app mentions [] after the paste" in str(e.value) and "'webcodex demo'" in str(e.value)
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


# ---- the composer is resolved once, from the form, and the send button belongs to that form -------
# Probed live 2026-09-29: a conversation re-opened by URL holds TWO forms, a hidden 0x0 one (with its own
# editor and buttons) before the visible one. document.querySelector took the hidden editor: typing and
# the picked app went to the visible one while every read (mention count, text, clear) saw the hidden one,
# so a mention that had landed read as "the composer took no app mention". The composer is the VISIBLE
# editor INSIDE a <form> (an edit-message box in a turn is not in one); focus breaks a tie; a hidden
# editor is never a fallback; the send button is the one INSIDE that composer's own form.

_NODE_FORMS = r"""
function el(t,o){o=o||{};var n={nodeType:1,tagName:t.toUpperCase(),_a:o.a||{},children:[],parent:null,hidden:!!o.hidden,
  disabled:!!o.disabled,clicked:0,
  getAttribute:function(k){return k in this._a?this._a[k]:null},
  hasAttribute:function(k){return k in this._a},
  getBoundingClientRect:function(){return this.hidden||(this.parent&&this.parent.hiddenDeep())?{width:0,height:0}:{width:500,height:40}},
  hiddenDeep:function(){return this.hidden||(this.parent&&this.parent.hiddenDeep())},
  closest:function(sel){var ok=sel.split(',').map(function(x){return x.trim()});var n=this;
    while(n){if(ok.indexOf(n.tagName.toLowerCase())>=0||(n._a['data-message-author-role']!==undefined&&ok.indexOf('[data-message-author-role]')>=0))return n;n=n.parent;}return null;},
  contains:function(x){while(x){if(x===this)return true;x=x.parent;}return false;},
  querySelectorAll:function(sel){return ALL.filter(function(x){return x!==n&&n.contains(x)&&match(x,sel)})},
  click:function(){this.clicked++},
  focus:function(){if(!this.hiddenDeep())document.activeElement=this;},   // a hidden editor cannot take focus
  _text:'',_sel:false};
  (o.c||[]).forEach(function(c){c.parent=n;n.children.push(c);});return n;}
var ALL=[];function reg(n){ALL.push(n);n.children.forEach(reg);return n;}
function match(x,sel){return sel.split(',').some(function(p){p=p.trim();
  if(p==='#prompt-textarea')return x._a.id==='prompt-textarea';
  if(p==='div[role="textbox"][contenteditable="true"]')return x.tagName==='DIV'&&x._a.role==='textbox'&&x._a.contenteditable==='true';
  if(p==='button[data-testid="send-button"]')return x.tagName==='BUTTON'&&x._a['data-testid']==='send-button';
  if(p==='button[aria-label*="Send" i]')return x.tagName==='BUTTON'&&/send/i.test(x._a['aria-label']||'');
  if(p==='button[aria-label*="\u53d1\u9001"]')return x.tagName==='BUTTON'&&/\u53d1\u9001/.test(x._a['aria-label']||'');
  if(p==='form')return x.tagName==='FORM';
  if(p==='[role="alert"]'||p==='[id*="challenge"]')return false;   // no rate-limit alert, no challenge
  throw new Error('unsupported selector: '+p);});}
function ed(o){o=o||{};o.a=Object.assign({role:'textbox',contenteditable:'true'},o.a||{});var n=el('div',o);
  Object.defineProperty(n,'innerText',{get:function(){return n._text}});
  // what was typed is one text node, as in the browser: the composer text read walks childNodes
  Object.defineProperty(n,'childNodes',{get:function(){return [{nodeType:3,nodeValue:n._text}]}});return n;}
function send(o){o=o||{};o.a=Object.assign({'aria-label':'Send'},o.a||{});return el('button',o);}
var ACTIVE=null;
var document={activeElement:null,readyState:'complete',
  querySelectorAll:function(sel){return ALL.filter(function(x){return match(x,sel)})},
  querySelector:function(sel){
    if(/input\[type="password"\]|iframe\[/.test(sel))return null;   // no login wall, no captcha frame
    return document.querySelectorAll(sel)[0]||null;}};
var location={hostname:'chatgpt.com',pathname:'/c/x'};
// execCommand acts on the FOCUSED editable, as in the browser: this is why a paste can land in one editor
// while a read of another editor sees nothing.
document.execCommand=function(cmd,ui,arg){var t=document.activeElement;if(!t)return false;
  if(cmd==='selectAll'){t._sel=true;}else if(cmd==='delete'){if(t._sel){t._text='';t._sel=false;}}
  else if(cmd==='insertText'){t._text+=arg;}return true;};
"""


def _run_forms(tree_js, expr, active=None):
    """Run a driver JS expression in node over a tree of forms/editors/buttons; returns its JSON result."""
    import shutil, subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    src = (_NODE_FORMS + "var ROOT=reg(" + tree_js + ");"
           + ("document.activeElement=%s;" % active if active else "")
           + "var R=" + expr + ";console.log(JSON.stringify(R));")
    return json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True,
                                     check=True).stdout)


_HIDDEN_FORM_FIRST = ("el('body',{c:[el('form',{hidden:true,c:[ed({a:{id:'H'}}),send()]}),"
                      "el('form',{c:[ed({a:{id:'V'}}),send({a:{id:'S'}})]})]})")


def _composer_id(tree_js, active=None):
    r = _run_forms(tree_js, "(function(){var d=" + CDP._composer_get_js() + ";return d?d._a.id||'?':null;})()",
                   active)
    return r


def test_composer_is_the_visible_editor_not_the_first_match():
    assert _composer_id(_HIDDEN_FORM_FIRST) == "V"      # the live shape: hidden form first
    assert _composer_id("el('body',{c:[el('form',{c:[ed({a:{id:'A'}})]})]})") == "A"   # project page


def test_composer_has_no_hidden_fallback():
    # hydrating: nothing visible yet means NO composer, not the first match. A read or paste that landed
    # in a hidden editor is silent, and readiness must not pass on it.
    assert _composer_id("el('body',{c:[el('form',{hidden:true,c:[ed({a:{id:'H'}})]})]})") is None
    assert _composer_id("el('body',{c:[]})") is None


def test_composer_ignores_an_edit_message_box_that_is_not_in_the_composer_form():
    # a visible editor inside a conversation turn (editing a sent message) is not the composer
    tree = ("el('body',{c:[el('article',{c:[el('div',{c:[ed({a:{id:'EDIT'}})]})]}),"
            "el('form',{c:[ed({a:{id:'MAIN'}})]})]})")
    assert _composer_id(tree) == "MAIN"
    # even an edit box wrapped in a form inside a turn is excluded
    tree = ("el('body',{c:[el('article',{c:[el('form',{c:[ed({a:{id:'EDIT'}})]})]}),"
            "el('form',{c:[ed({a:{id:'MAIN'}})]})]})")
    assert _composer_id(tree) == "MAIN"


def test_composer_prefers_the_focused_editor_when_several_qualify():
    tree = "el('body',{c:[el('form',{c:[ed({a:{id:'A'}})]}),el('form',{c:[ed({a:{id:'B'}})]})]})"
    assert _composer_id(tree, active="ALL.filter(function(x){return x._a.id==='B'})[0]") == "B"
    assert _composer_id(tree) == "A"


def test_readiness_uses_the_same_resolution_as_the_paste():
    # a page whose only editor is hidden is NOT ready, so _await_composer keeps waiting instead of
    # letting the paste run against nothing
    js = CDP._composer_state_js()
    hidden = _run_forms("el('body',{c:[el('form',{hidden:true,c:[ed()]})]})", "JSON.parse((" + js + "))")
    ready = _run_forms(_HIDDEN_FORM_FIRST, "JSON.parse((" + js + "))")
    assert hidden["composer"] is False and ready["composer"] is True


def _send(tree_js):
    return _run_forms(tree_js, CDP._send_click_js())


def test_send_clicks_the_button_in_the_composers_own_form():
    # two forms each with a send button: only the visible composer's form is clicked
    tree = _HIDDEN_FORM_FIRST
    r = _run_forms(tree, "(function(){var v=" + CDP._send_click_js() +
                   ";return [v,ALL.filter(function(x){return x.clicked}).map(function(x){return x._a.id||'?'})];})()")
    assert r == ["clicked", ["S"]], "the hidden form's send button is never touched"


def _send_with_focus(tree_js, focused_id=None):
    """Run the send eval with focus on the element whose id is `focused_id` (None: nothing focused)."""
    active = ("ALL.filter(function(x){return x._a.id===%s})[0]" % json.dumps(focused_id)) if focused_id else None
    return _run_forms(tree_js, CDP._send_click_js(), active=active)


def test_send_refuses_ambiguous_buttons_and_absent_ones():
    two = "el('body',{c:[el('form',{c:[ed(),send({a:{id:'1'}}),send({a:{id:'2'}})]})]})"
    r = _run_forms(two, "(function(){var v=" + CDP._send_click_js() +
                   ";return [v,ALL.filter(function(x){return x.clicked}).length];})()")
    assert r == ["ambiguous", 0], "several send buttons in the form is a guess: nothing is clicked"
    assert _send("el('body',{c:[]})") == "no-composer"
    assert _send("el('body',{c:[el('form',{c:[ed(),send({disabled:true})]})]})") == "disabled"


def test_send_with_no_button_yet_needs_focus_inside_the_composer_before_enter_is_allowed():
    # no send button rendered yet (empty draft): Enter is the pre-existing fallback, but Enter submits
    # whatever is FOCUSED, so it is only allowed when focus is verified inside the resolved editor.
    tree = "el('body',{c:[el('form',{c:[ed({a:{id:'E'}})]}),el('div',{a:{id:'OTHER'}})]})"
    assert _send_with_focus(tree, "E") == "no-send"
    assert _send_with_focus(tree, "OTHER") == "no-focus", "focus is elsewhere: Enter would act on it"
    assert _send_with_focus(tree, None) == "no-focus", "nothing focused"
    assert _send(tree) == "no-focus"


def test_send_verdict_lets_only_clicked_and_no_send_go_on_and_enter_only_for_no_send():
    assert CDP._send_verdict("clicked") == ("clicked", "")
    assert CDP._send_verdict("no-send") == ("enter", "")
    for neg in ("no-composer", "ambiguous", "disabled", "no-focus", "error-preclick:TypeError x"):
        verdict, why = CDP._send_verdict(neg)
        assert verdict == "not-sent" and "send target" in why, neg
    # No reply, or one nobody listed, is not evidence of anything: it may have come after click().
    for unknown in (None, "", "weird", 0, False, "error-postclick", "Clicked"):
        assert CDP._send_verdict(unknown)[0] == "unknown", unknown


def test_the_send_eval_returns_an_explicit_negative_for_a_throw_before_the_click():
    # f.querySelectorAll throws on a form that cannot be searched: caught before the gesture, so it is
    # an explicit pre-click negative, and no button is clicked.
    tree = "el('body',{c:[el('form',{c:[ed(),send({a:{id:'S'}})]})]})"
    expr = ("(function(){ALL.filter(function(x){return x.tagName==='FORM'})[0].querySelectorAll="
            "function(){throw new Error('boom')};var v=" + CDP._send_click_js() +
            ";return [v,ALL.filter(function(x){return x.clicked}).length];})()")
    v, clicked = _run_forms(tree, expr)
    assert v == "error-preclick:boom" and clicked == 0
    assert CDP._send_verdict(v)[0] == "not-sent"


def test_a_throw_from_the_click_itself_is_not_a_pre_click_negative():
    # click() is outside the try: if it throws the eval yields no value, which reads as unknown.
    tree = "el('body',{c:[el('form',{c:[ed(),send({a:{id:'S'}})]})]})"
    expr = ("(function(){ALL.filter(function(x){return x._a.id==='S'})[0].click="
            "function(){throw new Error('mid-click')};try{return " + CDP._send_click_js() +
            "}catch(e){return 'escaped:'+e.message;}})()")
    assert _run_forms(tree, expr) == "escaped:mid-click"


def test_send_never_reaches_a_send_button_outside_the_composers_form():
    # a Send-labelled button elsewhere on the page (another form) must not be clicked for this composer
    tree = ("el('body',{c:[el('form',{c:[ed({a:{id:'E'}})]}),el('form',{c:[el('div',{c:[send({a:{id:'X'}})]})]})]})")
    r = _run_forms(tree, "(function(){var v=" + CDP._send_click_js() +
                   ";return [v,ALL.filter(function(x){return x.clicked}).length];})()",
                   active="ALL.filter(function(x){return x._a.id==='E'})[0]")
    assert r == ["no-send", 0]


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


# ---- the paste on a re-opened page: it LANDED, and the verification read the wrong editor --------------
# Live 2026-09-29/30: every follow-up on a conversation re-opened by URL failed "composer holds 0 normalized
# chars, prompt needs >= N - paste looks truncated by a DOM stall", with or without a --mention. The whole
# prompt HAD landed (the visible editor held it, and it persisted as a draft): execCommand acts on the focused
# editable, the page autofocuses the visible editor, and a hidden editor cannot take focus, so the paste went
# to the visible editor while document.querySelector(...) handed the reads the hidden one. Not a hydration
# race (it reproduced with the thread fully rendered) and not the @ popup (it reproduced with no mention).

_CHUNK = "the whole prompt " * 40


def _paste_then_read(tree_js, get_js=None):
    """The exact JS the driver runs for clear -> paste -> verify, in ONE page, focus on the visible editor."""
    clear, paste, read = CDP._clear_composer_js(), CDP._paste_chunk_js(_CHUNK), CDP._composer_text_js()
    if get_js is not None:                     # counterfactual: swap in another way of finding the composer
        clear, paste, read = (x.replace(CDP._composer_get_js(), get_js) for x in (clear, paste, read))
    expr = ("(function(){var c=" + clear + ";var p=" + paste + ";var r=" + read + ";"
            "var v=ALL.filter(function(x){return x._a.id==='V'})[0];"
            "return {clear:c,pasted:p,read:(r||'').length,landed:v._text.length};})()")
    return _run_forms(tree_js, expr, active="ALL.filter(function(x){return x._a.id==='V'})[0]")


def test_the_paste_verification_reads_the_editor_the_paste_landed_in():
    r = _paste_then_read(_HIDDEN_FORM_FIRST)
    assert r["landed"] == len(_CHUNK) and r["read"] == len(_CHUNK), r
    assert r["clear"] == 0 and r["pasted"] == len(_CHUNK)


def test_the_old_first_match_expression_read_zero_chars_after_a_paste_that_landed():
    old = "document.querySelector(" + json.dumps(CDP._COMPOSER_SEL) + ")"
    r = _paste_then_read(_HIDDEN_FORM_FIRST, get_js=old)
    assert r["landed"] == len(_CHUNK), "the paste went to the visible editor, where the focus was"
    assert r["read"] == 0, "and the verification read the hidden one: 'composer holds 0 normalized chars'"


# ---- a follow-up with no --mention never opens the @ popup ---------------------------------------------

def test_a_paste_without_a_mention_never_types_an_at_or_touches_the_popup(monkeypatch):
    monkeypatch.setattr(CDP.time, "sleep", lambda s: None)
    c = _FakeComposer([])
    seen = []
    real = c.eval
    c.eval = lambda js, timeout=None: (seen.append(js), real(js, timeout))[1]
    for mentions in ([], None, ()):
        CDP._paste_prompt(c, "plain body", mentions)
    assert not any("data-cgc-pre" in js or "data-mention-list-scroll-area" in js for js in seen)
    chunks = [json.loads(js.split("insertText',false,", 1)[1].rsplit(");", 1)[0])
              for js in seen if "insertText',false," in js]
    assert chunks and not any(x.startswith("@") for x in chunks), "no '@' is ever typed"
    assert c.text == "plain body"


def test_the_mock_dom_reads_textcontent_like_a_browser_not_like_innertext():
    """Adjacent spans concatenate in textContent with no whitespace; innerText separates blocks with a
    newline. The connected-row test depends on both, and a mock that conflated them let a live regression
    through (the exact, connected app was refused as 'not connected')."""
    import shutil, subprocess
    if not shutil.which("node"):
        pytest.skip("node not installed")
    tree = _popup_tree("Plugins", _app("WebCodex Demo", desc="Local demo"))
    src = (_NODE_DOM + "var ROOT=build(" + json.dumps(tree) + ",null);"
           "var e=document.querySelectorAll('[data-list-navigation-item]')[0];"
           "console.log(JSON.stringify([e.textContent,e.innerText]));")
    tc, it = json.loads(subprocess.run(["node", "-e", src], capture_output=True, text=True, check=True).stdout)
    assert tc == "WebCodex DemoLocal demo" and it == "WebCodex Demo\nLocal demo"


def test_the_real_captured_connected_app_is_picked_by_the_real_markup():
    # the exact regression: WebCodex Demo, connected, exactly as the live page renders it
    assert _run_pick(_live("webcodex_demo"), "WebCodex Demo") == ["webcodex demo", ["WebCodex Demo"]]
    assert _run_pick(_live("all_plugins"), "WebCodex Demo") == ["webcodex demo", ["WebCodex Demo"]]
    assert _run_pick(_live("outlook"), "Outlook Email") == ["outlook email", ["Outlook Email"]]
