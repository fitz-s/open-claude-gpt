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
  var n={tagName:String(spec.t).toUpperCase(),_a:spec.a||{},innerText:spec.x==null?'':spec.x,
    parentElement:parent||null,children:[],clicked:0,previousElementSibling:null,
    getAttribute:function(k){return k in this._a?this._a[k]:null},
    hasAttribute:function(k){return k in this._a},
    getBoundingClientRect:function(){return {width:1,height:1}},
    click:function(){this.clicked++}};
  n.textContent=n.innerText;
  (spec.c||[]).forEach(function(c,i){var k=build(c,n);
    k.previousElementSibling=i?n.children[i-1]:null;n.children.push(k);});
  return n;
}
function all(r){var o=[];(function w(n){o.push(n);n.children.forEach(w);})(r);return o;}
function attrs(sel){return sel.split(' ').map(function(p){var m=/^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(p);
  if(!m)throw new Error('unsupported selector: '+sel);return [m[1],m[2]];});}
function has(n,q){return q[1]===undefined?n.hasAttribute(q[0]):n.getAttribute(q[0])===q[1];}
function qsa(sel){var a=attrs(sel);return all(ROOT).filter(function(n){
  if(!has(n,a[a.length-1]))return false;
  var i=a.length-2,p=n.parentElement;
  while(i>=0&&p){if(has(p,a[i]))i--;p=p.parentElement;}
  return i<0;});}
var document={querySelectorAll:qsa,querySelector:function(s){return qsa(s)[0]||null;}};
"""


def _btn(lines, **attrs):
    return {"t": "button", "a": {"data-list-navigation-item": "true", **attrs}, "x": "\n".join(lines)}


def _popup_tree(*items):
    """Real popup shape: scroll area > wrapper > [section header | row ...]. A str is a header, a list
    is a row's innerText lines, a (list, attrs) pair is a row carrying attributes."""
    kids = []
    for it in items:
        if isinstance(it, str):
            kids.append({"t": "div", "x": it})
        elif isinstance(it, tuple):
            kids.append(_btn(it[0], **it[1]))
        else:
            kids.append(_btn(it))
    return {"t": "body", "c": [{"t": "div", "a": {"data-mention-list-scroll-area": ""},
                                "c": [{"t": "div", "c": kids}]}]}


