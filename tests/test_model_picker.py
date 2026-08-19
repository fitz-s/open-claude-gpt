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
import os as _os

_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_CDP_PATH = _os.path.join(_REPO, "skill", "scripts", "cdp_consult.py")
_spec = _ilu.spec_from_file_location("cdp_consult", _CDP_PATH)
_CDP = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_CDP)


# ---- _slider_label: pure parse of the slider group's first line -----------------------------

def test_slider_label_pro():
    assert _CDP._slider_label("Pro, 5 of 5.") == "Pro"


def test_slider_label_extra_high():
    assert _CDP._slider_label("Extra High, 4 of 5.") == "Extra High"


def test_slider_label_no_comma_is_not_this_format():
    """A line with no comma is not the slider group's shape at all — there is nothing to
    parse a tier out of, so this must not guess."""
    assert _CDP._slider_label("Advanced") == ""


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
        if "aria-valuenow" in expr:                      # _SLIDER_STATE_JS
            if not self.menu_open:
                return None
            return {"first": "%s, %d of 5." % (self._label(), self.now + 1),
                    "now": self.now, "min": 0, "max": 4}
        if ".focus();return true" in expr:                # _SLIDER_FOCUS_JS
            return self.menu_open
        if "pointerover" in expr:                          # _open_submenu_js — none in this build
            return False
        if 'aria-haspopup="menu"' in expr:                 # _submenu_count_js
            return 0
        if "menuitemradio" in expr:                        # _click_item_js — no flat item here
            return False
        if "var EL=c[" in expr:                             # _open_cand_js
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
    ok, shown = _CDP._select_model(client, "Pro")
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
    ok, shown = _CDP._select_model(client, "Pro")
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
        if "var EL=c[" in expr:                               # _open_cand_js
            self.opened = True
            return "Medium"
        if ".map(function(b){" in expr:                       # _cand_labels_js
            return ["Pro"] if self.selected else ["Medium"]
        return 1                                                # _cand_count_js

    def key(self, key_name, code, keycode):
        pass


def test_old_flat_menu_still_selects_pro(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeFlatMenuClient()
    ok, shown = _CDP._select_model(client, "Pro")
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
        if "aria-valuenow" in expr:                          # _SLIDER_STATE_JS
            if self.opened_i != 1:
                return None                                    # mode-toggle menu has no slider
            return {"first": "%s, %d of 5." % (self._label(), self.now + 1),
                    "now": self.now, "min": 0, "max": 4}
        if ".focus();return true" in expr:                    # _SLIDER_FOCUS_JS
            return self.opened_i == 1
        if "pointerover" in expr:                              # _open_submenu_js
            return False
        if 'aria-haspopup="menu"' in expr:                     # _submenu_count_js
            return 0
        if "menuitemradio" in expr:                            # _click_item_js
            return False                                        # no flat 'Ultra' item anywhere
        if "var EL=c[" in expr:                                 # _open_cand_js(i)
            import re
            m = re.search(r"var EL=c\[(\d+)\]", expr)
            i = int(m.group(1)) if m else 0
            self.opened_i = i
            return ["Chat", self._label()][i] if i < 2 else None
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
    ok, _shown = _CDP._select_model(client, "Ultra")
    assert ok is False
    assert client.now == 2, (
        "a failed walk must leave the composer on the tier it FOUND, not wherever the "
        "doomed search gave up")


def test_unreachable_target_reports_the_sliders_tier_not_the_mode_toggle(monkeypatch):
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)
    client = _FakeUnreachableTargetClient(start=2)  # High
    ok, shown = _CDP._select_model(client, "Ultra")
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
                    "now": now, "min": 0, "max": 4}
        if ".focus();return true" in expr:
            return self.menu_open
        if "pointerover" in expr:
            return False
        if 'aria-haspopup="menu"' in expr:
            return 0
        if "menuitemradio" in expr:
            return False
        if "var EL=c[" in expr:
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
    ok, shown = _CDP._select_model(client, "Pro")
    assert ok is True and shown == "Pro", "a one-tick-late read must not be mistaken for stuck keys"


if __name__ == "__main__":
    test_slider_label_pro()
    test_slider_label_extra_high()
    test_slider_label_no_comma_is_not_this_format()
    test_slider_label_empty_string()
    print("OK")
