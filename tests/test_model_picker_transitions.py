#!/usr/bin/env python3
# Created: 2026-09-06
# Last audited: 2026-09-06
# Authority basis: an adversarial review of 2b7c8ac, which found four traces on which the
# model-selection guard fails OPEN — the exact bug class the guard exists to prevent. Each test
# below is one of those traces.
"""
Offline tests for the model-selection guard's TRANSITIONS, not its snapshots.

Why a second fixture family. The fakes in test_model_picker.py are DOM snapshots: a family switch
changes the checked value without closing the menu, without rebuilding the slider, and without
moving the badge; labels and positions update synchronously; nothing ever drifts. Those are
precisely the transitions the guard is judged on, so a suite built only on them cannot see a
fail-open that lives in a transition — and did not: 2b7c8ac shipped green.

`_Composer` below is therefore a state machine. Selecting a family can close and rebuild the menu,
reset the slider, and rename the switcher button (the GPT-6 badge IS the model). Closing a menu can
revert a tier. A tier walk can drift the family. A slider can alternate instead of moving. The
guard is driven against it exactly as it drives a real tab, through the same generated JS.

Run: python3 -m pytest tests/test_model_picker_transitions.py -q
"""
import importlib.util as _ilu
import json as _json
import os as _os
import re as _re

import pytest

_REPO = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_CDP_PATH = _os.path.join(_REPO, "skill", "scripts", "cdp_consult.py")
_spec = _ilu.spec_from_file_location("cdp_consult_transitions", _CDP_PATH)
_CDP = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_CDP)

_WANT_RE = _re.compile(r'var want=(".*?");')
_T_RE = _re.compile(r'var T=(".*?");')


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Every wait in the picker is a settle allowance, never a synchronisation primitive: the
    fixture's transitions are all synchronous with the gesture that causes them."""
    monkeypatch.setattr(_CDP.time, "sleep", lambda n: None)


