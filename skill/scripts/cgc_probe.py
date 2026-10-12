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
import signal
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
HYDRATE_S = 60            # how long the whole thread may take to hydrate before a miss is judged

# What the send path's JS looks like (its button selector, a form submit). A probe step that would
# evaluate one is refused; Enter is a key, refused in _NoSend.key.
_SEND_JS = re.compile(r"send-button|requestSubmit|\.submit\(")


class SendRefused(AssertionError):
    """The probe reached for the send path. A bug, never a verdict."""


class Transient(Exception):
    """Nothing about the page's contract is known: Chrome, the network, a login, a load."""


class Blocked(Transient):
    """The page shows a condition only a human can clear (login, captcha/challenge, rate limit)."""

    def __init__(self, blocker):
        super().__init__(f"ChatGPT shows {blocker} — a human must clear it in the debug Chrome")
        self.blocker = blocker


class Drift(Exception):
    """A deterministic contract mismatch: the page loaded and does not read the way the driver reads."""

    def __init__(self, cap, phase, cause):
        super().__init__(f"{cap}/{phase}: {cause}")
        self.cap, self.phase, self.cause = cap, phase, cause


def _confirm(t, drift):
    """Every broken verdict passes here: the send path's own blocker rule (cdp._blocker) runs on the
    page first, and a login wall, a challenge or a rate limit turns the verdict into Blocked."""
    b = cdp._blocker(t)
    return Blocked(b) if b else drift


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
        raise _confirm(t, Drift("composer", "clear", f"the composer still holds {left} chars after a clear"))


# ---- composer + mention -------------------------------------------------------------------------
def probe_composer(t, *, model, family, app):
    """{cap: (verdict, detail)} for a fresh project tab `t`; raises Drift/Transient for the composer.
    The mention probe runs only once the composer has passed."""
    ready, st = cdp._await_composer(t)
    if not ready:
        out = cdp._composer_outcome(st)
        if out.get("blocker"):
            raise Blocked(out["blocker"])
        # The app loaded (its form rendered, on chatgpt.com) and holds no visible editor: the
        # editor's markup moved. A page that never rendered the form says nothing about it.
        if out["reason"] == "composer_not_ready" and out.get("form"):
            raise _confirm(t, Drift("composer", "composer",
                                    f"the app rendered but no composer editor (path {st.get('path')!r})"))
        raise Transient(f"{out['reason']} (path {st.get('path')!r}, form={bool(out.get('form'))})")
    if st.get("blocker"):                   # a page showing a human condition proves nothing either way
        raise Blocked(st["blocker"])
    res = {}
    if model and model.lower() != "skip":
        # Its own capability: only rounds that select this tier on this family need it (round_needs).
        pick = cdp._select_model(t, model, family)
        if pick.confirmed:
            res["model"] = ("pass", f"{model!r} on {family!r} selected")
        else:
            why = pick.error or f"wanted {model!r} on {family!r}; the picker shows {pick.shown!r}"
            b = cdp._blocker(t)
            res["model"] = ("transient", "human:" + b, f"ChatGPT shows {b}") if b else ("broken", why)
    try:
        cdp._paste_prompt(t, DUMMY)
    except cdp._PreClickFailure as e:
        if e.__cause__ is not None:         # a CDP timeout or socket error, wrapped by the driver
            raise Transient(str(e)) from None
        raise _confirm(t, Drift("composer", "paste", str(e))) from None
    _cleared(t)
    res["composer"] = ("pass", f"{len(DUMMY)}-char draft pasted, read back whole, cleared")
    if app:
        try:
            cdp._insert_mention(t, app)
        except cdp._MentionFailure as e:
            if getattr(e, "drift", False):
                raise _confirm(t, Drift("mention", "mention", str(e))) from None
            res["mention"] = ("unestablished", str(e))   # this account's apps, not the page's markup
        except cdp._PreClickFailure as e:
            raise Transient(str(e)) from None
        else:
            res["mention"] = ("pass", f"@{app} chip confirmed")
        _cleared(t)
    return res


# ---- reader -------------------------------------------------------------------------------------
# A round in one of these states may have a browser tab driving its conversation right now. A QUEUED
# round does not (gated or not), so it never excludes a fixture: if it did, a broken reader whose
# only finished thread has a queued follow-up could never be re-checked, and nothing would clear it.
_ACTIVE = (store_mod.READY, store_mod.SENDING, store_mod.ACCEPTED, store_mod.WAITING)


