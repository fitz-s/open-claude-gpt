#!/usr/bin/env python3
"""The no-send UI-contract probe: is ChatGPT's page still the one the driver reads?

ChatGPT's web UI changed four times in three weeks, and each change was found one round at a time;
a reader break was found by spending a message and then waiting up to 90 minutes for nothing. This
checks the same contract before a message is spent, through the driver's own helpers (no selector is
written twice), and records a verdict per capability in the store (cgc_store.CAPABILITIES):

  composer  a fresh project tab: composer present, the configured model family + tier selectable,
            a >= 15 KB draft pasted and read back whole, then cleared.
  mention   the first CGC_APPS app inserted from the @ popup and its mention chip confirmed.
  reader    a finished conversation opened read-only: its source turn identified by its rid, the
            send contract's landed check passing on it, and its wrapped answer extracted.

IT CANNOT SEND. Every step runs through _NoSend, a tab handle that refuses the send button's JS, any
Enter, and every Input.* CDP call; the module never names the send helpers. The test suite runs the
probes with those helpers rigged to fail.

Each probe opens its OWN background tab and closes only that tab. A failure that says nothing about
the page (Chrome down, attach error, timeout, login) is retried once and then reported inconclusive,
leaving the recorded state alone. Only a deterministic contract mismatch marks a capability broken.

  cgc probe                    run every probe now, then print the capability states
  cgc probe --only reader      one probe (mention also runs the composer probe it needs)
  cgc probe --status           print the recorded states; no browser
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cdp_consult as cdp  # noqa: E402 — the driver's helpers, reused; never its send path
import cgc_backend  # noqa: E402
import cgc_spool as spool  # noqa: E402
import cgc_store as store_mod  # noqa: E402

DUMMY = "".join(f"cgc no-send composer probe, line {i:03d}: this draft is cleared and never sent.\n"
                for i in range(220))       # ~17 KB, past the length at which long pastes broke
LONG_PROMPT = 12_000      # a marker round at least this long exercises the collapsed-turn read
LEGACY_MAX = 6_000        # a legacy (no marker) turn this short is not collapsed (20.7 KB kept 10 KB)
RENDER_S = 30             # how long a loaded conversation may take to render its first user turn
ANSWER_S = 20             # how long its answer may take to mount after a force-render

# What the send path's JS and keys look like. A probe step that would evaluate one is refused.
_SEND_JS = re.compile(r"send-button|\bEnter\b|requestSubmit|\.submit\(")


class SendRefused(AssertionError):
    """The probe reached for the send path. A bug, never a verdict."""


class Transient(Exception):
    """Nothing about the page's contract is known: Chrome, the network, a login, a load."""


class Drift(Exception):
    """A deterministic contract mismatch: the page loaded and does not read the way the driver reads."""

    def __init__(self, cap, phase, cause):
        super().__init__(f"{cap}/{phase}: {cause}")
        self.cap, self.phase, self.cause = cap, phase, cause


class _NoSend:
    """The probe's only handle on its tab. The model picker clicks menu rows and presses arrows and
    Escape, the mention picker clicks a popup row; none of those can send. What can is refused."""

    def __init__(self, c):
        self._c = c

    def eval(self, js, timeout=None):
        if _SEND_JS.search(js or ""):
            raise SendRefused("probe evaluated send-path JS")
        return self._c.eval(js, timeout=timeout)

    def key(self, key_name, code, keycode):
        if key_name == "Enter" or keycode == 13:
            raise SendRefused("probe pressed Enter")
        return self._c.key(key_name, code, keycode)

    def call(self, method, params=None, timeout=None):
        if method.startswith("Input."):
            raise SendRefused(f"probe dispatched {method}")
        return self._c.call(method, params, timeout=timeout)


def _open(url):
    """A new background tab at `url`: (handle, raw), so the caller closes exactly what it opened."""
    try:
        raw = cdp.CDP(cdp.CGC_PORT, create_url=url)
    except SystemExit as e:                 # new_tab_failed / cdp_attach_failed: the browser, not the page
        raise Transient(str(e)) from None
    except Exception as e:
        raise Transient(f"{type(e).__name__}: {e}") from None
    return _NoSend(raw), raw