class _Composer:
    """A ChatGPT composer as a state machine.

    Knobs, each transcribing one thing a real build was observed (or reviewed) to do:

      schema                 "gpt6"   the picker owns a model radio group (live 2026-09-06)
                             "legacy" the picker exists and renders NO family control (2026-08-18)
                             "flat"   no picker at all; the tier is a plain menuitem (pre-rollout)
      probe                  "ok" | "fam_null" (the radio group reads back null while the slider
                             reads fine — the reviewer's defect-1 trace) | "malformed" (an item
                             with no label) | "ambiguous" (two pickers open at once)
      badge_mode             "family" the switcher shows the model badge (GPT-6) — closed, it is
                             "6"/"5.6"/"5.5"; "tier" the pre-GPT-6 switcher, which shows the tier
      commit_closes_menu     a Radix radio click commits AND closes the menu
      rebuild_tier_on_family a family change rebuilds the slider at this position
      flip_family_on_walk    the first arrow press drifts the family to this one
      revert_tier_on_close   closing the picker reverts the tier to this position
      oscillate              an arrow press alternates the position instead of stepping it
      unreadable_after       after this many arrow presses the picker stops reading at all
      mode_toggle            a Chat/Agent switcher sits AHEAD of the picker in the candidate list
      picker_unopenable      the picker's own switcher never opens (React re-render, missed click)
      mounted_when_closed    the picker node stays MOUNTED and readable after dismissal
                             (data-state="closed"), which is what Radix actually does here
      escape_dismisses       whether a CDP Input.dispatchKeyEvent Escape closes the menu. FALSE by
                             default, because that is what the live 2026-09-06 build does: measured
                             on a logged-in Pro tab, the trigger stayed aria-expanded="true" across
                             two CDP Escapes and only a KeyboardEvent dispatched at the focused
                             dismissable layer closed it. A menu that never closes makes every
                             "resting composer" read describe a transient, which is precisely what
                             the removed `if not ok and slid_ok: ok = True` line used to hide.
    """

    TIERS = ["Instant", "Medium", "High", "Extra High", "Pro"]
    FAMILIES = ["Latest", "GPT-5.6 Sol", "GPT-5.5"]
    BADGE = {"Latest": "6", "GPT-5.6 Sol": "5.6", "GPT-5.5": "5.5"}

    def __init__(self, tier=4, family="Latest", schema="gpt6", probe="ok",
                 badge_mode="family", commit_closes_menu=False, rebuild_tier_on_family=None,
                 flip_family_on_walk=None, revert_tier_on_close=None, oscillate=False,
                 unreadable_after=None, mode_toggle=False, picker_unopenable=False,
                 escape_dismisses=False, mounted_when_closed=False):
        self.tier = tier
        self.family = family
        self.schema = schema
        self.probe = probe
        self.badge_mode = badge_mode
        self.commit_closes_menu = commit_closes_menu
        self.rebuild_tier_on_family = rebuild_tier_on_family
        self.flip_family_on_walk = flip_family_on_walk
        self.revert_tier_on_close = revert_tier_on_close
        self.oscillate = oscillate
        self.unreadable_after = unreadable_after
        self.mode_toggle = mode_toggle
        self.picker_unopenable = picker_unopenable
        self.escape_dismisses = escape_dismisses
        self.mounted_when_closed = mounted_when_closed
        self.menu = None            # None | "picker" | "mode" | "flat"
        self.presses = 0            # ARROW presses only (Escapes are not slider operations)
        self.escapes = 0
        self.family_clicks = 0
        self.rebuilds = 0
        self.walked = False

    # ---- what the composer's own switcher reads, resting and open ----
    def _switcher(self):
        if self.menu == "picker":
            return "Thinking effort"     # live 2026-09-06: the OPEN button is neither model nor tier
        if self.badge_mode == "family":
            return self.BADGE[self.family]
        return self.TIERS[self.tier]

    def _labels(self):
        return (["Chat"] if self.mode_toggle else []) + [self._switcher()]

    # ---- the picker snapshot, honouring the schema and the probe knob ----
    def _readable(self):
        """Whether the picker's contents can be read right now. Radix leaves the node mounted with
        data-state="closed" on the live build, so "closed" and "unreadable" are not the same fact."""
        return self.schema != "flat" and (self.menu == "picker" or self.mounted_when_closed)

    def _fam_block(self):
        if not self._readable():
            return {"owners": 0, "menus": 1 if self.menu else 0, "radios": None, "scaffold": 0}
        if self.probe == "ambiguous":
            return {"owners": 2, "menus": 2, "radios": None, "scaffold": 0}
        if self.schema == "legacy":
            return {"owners": 1, "menus": 1, "radios": [], "scaffold": 0}
        if self.probe == "fam_null":
            return {"owners": 1, "menus": 1, "radios": None, "scaffold": 0}
        if self.probe == "malformed":
            return {"owners": 1, "menus": 1, "scaffold": 1,
                    "radios": [{"label": "", "checked": True}]}
        return {"owners": 1, "menus": 1, "scaffold": 1,
                "radios": [{"label": f, "checked": f == self.family} for f in self.FAMILIES]}

    def _picker_state(self):
        if self.unreadable_after is not None and self.presses >= self.unreadable_after:
            return None                                   # the control stopped answering entirely
        fam = self._fam_block()
        blank = {"fam": fam, "first": "", "lines": [], "descs": [], "btnLabel": "",
                 "now": None, "min": None, "max": None}
        if not self._readable():
            return blank
        tier = self.TIERS[self.tier]
        lines = ([self.BADGE[self.family]] if self.schema == "gpt6" else []) + [
            tier, "%s, %d of 5." % (tier, self.tier + 1),
            "Use Left and Right arrow keys to adjust power."]
        return {"fam": fam, "first": lines[0], "lines": lines,
                "descs": ["%s, %d of 5." % (tier, self.tier + 1)],
                "btnLabel": "Thinking effort",
                "now": self.tier, "min": 0, "max": 4}

    # ---- transitions ----
    def _open(self, want):
        if self.mode_toggle and want == "Chat":
            self.menu = "mode"
            return "Chat"
        if want != self._switcher() or self.picker_unopenable:
            return None
        self.menu = "flat" if self.schema == "flat" else "picker"
        return want

    def _select_family(self, want):
        for f in self.FAMILIES:
            if f.lower() == want:
                self.family_clicks += 1
                self.family = f
                if self.rebuild_tier_on_family is not None:
                    self.tier = self.rebuild_tier_on_family
                    self.rebuilds += 1
                if self.commit_closes_menu:
                    self.menu = None
                return True
        return False

    def eval(self, expr):
        if "KeyboardEvent" in expr:                          # _DISMISS_JS
            self._dismiss()
            return 0 if self.menu is None else 1
        if "var FAMS=" in expr:                              # _click_family_js
            if self.schema != "gpt6" or self.menu != "picker" or self.probe != "ok":
                return False
            return self._select_family(_json.loads(_T_RE.search(expr).group(1)))
        if "aria-valuenow" in expr:                          # _PICKER_STATE_JS
            return self._picker_state()
        if ".focus();return true" in expr:                   # _SLIDER_FOCUS_JS
            return self.menu == "picker" and self.schema != "flat"
        if "pointerover" in expr:                            # _open_submenu_js
            return False
        if 'aria-haspopup="menu"' in expr:                   # _submenu_count_js
            return 0
        if "menuitemradio" in expr:                          # _click_item_js (the flat tier item)
            if self.menu != "flat":
                return False
            want = _json.loads(_T_RE.search(expr).group(1))
            for i, t in enumerate(self.TIERS):
                if t.lower() == want or (want.startswith("pro") and t.lower().startswith("pro")):
                    self.tier = i
                    self.menu = None
                    return True
            return False
        if "var EL=c[" in expr:                              # _open_cand_by_label_js
            return self._open(_json.loads(_WANT_RE.search(expr).group(1)))
        if ".map(function(b){" in expr:                      # _cand_labels_js
            return self._labels()
        return 1

    def _dismiss(self):
        if self.menu == "picker" and self.revert_tier_on_close is not None:
            self.tier = self.revert_tier_on_close
        self.menu = None

    def key(self, key_name, code, keycode):
        if key_name == "Escape":
            self.escapes += 1
            if self.escape_dismisses:
                self._dismiss()
            return
        if self.menu != "picker" or self.schema == "flat":
            return
        self.presses += 1
        if self.flip_family_on_walk and not self.walked:
            self.family = self.flip_family_on_walk
        self.walked = True
        if self.oscillate:
            self.tier = 2 if self.tier != 2 else 0
            return
        if key_name == "ArrowLeft":
            self.tier = max(0, self.tier - 1)
        elif key_name == "ArrowRight":
            self.tier = min(4, self.tier + 1)