def _thread_active(s, conv) -> bool:
    return s.db.execute(
        "SELECT 1 FROM rounds r JOIN threads t ON t.thread_id=r.thread_id "
        "WHERE t.conversation_id=? AND r.state IN (?,?,?,?) LIMIT 1", (conv, *_ACTIVE)).fetchone() is not None


def pick_reader_round(s):
    """The finished round the reader probe reads, or None: a completed_verified round that is the
    LAST turn its thread was sent (so it is the turn a force-render mounts, never one a long thread
    has virtualized away), in a thread with no round in an active browser state. "Sent" is the
    ledger's evidence: an attempt exists (begin_send ran) and no not-sent proof. A round that never
    crossed the send fence (queued, gated or not) never touched the thread, so it disqualifies
    nothing. Preferred: a CGC_RID-marker prompt long enough to collapse (the path that broke last),
    then any marker round, then the shortest legacy round under the collapse length, whose rid its
    tail pair proves. A long legacy round is never picked: its rid is unprovable by design."""
    rows = s.db.execute(
        "SELECT r.rid, r.rendered_prompt p, t.conversation_id conv FROM rounds r "
        "JOIN threads t ON t.thread_id=r.thread_id "
        "WHERE r.state=? AND r.kind IN ('submit','followup') AND t.conversation_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM rounds o WHERE o.thread_id=r.thread_id AND ("
        "  o.state IN (?,?,?,?) OR (o.created_at>r.created_at AND o.current_attempt_id IS NOT NULL "
        "  AND (o.send_disposition IS NULL OR o.send_disposition<>?)))) "
        "ORDER BY r.updated_at DESC LIMIT 200",
        (store_mod.COMPLETED_VERIFIED, *_ACTIVE, store_mod.NOT_SENT_PROVEN)).fetchall()
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


def _read(t, rid):
    """One pass of the reads the send contract and the waiter make: (answer, miss), where miss is
    (phase, cause) for the first read that came up empty, or None when the answer was read."""
    t.eval(cdp._FORCE_RENDER_JS)
    texts = t.eval(cdp._all_user_texts_js()) or []
    loc = cdp._locate_source_turn(texts, rid)
    if not loc["present"]:
        return "", ("rid", f"{rid}'s turn not identified among {len(texts)} user turns "
                           f"(the last reads as {loc['observed_rid'] or 'no rid'})")
    # The post-click send contract's own "ok": a turn family carries the rid AND shows an assistant
    # turn. A send landing on a page that fails this ends unknown_send or selector_drift.
    contract = json.loads(t.eval(cdp._contract_js(rid)) or "{}")
    if not any(f.get("ridLanded") and f.get("assistants", 0) > 0 for f in contract.get("families", [])):
        return "", ("contract", f"the send contract would not confirm {rid} on its own finished "
                                f"thread: {json.dumps(contract)[:300]}")
    st = json.loads(t.eval(cdp._detect_js(rid, scoped=True)) or "{}")
    ans = (t.eval(cdp._extract_js(rid, scoped=True)) or "") if st.get("done") else ""
    if ans:
        return ans, None
    return "", ("answer", f"no wrapped answer read for {rid} (node={st.get('node')} "
                          f"len={st.get('len')} done={st.get('done')})")


def probe_reader(t, rid, marker=True, settle=None):
    """The answer's length, or Drift/Transient. `t` is a tab on rid's finished conversation.

    The reads are polled for `settle` seconds (default HYDRATE_S): a re-opened thread renders its
    user turns first and the assistant turns seconds later, so an early miss is hydration, not drift.
    Only the reads' state once that window has passed is judged, and the waiter's one reload is spent
    halfway. A miss is a drift only when the page shows no human condition (the shared blocker rule)
    AND the rid is visibly on it (_rid_in_main, which reads no turn selector): then the conversation
    is there and the reader cannot see it. Otherwise nothing proves the page is that conversation.
    `marker`: rid's prompt opens with CGC_RID. A legacy turn can only be named by its tail pair, which
    a collapsed turn hides, so a legacy rid miss is never evidence of drift."""
    settle = HYDRATE_S if settle is None else settle
    start = time.time()
    reloaded, miss = False, ("turns", "a loaded conversation shows no user turn")
    # The settle signal a re-opened thread's follow-up waits on (_settled_labels): the composer's
    # switchers render last, so labels that stopped changing mean the page finished hydrating.
    # Labels that never settle make any miss below inconclusive, never a break.
    settled = _hydrated(t)
    while True:
        if cdp._await_thread_rendered(t, seconds=min(RENDER_S, settle)):
            ans, miss = _read(t, rid)
            if not miss:
                return len(ans)
        elapsed = time.time() - start
        if elapsed >= settle:
            break
        if not reloaded and elapsed >= settle / 2:
            reloaded = True                 # the waiter's own one reload for a tab that fell behind
            cdp._reload_stale_tab(t, f"probe: {rid} not read yet ({miss[0]})")
            continue
        time.sleep(2.0)
    phase, cause = miss
    b = cdp._blocker(t)
    if b:
        raise Blocked(b)
    # A break needs a page that finished hydrating (settled, re-checked now after the reload) and
    # shows the rid in <main>; anything less is inconclusive.
    if not (settled or _hydrated(t)):
        raise Transient(f"{rid} not read ({phase}: {cause}); the page never settled")
    if (marker or phase != "rid") and cdp._rid_in_main(t, rid):
        raise Drift("reader", phase, cause)
    raise Transient(f"{rid} not read ({phase}: {cause})")