def _close(raw):
    try:
        raw.close_tab()                     # by its own target id, never another tab
    finally:
        raw.close()


def _cleared(t):
    left = t.eval(cdp._clear_composer_js(), timeout=45)
    if left != 0:
        raise Drift("composer", "clear", f"the composer still holds {left} chars after a clear")


# ---- composer + mention -------------------------------------------------------------------------
def probe_composer(t, *, model, family, app):
    """{cap: (verdict, detail)} for a fresh project tab `t`; raises Drift/Transient for the composer.
    The mention probe runs only once the composer has passed."""
    ready, st = cdp._await_composer(t)
    if not ready:
        out = cdp._composer_outcome(st, fresh=True)
        if out.get("drift"):
            raise Drift("composer", "composer", f"no composer on a loaded page (path {st.get('path')!r})")
        raise Transient(out["reason"])
    if model and model.lower() != "skip":
        pick = cdp._select_model(t, model, family)
        if not pick.confirmed:
            raise Drift("composer", "model", pick.error or
                        f"wanted {model!r} on {family!r}; the picker shows {pick.shown!r}")
    try:
        cdp._paste_prompt(t, DUMMY)
    except cdp._PreClickFailure as e:
        if e.__cause__ is not None:         # a CDP timeout or socket error, wrapped by the driver
            raise Transient(str(e)) from None
        raise Drift("composer", "paste", str(e)) from None
    _cleared(t)
    res = {"composer": ("pass", f"{len(DUMMY)}-char draft pasted, read back whole, cleared")}
    if app:
        try:
            cdp._insert_mention(t, app)
        except cdp._MentionFailure as e:
            if getattr(e, "drift", False):
                raise Drift("mention", "mention", str(e)) from None
            res["mention"] = ("unestablished", str(e))   # this account's apps, not the page's markup
        except cdp._PreClickFailure as e:
            raise Transient(str(e)) from None
        else:
            res["mention"] = ("pass", f"@{app} chip confirmed")
        _cleared(t)
    return res


# ---- reader -------------------------------------------------------------------------------------
_LIVE = tuple(sorted(set(store_mod._LEGAL) - store_mod.TERMINAL))


def pick_reader_round(s):
    """The finished round the reader probe reads, or None: the LAST round of a thread with no live
    round (nothing else drives that conversation, and its turn is the one a force-render mounts).
    Preferred: a CGC_RID-marker prompt long enough to collapse (the path that broke last), then any
    marker round, then a legacy round short enough never to collapse, whose rid its tail pair proves.
    A long legacy round is never picked: its rid is unprovable by design."""
    rows = s.db.execute(
        "SELECT r.rid, r.rendered_prompt p, t.conversation_id conv FROM rounds r "
        "JOIN threads t ON t.thread_id=r.thread_id "
        "WHERE r.state=? AND r.kind IN ('submit','followup') AND t.conversation_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM rounds o WHERE o.thread_id=r.thread_id AND ("
        "  o.state IN (%s) OR (o.kind IN ('submit','followup') AND o.created_at>r.created_at))) "
        "ORDER BY r.updated_at DESC LIMIT 200" % ",".join("?" * len(_LIVE)),
        (store_mod.COMPLETED_VERIFIED, *_LIVE)).fetchall()
    best = None
    for r in rows:
        p = r["p"] or ""
        if not cdp._is_canonical(r["conv"]) or cdp._turn_canonical_rid(p) != r["rid"]:
            continue
        marker = bool(cdp._TURN_MARKER_RE.search(p))
        # (tier, length key): a long marker turn first, then any marker turn, then the SHORTEST
        # legacy turn — the one furthest from ChatGPT's collapse threshold.
        rank = ((0, 0) if len(p) > LONG_PROMPT else (1, 0)) if marker else \
            ((2, len(p)) if len(p) < LEGACY_MAX else None)
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, {"rid": r["rid"], "conversation": r["conv"], "chars": len(p),
                           "kind": "marker" if marker else "legacy"})
    return best[1] if best else None