# ---- defect 1: "unreadable means legacy" was a fail-open -----------------------------------
#
# _family_entries turned a null, non-list or malformed read into [], and [] meant "this build has
# no family control — silent exemption". The two clients below differ in exactly one thing: whether
# the picker's radio group READ BACK. They must not reach the same verdict.

def test_legacy_picker_without_a_family_control_is_exempt():
    """A picker that positively renders no radio group at all is the pre-GPT-6 composer. Refusing
    an old account over a control its ChatGPT does not render would be the opposite outage."""
    c = _Composer(tier=2, schema="legacy", badge_mode="tier")
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Pro")
    assert pick.error is None and pick.dirty is False
    assert c.tier == 4 and c.menu is None


def test_unreadable_family_radios_refuse_instead_of_taking_the_exemption():
    """The reviewer's trace: the composer is on GPT-5.5, the family probe reads back null during a
    React render transition, and the Pro slider reads fine. The old code exempted the family and
    reported the model confirmed — a fail-open in the middle of a fail-closed guard. Nothing may be
    pinned behind a model control that did not read."""
    c = _Composer(tier=4, family="GPT-5.5", probe="fam_null")
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert pick.confirmed is False
    assert pick.error and "did not read back" in pick.error
    assert (c.family, c.tier) == ("GPT-5.5", 4), "a refusal must not have driven anything"
    assert c.family_clicks == 0 and c.presses == 0 and c.menu is None