def _hydrated(t) -> bool:
    """Did the re-opened conversation finish hydrating? The driver's own settle signal for that
    (cdp._settled_labels, what a follow-up waits on before acting on a re-opened thread), read as
    a verdict: labels present and unchanged for its hold. Bounded at its own 20 s."""
    hold, bound = 4.0, 20.0
    start = time.time()
    labels = cdp._settled_labels(t, hold=hold, bound=bound)
    return bool(labels) and time.time() - start < bound


_APP_REF = "app:"


def probe_app(s, override=None):
    """The app the mention probe inserts, or None: `--mention`; else, while mention is recorded broken,
    the app that failure was recorded on (its `rid_or_conversation` is `app:<name>`), so the break is
    re-checked — and can be cleared — on the app that broke even if CGC_APPS has changed since; else
    the first CGC_APPS app."""
    if override:
        return override
    import consult
    rec = s.capabilities().get("mention") or {}
    ref = rec.get("rid_or_conversation") or ""
    if rec.get("ok") is False and ref.startswith(_APP_REF):
        return ref[len(_APP_REF):]
    apps = consult.known_apps()
    return apps[0] if apps else None


# ---- one run ------------------------------------------------------------------------------------
def _composer_caps(model, app):
    """What the composer probe reports on: composer, plus model unless auto-model is off, plus mention
    when there is an app to insert."""
    return (("composer",) + (("model",) if model and model.lower() != "skip" else ())
            + (("mention",) if app else ()))


def _attempt(composer, reader, pick, *, model, family, app):
    """{cap: (verdict, phase, detail)} for one pass of the composer (+ mention when `app`) and/or the
    reader probe; verdict: pass, broken, unestablished or transient."""
    out = {}

    def run(caps, fn):
        try:
            for cap, v in fn().items():     # (verdict, detail) or (verdict, phase, detail)
                out[cap] = (v[0], "done", v[1]) if len(v) == 2 else v
        except Drift as d:
            out[d.cap] = ("broken", d.phase, d.cause)
            for cap in caps:
                out.setdefault(cap, ("unestablished", d.phase, f"not reached: {d.cap} broke first"))
        except Blocked as e:
            for cap in caps:
                out[cap] = ("transient", "human:" + str(e.blocker), str(e))
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
        run(_composer_caps(model, app), composer_probe)
    if reader:
        if pick is None:
            out["reader"] = ("unprovable", "pick", "no completed_verified round whose rid the "
                                                   "reader can prove")
            return out

        def reader():
            # The mutators' own conversation lease, then a re-check under it: a round that began
            # driving this thread after the pick (its waiter needs no lease) makes the probe step
            # aside rather than read a thread that is moving.
            lease = spool.acquire_conversation_lease(pick["conversation"])
            if lease is None:
                raise Transient(f"conversation {pick['conversation']} is busy")
            try:
                with store_mod.Store() as s:
                    if _thread_active(s, pick["conversation"]):
                        raise Transient(f"conversation {pick['conversation']} became active")
                t, raw = _open(f"https://chatgpt.com/c/{pick['conversation']}")
                try:
                    n = probe_reader(t, pick["rid"], marker=pick["kind"] == "marker")
                finally:
                    _close(raw)
            finally:
                lease.close()
            return {"reader": ("pass", f"{pick['kind']} round, {pick['chars']}-char prompt, "
                                       f"{n}-char answer")}
        run(("reader",), reader)
    return out


