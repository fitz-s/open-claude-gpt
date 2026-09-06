#!/usr/bin/env python3
# Created: 2026-08-18
# Last audited: 2026-08-18
# Authority basis: live DOM probe of ChatGPT's new composer (2026-08-18) — the top-level
# switcher menu stopped containing a flat 'Pro' menuitem and moved the tier onto a Radix
# power/effort slider (span[role="slider"], valuenow 0..4 = Instant..Pro). _select_model
# had to grow a slider-first path while keeping the old flat-menu path as a fallback, since
# ChatGPT is mid-rollout and both forms are live somewhere.
"""
Offline unit tests for the model-tier picker in cdp_consult.py: the pure slider-label
parser, and _select_model driven against a small fake-CDP state machine (no browser, no
network). Run: python3 -m pytest tests/test_model_picker.py -q
"""
import importlib.util as _ilu
import json as _json
import os as _os
import re as _re

_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_CDP_PATH = _os.path.join(_REPO, "skill", "scripts", "cdp_consult.py")
_spec = _ilu.spec_from_file_location("cdp_consult", _CDP_PATH)
_CDP = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_CDP)

_WANT_RE = _re.compile(r'var want=(".*?");')

# The tier read and the family read became ONE snapshot on 2026-09-06 (_PICKER_STATE_JS), because
# a (family, tier) pair stitched from two Runtime.evaluate round-trips is a pair that was never
# simultaneously true. Every fake below therefore answers the slider read with a `fam` field.
#
#   _LEGACY_FAM  a picker that positively renders NO family control: it exists (owners == 1), its
#                radio list read back (an empty list, not null), and there is no radio-group
#                scaffolding either. This is the pre-GPT-6 composer, and the ONLY shape that earns
#                the silent family exemption. Note it is NOT the same value as an unreadable probe
#                — that distinction is the whole of defect 1.
#   _NO_PICKER   no intelligence picker is open at all (a mode-toggle menu, or a closed composer).
_LEGACY_FAM = {"owners": 1, "menus": 1, "radios": [], "scaffold": 0}
_NO_PICKER = {"fam": {"owners": 0, "menus": 1, "radios": None, "scaffold": 0},
              "first": "", "lines": [], "descs": [], "btnLabel": "",
              "now": None, "min": None, "max": None}


def _pick(client, target, family=None):
    """_select_model returns a _ModelPick (confirmed/shown/badge/error) as of 2026-09-06 — it now
    reports the composer's model badge and a family-miss sentence alongside the tier verdict. The
    tests that predate the family dimension only ever asserted on the tier verdict, so they go
    through this two-value view; the family tests below read the full pick."""
    p = _CDP._select_model(client, target, family)
    return p.confirmed, p.shown


def _wanted_label(expr):
    """Pull the label _open_cand_by_label_js asked to open out of its generated JS text — the
    fakes below dispatch on substrings of the generated JS rather than running a JS engine (see
    the module docstring), so this is how they see WHICH candidate a Runtime.evaluate call was
    actually addressing, now that addressing is by label instead of by a positional index."""
    m = _WANT_RE.search(expr)
    return _json.loads(m.group(1)) if m else None


# ---- _slider_label: pure parse of the slider group's first line -----------------------------
#
# Superseded 2026-08-28: a live fresh-tab probe showed the group's first line is sometimes the
# BARE tier word ('Pro'), not just the ', N of M.' announced form ('Pro, 5 of 5.') — the suffix
# is an accessibility announcement present only once the control has been driven by keyboard
# (data-keyboard-interaction-active). The old contract ("no comma -> not this format -> return
# '' ") could not tell a bare label apart from genuinely unrelated text and silently discarded
# BOTH, which is why a walk that landed exactly on Pro still reported (False, ''). The new
# contract: strip the announcement suffix when present, else trust the (short, single-line)
# text as-is.

def test_slider_label_pro():
    assert _CDP._slider_label("Pro, 5 of 5.") == "Pro"


def test_slider_label_extra_high():
    assert _CDP._slider_label("Extra High, 4 of 5.") == "Extra High"


def test_slider_label_bare_pro():
    """Live-verified 2026-08-28: a fresh tab's picker group renders the bare word with no
    ', N of M.' suffix at all until the control has been driven by keyboard. Must parse through,
    not be discarded for lacking a comma."""
    assert _CDP._slider_label("Pro") == "Pro"


def test_slider_label_bare_extra_high():
    assert _CDP._slider_label("Extra High") == "Extra High"


def test_slider_label_long_prose_is_not_a_label():
    """The sanity bound: a control's instructional prose must not be mistaken for a tier label,
    even though _matches() could never have turned it into a false MATCH on its own — this just
    keeps such text out of the reported 'shown' value on failure."""
    assert _CDP._slider_label("Use Left and Right arrow keys to adjust power.") == ""