def test_malformed_family_radios_refuse():
    """A radio with no label is a half-rendered group, not a build without one."""
    c = _Composer(tier=4, family="GPT-5.5", probe="malformed")
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert pick.confirmed is False
    assert pick.error and "malformed" in pick.error
    assert (c.family, c.tier) == ("GPT-5.5", 4) and c.menu is None


def test_two_open_pickers_refuse_rather_than_scan_both():
    """The family used to be scanned out of EVERY open menu's radios globally. With no unique
    owning picker there is no coherent group to read, and 'some radio somewhere is checked' is not
    a statement about this composer."""
    c = _Composer(tier=4, family="GPT-5.5", probe="ambiguous")
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert pick.confirmed is False
    assert pick.error and "uniquely owns" in pick.error
    assert (c.family, c.tier) == ("GPT-5.5", 4) and c.menu is None


def test_unrelated_menu_without_radios_does_not_satisfy_family_inspection():
    """The composer's own switcher never opens; only the Chat/Agent MODE TOGGLE does, and its menu
    contains no radios. The old bookkeeping counted that as "the family dimension was inspected",
    so a composer whose resting label already showed the target tier returned confirmed=True with
    the model never established — on a tab actually sitting on GPT-5.5."""
    c = _Composer(tier=4, family="GPT-5.5", badge_mode="tier",
                  mode_toggle=True, picker_unopenable=True)
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert pick.confirmed is False, "an unrelated menu is not the picker, inspected"
    assert pick.error and "Latest" in pick.error
    assert (c.family, c.tier) == ("GPT-5.5", 4) and c.menu is None


def test_flat_menu_build_still_exempts_the_family_it_cannot_express():
    """The pre-rollout composer serves the tier as a plain menuitem and renders no intelligence
    picker at all. That build predates the family control entirely, so enforcing a family on it
    must stay a silent no-op — the exemption is earned by the tier arriving through the flat path,
    never by a menu merely having no radios in it."""
    c = _Composer(tier=1, schema="flat", badge_mode="tier")
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Pro")
    assert pick.error is None and c.tier == 4 and c.menu is None


# ---- defect 2: an intermediate slider match overrode a final contradiction -----------------

def test_a_tier_that_reverts_when_the_menu_closes_is_vetoed():
    """The trace: the slider announces Pro mid-walk, and closing the menu reverts the control to
    High. `if not ok and slid_ok: ok = True` could not tell that apart from "the resting button
    carries no tier on this schema, as expected", so it passed on an observation that was no longer
    true. The fix is not to demand the closed button read Pro — that requirement is what took the
    tool down when the badge replaced the tier on it — but to re-read the CONTROLS after they
    settle. An explicit contradiction is a veto."""
    c = _Composer(tier=2, revert_tier_on_close=2)
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert pick.confirmed is False
    assert pick.error and "'High'" in pick.error and "not 'Pro'" in pick.error
    assert c.tier == 2, "and the composer is left on the tier it was actually found on"
    assert c.menu is None and pick.dirty is False


def test_a_family_that_drifts_during_the_tier_walk_is_caught_by_the_joint_confirmation():
    """The family was pinned first and then never revalidated, so a tier walk that reset the model
    passed on two observations that were never simultaneously true. The confirming read takes the
    tuple TOGETHER, after the commit — and here it reads (GPT-5.5, Pro) where (Latest, Pro) was
    promised."""
    c = _Composer(tier=2, family="Latest", flip_family_on_walk="GPT-5.5")
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert pick.confirmed is False
    assert pick.error and "GPT-5.5" in pick.error and "Latest" in pick.error
    assert (c.family, c.tier) == ("Latest", 2), "the vetoed attempt is rolled back, both halves"
    assert c.menu is None and pick.dirty is False