def run_probe(only=None, mention=None) -> dict:
    """Run the probes, retry once what failed, record the verdicts. {cap: {verdict, phase, detail,
    recorded}}; a pass started before a newer failure is not recorded (Store.mark_passed).
    `mention`: the app the mention probe inserts (default: probe_app)."""
    model, family = cdp.CGC_MODEL, cdp.CGC_MODEL_FAMILY
    composer, reader = only != "reader", only in (None, "reader")
    with store_mod.Store() as s:
        started = s.capability_epoch()
        pick = pick_reader_round(s)
        app = probe_app(s, mention) if only in (None, "mention") else None
    no_app = None
    if only in (None, "mention") and not app:
        no_app = {"verdict": "unestablished", "phase": "app", "recorded": False,
                  "detail": "no app to insert: set CGC_APPS, or run `cgc probe --mention \"<app>\"`"}
    lease = spool.acquire_browser_lease(shared=True)
    if lease is None:
        caps = _composer_caps(model, app) * composer + ("reader",) * reader
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
            ref = (f"{pick['rid']}@{pick['conversation']}" if cap == "reader" and pick
                   else _APP_REF + app if cap == "mention" and app else None)
            rec = False
            if verdict == "pass":
                rec = s.mark_passed(cap, started_epoch=started, phase="probe", cause=detail, ref=ref,
                                    driver_rev=rev)
            elif verdict == "broken":
                s.mark_broken(cap, phase=f"probe:{phase}", cause=detail, ref=ref, driver_rev=rev)
                rec = True
            elif verdict == "unprovable" and (s.capabilities().get(cap) or {}).get("ok") is False:
                # A break no probe can re-check would hold every send forever. Clear it, saying so;
                # the next round that lands proves the reader or breaks it again. The epoch guard
                # still keeps a break recorded after this run started.
                rec = s.mark_passed(cap, started_epoch=started, phase="unprovable", driver_rev=rev,
                                    cause="the reader could not be re-proven (no finished round to "
                                          "read); gate cleared so sends are not locked")
            report[cap] = {"verdict": verdict, "phase": phase, "detail": detail, "recorded": rec}
    if no_app:
        report.setdefault("mention", no_app)
    return report


def print_states(report=None) -> int:
    """Print the probe report and every recorded capability. Exit 0 all recorded ok, 3 something
    broken (a human/driver fix), 1 the probe was inconclusive."""
    with store_mod.Store() as s:
        caps = s.capabilities()
    print(json.dumps({"probe": report, "capabilities": caps}, sort_keys=True))
    words = {"pass": "ok", "broken": "BROKEN", "unestablished": "not established",
             "unprovable": "cannot be re-proven (a break is cleared, not kept)",
             "transient": "probe inconclusive (state unchanged)"}
    for cap, v in (report or {}).items():
        stale = v["verdict"] == "pass" and not v["recorded"]
        sys.stderr.write(f"CGC_PROBE {cap}: {words[v['verdict']]} — {v['detail']}"
                         + (" (not recorded: a newer failure arrived while it ran)" if stale else "")
                         + "\n")
    human = sorted({v["phase"][6:] for v in (report or {}).values() if v["phase"].startswith("human:")})
    if human:
        sys.stderr.write(f"CGC_PROBE_BLOCKER {', '.join(human)}: ChatGPT is showing a page a human must "
                         "clear in the debug Chrome window (login, challenge or rate limit). No "
                         "capability was changed.\n")
    for cap in store_mod.CAPABILITIES:
        v = caps.get(cap)
        sys.stderr.write(f"CGC_UI {cap}: " + ("never established" if v is None else
                         f"{'ok' if v.get('ok') else 'BROKEN'} at {v.get('checked_at')} "
                         f"({v.get('phase')}: {v.get('cause')})") + "\n")
    if any(v.get("ok") is False for v in caps.values()):
        return 3
    return 1 if any(v["verdict"] == "transient" for v in (report or {}).values()) else 0


def main() -> int:
    # The daemon ends a probe it owns with SIGTERM (shutdown, or the timeout). Raised as SystemExit,
    # so every `finally` runs: the probe's own tab is closed and its leases released; nothing is
    # recorded for an unfinished run.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(1))
    p = argparse.ArgumentParser(prog="cgc_probe.py",
                                description="no-send check of the ChatGPT UI contract the driver reads")
    p.add_argument("--only", choices=("composer", "mention", "reader"))
    p.add_argument("--mention", metavar="APP",
                   help="the app the mention probe inserts (default: the app a recorded mention "
                        "failure names, else the first CGC_APPS app)")
    p.add_argument("--status", action="store_true", help="print the recorded states; no browser")
    a = p.parse_args()
    return print_states(None if a.status else run_probe(a.only, a.mention))


if __name__ == "__main__":
    raise SystemExit(main())