def _tree_from_html(html, row_lines):
    """Parse a captured popup outerHTML into the tree spec. Rows take their captured innerText."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        VOID = {"br", "img", "input", "hr", "meta", "link"}

        def __init__(self):
            super().__init__()
            self.root = {"t": "body", "c": [], "_p": []}
            self.st = [self.root]

        def _open(self, tag, attrs):
            n = {"t": tag, "a": {k: (v or "") for k, v in attrs}, "c": [], "_p": []}
            self.st[-1]["c"].append(n)
            self.st[-1]["_p"].append(n)
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
            self.st[-1]["_p"].append(d)

    p = P()
    p.feed(html)
    rows = iter(row_lines)

    def fin(n):  # innerText: a row's is the captured one; any other node's is its text content
        for c in n["c"]:
            fin(c)
        if "data-list-navigation-item" in n.get("a", {}):
            n["x"] = "\n".join(next(rows))
        else:
            n["x"] = "".join(x if isinstance(x, str) else x["x"] for x in n["_p"])
        del n["_p"]

    fin(p.root)
    assert next(rows, None) is None, "fixture rows and popup HTML disagree on the row count"
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
    tree = _popup_tree("Plugins", ["WebCodex Demo Pro"], ["WebCodex Demo", "Runs code"])
    assert _run_pick(tree, "WebCodex Demo") == ["webcodex demo", ["WebCodex Demo"]]


def test_picker_takes_a_unique_prefix():
    tree = _popup_tree("Plugins", ["WebCodex Demo — sandbox"], ["Canva"])
    assert _run_pick(tree, "WebCodex Demo") == ["webcodex demo — sandbox", ["WebCodex Demo — sandbox"]]


def test_picker_refuses_an_ambiguous_prefix():
    tree = _popup_tree("Plugins", ["WebCodex Demo Pro"], ["WebCodex Demo Lite"])
    assert _run_pick(tree, "WebCodex Demo") == [None, []]


def test_picker_ignores_controls_that_were_on_screen_before_the_typing():
    # a same-label control that pre-dates the "@name" keystrokes is not the popup
    assert _run_pick(_popup_tree("Plugins", (["WebCodex Demo"], {"data-cgc-pre": "t0"})),
                     "WebCodex Demo") == [None, []]
    tree = _popup_tree("Plugins", (["WebCodex Demo"], {"data-cgc-pre": "t0"}), ["WebCodex Demo"])
    assert _run_pick(tree, "WebCodex Demo") == ["webcodex demo", ["WebCodex Demo"]]


def test_picker_only_picks_plugin_rows_never_files():
    # a query also lists Files; a file named like the app must not be clicked in its place
    tree = _popup_tree("Plugins", ["Canva"], "Files", ["webcodex demo notes.md"])
    r, clicked = _run_pick_tree(tree, "WebCodex Demo")
    assert r["hit"] is None and clicked == [] and r["seen"] == ["canva"]


def test_picker_never_clicks_an_unconnected_app():
    # a row with a Connect control starts an authorization flow: reported, never clicked
    tree = _popup_tree("Plugins", ["Notion", "Notion docs", "Connect"], ["Canva"])
    r, clicked = _run_pick_tree(tree, "Notion")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["notion"]
    assert r["open"] is True and r["seen"] == ["canva"]


def test_picker_never_substitutes_a_connected_prefix_sibling_for_an_unconnected_exact_app():
    # 'Notion' is unconnected; 'Notion Calendar' is connected and starts with the same text. The
    # exact app is refused, and the sibling is NOT clicked in its place (it would be the wrong app).
    tree = _popup_tree("Plugins", ["Notion", "Notion docs", "Connect"], ["Notion Calendar", "Calendar"])
    r, clicked = _run_pick_tree(tree, "Notion")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["notion"]
    # asked for by its own name, the connected sibling is still pickable
    assert _run_pick(tree, "Notion Calendar") == ["notion calendar", ["Notion Calendar"]]


def test_picker_recognizes_an_unconnected_row_in_any_ui_language():
    # the extra control's text is localized; the row's SHAPE (a third line) is not
    tree = _popup_tree(["Notion", "Notion docs", "连接"])
    r, clicked = _run_pick_tree(tree, "Notion")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["notion"]


def test_picker_takes_no_bare_row_by_prefix_and_no_row_after_an_empty_spacer():
    # a headerless popup may hold a lone FILE row: only an exact label counts there
    r, clicked = _run_pick_tree(_popup_tree(["atlas_notes.md"]), "Atlas")
    assert r["hit"] is None and clicked == []
    assert _run_pick(_popup_tree(["Atlas"]), "Atlas") == ["atlas", ["Atlas"]]
    # an empty-text spacer between the Files header and its rows must not hide the header
    tree = _popup_tree("Plugins", ["Canva"], "Files", "", ["atlas_notes.md"])
    r, clicked = _run_pick_tree(tree, "Atlas")
    assert r["hit"] is None and clicked == []
    # a header-less row in a popup that HAS headers belongs to no section: not a plugin
    r, clicked = _run_pick_tree(_popup_tree(["Atlas"], "Files", ["a.md"]), "Atlas")
    assert r["hit"] is None and clicked == []


def test_popup_offer_is_reported_when_nothing_matches():
    r, clicked = _run_pick_tree(_popup_tree("Plugins", ["Canva"]), "WebCodex Demo")
    assert r == {"hit": None, "open": True, "loading": False, "seen": ["canva"],
                 "unconnected": []} and clicked == []


def test_popup_never_opened_is_reported_as_such():
    r, _ = _run_pick_tree({"t": "body", "c": []}, "WebCodex Demo")
    assert r["hit"] is None and r["open"] is False and r["seen"] == []


# ---- the live DOM, captured 2026-09-29 -----------------------------------------------------------
# The composer's @ popup carries no ARIA roles at all, which is why the role-based selector matched
# nothing and every mention failed with "The popup offered: nothing". These pin the picker against
# the real markup and innerText, not a hand-written imitation of it.
FIXTURE = json.load(open(os.path.join(ROOT, "tests", "fixtures", "mention_popup.json")))


def _live(case):
    c = FIXTURE["cases"][case]
    return _tree_from_html(c["popup_html"], [r["lines"] for r in c["rows"]])


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


def test_live_popup_never_picks_a_file_row():
    for c in FIXTURE["cases"]["atlas"]["rows"]:
        if c["section"] == "Files":
            r, clicked = _run_pick_tree(_live("atlas"), c["lines"][0])
            assert r["hit"] is None and clicked == []


def test_live_popup_refuses_an_unconnected_app():
    r, clicked = _run_pick_tree(_live("all_plugins"), "Notion")
    assert r["hit"] is None and clicked == [] and r["unconnected"] == ["notion"]


def test_live_popup_with_no_match_is_open_empty_and_still_loading():
    r, clicked = _run_pick_tree(_live("no_match"), "Qzxvkw")
    assert r["hit"] is None and r["open"] is True and r["seen"] == [] and clicked == []
    assert r["loading"] is True, "a query nothing matches leaves the Loading suggestions skeleton"
    assert _run_pick_tree(_live("atlas"), "Atlas")[0]["loading"] is False


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