def probe_reader(t, rid):
    """The answer's length, or Drift/Transient. `t` is a tab on rid's finished conversation; the
    reads are the ones the send contract and the waiter make. A miss is a drift only while the rid
    is visibly on the page (_rid_in_main, which reads no turn selector): then the conversation is
    there and the reader cannot see it. Otherwise nothing proves the page is that conversation."""
    def miss(phase, cause):
        if cdp._rid_in_main(t, rid):
            return Drift("reader", phase, cause)
        return Transient(f"{rid} is not on the page ({phase}: {cause})")

    if not cdp._await_thread_rendered(t, seconds=RENDER_S):
        raise miss("turns", "a loaded conversation shows no user turn")
    texts, loc = [], {"present": False, "observed_rid": None}
    for reload in (False, True):           # the waiter's own one reload for a tab that fell behind
        if reload:
            cdp._reload_stale_tab(t, f"probe: {rid} not among the rendered user turns")
            cdp._await_thread_rendered(t, seconds=RENDER_S)
        t.eval(cdp._FORCE_RENDER_JS)
        texts = t.eval(cdp._all_user_texts_js()) or []
        loc = cdp._locate_source_turn(texts, rid)
        if loc["present"]:
            break
    if not loc["present"]:
        raise miss("rid", f"{rid}'s turn not identified among {len(texts)} user turns "
                          f"(the last reads as {loc['observed_rid'] or 'no rid'})")
    # The post-click send contract's own "ok": a turn family carries the rid AND shows an assistant
    # turn. A send that lands on a page failing this ends unknown_send or selector_drift.
    contract = json.loads(t.eval(cdp._contract_js(rid)) or "{}")
    if not any(f.get("ridLanded") and f.get("assistants", 0) > 0 for f in contract.get("families", [])):
        raise Drift("reader", "contract", f"the send contract would not confirm {rid} on its own "
                                          f"finished thread: {json.dumps(contract)[:300]}")
    st, ans, end = {}, "", time.time() + ANSWER_S
    while not ans:
        t.eval(cdp._FORCE_RENDER_JS)
        st = json.loads(t.eval(cdp._detect_js(rid, scoped=True)) or "{}")
        if st.get("done"):
            ans = t.eval(cdp._extract_js(rid, scoped=True)) or ""
        if ans or time.time() >= end:
            break
        time.sleep(1.0)
    if not ans:
        raise Drift("reader", "answer", f"no wrapped answer read for {rid} (node={st.get('node')} "
                                        f"len={st.get('len')} done={st.get('done')})")
    return len(ans)


# ---- one run ------------------------------------------------------------------------------------
def _attempt(composer, reader, pick, *, model, family, app):
    """{cap: (verdict, phase, detail)} for one pass of the composer (+ mention when `app`) and/or the
    reader probe; verdict: pass, broken, unestablished or transient."""
    out = {}

    def run(caps, fn):
        try:
            for cap, (verdict, detail) in fn().items():
                out[cap] = (verdict, "done", detail)
        except Drift as d:
            out[d.cap] = ("broken", d.phase, d.cause)
            for cap in caps:
                out.setdefault(cap, ("unestablished", d.phase, f"not reached: {d.cap} broke first"))
        except Transient as e:
            for cap in caps:
                out[cap] = ("transient", "browser", str(e))
        except SendRefused:
            raise
        except Exception as e:             # a CDP timeout or a closed socket mid-step
            for cap in caps:
                out.setdefault(cap, ("transient", "browser", f"{type(e).__name__}: {e}"))

    if composer:
        def composer_probe():
            t, raw = _open(os.environ.get("CGC_PROJECT_URL", cdp.CGC_PROJECT_URL))
            try:
                return probe_composer(t, model=model, family=family, app=app)
            finally:
                _close(raw)
        run(("composer", "mention") if app else ("composer",), composer_probe)
    if reader:
        if pick is None:
            out["reader"] = ("unestablished", "pick", "no completed_verified round whose rid the "
                                                      "reader can prove")
            return out

        def reader():
            lease = spool.acquire_conversation_lease(pick["conversation"])
            if lease is None:
                raise Transient(f"conversation {pick['conversation']} is busy")
            try:
                t, raw = _open(f"https://chatgpt.com/c/{pick['conversation']}")
                try:
                    n = probe_reader(t, pick["rid"])
                finally:
                    _close(raw)
            finally:
                lease.close()
            return {"reader": ("pass", f"{pick['kind']} round, {pick['chars']}-char prompt, "
                                       f"{n}-char answer")}
        run(("reader",), reader)
    return out