def test_slider_label_empty_string():
    assert _CDP._slider_label("") == ""


# ---- _select_model against a fake CDP: the new slider-based composer ------------------------
#
# The fake dispatches on distinctive substrings of the generated JS (the same white-box style
# already used elsewhere in this suite for _JS_RID_IN_MAIN) rather than parsing JS, since the
# whole point is exercising the Python control flow in _select_model / _slider_set, not a JS
# engine.

class _FakeSliderClient:
    """Simulates the build probed live 2026-08-18: one composer switcher, a group first line
    '<Label>, N of 5.', a slider valuenow 0..4 mapping Instant/Medium/High/Extra High/Pro, no
    flat menuitem and no submenu trigger. `keys_work=False` simulates arrow presses that never
    reach the slider (unfocused control, or a build that silently dropped keyboard support)."""

    LABELS = ["Instant", "Medium", "High", "Extra High", "Pro"]

    def __init__(self, start=2, keys_work=True):
        self.now = start
        self.keys_work = keys_work
        self.menu_open = False
        self.key_presses = 0

    def _label(self):
        return self.LABELS[self.now]

    def eval(self, expr):
        if "KeyboardEvent" in expr:                        # _DISMISS_JS
            # These fakes model builds whose Escape genuinely dismisses (verified pre-GPT-6): by
            # the time _close_menus checks, nothing reads aria-expanded any more.
            return 0
        if "aria-valuenow" in expr:                      # _PICKER_STATE_JS
            if not self.menu_open:
                return None
            return {"first": "%s, %d of 5." % (self._label(), self.now + 1),
                    "fam": _LEGACY_FAM, "now": self.now, "min": 0, "max": 4}
        if ".focus();return true" in expr:                # _SLIDER_FOCUS_JS
            return self.menu_open
        if "pointerover" in expr:                          # _open_submenu_js — none in this build
            return False
        if 'aria-haspopup="menu"' in expr:                 # _submenu_count_js
            return 0
        if "var FAMS=" in expr:                            # _FAMILY_STATE_JS / _click_family_js
            return []                                       # pre-GPT-6: no model radios at all
        if "menuitemradio" in expr:                        # _click_item_js — no flat item here
            return False
        if "var EL=c[" in expr:                             # _open_cand_by_label_js
            want = _wanted_label(expr)
            if want != self._label():
                return None
            self.menu_open = True
            return self._label()
        if ".map(function(b){" in expr:                     # _cand_labels_js
            return [self._label()]
        return 1                                             # _cand_count_js

    def key(self, key_name, code, keycode):
        self.key_presses += 1
        if key_name == "Escape":
            self.menu_open = False
            return
        if not self.keys_work:
            return
        if key_name == "ArrowLeft":
            self.now = max(0, self.now - 1)
        elif key_name == "ArrowRight":
            self.now = min(4, self.now + 1)