def test_a_correct_pin_still_confirms_through_the_joint_read():
    """The veto must not become the new outage: the ordinary GPT-6 walk still confirms, and the
    receipt still records the badge that answered."""
    c = _Composer(tier=2, family="Latest")
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert (pick.confirmed, pick.shown, pick.badge) == (True, "Pro", "6")
    assert (c.family, c.tier) == ("Latest", 4) and c.menu is None and pick.dirty is False


# ---- defect 3: failure was not transactional ----------------------------------------------

def test_family_selection_that_closes_and_rebuilds_the_menu_is_still_verified():
    """Clicking an unchecked Radix radio commits AND closes the menu. The confirming re-read did
    not reopen it, saw no radios, and reported the click had failed — AFTER the mutation had
    actually landed. The run then refused while leaving the composer on the new family. Reopening
    has to survive the switcher being RENAMED by that very commit: the GPT-6 badge is the model, so
    it read '5.5' before the click and '6' after."""
    c = _Composer(tier=4, family="GPT-5.5", commit_closes_menu=True, rebuild_tier_on_family=0)
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Pro")
    assert c.rebuilds >= 1, "the fixture must actually have rebuilt the menu"
    assert (c.family, c.tier) == ("Latest", 4) and c.menu is None
    assert pick.badge == "6" and pick.dirty is False


def test_family_success_then_tier_failure_restores_the_original_family_and_tier():
    """The reviewer's trace: the composer starts on GPT-5.5 and a non-target tier, the caller asks
    for Latest and a tier this account does not have. The family switch succeeds; the tier walk
    fails and restores only ITS OWN entry position — captured AFTER the family change, so on a
    build where that change rebuilt the slider it restores a position the composer was never on.
    The call returned False with the composer left on Latest. Both halves go back, family first."""
    c = _Composer(tier=2, family="GPT-5.5", commit_closes_menu=True, rebuild_tier_on_family=0)
    pick = _CDP._select_model(c, "Ultra", "Latest")
    assert pick.confirmed is False
    assert c.family == "GPT-5.5", "a failed run must not leave the composer on the target family"
    assert c.tier == 2, "nor on the position the family change rebuilt the slider at"
    assert pick.dirty is False and c.menu is None


def test_an_unavailable_family_refuses_without_touching_the_tier():
    c = _Composer(tier=3, family="GPT-5.5")
    pick = _CDP._select_model(c, "Pro", "GPT-7")
    assert pick.confirmed is False
    assert pick.error and "GPT-7" in pick.error and "GPT-5.5" in pick.error
    assert (c.family, c.tier) == ("GPT-5.5", 3)
    assert c.family_clicks == 0 and c.presses == 0 and c.menu is None


# ---- defect 4: restoration was not actually bounded ---------------------------------------

def test_oscillating_restoration_terminates_and_reports_dirty():
    """`_restore` looped until it reached the entry position, stopping only on an unreadable read
    or an unchanged one. A control alternating 0 -> 2 -> 0 -> 2 around an entry of 1 satisfies
    neither, so nothing but the subprocess timeout ended it — a process being killed, not an
    algorithm terminating. Monotonic-progress-in-range now ends it on the first non-approaching
    observation, and an unproven restore is reported as dirty rather than as a restore."""
    c = _Composer(tier=1, oscillate=True)
    pick = _CDP._select_model(c, "Ultra", "Latest")
    assert pick.confirmed is False
    assert pick.dirty is True, "an unproven restoration is a dirty composer, not a clean one"
    assert c.presses < 40, "the walk must be bounded by construction: %d presses" % c.presses
    assert pick.shown == "6", (
        "with the restoration unproven there is no observed tier to report, so the verdict falls "
        "back to the composer's own badge — never to a remembered entry label")