def run_probe(only=None) -> dict:
    """Run the probes, retry once what failed, record the verdicts. {cap: {verdict, phase, detail,
    recorded}}; a pass started before a newer failure is not recorded (Store.mark_passed)."""
    import consult
    apps = consult.known_apps()
    app = apps[0] if apps and only in (None, "mention") else None
    model, family = cdp.CGC_MODEL, cdp.CGC_MODEL_FAMILY
    composer, reader = only != "reader", only in (None, "reader")
    with store_mod.Store() as s:
        started = s.capability_epoch()
        pick = pick_reader_round(s)
    lease = spool.acquire_browser_lease(shared=True)
    if lease is None:
        caps = (("composer", "mention") if app else ("composer",)) * composer + ("reader",) * reader
        return {c: {"verdict": "transient", "phase": "browser", "recorded": False,
                    "detail": "browser under exclusive maintenance"} for c in caps}
    try:
        res = _attempt(composer, reader, pick, model=model, family=family, app=app)
        # Once more for what did not pass: a transient may clear, and a break is confirmed before it
        # pauses every send. The second verdict stands.
        bad = {c for c, v in res.items() if v[0] in ("broken", "transient")}
        if bad:
            res.update(_attempt(bool(bad - {"reader"}), "reader" in bad, pick,
                                model=model, family=family, app=app))
    finally:
        lease.close()
    rev = cgc_backend.driver_rev()
    report = {}
    with store_mod.Store() as s:
        for cap, (verdict, phase, detail) in res.items():
            ref = f"{pick['rid']}@{pick['conversation']}" if cap == "reader" and pick else None
            rec = False
            if verdict == "pass":
                rec = s.mark_passed(cap, started_epoch=started, phase="probe", cause=detail, ref=ref,
                                    driver_rev=rev)
            elif verdict == "broken":
                s.mark_broken(cap, phase=f"probe:{phase}", cause=detail, ref=ref, driver_rev=rev)
                rec = True
            report[cap] = {"verdict": verdict, "phase": phase, "detail": detail, "recorded": rec}
    return report


def print_states(report=None) -> int:
    """Print the probe report and every recorded capability. Exit 0 all recorded ok, 3 something
    broken (a human/driver fix), 1 the probe was inconclusive."""
    with store_mod.Store() as s:
        caps = s.capabilities()
    print(json.dumps({"probe": report, "capabilities": caps}, sort_keys=True))
    words = {"pass": "ok", "broken": "BROKEN", "unestablished": "not established",
             "transient": "probe inconclusive (state unchanged)"}
    for cap, v in (report or {}).items():
        stale = v["verdict"] == "pass" and not v["recorded"]
        sys.stderr.write(f"CGC_PROBE {cap}: {words[v['verdict']]} — {v['detail']}"
                         + (" (not recorded: a newer failure arrived while it ran)" if stale else "")
                         + "\n")
    for cap in store_mod.CAPABILITIES:
        v = caps.get(cap)
        sys.stderr.write(f"CGC_UI {cap}: " + ("never established" if v is None else
                         f"{'ok' if v.get('ok') else 'BROKEN'} at {v.get('checked_at')} "
                         f"({v.get('phase')}: {v.get('cause')})") + "\n")
    if any(v.get("ok") is False for v in caps.values()):
        return 3
    return 1 if any(v["verdict"] == "transient" for v in (report or {}).values()) else 0


def main() -> int:
    p = argparse.ArgumentParser(prog="cgc_probe.py",
                                description="no-send check of the ChatGPT UI contract the driver reads")
    p.add_argument("--only", choices=("composer", "mention", "reader"))
    p.add_argument("--status", action="store_true", help="print the recorded states; no browser")
    a = p.parse_args()
    return print_states(None if a.status else run_probe(a.only))


if __name__ == "__main__":
    raise SystemExit(main())