def test_slider_reaches_pro_from_high_with_bounded_presses(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeSliderClient(start=2, keys_work=True)  # starts on High
    ok, shown = _pick(client, "Pro")
    assert ok is True and shown == "Pro"
    # worst case is 2*(valuemax-valuemin) = 8 arrow presses, plus a couple of Escapes to leave
    # the composer clean — nowhere near a hang.
    assert client.key_presses <= 12, client.key_presses


def test_slider_keys_swallowed_does_not_hang_and_fails_closed(monkeypatch):
    """Arrow presses land on the fake but never move the slider — the 'keys not landing' case
    _slider_set is specifically supposed to detect and bail out of, rather than looping the
    full walk for nothing."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeSliderClient(start=2, keys_work=False)  # High, but ArrowLeft/Right do nothing
    ok, shown = _pick(client, "Pro")
    assert ok is False
    assert shown == "High", "verdict must fall through to the unmatched switcher's real label"
    # Must bail after the very first unproductive press per attempt, not walk the full range.
    assert client.key_presses <= 6, client.key_presses


# ---- _select_model against a fake CDP: the old flat-menu composer ---------------------------

class _FakeFlatMenuClient:
    """The pre-rollout build: opening the switcher shows a flat [role=menuitem] list containing
    'Pro' directly, with no slider and no submenu trigger anywhere. Proves the new slider-first
    path falls through cleanly to the path that used to work alone."""

    def __init__(self):
        self.opened = False
        self.selected = False

    def eval(self, expr):
        if "KeyboardEvent" in expr:                        # _DISMISS_JS
            # These fakes model builds whose Escape genuinely dismisses (verified pre-GPT-6): by
            # the time _close_menus checks, nothing reads aria-expanded any more.
            return 0
        if "aria-valuenow" in expr:                         # no slider in this build
            return None
        if ".focus();return true" in expr:
            return False
        if "pointerover" in expr:
            return False
        if 'aria-haspopup="menu"' in expr:                   # no submenu trigger either
            return 0
        if "menuitemradio" in expr:                          # _click_item_js
            if self.opened:
                self.selected = True
                return True
            return False
        if "var EL=c[" in expr:                               # _open_cand_by_label_js
            cur = "Pro" if self.selected else "Medium"
            if _wanted_label(expr) != cur:
                return None
            self.opened = True
            return cur
        if ".map(function(b){" in expr:                       # _cand_labels_js
            return ["Pro"] if self.selected else ["Medium"]
        return 1                                                # _cand_count_js

    def key(self, key_name, code, keycode):
        pass


def test_old_flat_menu_still_selects_pro(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeFlatMenuClient()
    ok, shown = _pick(client, "Pro")
    assert ok is True and shown == "Pro"


# ---- fail-closed must be side-effect-free -----------------------------------------------
#
# Live bug: composer on High, target "Ultra" (a tier this account never offers) — the slider
# walk saturated left then right to the ceiling, never matched, and gave up leaving the
# composer wherever it stopped (Pro) while reporting confirmed=False. Both halves of the
# caller's own message ("switcher shows X and it could not be changed") were false: it WAS
# changed, and the reported label ('Chat', the mode toggle) wasn't even a tier.

class _FakeUnreachableTargetClient:
    """Reproduces the live layout: two switchers, cand_labels_js() == ['Chat', '<tier>'] — a
    mode toggle first (no slider inside its own menu) and the tier switcher second (the
    slider). Target 'Ultra' never matches anything, on either switcher."""

    LABELS = ["Instant", "Medium", "High", "Extra High", "Pro"]

    def __init__(self, start=2):
        self.now = start          # High
        self.opened_i = None      # which candidate's menu is open: 0=mode toggle, 1=tier
        self.key_presses = 0

    def _label(self):
        return self.LABELS[self.now]

    def eval(self, expr):
        if "KeyboardEvent" in expr:                        # _DISMISS_JS
            # These fakes model builds whose Escape genuinely dismisses (verified pre-GPT-6): by
            # the time _close_menus checks, nothing reads aria-expanded any more.
            return 0
        if "aria-valuenow" in expr:                          # _PICKER_STATE_JS
            if self.opened_i != 1:
                return _NO_PICKER                              # mode-toggle menu has no slider
            return {"first": "%s, %d of 5." % (self._label(), self.now + 1),
                    "fam": _LEGACY_FAM, "now": self.now, "min": 0, "max": 4}
        if ".focus();return true" in expr:                    # _SLIDER_FOCUS_JS
            return self.opened_i == 1
        if "pointerover" in expr:                              # _open_submenu_js
            return False
        if 'aria-haspopup="menu"' in expr:                     # _submenu_count_js
            return 0
        if "menuitemradio" in expr:                            # _click_item_js
            return False                                        # no flat 'Ultra' item anywhere
        if "var EL=c[" in expr:                                 # _open_cand_by_label_js
            cands = ["Chat", self._label()]
            want = _wanted_label(expr)
            if want not in cands:
                return None
            self.opened_i = cands.index(want)
            return want
        if ".map(function(b){" in expr:                         # _cand_labels_js
            return ["Chat", self._label()]
        return 2                                                  # _cand_count_js: 2 switchers

    def key(self, key_name, code, keycode):
        self.key_presses += 1
        if key_name == "Escape":
            self.opened_i = None
            return
        if self.opened_i != 1:
            return                                                # keys land only on the slider
        if key_name == "ArrowLeft":
            self.now = max(0, self.now - 1)
        elif key_name == "ArrowRight":
            self.now = min(4, self.now + 1)


def test_unreachable_target_restores_slider_to_entry_position(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeUnreachableTargetClient(start=2)  # High
    ok, _shown = _pick(client, "Ultra")
    assert ok is False
    assert client.now == 2, (
        "a failed walk must leave the composer on the tier it FOUND, not wherever the "
        "doomed search gave up")


def test_unreachable_target_reports_the_sliders_tier_not_the_mode_toggle(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeUnreachableTargetClient(start=2)  # High
    ok, shown = _pick(client, "Ultra")
    assert ok is False
    assert shown == "High", "must report the tier switcher's real label, not the mode toggle"


# ---- the settle poll must not false-bail on a merely-slow frame -------------------------

class _FakeLaggingSliderClient:
    """The state read right after a key press can lag one poll tick: this fake returns the
    STALE valuenow on the first read following a press and the settled value from the second
    read onward — reproducing the live probe's 0.35-0.45s settle time landing outside a single
    0.2s tick. A single-shot read (the pre-fix design) would misread this as keys not landing
    and bail; the bounded poll must not."""

    LABELS = ["Instant", "Medium", "High", "Extra High", "Pro"]

    def __init__(self, start=2):
        self.now = start
        self.pending = None        # (stale_now, true_now) while a press's read is lagging
        self.reads_since_press = 0
        self.menu_open = False

    def _label(self, now):
        return self.LABELS[now]

    def eval(self, expr):
        if "KeyboardEvent" in expr:                        # _DISMISS_JS
            # These fakes model builds whose Escape genuinely dismisses (verified pre-GPT-6): by
            # the time _close_menus checks, nothing reads aria-expanded any more.
            return 0
        if "aria-valuenow" in expr:
            if not self.menu_open:
                return None
            if self.pending is not None:
                self.reads_since_press += 1
                stale, true = self.pending
                now = stale if self.reads_since_press == 1 else true
                if self.reads_since_press >= 2:
                    self.pending = None
            else:
                now = self.now
            return {"first": "%s, %d of 5." % (self._label(now), now + 1),
                    "fam": _LEGACY_FAM, "now": now, "min": 0, "max": 4}
        if ".focus();return true" in expr:
            return self.menu_open
        if "pointerover" in expr:
            return False
        if 'aria-haspopup="menu"' in expr:
            return 0
        if "menuitemradio" in expr:
            return False
        if "var EL=c[" in expr:                                  # _open_cand_by_label_js
            if _wanted_label(expr) != self._label(self.now):
                return None
            self.menu_open = True
            return self._label(self.now)
        if ".map(function(b){" in expr:
            return [self._label(self.now)]
        return 1

    def key(self, key_name, code, keycode):
        if key_name == "Escape":
            self.menu_open = False
            return
        stale = self.now
        if key_name == "ArrowLeft":
            self.now = max(0, self.now - 1)
        elif key_name == "ArrowRight":
            self.now = min(4, self.now + 1)
        if self.now != stale:
            self.pending = (stale, self.now)
            self.reads_since_press = 0


def test_settle_poll_tolerates_a_lagging_read_without_bailing(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeLaggingSliderClient(start=2)  # High
    ok, shown = _pick(client, "Pro")
    assert ok is True and shown == "Pro", "a one-tick-late read must not be mistaken for stuck keys"


# ---- addressing fix: identity, not index --------------------------------------------------
#
# Commit 601771d's _select_model was verified live only against an ALREADY-OPEN, settled tab.
# The daemon's real path opens a FRESH tab, where the candidate NodeList is rebuilt on every
# Runtime.evaluate and its size FLAPS as the composer re-renders — live-verified 2026-08-28: one
# read saw ['Chat', 'Extra High'], the next (two seconds later) saw only ['Extra High']. The old
# _open_cand_js(i) read labels in one round-trip and opened candidate[i] in a SECOND, separate
# round-trip against a freshly re-queried list — so index i could name a different button than
# the one whose label was read. In the repro this opened the Chat/Agent MODE TOGGLE (whose menu
# has no slider and no tier items) instead of the tier switcher, and the consult refused to send
# with the tier button sitting right there. The tests below pin the fix: candidates are found
# and opened by their OWN label inside a single evaluation (_open_cand_by_label_js), so an
# addressing miss can only ever return None or a mismatched label — never a wrong-but-real
# element acted on as if it were the one asked for.

def test_composer_scoped_switcher_tried_before_document_wide_scan():
    """_FORM_CAND_JS (the composer's own <form>-scoped switcher — the tier control on the build
    probed 2026-08-28, and what the live repro proved actually works) must be concatenated
    AHEAD of what the wide, document-wide _CAND_JS scan adds, so _select_model tries it first."""
    all_js = _CDP._ALL_CAND_JS
    form_marker = "composer-plus-btn"  # unique to _FORM_CAND_JS
    wide_marker = "new chat"           # unique to _CAND_JS's deny-list regex
    assert form_marker in all_js and wide_marker in all_js
    assert all_js.index(form_marker) < all_js.index(wide_marker), (
        "form-scoped candidates must be concatenated first, ahead of the wide scan's additions")


class _FakeAlwaysMismatchedOpenClient:
    """Every open-by-label call resolves to a label OTHER than the one requested — as if the
    composer re-rendered between the labels read and the open landing, or the click simply
    missed. A slider genuinely exists and would satisfy the target if searched — proving
    _select_model never acts on a mismatched open is exactly proving it never gets searched."""

    def __init__(self):
        self.slider_reads = 0

    def eval(self, expr):
        if "KeyboardEvent" in expr:                        # _DISMISS_JS
            # These fakes model builds whose Escape genuinely dismisses (verified pre-GPT-6): by
            # the time _close_menus checks, nothing reads aria-expanded any more.
            return 0
        if "aria-valuenow" in expr:
            self.slider_reads += 1
            return {"first": "Pro, 5 of 5.", "fam": _LEGACY_FAM, "now": 4, "min": 0, "max": 4}
        if ".focus();return true" in expr:
            return True
        if "pointerover" in expr:
            return False
        if 'aria-haspopup="menu"' in expr:
            return 0
        if "menuitemradio" in expr:
            return False
        if "var EL=c[" in expr:                # _open_cand_by_label_js — always the wrong label
            return "Something Else"
        if ".map(function(b){" in expr:
            return ["Extra High"]
        return 1

    def key(self, key_name, code, keycode):
        pass


def test_mismatched_open_is_never_acted_on(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeAlwaysMismatchedOpenClient()
    ok, shown = _pick(client, "Pro")
    assert ok is False
    assert client.slider_reads == 0, "an open that didn't land on the requested label must never be searched"


class _FakeFlappingCandidatesClient:
    """Reproduces the live repro verbatim: the candidate NodeList is rebuilt fresh on EVERY
    evaluation and its size FLAPS between reads — odd opens see ['Chat', '<tier>'] (mode toggle
    plus tier switcher both rendered), even opens see only ['<tier>'] (mode toggle transiently
    unrendered). Confirmed (2026-08-28) to FAIL against the pre-fix index-addressed
    _open_cand_js: with 'candidates' always reported as 2 (a moment-ago, now-stale count),
    index 0 keeps landing on whichever read is the 2-item shape — the Chat/Agent MODE TOGGLE,
    not the tier switcher — so the walk that would reach Pro never starts, on either retry pass.
    The fix (_open_cand_by_label_js) finds the tier switcher by ITS OWN LABEL inside a single
    evaluation, so it is found wherever it actually sits in THAT snapshot, never mistaken for
    the mode toggle."""

    LABELS = ["Instant", "Medium", "High", "Extra High", "Pro"]

    def __init__(self):
        self.now = 3  # Extra High
        self.open_calls = 0
        self.menu_open = False
        self.opened_chat = False

    def _tier_label(self):
        return self.LABELS[self.now]

    def eval(self, expr):
        if "KeyboardEvent" in expr:                        # _DISMISS_JS
            # These fakes model builds whose Escape genuinely dismisses (verified pre-GPT-6): by
            # the time _close_menus checks, nothing reads aria-expanded any more.
            return 0
        if "aria-valuenow" in expr:                          # _PICKER_STATE_JS
            if not self.menu_open or self.opened_chat:
                return _NO_PICKER
            return {"first": "%s, %d of 5." % (self._tier_label(), self.now + 1),
                    "fam": _LEGACY_FAM, "now": self.now, "min": 0, "max": 4}
        if ".focus();return true" in expr:                    # _SLIDER_FOCUS_JS
            return self.menu_open and not self.opened_chat
        if "pointerover" in expr:                              # _open_submenu_js
            return False
        if 'aria-haspopup="menu"' in expr:                     # _submenu_count_js
            return 0
        if "menuitemradio" in expr:                            # _click_item_js
            return False
        if ".length;})()" in expr:                             # _cand_count_js (old code only) —
            return 2                                            # a stale "2 a moment ago" read
        if "var EL=c[" in expr:                                # _open_cand_js(i) / by-label open
            self.open_calls += 1
            state_a = (self.open_calls % 2 == 1)                # flaps every open call
            cands = ["Chat", self._tier_label()] if state_a else [self._tier_label()]
            want = _wanted_label(expr)
            if want is not None:                                # new: identity search
                if want not in cands:
                    return None
                got = want
            else:                                               # old: positional index
                m = _re.search(r"var EL=c\[(\d+)\]", expr)
                i = int(m.group(1)) if m else 0
                if i >= len(cands):
                    return None
                got = cands[i]
            self.opened_chat = (got == "Chat")
            self.menu_open = True
            return got
        if ".map(function(b){" in expr:                         # _cand_labels_js
            return [self._tier_label()]
        return 1

    def key(self, key_name, code, keycode):
        if key_name == "Escape":
            self.menu_open = False
            self.opened_chat = False
            return
        if not self.menu_open or self.opened_chat:
            return
        if key_name == "ArrowLeft":
            self.now = max(0, self.now - 1)
        elif key_name == "ArrowRight":
            self.now = min(4, self.now + 1)


def test_flapping_candidate_list_still_reaches_pro(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeFlappingCandidatesClient()
    ok, shown = _pick(client, "Pro")
    assert ok is True and shown == "Pro"


# ---- the bare-label form (no keyboard-interaction announcement) --------------------------
#
# Live-verified 2026-08-28 on a FRESH tab at the project URL: _slider_set walked the slider
# correctly, landing exactly on Pro (see the ArrowLeft/ArrowRight trace in the fix commit), and
# still reported (False, '') — because the group's first line was the bare word 'Pro', not
# 'Pro, 5 of 5.', and the pre-fix _slider_label discarded any line without a comma.

class _FakeBareLabelSliderClient:
    """Same shape as _FakeSliderClient, except the picker group's first line is the BARE tier
    word with no ', N of M.' accessibility suffix at all — the live fresh-tab shape."""

    LABELS = ["Instant", "Medium", "High", "Extra High", "Pro"]

    def __init__(self, start=2):
        self.now = start
        self.menu_open = False

    def _label(self):
        return self.LABELS[self.now]

    def eval(self, expr):
        if "KeyboardEvent" in expr:                        # _DISMISS_JS
            # These fakes model builds whose Escape genuinely dismisses (verified pre-GPT-6): by
            # the time _close_menus checks, nothing reads aria-expanded any more.
            return 0
        if "aria-valuenow" in expr:                        # _PICKER_STATE_JS
            if not self.menu_open:
                return None
            return {"first": self._label(), "fam": _LEGACY_FAM,
                    "now": self.now, "min": 0, "max": 4}
        if ".focus();return true" in expr:
            return self.menu_open
        if "pointerover" in expr:
            return False
        if 'aria-haspopup="menu"' in expr:
            return 0
        if "menuitemradio" in expr:
            return False
        if "var EL=c[" in expr:                             # _open_cand_by_label_js
            if _wanted_label(expr) != self._label():
                return None
            self.menu_open = True
            return self._label()
        if ".map(function(b){" in expr:                     # _cand_labels_js
            return [self._label()]
        return 1

    def key(self, key_name, code, keycode):
        if key_name == "Escape":
            self.menu_open = False
            return
        if key_name == "ArrowLeft":
            self.now = max(0, self.now - 1)
        elif key_name == "ArrowRight":
            self.now = min(4, self.now + 1)


def test_bare_label_form_still_reaches_pro(monkeypatch):
    """Confirmed (2026-08-28) to FAIL against the pre-fix _slider_label, which returned '' for
    any line without a comma: label stayed '' through the whole walk, _matches() never fired
    even after landing exactly on Pro, and _select_model reported failure with the tier button
    already showing the right value."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeBareLabelSliderClient(start=2)  # High
    ok, shown = _pick(client, "Pro")
    assert ok is True and shown == "Pro"


class _FakeGroupMissingButtonLabelClient:
    """The picker group's own text is unusable (empty first line — group absent, or some third
    shape) but the composer switcher BUTTON's own label is present and tracks the slider
    position live. Proves the state read's fallback source (btnLabel, see _slider_state_label)
    lets the walk proceed even when the group text gives nothing to parse."""

    LABELS = ["Instant", "Medium", "High", "Extra High", "Pro"]

    def __init__(self, start=2):
        self.now = start
        self.menu_open = False

    def _label(self):
        return self.LABELS[self.now]

    def eval(self, expr):
        if "KeyboardEvent" in expr:                        # _DISMISS_JS
            # These fakes model builds whose Escape genuinely dismisses (verified pre-GPT-6): by
            # the time _close_menus checks, nothing reads aria-expanded any more.
            return 0
        if "aria-valuenow" in expr:                        # _PICKER_STATE_JS
            if not self.menu_open:
                return None
            return {"first": "", "btnLabel": self._label(), "fam": _LEGACY_FAM,
                    "now": self.now, "min": 0, "max": 4}
        if ".focus();return true" in expr:
            return self.menu_open
        if "pointerover" in expr:
            return False
        if 'aria-haspopup="menu"' in expr:
            return 0
        if "menuitemradio" in expr:
            return False
        if "var EL=c[" in expr:                             # _open_cand_by_label_js
            if _wanted_label(expr) != self._label():
                return None
            self.menu_open = True
            return self._label()
        if ".map(function(b){" in expr:                     # _cand_labels_js
            return [self._label()]
        return 1

    def key(self, key_name, code, keycode):
        if key_name == "Escape":
            self.menu_open = False
            return
        if key_name == "ArrowLeft":
            self.now = max(0, self.now - 1)
        elif key_name == "ArrowRight":
            self.now = min(4, self.now + 1)


def test_group_text_missing_falls_back_to_button_label(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeGroupMissingButtonLabelClient(start=2)  # High
    ok, shown = _pick(client, "Pro")
    assert ok is True and shown == "Pro"


# ---- the GPT-6 composer (live 2026-09-06) ------------------------------------------------
#
# The previous picker break shipped with a green suite because the FIXTURES encoded the same
# wrong assumption as the code (the tier is the picker group's first line). The fake below is
# therefore transcribed from a DOM dump of the user's own logged-in tab on 2026-09-06 rather
# than from what the code expects to find:
#
#   [data-testid="composer-intelligence-picker-content"] innerText lines:
#     0 "6"                                              <- the MODEL BADGE, where the tier used
#     1 "Pro"                                               to be. This one line is the entire
#     2 "Pro, 5 of 5."                                      regression.
#     3 "Use Left and Right arrow keys to adjust power."
#     4 "Latest"      role=menuitemradio aria-checked=true
#     5 "GPT-5.6 Sol" role=menuitemradio aria-checked=false
#     6 "GPT-5.5"     role=menuitemradio aria-checked=false
#   span[role=slider] aria-valuemin=0 aria-valuemax=4 aria-valuenow=4, textContent "", no
#     aria-valuetext. Its [role=menuitem] ancestor: aria-label="Power", innerText AND textContent
#     both EMPTY, aria-describedby="_r_cn_ _r_co_" -> "Pro, 5 of 5." and the arrow-keys prose.
#   the composer switcher button: "6" while closed, "Thinking effort" while its menu is OPEN.
#   _submenu_count_js() -> 0.
#
# Measured against the pre-fix code, live: _cand_labels_js() -> ["6"], _model_verdict(["6"],
# "Pro") -> (False, '6'), _select_model(c, "Pro") -> (False, '6') in 4.0s. Every consult refused.

class _FakeGpt6PickerClient:
    """The 2026-09-06 GPT-6 composer. `describedby=False` drops the aria-describedby source so the
    ", N of M." line scan (source 2 of _slider_state_label's chain) has to carry the read alone —
    which is also the pre-GPT-6 shape, where that announcement WAS line 0."""

    LABELS = ["Instant", "Medium", "High", "Extra High", "Pro"]
    FAMILIES = ["Latest", "GPT-5.6 Sol", "GPT-5.5"]

    def __init__(self, start=4, checked="Latest", describedby=True, radios=True):
        self.now = start
        self.checked = checked
        self.describedby = describedby
        self.radios = radios
        self.menu_open = False
        self.key_presses = 0
        self.family_clicks = 0

    def _tier(self):
        return self.LABELS[self.now]

    def _announce(self):
        return "%s, %d of 5." % (self._tier(), self.now + 1)

    def _group_lines(self):
        # NOTE the ordering: the badge first, the bare tier second, the announcement THIRD. The
        # tier is not at any fixed index, which is the whole point.
        lines = ["6", self._tier(), self._announce(),
                 "Use Left and Right arrow keys to adjust power."]
        if self.radios:
            lines += list(self.FAMILIES)
        return lines

    def _fam(self):
        if not self.menu_open:
            return {"owners": 0, "menus": 0, "radios": None, "scaffold": 0}
        if not self.radios:
            return dict(_LEGACY_FAM)
        return {"owners": 1, "menus": 1, "scaffold": 1,
                "radios": [{"label": f, "checked": f == self.checked} for f in self.FAMILIES]}

    def eval(self, expr):
        if "KeyboardEvent" in expr:                        # _DISMISS_JS
            # These fakes model builds whose Escape genuinely dismisses (verified pre-GPT-6): by
            # the time _close_menus checks, nothing reads aria-expanded any more.
            return 0
        if "var FAMS=" in expr:                                 # _click_family_js
            if not self.radios or not self.menu_open:
                return False
            want = _json.loads(_re.search(r'var T=(".*?");', expr).group(1))
            for f in self.FAMILIES:
                if f.lower() == want:
                    self.family_clicks += 1
                    self.checked = f
                    return True
            return False
        if "aria-valuenow" in expr:                             # _PICKER_STATE_JS
            if not self.menu_open:
                return _NO_PICKER
            lines = self._group_lines()
            return {"first": lines[0], "lines": lines, "fam": self._fam(),
                    # the Power menuitem's own text is EMPTY on this build; everything the tier
                    # can be read from lives behind aria-describedby.
                    "descs": ([self._announce(),
                               "Use Left and Right arrow keys to adjust power."]
                              if self.describedby else []),
                    "btnLabel": "Thinking effort",              # the OPEN button, not a tier
                    "now": self.now, "min": 0, "max": 4}
        if ".focus();return true" in expr:                      # _SLIDER_FOCUS_JS
            return self.menu_open
        if "pointerover" in expr:                                # _open_submenu_js
            return False
        if 'aria-haspopup="menu"' in expr:                       # _submenu_count_js -> 0 live
            return 0
        if "menuitemradio" in expr:                              # _click_item_js: no flat tier item
            return False
        if "var EL=c[" in expr:                                   # _open_cand_by_label_js
            if _wanted_label(expr) != "6":
                return None
            self.menu_open = True
            return "6"
        if ".map(function(b){" in expr:                           # _cand_labels_js
            return ["Thinking effort"] if self.menu_open else ["6"]
        return 1

    def key(self, key_name, code, keycode):
        self.key_presses += 1
        if key_name == "Escape":
            self.menu_open = False
            return
        if not self.menu_open:
            return
        if key_name == "ArrowLeft":
            self.now = max(0, self.now - 1)
        elif key_name == "ArrowRight":
            self.now = min(4, self.now + 1)


def test_gpt6_picker_confirms_pro_where_it_already_sits(monkeypatch):
    """The live tab verbatim: slider at 4, family Latest. Pre-fix this returned (False, '6') and
    refused to send; it must confirm without touching anything."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeGpt6PickerClient(start=4, checked="Latest")
    pick = _CDP._select_model(client, "Pro", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Pro")
    assert client.now == 4
    assert pick.badge == "6", "the receipt must record WHICH MODEL answered, not just the tier"


def test_gpt6_picker_walks_the_slider_to_pro(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeGpt6PickerClient(start=2, checked="Latest")   # High
    pick = _CDP._select_model(client, "Pro", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Pro")
    assert client.now == 4


def test_gpt6_tier_survives_a_missing_describedby(monkeypatch):
    """Source 1 of the chain gone: the tier is then only in the group's ', N of M.' line — which
    on this build is line 2, not line 0, so it has to be FOUND by shape, not read by index."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeGpt6PickerClient(start=2, checked="Latest", describedby=False)
    pick = _CDP._select_model(client, "Pro", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Pro")


def test_gpt6_family_already_checked_is_not_clicked(monkeypatch):
    """A checked Radix radio must be left alone: clicking one is not a guaranteed no-op (it
    commits the menu closed), and there is nothing to change."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeGpt6PickerClient(start=4, checked="Latest")
    pick = _CDP._select_model(client, "Pro", "Latest")
    assert pick.confirmed is True
    assert client.family_clicks == 0, "no gesture may be dispatched at an already-correct family"


def test_gpt6_family_is_switched_off_gpt55_and_confirmed(monkeypatch):
    """The hole this closes: 'Pro tier on GPT-5.5' passes every tier check while being a
    different model than the receipt names."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeGpt6PickerClient(start=4, checked="GPT-5.5")
    pick = _CDP._select_model(client, "Pro", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Pro")
    assert client.family_clicks == 1
    assert client.checked == "Latest", "the click must be confirmed by re-reading aria-checked"


def test_gpt6_missing_family_fails_closed_and_names_what_is_offered(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeGpt6PickerClient(start=4, checked="Latest")
    pick = _CDP._select_model(client, "Pro", "GPT-7")
    assert pick.confirmed is False, "an unavailable family must refuse, exactly like a tier miss"
    assert pick.error and "GPT-7" in pick.error
    for offered in _FakeGpt6PickerClient.FAMILIES:
        assert offered in pick.error, "the error must name the families the account actually has"
    assert client.family_clicks == 0
    assert client.now == 4, "a family refusal must not leave the tier moved"


def test_gpt6_unreachable_tier_restores_the_slider(monkeypatch):
    """The side-effect-free property, on the new DOM: a target this account does not have must
    leave the slider exactly where the walk found it, never at whatever tier it gave up on."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeGpt6PickerClient(start=3, checked="Latest")   # Extra High
    pick = _CDP._select_model(client, "Ultra", "Latest")
    assert pick.confirmed is False
    assert client.now == 3, "a doomed walk must restore the entry position"
    assert pick.shown == "Extra High", "and report the tier the composer is actually left on"


def test_old_build_without_model_radios_ignores_the_family(monkeypatch):
    """A pre-GPT-6 account has no radio group at all. Enforcing a family there must be a SILENT
    no-op — never a refusal over a control that build does not render."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeSliderClient(start=2, keys_work=True)          # the 2026-08-18 build
    pick = _CDP._select_model(client, "Pro", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Pro")
    assert pick.error is None


def test_family_unverifiable_does_not_confirm(monkeypatch):
    """A composer already sitting on the target tier whose picker never opens must NOT report
    success while enforcing a family: the radios are only readable inside an open menu, so
    "the tier looks right" is not evidence about the model. Fail-open in the middle of a
    fail-closed guard is the whole bug class this change exists to remove."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeAlwaysMismatchedOpenClient()          # every open lands on the wrong label
    client.eval = _never_opens(client)
    pick = _CDP._select_model(client, "Extra High", "Latest")
    assert pick.confirmed is False
    assert pick.error and "Latest" in pick.error
    assert client.slider_reads == 0


def _never_opens(client):
    """`_FakeAlwaysMismatchedOpenClient` with its switcher label made to equal the target tier, so
    the tier verdict passes on the very first read while every open still misses."""
    inner = client.eval

    def eval_(expr):
        if ".map(function(b){" in expr:
            return ["Extra High"]
        if "var FAMS=" in expr:
            return []
        return inner(expr)
    return eval_


def test_family_skip_disables_the_check(monkeypatch):
    """`CGC_MODEL_FAMILY=skip` mirrors `CGC_MODEL=skip`: the dimension is simply not enforced."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeGpt6PickerClient(start=4, checked="GPT-5.5")
    pick = _CDP._select_model(client, "Pro", "skip")
    assert pick.confirmed is True
    assert client.checked == "GPT-5.5" and client.family_clicks == 0


if __name__ == "__main__":
    test_slider_label_pro()
    test_slider_label_extra_high()
    test_slider_label_bare_pro()
    test_slider_label_bare_extra_high()
    test_slider_label_long_prose_is_not_a_label()
    test_slider_label_empty_string()
    print("OK")