def test_unreadable_final_restoration_state_reports_unknown_not_history():
    """`_restore` returned `entry_label` when the final read failed: historical evidence presented
    as an observation of what is currently selected. The composer is genuinely somewhere unknown
    here, and the verdict must say so."""
    c = _Composer(tier=2, unreadable_after=3)
    pick = _CDP._select_model(c, "Ultra", "Latest")
    assert pick.confirmed is False
    assert pick.dirty is True
    assert pick.shown != "High", "the entry label must not be quoted as the current state"
    assert c.presses <= 6, "an unreadable control ends the walk, it does not extend it"


def test_a_reachable_tier_still_walks_and_is_not_reported_dirty():
    """The bounds must not make an ordinary walk look like a failure."""
    c = _Composer(tier=0, family="Latest")
    pick = _CDP._select_model(c, "Extra High", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Extra High")
    assert c.tier == 3 and pick.dirty is False and c.menu is None


# ---- what the live tab taught, 2026-09-06 -------------------------------------------------
#
# Three facts measured on the user's own logged-in Pro tab while verifying the four fixes above.
# Each one breaks a piece of the guard that reads correct on paper, and each one was invisible to a
# snapshot fixture because it only exists in a transition.

def test_a_menu_that_ignores_the_cdp_escape_is_still_closed_before_the_verdict():
    """Measured live: Input.dispatchKeyEvent Escape did NOT dismiss this build's picker (the
    trigger stayed aria-expanded="true" across two of them), and a KeyboardEvent dispatched at the
    focused dismissable layer closed it at once. Every fixture in this file defaults to that
    behaviour. The verdict is taken from the RESTING composer, whose switcher reads "Thinking
    effort" while the menu is open — so a guard that cannot actually close the menu judges the
    composer by a string that is neither a model nor a tier."""
    c = _Composer(tier=2, family="Latest", escape_dismisses=False)
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert (pick.confirmed, pick.shown, pick.badge) == (True, "Pro", "6")
    assert c.menu is None, "the composer must actually be resting when the verdict is taken"


def test_dismissal_is_measured_composer_scoped_not_page_wide():
    """A resting ChatGPT page already carries four button[aria-expanded="true"] (sidebar and
    account disclosures, counted live). Measuring dismissal page-wide never reads zero, so the
    dismissal loop can never observe its own success."""
    assert "querySelector('form')" in _CDP._DISMISS_JS, (
        "the expanded-trigger count must be scoped to the composer's form, like every other read "
        "in this section")


def test_the_open_echo_is_captured_before_the_gesture():
    """This build's switcher renames itself on open — "6" closed, "Thinking effort" open — and
    React had already committed that rename by the time a post-gesture read ran inside the same
    evaluation. The echo then disagreed with the label asked for, the caller discarded its own
    successful open as an addressing miss, and the run refused with "the composer's picker menu
    never opened" while the picker stood open. Measured live before the echo was moved."""
    js = _CDP._open_cand_by_label_js("6")
    assert js.index("var got=") < js.index("pointerdown"), (
        "the identity echo must be read before the gesture, not after it")


def test_a_picker_left_mounted_after_dismissal_is_read_where_it_stands():
    """Radix leaves this picker mounted (data-state="closed") with its contents still tracking the
    real state. When the authoritative controls can be read without touching anything, reopening
    them is not a stronger observation, only a more disruptive one — and reopening is where the
    flake lives."""
    c = _Composer(tier=2, family="GPT-5.5", mounted_when_closed=True)
    pick = _CDP._select_model(c, "Pro", "Latest")
    assert (pick.confirmed, pick.shown) == (True, "Pro")
    assert (c.family, c.tier) == ("Latest", 4) and c.menu is None
