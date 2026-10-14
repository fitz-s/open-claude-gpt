#!/usr/bin/env python3
"""Store-backed control plane — the enqueue / await / worker logic over the SQLite store, the SOLE
round authority (the file-spool control plane it replaced is gone). Kept in ONE module so cgc_spool
(enqueue/await CLI) and cgc_daemon (the worker loop) delegate here, and so the whole round lifecycle
is testable with a stubbed CDP driver — no browser.

The worker's mapping of a submit outcome to a round state is the load-bearing part. begin_send
durably commits `sending` before the click, so:
  - exit 6 (EXIT_NOT_SENT_PRECLICK)        -> failed    (cdp_consult's own pre-click boundary proved
                                                          the click was never issued; auto-stamped
                                                          not-sent-proven, key-retry-eligible with no
                                                          operator step — see mark_send_not_sent)
  - a returned conversation id             -> accepted  (the click landed)
  - a returned PROVEN-not-sent verdict (stderr marker):
        login / captcha / rate-limit       -> blocked   (human acts; terminal, never resent)
        model-not-selectable / composer     -> queued    (retry; proven the click never happened)
  - anything else (unknown_send, a crash)  -> possibly_accepted  (uncertain; NEVER auto-resent)
A crash mid-send leaves the round in `sending`; recover() turns that into possibly_accepted too, so
the anti-duplicate invariant holds on every path.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time

import cgc_store as store_mod

# stderr markers cdp_consult emits FAIL-CLOSED, before the click — so their presence PROVES this
# attempt did not send. Safe to trust because _run scopes stderr to the current invocation (an
# earlier attempt's marker can no longer leak in): a marker here is always this attempt's own.
#   _NOT_SENT_BLOCK: needs a human (login/captcha/rate-limit) → terminal blocked, never resent.
#   _NOT_SENT_RETRY: a transient pre-click failure (bad tab/composer/model/page) → requeue; retry may
#       immediately succeed. Includes the "browser can't open a working tab" family (attach_failed /
#       new_tab*) that the file-spool path handled via browser-repair — requeuing lets a transient
#       tab-open glitch retry instead of stranding a provably-unsent round as uncertain.
#   contract_invalid: chatgpt_contract.json failed its schema at the driver's import, before any
#       browser call — not sent, and no retry can help until the contract is fixed: blocked, so
#       `refire` resends it once the edit replays.
_NOT_SENT_BLOCK = ("login_needed", "CGC_LOGIN", "captcha", "rate_limit", "usage",
                   "mention_not_found", "contract_invalid")
_NOT_SENT_RETRY = ("model_not_selectable", "composer_not_ready", "no_page_target",
                   "attach_failed", "new_tab", "wrong_page", "gate_refused")

# cdp_consult.py's EXIT_NOT_SENT_PRECLICK: its pre-click boundary (composer settle/clear/chunked-paste
# /verify) catches EVERY exception raised inside it, so this exit code is trusted directly by NUMBER,
# not by matching a stderr string — a raw traceback (the recorded field incident: a websocket read
# timeout mid-paste) carries no marker text and used to fall through to possibly_accepted uncaught.
_EXIT_NOT_SENT_PRECLICK = 6


class ConversationLeaseRefused(Exception):
    """Another worker holds the per-conversation exclusive mutator lease for this follow-up's target
    thread, so ours may not touch that thread's shared composer. Raised BEFORE begin_send, so the
    round is untouched (still READY) — nothing was sent and no state moved. The worker catches this
    and exits EXIT_LEASE_REFUSED, exactly like a lost rid-lease race: the round is redispatched later
    and the reaper treats the death as inert."""
    def __init__(self, conversation):
        super().__init__(conversation)
        self.conversation = conversation


# A pre-click failure is called transient because a retry MAY fix it — not because it will. Three
# begun sends that all died before the click is no longer evidence of a blip; it is a standing
# condition (the observed one: the thread's model tier had dropped off Pro, which no retry can
# restore). Unbounded, that retry is a hot loop against the daemon poll, and each turn of it opens
# a browser tab — exhausting the one resource whose exhaustion ends every consult. So it is bounded,
# and the round then BLOCKS: terminal, never resent, addressed to the human who can actually fix it.
_MAX_NOT_SENT_RETRIES = 3


def _requeue_or_block(store, rid: str, marker: str) -> str:
    """Route a PROVEN not-sent round: retry while the evidence still reads transient, then block.

    The incident this fixed: a UI change (ChatGPT's composer) made `model_not_selectable` fire on
    every attempt, so two real rounds ran out their 3 retries and landed BLOCKED with
    send_disposition left NULL — the terminal round's error text asserted "nothing was ever sent"
    but the durable proof was never written down. `_release_eligible` only ever looked at
    GATE_REJECTED/FAILED, so a proven-not-sent BLOCKED round could never release its request-key,
    and `_idempotent_receipt` fell through to treating it as a live/completed prior and handed back
    an idempotent receipt pointing at a dead round — no error, no retry, no path forward short of a
    brand-new key. Stamped here, in the SAME transaction as the state change (exactly like
    mark_send_not_sent does for exit 6), because the marker that got us here is one of _NOT_SENT_RETRY
    — a fail-closed pre-click stderr marker the code already treats as proof this attempt did not
    send (see the module docstring and the _LEGAL comment on SENDING)."""
    if store.attempt_count(rid) >= _MAX_NOT_SENT_RETRIES:
        store.set_state(rid, store_mod.BLOCKED, expect=store_mod.SENDING,
                        send_disposition=store_mod.NOT_SENT_PROVEN,
                        error_code=(f"{marker} — still failing after {_MAX_NOT_SENT_RETRIES} "
                                    "automatic retries; nothing was ever sent, but this needs a "
                                    "human (check the ChatGPT window: model tier, login, the tab)"))
        return store_mod.BLOCKED
    # The requeue is stamped too: this is the ONLY sending -> queued edge, and it is only ever taken on
    # a proven not-sent, while an attempt that may have clicked can never return to queued. So a
    # queued round's every attempt so far is proven unsent, which is exactly what the stamp says;
    # begin_send clears it the moment a new attempt may click. Without it the ledger charged a round
    # that never sent, and a cancel after the requeue held its request-key forever.
    store.set_state(rid, store_mod.QUEUED, expect=store_mod.SENDING, error_code=marker,
                    send_disposition=store_mod.NOT_SENT_PROVEN)
    return store_mod.QUEUED



# Backoff for the egress gate's TRANSIENT ('unverified:') failure — a `ready` round bounced back to
# `queued` because gh could not CONFIRM a repo's visibility (missing/timed out/API error), not
# because it confirmed anything private. Evidence this fixes: ONE followup round hit ready->queued
# 2461 times in 91 minutes (~1 every 2.2s) — a requeue with no backoff is instantly re-claimable, so
# the daemon's own 2s poll became the retry pace a since-removed docstring claimed was "paced",
# until the caller cancelled it 91 minutes later. Exponential, capped — "a few minutes is plenty" —
# and bounded: past _MAX_GATE_RETRIES this is not a blip, it is a standing gh problem (not installed,
# not authenticated, network-partitioned), and a human needs to look rather than the daemon looping a
# browser-tab's worth of gh calls forever.
_MAX_GATE_RETRIES = 5
_GATE_BACKOFF_BASE_S = 5.0
_GATE_BACKOFF_CAP_S = 180.0


def _gate_requeue_or_block(store, rid: str, reason: str, retry_count: int) -> str:
    """Route a TRANSIENT gate failure: backoff-requeue while it still reads like a blip, then BLOCK.
    Mirrors `_requeue_or_block`'s bound-then-block shape (same file, the send-phase precheck's
    equivalent) rather than inventing a parallel one — and for the same reason that function stamps
    NOT_SENT_PROVEN: the gate runs on a `ready` round, strictly BEFORE begin_send, so a gate failure
    is exactly as pre-send-proven as a maxed-out send precheck. Without the stamp a maxed-out gate
    failure would BLOCK holding its request-key forever, the same trap aa9ea36 closed for the send
    side (see _release_eligible)."""
    n = retry_count + 1  # this attempt
    if n > _MAX_GATE_RETRIES:
        store.set_state(rid, store_mod.BLOCKED, expect=store_mod.READY,
                        send_disposition=store_mod.NOT_SENT_PROVEN,
                        error_code=(f"{reason} — still unverified after {_MAX_GATE_RETRIES} "
                                    "automatic gate retries; nothing was ever sent, but the egress "
                                    "gate needs a human (is `gh` installed, authenticated, and "
                                    "reachable?)"))
        return store_mod.BLOCKED
    delay_s = min(_GATE_BACKOFF_BASE_S * (2 ** (n - 1)), _GATE_BACKOFF_CAP_S)
    not_before = (datetime.datetime.now(datetime.timezone.utc)
                  + datetime.timedelta(seconds=delay_s)).isoformat()
    store.set_state(rid, store_mod.QUEUED, expect=store_mod.READY, error_code=reason,
                    not_before=not_before, gate_retry_count=n)
    return store_mod.QUEUED


def _gate(store, rid: str, prompt: str, validate) -> str | None:
    """Run the egress gate on a `ready` round. Two failure kinds, two outcomes:
      - `refused:`     — an AUTHORITATIVE denial (secret detected / no public link / repo confirmed
                         not public). Terminal GATE_REJECTED; the payload must never leave.
      - `unverified:`  — a TRANSIENT gate failure (gh not installed, gh timed out, gh API error). The
                         gate could not CONFIRM the repo is public, not that it is private. Requeue
                         (the round is still `ready`, pre-send — nothing has left) with a growing
                         backoff (_gate_requeue_or_block) so a temporary gh outage does not
                         permanently strand a legitimate consult behind a terminal reject, and does
                         not hot-spin the daemon while it waits — the claim query itself now honours
                         the backoff (cgc_store.claim_ready), so this is a real delay, not a poll-paced
                         one nothing actually paced.
    Returns the resulting state if the gate did not pass, else None (passed — caller proceeds to send)."""
    ok, reason = validate(prompt)
    if ok:
        return None
    if reason.startswith("unverified:"):
        row = store.get_round(rid)
        retry_count = (row or {}).get("gate_retry_count") or 0
        return _gate_requeue_or_block(store, rid, reason, retry_count)
    store.gate_reject(rid, reason)
    return store_mod.GATE_REJECTED


# ---- observability -----------------------------------------------------------
def store_status(up: bool, rid: str | None = None) -> int:
    """Post-cutover `cgc queue`/`status`: round counts by state + the active buckets, so the store is
    as inspectable as the spool dirs were. Surfaces `uncertain` rounds prominently — those are the
    only ones that need a human (possibly sent, deliberately not auto-resent)."""
    with store_mod.Store() as s:
        rows = s.db.execute(
            "SELECT state, count(*) c FROM rounds GROUP BY state ORDER BY state").fetchall()
        active = s.recover()
        # possibly_accepted is the only bucket that needs a HUMAN; a `sending` round is just in-flight.
        needs_human = [row["rid"] for row in s.db.execute(
            "SELECT rid FROM rounds WHERE state=?", (store_mod.POSSIBLY_ACCEPTED,))]
        print(f"daemon: {'UP' if up else 'DOWN'}   backend: STORE ({store_mod.db_path()})")
        for r in rows:
            print(f"  {r['state']:20s} {r['c']:3d}")
        print(f"  active: dispatchable={len(active['dispatchable'])} "
              f"reattach={len(active['reattach'])} in-flight/uncertain={len(active['uncertain'])}")
        if needs_human:
            print("  NEEDS RECONCILE (possibly sent, NOT auto-resent — retrieve, don't resend): "
                  + " ".join(needs_human[:8]))
        broken = s.broken_capabilities()
        if broken:
            print(f"  UI CONTRACT: {paused_text(broken)}")
        if rid:
            rr = s.get_round(rid)
            if rr:
                # The addresses, not just the verdict: where the answer is (or will be) and which
                # ChatGPT thread holds it. A status that reports only a state word makes the reader
                # go derive both by hand — and the conversation is exactly what a stranded round's
                # recovery is addressed by.
                fields = {k: rr[k] for k in ("state", "kind", "error_code", "current_attempt_id")}
                out = rr.get("out_path")
                fields["out_path"] = out
                # An address can lie: /tmp is evicted, and a round can complete while no waiter is
                # alive to write the file. Report whether the artifact is actually THERE, so nobody
                # ever again watches a promised path that no writer is keeping. `await --rid`
                # rewrites it from the store on demand.
                fields["answer_on_disk"] = bool(out and os.path.exists(out))
                fields["conversation"] = s.conversation_of(rid)
                fields["send"] = s.send_evidence(rid)
                gate = s.gated_by(rr) if rr["state"] == store_mod.QUEUED else {}
                if gate:
                    fields["human_action"] = paused_text(gate)
                print(f"  round[{rid}]: {json.dumps(fields)}")
                d = decide(s, rr, daemon_up=up)
                envelope(rid=rid, state=rr["state"], code=d["code"],
                         next_command=d["next_command"], human_action=d["human_action"],
                         answer_path=d["answer_path"],
                         detail=None if rr["state"] in _DONE_STATES else rr["error_code"],
                         retryable=d["retryable"], answer_on_disk=d["answer_path"] is not None,
                         conversation=fields["conversation"], daemon_up=up)
            else:
                print(f"  round[{rid}]: (not in store)")
                envelope(rid=rid, state="missing", code="not_found", detail="no such round in the store")
        else:
            envelope(code="ok" if up else "service_unavailable", daemon_up=up,
                     human_action=None if up else "run `cgc install-daemon` (daemon down)",
                     counts={r["state"]: r["c"] for r in rows}, needs_reconcile=needs_human[:8])
    return 0


def stats_report() -> int:
    """Aggregate reliability metrics from data the store already holds. The raw material was always
    there (rounds + timestamps); this makes it a number someone actually looks at — the difference
    between 'sentinel drift is happening' being an anecdote and being an alarm."""
    import datetime as _dt

    def _secs(a, b):
        try:
            return (_dt.datetime.fromisoformat(b) - _dt.datetime.fromisoformat(a)).total_seconds()
        except (ValueError, TypeError):
            return None

    with store_mod.Store() as s:
        rows = [dict(r) for r in s.db.execute("SELECT state, kind, created_at, updated_at FROM rounds")]
        spent = store_mod.spent_messages(s.db)
    total = len(rows)
    by = {}
    for r in rows:
        by[r["state"]] = by.get(r["state"], 0) + 1
    ok = by.get(store_mod.COMPLETED_VERIFIED, 0)
    unv = by.get(store_mod.COMPLETED_UNVERIFIED, 0)
    fail = by.get(store_mod.FAILED, 0) + by.get(store_mod.GATE_REJECTED, 0)
    completed = ok + unv
    durations = sorted(d for r in rows
                       if r["state"] in (store_mod.COMPLETED_VERIFIED, store_mod.COMPLETED_UNVERIFIED)
                       for d in [_secs(r["created_at"], r["updated_at"])] if d is not None and d > 0)

    def _pct(p):
        return durations[min(len(durations) - 1, int(len(durations) * p))] if durations else None

    unv_rate = (unv / completed) if completed else None

    # ---- the message ledger ----------------------------------------------------------------
    # A ChatGPT Pro plan allows tens of messages a week, so "how many did we actually spend" is a
    # fact worth being able to read. It needs NO new column: the store already answers it exactly —
    # see store_mod.spent_messages for what counts (a base send that crossed the fence without a
    # not-sent proof, plus an automatic continue that landed, is unsure, or never recorded a result).
    # Consequences, all correct and all deliberate: a `retrieve` counts zero; a gate-rejected or
    # cancelled-while-queued round counts zero; an UNCERTAIN send counts one, because the click may
    # have landed and the honest ledger charges for what may have been spent. Messages can exceed
    # rounds, so `free` is the number of ROUNDS that provably spent nothing, not total - spent.
    # RECORDING ONLY — nothing here reserves, admits, throttles or refuses. A number to read.
    spent_by_kind = {}
    for _rid, k, _at in spent:
        spent_by_kind[k or "unknown"] = spent_by_kind.get(k or "unknown", 0) + 1
    spent_rids = {x[0] for x in spent}
    free = total - len(spent_rids)

    report = {
        "rounds": total, "by_state": by,
        "completed": completed, "failed": fail,
        "unverified_rate": round(unv_rate, 3) if unv_rate is not None else None,
        "latency_s": {"p50": _pct(0.50), "p90": _pct(0.90), "n": len(durations)},
        "messages": {"potentially_spent": len(spent), "free": free, "by_kind": spent_by_kind},
    }
    print(json.dumps(report))
    if total:
        sys.stderr.write(f"CGC_STATS {len(spent)} potentially spent messages across {total} rounds "
                         f"({free} rounds provably spent none) — {spent_by_kind or '{}'}.\n")
    if completed:
        sys.stderr.write(f"CGC_STATS {completed} completed ({ok} verified / {unv} unverified), "
                         f"{fail} failed, of {total} rounds.\n")
        if durations:
            sys.stderr.write(f"CGC_STATS completion latency p50={int(_pct(0.50))}s "
                             f"p90={int(_pct(0.90))}s over {len(durations)} rounds.\n")
        # The drift alarm: the sentinel wrapper failing often is the earliest cheap signal that the
        # ChatGPT UI or model behavior changed under us.
        if unv_rate is not None and unv_rate > 0.25 and completed >= 8:
            sys.stderr.write(
                f"CGC_ALERT unverified-completion rate is {unv_rate:.0%} — the model is frequently "
                "skipping the BEGIN/END sentinel wrapper. That is the early signal of UI/model "
                "drift: check a recent answer file for truncation and consider re-testing the "
                "prompt template.\n")
    return 0


def week_report() -> int:
    """`stats --week`: the rolling-7-day budget, backlog, grading and policy picture. JSON on
    stdout, a short human digest on stderr — same shape as `stats`. See store_mod.week_view."""
    with store_mod.Store() as s:
        w = store_mod.week_view(s.db)
    print(json.dumps(w))
    b = w["budget"]
    sys.stderr.write(
        f"CGC_WEEK {b['spent']} potentially spent messages in the last 7 days — "
        + (f"{b['available']} of {b['weekly']} available" if b["weekly"] is not None
           else b["note"]) + f"; {w['useful']['outcomes']} useful outcome(s)"
        + (f" ({w['useful']['per_spent_message']}/message)" if w["useful"]["per_spent_message"]
           is not None else "") + ".\n")
    for r in w["ungraded"]:
        sys.stderr.write(f"CGC_WEEK ungraded {r['rid']}  {r['title']}\n")
    for r in w["backlog"]:
        sys.stderr.write(f"CGC_WEEK backlog {r['rid']}  not before {r['not_before']}  {r['title']}\n")
    for r in w["expired"]:
        sys.stderr.write(f"CGC_WEEK expired {r['rid']}  {r['title']}\n")
    sys.stderr.write("CGC_WEEK policy review " + ("done this week." if w["policy_update"]["happened"]
                                                  else "NOT done this week: feedback --policy \"...\"") + "\n")
    return 0


FEEDBACK_REASONS = ("useful", "decision-changed", "defect-caught", "duplicate", "late", "refuted",
                    "unused", "transport", "missing-source", "wrong")


def record_feedback(a) -> int:
    """`feedback`: grade one round's answer, or record the weekly policy review. LOCAL only — an
    event in the store, no network, no browser. Refuses what would corrupt the signal: an unknown
    rid, and a positive grade with no evidence (reassurance alone earns 0)."""
    def refuse(code, msg):
        sys.stderr.write(f"CGC_ERROR {code}: {msg}\n")
        return 2

    if a.policy is not None:
        if any(v is not None for v in (a.rid, a.grade, a.reason, a.action, a.evidence)):
            return refuse("feedback_mixed", "--policy records the weekly review and takes no other "
                                            "flag; grade a round in a separate call.")
        text = a.policy.strip()
        if not text:
            return refuse("policy_empty", "--policy needs the text of what changed and why.")
        import consult as _consult
        rev = _consult.policy_rev()
        with store_mod.Store() as s:
            s.record_policy_update(rev, text)
        envelope(code="ok", recorded="policy_update", policy_rev=rev)
        return 0

    missing = [f"--{n}" for n in ("rid", "grade", "reason", "action")
               if getattr(a, n) in (None, "")]
    if missing:
        return refuse("feedback_incomplete", f"{', '.join(missing)} required (or --policy \"...\").")
    evidence = (a.evidence or "").strip()
    if a.grade >= 1 and not evidence:
        return refuse("evidence_required", "a grade of 1 or 2 needs --evidence: a commit SHA, a test, "
                                           "a file, or a concrete acceptance judgment. If you cannot "
                                           "name one, the answer earned 0.")
    with store_mod.Store() as s:
        row = s.get_round(a.rid)
        if row is None:
            return refuse("no_such_round", f"{a.rid} is not in the store.")
        target = a.rid
        if row["kind"] == "retrieve":
            # A retrieve recovers ANOTHER round's answer, and the store links them through
            # parent_rid; the answer being judged is the source's.
            target = row.get("parent_rid") or _spec_of(row).get("parent_rid")
            if not target or s.get_round(target) is None:
                return refuse("retrieve_source_unknown",
                              f"{a.rid} is a retrieve whose source round is not in the store.")
        detail = {"grade": a.grade, "reason": a.reason, "action": a.action.strip()}
        if evidence:
            detail["evidence"] = evidence
        if target != a.rid:
            detail["via_retrieve"] = a.rid
        s.record_outcome(target, detail)
    envelope(rid=target, code="ok", recorded="consult_outcome", **detail)
    return 0


def _spec_of(row) -> dict:
    try:
        return json.loads(row.get("spec_json") or "{}")
    except ValueError:
        return {}


# ---- enqueue -----------------------------------------------------------------
def _spec_model_family(a) -> str:
    """The model family this round is PINNED to, resolved ONCE at enqueue and frozen into its spec.

    It used to be resolved nowhere: the queued spec carried `model` but no family, the daemon's
    child argv passed only `--model`, and cdp_consult read CGC_MODEL_FAMILY out of the DAEMON's
    ambient environment at send time. So a round was pinned to whatever the daemon's config said
    when it happened to run, not to what the caller had configured when they fired it — and a
    daemon config change between enqueue and send silently retargeted every queued round. Reading
    the environment HERE, in the caller's process, is what makes the family part of the request
    instead of part of the runtime.

    `a` may legitimately not carry the attribute (consult.py's `fire` builds its enqueue namespace
    without one); the env default then applies, which is the same value that CLI flag defaults to."""
    return getattr(a, "model_family", None) or os.environ.get("CGC_MODEL_FAMILY", "GPT-6")


def _spec_mentions(a) -> list:
    """ChatGPT apps to @mention before the prompt, frozen into the spec like the model: they route
    the request to a different tool, so they are part of the request, not of the runtime."""
    return [m for m in (getattr(a, "mentions", None) or []) if m]


# The caller's declared intent for a consult (fire --origin/--trigger/--decision/--baseline/--check).
# Local bookkeeping for the feedback loop, never rendered into the prompt. These five, and only when
# set, are part of a request's identity. policy_rev (derived from install state, not caller intent)
# and the schedule (use_by, not_before) deliberately are not: an idempotent retry must not conflict
# because SKILL.md was edited since, or because a relative `+2h` resolved a minute later.
_INTENT_KEYS = ("origin", "trigger", "decision", "baseline", "check")


def _spec_consult_meta(a) -> dict:
    """The round's consult_meta as frozen into spec_json; {} when the caller supplied none (a
    low-level enqueue), so such rounds' specs stay byte-identical to before."""
    return dict(getattr(a, "consult_meta", None) or {})


def _request_fingerprint(a, prompt: str) -> str:
    """The identity of one LOGICAL request — canonical JSON over every CALLER-side field that
    routes or shapes it: kind, the rid-independent prompt identity, project, model, and the
    caller's explicit thread selection (--parent / a concrete --conversation). Same key + a
    different ANY of these is a conflict, never a silently-returned old receipt.

    Caller-side fields only, deliberately not the enqueue-time RESOLVED conversation: resolution
    is derived state (and for a bare-auto followup it depends on what is in flight — including the
    original round a retry is trying to recover, which would deadlock the idempotent re-fire that
    is this feature's whole point). The prompt identity is --logical-sha (the prompt rendered with a
    literal <RID> placeholder, so user text containing a rid is never normalized away); enqueue
    REFUSES a --request-key without it, so a keyed round's fingerprint is always logical-sha-based.
    The raw-prompt hash below is only ever reached by a NON-keyed round, whose stored fingerprint is
    never compared — so it must NOT normalize the rid out of user text (the S2 defect: a literal rid
    in caller-supplied content could be erased and forge a false match)."""
    logical = getattr(a, "logical_sha", None) or store_mod.sha256(prompt or "")
    conv = getattr(a, "conversation", None)
    # model_family joins the fingerprint for exactly the reason `model` is already in it: it routes
    # the request to a different model, so the same key under a different family is a DIFFERENT
    # logical request and must CONFLICT rather than hand back the old round's receipt. fp stays 1:
    # the field's presence already changes every hash, and bumping the marker would only add a
    # second reason for the same break. Consequence, deliberate and one-time: a request-key fired
    # before this change and re-fired after it conflicts, because the stored fingerprint predates
    # the field. That is the honest report — the old round genuinely was not pinned to a family.
    fp = {
        "fp": 1, "kind": a.kind, "prompt": logical,
        "project_url": getattr(a, "project_url", None), "model": getattr(a, "model", "Pro"),
        "model_family": _spec_model_family(a),
        "parent": getattr(a, "parent", None),
        "conversation": None if conv in (None, "auto", "last") else conv,
    }
    if _spec_mentions(a):  # only when set, so every existing request-key keeps its fingerprint
        fp["mentions"] = _spec_mentions(a)
    intent = {k: v for k, v in _spec_consult_meta(a).items() if k in _INTENT_KEYS and v}
    if intent:  # likewise only when set
        fp["consult_meta"] = intent
    return store_mod.sha256(json.dumps(fp, sort_keys=True))


def _release_eligible(s, prior: dict) -> bool:
    """Does the store DURABLY prove the prior round's send never reached ChatGPT, so its request-key
    may move to a retry? Four sufficient proofs, and nothing else:
      - GATE_REJECTED — pre-send by construction (the gate runs while `ready`, before begin_send).
      - FAILED with no send attempt EVER — has_send_attempt is False, i.e. the round never entered
        `sending`, so nothing could have been sent (cancel-while-queued, no-thread, gate paths).
      - FAILED carrying a not-sent proof (send_disposition) — an operator's explicit reconcile, OR the
        CDP driver's own pre-click exit (mark_send_not_sent, exit 6: a paste/verify failure proven to
        precede the send click) recorded automatically, no operator step needed.
      - BLOCKED carrying a not-sent proof (send_disposition) — stamped automatically at both
        _NOT_SENT_BLOCK sites (login_needed/captcha/rate_limit/usage — blocked immediately, no
        retry) and by the retry-exhaustion path (_requeue_or_block, for _NOT_SENT_RETRY) — every one
        of them is a fail-closed pre-click stderr marker in the send-phase classifier, so exhausting
        the retry budget or blocking immediately is not new uncertainty, just how long a
        proven-not-sent condition gets retried (if at all) before a human is asked to look. A BLOCKED
        round WITHOUT the stamp — the one case left: a blocker hit post-send, while already `waiting`
        on an answer (_wait_phase's `code == 3` branch, a separate invocation from a confirmed send)
        — is NOT release-eligible: the stamp, not the state name, is what proves no-send.
    A FAILED or BLOCKED round that DID cross the send fence (an attempt exists) with no such proof is
    post-send-uncertain — possibly_accepted->failed / waiting->failed are legal reconciles that do
    NOT prove no-send — so its key stays owned. The state alone is never sufficient."""
    if prior["state"] == store_mod.GATE_REJECTED:
        return True
    if prior["state"] == store_mod.BLOCKED:
        return prior.get("send_disposition") == store_mod.NOT_SENT_PROVEN
    if prior["state"] != store_mod.FAILED:
        return False
    if prior.get("send_disposition") == store_mod.NOT_SENT_PROVEN:
        return True
    return not s.has_send_attempt(prior["rid"])


def _idempotent_receipt(s, prior: dict, fingerprint: str, rkey: str, out: str, a):
    """Decide what a repeated request-key means. FINGERPRINT FIRST, always: a different logical
    request under the same key is a conflict REGARDLESS of the prior's state — one key names one
    intent, and releasing a key before comparing (the S3 defect) let a different request reuse it
    after a failure. Rows predating the structured fingerprint carry none and so never match —
    conflict, the safe direction.

    With a MATCHING fingerprint:
      - a live/completed prior returns its ORIGINAL receipt (idempotent repeat);
      - a terminal prior (FAILED, GATE_REJECTED, or BLOCKED — all three are terminal, see
        cgc_store._LEGAL) releases its key to the retry ONLY when the store durably proves no send
        ever happened (_release_eligible); otherwise the key stays owned and the retry is refused as
        possibly-already-sent. BLOCKED is handled HERE, not as a live round: a BLOCKED prior is over,
        the human it is addressed to has not necessarily acted yet, and a caller re-firing the same
        key must get either the release or an explicit refusal — never a receipt pointing at a round
        that will never move again.

    Returns 0 (idempotent receipt printed), 2 (conflict), or prior['rid'] (str) — the row to
    transfer the key FROM into a fresh successor.

    On the 0 outcome the round that exists is the PRIOR one, not the one the caller just rendered
    (a re-fire mints a fresh rid, and nothing was queued under it). The caller's namespace `a` is
    therefore re-pointed at the matched round — rid and answer path — the same way cmd_enqueue
    re-points `a.rid` at the rid its prompt carries, so a caller that goes on to build an `await`
    from `a` names a row that exists."""
    pspec = json.loads(prior["spec_json"]) if prior.get("spec_json") else {}
    if pspec.get("request_fingerprint") != fingerprint:
        sys.stderr.write(f"CGC_ERROR request_key_conflict: request-key {rkey!r} was already "
                         f"used by {prior['rid']} with a DIFFERENT logical request (content, "
                         "kind, parent, conversation, project, or model differ). One key names "
                         "one logical request — use a new key.\n")
        return 2
    if prior["state"] in (store_mod.FAILED, store_mod.GATE_REJECTED, store_mod.BLOCKED):
        if _release_eligible(s, prior):
            sys.stderr.write(f"CGC_KEY_RELEASED request-key {rkey!r}: prior {prior['rid']} "
                             f"({prior['state']}) is durably proven not-sent — moving the key to a "
                             "fresh round.\n")
            return prior["rid"]  # caller performs the atomic transfer
        sys.stderr.write(
            f"CGC_ERROR request_key_locked: request-key {rkey!r} names {prior['rid']}, which is "
            f"terminal ({prior['state']}) but its send was never proven not to have reached ChatGPT "
            "(no not-sent proof recorded). Re-using the key could send a duplicate consult. "
            "Retrieve/reconcile that round — record a not-sent proof via `cgc_spool.py reconcile "
            f"--rid {prior['rid']} --not-sent --evidence \"...\"` if you have verified it never "
            "sent — or use a new key.\n")
        return 2
    global _COMMITTED
    a.rid, a.out = prior["rid"], prior["out_path"] or out
    _COMMITTED = prior["rid"]
    sys.stderr.write(f"CGC_IDEMPOTENT request-key {rkey!r} already enqueued as "
                     f"{prior['rid']} — returning the original receipt, nothing new "
                     "queued.\n")
    if getattr(a, "quiet", False):
        return 0   # composed into `fire`, which prints the one envelope
    envelope(rid=prior["rid"], state=prior["state"], code="ok",
             next_command=_cmd("cgc_spool.py", "await", "--rid", prior["rid"]),
             detail="idempotent repeat: the original receipt, nothing new queued",
             queued=True, out=prior["out_path"] or out, backend="store", idempotent_repeat=True)
    return 0


def _committed(rid: str) -> None:
    global _COMMITTED
    _COMMITTED = rid


def _post_create_receipt(a, out: str, queued: bool = True) -> int:
    """The shared tail after a round is created (fresh insert OR key transfer) — and after an
    idempotent repeat, where `queued=False`: the round was already there, so claiming it was queued
    would be a receipt that lies. Warns if no daemon will send it, then prints the one receipt
    unless composed into `fire` (quiet). Either way the caller's next step is the same `await`."""
    import cgc_spool as _spool
    if queued and not _spool.daemon_alive():
        sys.stderr.write("CGC_WARN daemon_down: queued, but nothing will send it until the daemon "
                         "runs. Relay ONE line to the user: cgc install-daemon\n")
    if getattr(a, "quiet", False):
        # Composed into `fire`, which prints the one receipt that matters. Two receipts for one
        # action is pure noise.
        return 0
    envelope(rid=a.rid, state=store_mod.QUEUED if queued else None, code="ok",
             next_command=_cmd("cgc_spool.py", "await", "--rid", a.rid),
             queued=queued, out=out, backend="store")
    argv = ["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cgc_spool.py"),
            "await", "--rid", a.rid]
    import shlex
    sys.stderr.write(f"{'CGC_QUEUED' if queued else 'CGC_ALREADY'} {a.rid} (store). Await it:\n"
                     f"  {' '.join(shlex.quote(x) for x in argv)}\n")
    return 0


def _enqueue_transfer(s, a, prompt: str, out: str, prior: dict, fingerprint: str, rkey: str) -> int:
    """Enqueue a same-fingerprint retry that inherits a released key from a proven-not-sent `prior`,
    atomically. Route PRESERVATION (audit req 5): the successor reuses the prior's already-RESOLVED
    conversation/parent rather than re-resolving — two same-fingerprint requests (e.g. a bare-auto
    followup, whose fingerprint excludes the resolved thread) must never diverge to different
    conversations because the 'last completed' thread moved between the two fires. The key transfer
    and successor insert are one transaction; a lost race re-resolves to the winner's receipt."""
    pspec = json.loads(prior["spec_json"]) if prior.get("spec_json") else {}
    inh_conv = pspec.get("conversation")
    inh_parent = pspec.get("parent_rid") or prior.get("parent_rid")
    spec = {"project_url": getattr(a, "project_url", None), "model": getattr(a, "model", "Pro"),
            "model_family": _spec_model_family(a), "mentions": _spec_mentions(a),
            "conversation": inh_conv, "parent_rid": inh_parent, "poll": getattr(a, "poll", None),
            "timeout": getattr(a, "timeout", None), "request_fingerprint": fingerprint}
    if _spec_consult_meta(a):
        spec["consult_meta"] = _spec_consult_meta(a)
    thread = inh_conv if inh_conv not in (None, "auto", "last") else None
    try:
        s.transfer_request_key(prior["rid"], rid=a.rid, kind=a.kind, thread_id=thread,
                               out_path=out, spec_json=json.dumps(spec), prompt=prompt,
                               request_key=rkey, parent_rid=inh_parent,
                               not_before=getattr(a, "not_before", None))
    except store_mod.RequestKeyRaced:
        # A concurrent retry moved the key first — re-resolve to the current owner (the winner) and
        # take its receipt, so exactly one successor ever owns the key.
        winner = s.round_by_request_key(rkey)
        if winner is None:
            raise
        res = _idempotent_receipt(s, winner, fingerprint, rkey, out, a)
        if isinstance(res, int):
            return res
        return _enqueue_transfer(s, a, prompt, out, winner, fingerprint, rkey)
    _committed(a.rid)
    return _post_create_receipt(a, out)


def enqueue_round(a, prompt: str, out: str) -> int:
    """Create a queued round from the CLI args. The prompt BYTES live on the round (no pathname to
    drift); project_url/model/conversation/poll/timeout ride in spec_json for the worker.

    `--request-key K` makes the enqueue LOGICALLY idempotent: repeating the same key with the same
    logical request returns the ORIGINAL round's receipt (an agent that lost the output of its
    first call can safely re-run it), while the same key with a different one is refused — one key,
    one request. RID uniqueness alone only prevents row collisions, not duplicate intents."""
    import sqlite3
    import cgc_spool as _spool
    conv = getattr(a, "conversation", None)
    parent = getattr(a, "parent", None)
    rkey = getattr(a, "request_key", None)
    # S3-B: an explicit --conversation must ALREADY be a canonical conversation id. A rid/URL/path
    # alias is resolvable, but it would be stored and locked verbatim while the --parent path resolves
    # the same thread to its canonical id and locks THAT — two lock files for one composer. Fail
    # closed (canonical-by-construction): reject the alias here, before it is stored, and name the fix.
    if conv not in (None, "auto", "last") and not _spool.is_canonical_conversation(conv):
        sys.stderr.write(
            "CGC_ERROR conversation_not_canonical: --conversation must be a bare ChatGPT conversation "
            f"id (uuid), got {conv!r}. To target a consult by its rid use --parent <rid>; otherwise "
            "pass the bare conversation id.\n")
        return 2
    # A request-key's idempotency identity is its fingerprint, whose prompt component is --logical-sha
    # (the placeholder render). Without it the low-level enqueue would fall back to hashing the raw
    # prompt — which cannot safely distinguish logical requests when the caller supplies the prompt
    # directly (S2). Refuse rather than key off an unsafe identity; the canonical fire/prep path
    # always passes --logical-sha.
    if rkey and not getattr(a, "logical_sha", None):
        sys.stderr.write(
            "CGC_ERROR request_key_needs_logical_sha: --request-key requires --logical-sha (the "
            "placeholder-rendered content identity from prep). Without it the request-key "
            "fingerprint cannot safely tell one logical request from another. Render via "
            "prep/fire, or pass --logical-sha.\n")
        return 2
    fingerprint = _request_fingerprint(a, prompt)
    with store_mod.Store() as s:
        # Writer-identity fence: if a daemon is LIVE, it must be operating THIS store. An old-code
        # daemon (no identity fields) or one holding a different DB (the pre-cutover /tmp file — the
        # recorded live split-brain) would accept this row into a store nobody serves, or serve a
        # store this row never reaches. Refuse now, in seconds, with the fix.
        import cgc_spool as _spool
        ident = _spool.daemon_identity()
        if ident is not None:
            my_path, my_uuid = os.path.abspath(store_mod.db_path()), s.store_uuid()
            if ident.get("db_path") != my_path or ident.get("store_uuid") != my_uuid:
                sys.stderr.write(
                    "CGC_ERROR store_mismatch: the running daemon is not serving this store "
                    f"(daemon: db={ident.get('db_path')!r} uuid={ident.get('store_uuid')!r} / "
                    f"CLI: db={my_path!r} uuid={my_uuid!r}). It is running old code or holding a "
                    "relocated-away DB. Restart it:\n"
                    "  launchctl kickstart -k gui/$(id -u)/com.open-claude-gpt.daemon\n")
                return 2
        if rkey:
            prior = s.round_by_request_key(rkey)
            if prior is not None:
                res = _idempotent_receipt(s, prior, fingerprint, rkey, out, a)
                if isinstance(res, int):
                    return res
                # res is the prior rid → move its released key onto this retry, atomically.
                return _enqueue_transfer(s, a, prompt, out, prior, fingerprint, rkey)
        existing = s.get_round(a.rid)
        if existing is not None:
            # A rid is minted per render, so re-enqueuing one is always a repeat of a request the
            # store already owns — and the caller's next move depends entirely on WHICH state it is
            # in. Saying only "already exists" left a retry loop with nothing to act on: the same
            # command failed the same way every time, and the round it was colliding with had
            # already answered. Name the state and the one command that follows from it.
            import cgc_spool as _spool
            st = existing["state"]
            # SAME rid + SAME bytes is the same request, so say so and SUCCEED. The caller's next
            # step (`await`) is what actually delivers the answer, and the canonical invocation
            # chains the two with `&&` — refusing here short-circuited that chain and left an
            # answer sitting completed in the store while the command failed identically on every
            # retry. Nothing is enqueued either way; this only decides whether the chain proceeds.
            # A DIFFERENT prompt under the same rid is a different request and still refuses.
            if existing["prompt_sha256"] == (store_mod.sha256(prompt) if prompt else None):
                sys.stderr.write(
                    f"CGC_IDEMPOTENT {a.rid} is already in the store ('{st}') with these exact "
                    "bytes — nothing new queued; await it for the answer.\n")
                _committed(a.rid)
                return _post_create_receipt(a, out, queued=False)
            if st in (store_mod.COMPLETED_VERIFIED, store_mod.COMPLETED_UNVERIFIED):
                nxt = (f"read its answer: {existing['out_path']}  (or re-materialize it: python3 "
                       f"{_spool.__file__} await --rid {a.rid})\n"
                       "  To ask something NEW on that thread, render a fresh round and continue it:\n"
                       f"  consult.py fire --followup --parent {a.rid} --task \"<...>\"")
            elif st in (store_mod.FAILED, store_mod.GATE_REJECTED, store_mod.BLOCKED):
                nxt = (f"it ended '{st}' ({existing['error_code'] or 'no detail'}) — render a FRESH "
                       "round (new rid) rather than re-enqueuing this one")
            elif st in store_mod.UNCERTAIN:
                nxt = ("its send may already have reached ChatGPT — retrieve it, never re-send: "
                       f"python3 {os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cdp_consult.py')}"
                       f" find-conversation --rid {a.rid}")
            else:
                nxt = (f"it is '{st}' and the daemon is working it: python3 {_spool.__file__} "
                       f"await --rid {a.rid}")
            sys.stderr.write(f"CGC_ERROR already_enqueued: {a.rid} is already in the store — {nxt}\n")
            return 2
        # Resolve the follow-up's thread NOW, while the agent's intent is fresh (pinning at enqueue,
        # not at process time, keeps a concurrent consult from stealing the selection). Prefer CAUSAL
        # identity — an explicit --parent <rid> resolves to THAT consult's conversation — over the
        # "last completed" heuristic, which under concurrency or near-simultaneous completions is
        # refused as ambiguous (latest_conversation_strict). Either way FAIL CLOSED: if nothing
        # resolves, refuse — never silently open a fresh conversation and lose the thread's context.
        if a.kind == "followup":
            if parent:
                conv = s.conversation_of(parent)
                if not conv:
                    sys.stderr.write(f"CGC_ERROR followup_parent_unresolved: --parent {parent} has no "
                                     "recorded conversation to continue.\n")
                    return 2
            elif conv in (None, "auto", "last"):
                conv, why = s.latest_conversation_strict(exclude_rid=a.rid)
                if not conv:
                    sys.stderr.write(f"CGC_ERROR followup_no_thread: {why}\n")
                    return 2
        # A retrieve round recovers ANOTHER round's answer read-only. It must pin THAT round's rid
        # (via --parent) as the source to verify against on the page — never resolve "whatever the
        # conversation's latest turn is" (auto), which silently adopts a later same-thread send's
        # answer if one lands before/during recovery (the causal-substitution bug this fixes).
        if a.kind == "retrieve":
            if not parent:
                sys.stderr.write(
                    "CGC_ERROR retrieve_needs_source_rid: --kind retrieve requires --parent <rid> (the "
                    "rid of the uncertain round being recovered) — the recovery path must never "
                    "auto-adopt the conversation's latest turn.\n")
                return 2
            # The store already knows the parent round's conversation — requiring the caller to
            # repeat it was a pure round-trip (the retrieve path exists precisely to recover a round,
            # and the store has the answer). An explicit --conversation still OVERRIDES (the caller
            # may know something the stored thread does not, e.g. it moved); resolve from the parent
            # only when none was given. The one case that genuinely cannot be resolved this way — the
            # parent has no recorded conversation — still refuses, naming the one command that can
            # actually find it.
            prow = s.get_round(parent)
            if prow is not None and prow["kind"] == "retrieve":
                # A retrieve sends nothing, so its rid was never on any page: whatever conversation is
                # named, the wait could only run out its window. The missing thing is on the page, not in
                # the store, so an explicit --conversation does not help.
                sys.stderr.write(
                    f"CGC_ERROR retrieve_parent_sends_nothing: --parent {parent} is itself a retrieve, "
                    "which sends nothing — there is no turn to recover. Retrieve the round that actually "
                    "sent (its own --parent chain leads to it).\n")
                return 2
            if prow is not None and conv in (None, "auto") and not prow.get("rendered_prompt"):
                # No stored prompt means no rid was ever recorded as sent from here, so with the
                # conversation coming FROM the store there is nothing to verify against. A round migrated
                # from the old spool (import_round writes none) is a legitimate retrieve target only when
                # the caller says where it lives: an explicit --conversation is honoured, as it is for a
                # never-sent parent below.
                sys.stderr.write(
                    f"CGC_ERROR retrieve_parent_sends_nothing: --parent {parent} has no stored prompt, so "
                    "the store cannot say which conversation its rid lives in. If it was sent (for example "
                    "migrated from the old spool), pass --conversation <id> explicitly.\n")
                return 2
            if prow is not None and conv in (None, "auto") and _release_eligible(s, prow):
                # The store durably proves the parent never sent (a stamped pre-click refusal, a
                # failure with no send attempt, or a gate rejection), so the page holds no answer and
                # the wait would only watch ten minutes for a turn that never existed. Only when the
                # conversation is being taken FROM the store: an explicit --conversation is the caller
                # asserting something the store cannot know (a send made out of band), and a retrieve
                # is read-only, so it is honoured rather than second-guessed.
                sys.stderr.write(
                    f"CGC_ERROR retrieve_parent_never_sent: --parent {parent} is durably proven NOT "
                    f"sent ({prow['state']}"
                    + ("" if prow["state"] == store_mod.GATE_REJECTED else
                       f", {prow.get('send_disposition') or 'no send attempt'}")
                    + "), so there is no answer on the page to retrieve — a retrieve would only watch "
                    "for a turn that never existed. "
                    + ("The gate refused it before any send: fix what it reported, then re-fire: "
                       if prow["state"] == store_mod.GATE_REJECTED else
                       "Re-fire it instead (nothing can duplicate): ")
                    + f"python3 {_spool.__file__} refire --rid {parent}\n"
                    "(If you know it was sent outside this store, pass --conversation <id> explicitly.)\n")
                return 2
            if conv in (None, "auto"):
                conv = s.conversation_of(parent)
                if not conv:
                    sys.stderr.write(
                        f"CGC_ERROR retrieve_parent_unresolved: --parent {parent} has no recorded "
                        "conversation to retrieve from. Find it first: python3 "
                        f"{os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cdp_consult.py')}"
                        f" find-conversation --rid {parent}\n")
                    return 2
        spec = {"project_url": getattr(a, "project_url", None), "model": getattr(a, "model", "Pro"),
                "model_family": _spec_model_family(a), "mentions": _spec_mentions(a),
                "conversation": conv, "parent_rid": parent, "poll": getattr(a, "poll", None),
                "timeout": getattr(a, "timeout", None), "request_fingerprint": fingerprint}
        if _spec_consult_meta(a):
            spec["consult_meta"] = _spec_consult_meta(a)
        thread = conv if conv not in (None, "auto", "last") else None
        try:
            s.create_round(a.rid, a.kind, thread_id=thread, out_path=out, prompt=prompt,
                           spec_json=json.dumps(spec), request_key=rkey, parent_rid=parent,
                           not_before=getattr(a, "not_before", None))
        except sqlite3.IntegrityError:
            # Two concurrent enqueues with the same key both saw "no prior row"; the unique index
            # on request_key made exactly one insert win. The loser resolves DETERMINISTICALLY to
            # the winner's receipt (or a conflict, or a proven-not-sent key transfer) instead of
            # surfacing a raw constraint error.
            prior = s.round_by_request_key(rkey) if rkey else None
            if prior is None:
                raise
            res = _idempotent_receipt(s, prior, fingerprint, rkey, out, a)
            if isinstance(res, int):
                return res
            # the racing winner had itself terminally failed proven-not-sent — its key is released;
            # move it onto this retry atomically (a second conflict is a real error and propagates).
            return _enqueue_transfer(s, a, prompt, out, prior, fingerprint, rkey)
        _committed(a.rid)
    return _post_create_receipt(a, out)


# ---- cancel ------------------------------------------------------------------
def cancel_round(rid: str) -> int:
    """Cancel a round BEFORE it sends. Only queued/ready are cancellable — past begin_send the click
    may have reached ChatGPT, and cancelling a possible send is indistinguishable from ignoring its
    answer, so it is refused with the states that explain themselves. Idempotent: cancelling an
    already-cancelled round is a no-op success."""
    with store_mod.Store() as s:
        r = s.get_round(rid)
        if r is None:
            sys.stderr.write(f"CGC_ERROR no_such_round: {rid}\n")
            return 2
        if r["state"] in (store_mod.FAILED,) and (r["error_code"] or "").startswith("cancelled"):
            sys.stderr.write(f"CGC_CANCELLED {rid} (already cancelled — idempotent no-op).\n")
            envelope(rid=rid, state=r["state"], code="ok", detail="already cancelled")
            return 0
        if r["state"] not in (store_mod.QUEUED, store_mod.READY):
            sys.stderr.write(
                f"CGC_ERROR not_cancellable: {rid} is '{r['state']}' — past the send fence, the "
                "click may already have reached ChatGPT, so cancel would not undo anything. Await "
                "it, or reconcile via `cgc queue --rid` if it is uncertain.\n")
            return 2
        try:
            s.set_state(rid, store_mod.FAILED, expect=r["state"],
                        error_code="cancelled by caller before send")
        except store_mod.IllegalTransition:
            sys.stderr.write(f"CGC_ERROR cancel_raced: {rid} changed state while cancelling — "
                             "it is being dispatched. Await it instead.\n")
            return 2
    sys.stderr.write(f"CGC_CANCELLED {rid}: cancelled before send.\n")
    envelope(rid=rid, state=store_mod.FAILED, code="ok", detail="cancelled before send")
    return 0


# ---- operator reconcile -------------------------------------------------------
def reconcile_not_sent(rid: str, evidence: str) -> int:
    """The operator act `Store.record_not_sent_proof` documents but nothing invoked: a human looked at
    ChatGPT (its history, an open tab `find-conversation` came up empty on) and confirmed a round's
    send never happened, and needs to record that proof durably so the round's request-key can
    release. Without this, `find-conversation`'s own miss path told the operator to "look before
    re-sending" and then left them nowhere to record what they saw — the round stayed
    possibly_accepted forever, re-triggering the daemon's HELD-tabs warning on every poll.
    Never raises: an unknown rid or an illegal-from-here round prints a CGC_ERROR line and returns
    non-zero, the same contract as cancel_round above."""
    with store_mod.Store() as s:
        row = s.get_round(rid)
        if row is None:
            sys.stderr.write(f"CGC_ERROR no_such_round: {rid}\n")
            return 2
        old_state = row["state"]
        try:
            s.record_not_sent_proof(rid, evidence)
        except store_mod.IllegalTransition as e:
            sys.stderr.write(f"CGC_ERROR not_sent_reconcile_refused: {e}\n")
            return 2
        new_row = s.get_round(rid)
    sys.stderr.write(
        f"CGC_RECONCILED {rid}: {old_state} -> {new_row['state']} "
        f"(send_disposition={new_row['send_disposition']}).\n")
    envelope(rid=rid, state=new_row["state"], code="ok",
             detail=f"{old_state} -> {new_row['state']} ({new_row['send_disposition']}); "
                    "`refire --rid` now replays it if still needed")
    return 0


# ---- operator refire -----------------------------------------------------------
def refire_round(old_rid: str) -> int:
    """Re-fire a proven-not-sent round under a fresh rid, replaying its exact stored prompt and spec.

    The operator cost this replaces: read `rendered_prompt` out of the DB, hand-substitute the old
    rid for a fresh one everywhere it appears (the prompt carries BEGIN_RESPONSE:<rid>/
    END_RESPONSE:<rid>), write a temp file, and re-enqueue with the original spec's project URL,
    model, and timeout copied by hand — done twice by hand against the live store in one session.

    REFUSES unless `_release_eligible` durably proves the prior never sent — the exact bar
    `_idempotent_receipt` already applies to a request-key retry, reused rather than reinvented: a
    round that MAY have sent must never be refired, which is the invariant the whole store is built
    around. The rewritten prompt is verified to carry the NEW rid and NONE of the old one before
    enqueueing — a rewrite that failed silently would enqueue a round whose sentinel never matches
    its own rid, an unconfirmable send by construction."""
    import cgc_spool as _spool
    with store_mod.Store() as s:
        prior = s.get_round(old_rid)
        if prior is None:
            sys.stderr.write(f"CGC_ERROR no_such_round: {old_rid}\n")
            return 2
        if not _release_eligible(s, prior):
            sys.stderr.write(
                f"CGC_ERROR refire_refused: {old_rid} ({prior['state']}) is not durably proven "
                "not-sent — refiring it could duplicate a consult that may have already reached "
                "ChatGPT. If you have independently verified it never sent, reconcile it first "
                f"(`cgc_spool.py reconcile --rid {old_rid} --not-sent --evidence \"...\"`), then "
                "refire again.\n")
            return 2
        succ = _refired_to(s, old_rid)
        if succ:
            # One refire per proven-unsent round: a second would send the same request twice.
            sys.stderr.write(f"CGC_ALREADY {old_rid} was refired as {succ}; await that round.\n")
            envelope(rid=succ, state=None, code="ok",
                     next_command=_cmd("cgc_spool.py", "await", "--rid", succ),
                     detail=f"{old_rid} already refired")
            return 0
        old_prompt = prior.get("rendered_prompt") or ""
        if not old_prompt:
            sys.stderr.write(f"CGC_ERROR refire_no_prompt: {old_rid} has no stored prompt to replay "
                             "(a retrieve round sends nothing — there is nothing to refire).\n")
            return 2
        pspec = json.loads(prior["spec_json"]) if prior.get("spec_json") else {}

    new_rid = _spool.new_rid()
    new_prompt = old_prompt.replace(old_rid, new_rid)
    if new_rid not in new_prompt or old_rid in new_prompt:
        sys.stderr.write(
            f"CGC_ERROR refire_rewrite_failed: substituting {old_rid} -> {new_rid} in the stored "
            "prompt did not produce a clean rewrite (the new rid is missing, or the old one is "
            "still present) — refusing to enqueue a round whose sentinel would not match its own "
            "rid.\n")
        return 2

    import types
    a = types.SimpleNamespace(
        rid=new_rid, kind=prior["kind"],
        project_url=pspec.get("project_url"), model=pspec.get("model", "Pro"),
        # INHERIT the recorded family, never re-read the environment: a refire replays a stored
        # request, and re-resolving from env would silently retarget it to today's config.
        model_family=pspec.get("model_family"), mentions=pspec.get("mentions") or [],
        conversation=pspec.get("conversation"),
        parent=pspec.get("parent_rid") or prior.get("parent_rid"),
        poll=pspec.get("poll"), timeout=pspec.get("timeout"),
        request_key=None, logical_sha=None, out=None, quiet=False,
        # The intent and policy_rev replay with the request. use_by does not: an expired round is
        # exactly what an operator refires, and the same deadline would expire it again.
        consult_meta={k: v for k, v in (pspec.get("consult_meta") or {}).items() if k != "use_by"})
    out = os.path.abspath(_spool.default_out(new_rid))
    sys.stderr.write(f"CGC_REFIRE {old_rid} -> {new_rid}: replaying its stored prompt as a fresh "
                     f"{prior['kind']} round.\n")
    rc = enqueue_round(a, new_prompt, out)
    if rc == 0:
        with store_mod.Store() as s:
            s.record_event("refired", rid=old_rid, detail=new_rid)
    return rc


def _refired_to(s, rid):
    """The rid this round was refired as, or None."""
    return s.refired_to(rid) if hasattr(s, "refired_to") else None


# ---- the agent envelope: one JSON line, one decision ------------------------------------------
# Every agent-path verb (fire, await, status, cancel, refire, feedback, reconcile, diagnose) ENDS
# stdout with exactly one JSON line led by ENVELOPE_KEYS — refusals and argument errors included
# (cgc_spool.main / consult.main guarantee the line on every exit). `code` is the action class; the
# detailed cause stays in `detail`/`error`, and `diagnose` holds the rest.
ENVELOPE_KEYS = ("schema", "rid", "state", "code", "next_command", "human_action", "answer_path",
                 "detail")
CODES = ("ok", "invalid_request", "policy_denied", "service_unavailable", "human_required",
         "ui_contract_broken", "send_uncertain", "source_unresolved", "remote_failed",
         "observation_deadline", "result_unverified", "not_found")

# THE table. A state's code when its error names nothing more specific (in-flight = ok: nothing has
# gone wrong yet). missing/stuck/error are await's synthetic outcomes.
STATE_CODE = {
    store_mod.QUEUED: "ok", store_mod.READY: "ok", store_mod.SENDING: "ok",
    store_mod.ACCEPTED: "ok", store_mod.WAITING: "ok", store_mod.COMPLETED_VERIFIED: "ok",
    store_mod.COMPLETED_UNVERIFIED: "result_unverified",
    store_mod.POSSIBLY_ACCEPTED: "send_uncertain",
    store_mod.BLOCKED: "human_required",
    store_mod.GATE_REJECTED: "policy_denied",
    store_mod.FAILED: "remote_failed",
    "missing": "not_found", "stuck": "observation_deadline", "error": "service_unavailable",
}
# An error's code by its leading name: every `CGC_ERROR <name>` the scripts write, plus the first
# words of the marker-free error_code texts the worker stores. service_unavailable = nothing went
# out and a later retry is safe; send_uncertain = it may have gone out.
_ERRORS = {
    "ok": "cancelled",
    "invalid_request": (
        "already_enqueued bad_rid bad_schedule cancel_raced conversation_not_canonical "
        "evidence_required fire_no_refs followup_no_thread followup_parent_unresolved "
        "need_conversation need_prompt_file no_code_source no_reconcile_action not_cancellable "
        "not_sent_reconcile_refused refire_no_prompt refire_rewrite_failed refs_source_conflict "
        "request_key_conflict request_key_needs_logical_sha retrieve_needs_source_rid "
        "retrieve_parent_never_sent retrieve_parent_sends_nothing rid_prompt_mismatch "
        "workspace_conflict workspace_needs_mention feedback_mixed policy_empty feedback_incomplete "
        "retrieve_source_unknown ambiguous_followup ambiguous_rid_in_prompt_file "
        "rid_not_found_in_prompt_file prompt_file_unreadable followup_no_input followup_prep_failed "
        "followup_prep_unparseable no_active_thread rid_autodetect_failed missing_dep usage "
        "retrieve_needs_an followup_has_no followup_conversation_is"),
    "policy_denied": "gate_secret gate_refused refused",
    "service_unavailable": (
        "store_mismatch attach_failed cdp_attach_failed new_tab new_tab_failed new_tab_no_ws no_page_target "
        "browser_unreachable chrome chrome_unavailable not_sent_preclick unverified daemon_down "
        "submit_failed_before followup_failed_before reconciled_not_sent"),
    "human_required": "login_needed captcha rate_limit approval_needed mention_not_found "
                      "request_key_locked attribution_mismatch",
    "ui_contract_broken": "contract_invalid selector_drift composer_not_ready model_not_selectable wrong_page "
                          "ui_contract",
    "send_uncertain": (
        "unknown_send wait_produced_no submit_did_not followup_send_failed daemon_died "
        "worker_exited daemon_startup periodic_sweep refire_refused "
        "chatgpt_failed_the accepted_but_no"),
    "source_unresolved": (
        "rid_absent rid_superseded rid_ambiguous ambiguous_rid ambiguous_target rid_mismatch "
        "rid_echo_mismatch rid_unresolved rid_not_on_any_open_tab conversation_mismatch "
        "conversation_not_found no_conversation_id retrieve_could_not retrieve_parent_unresolved"),
    "remote_failed": "turn_failed",
    "observation_deadline": "timeout_no_answer observation_deadline_passed expired",
    "not_found": "no_such_round",
}
ERROR_CODE = {name: code for code, names in _ERRORS.items() for name in names.split()}


def _error_words(error) -> list:
    """'CGC_ERROR rid_absent: …' -> ['rid','absent']; 'submit did not confirm …' -> its words."""
    text = re.sub(r"^\s*CGC_[A-Z]+\s+", "", str(error or "")).lower()
    return re.findall(r"[a-z]+", text.split(":")[0])[:6]


def code_of(state, error=None) -> str:
    """The one state/error -> code mapping: the error decides when it names a known cause (a
    `failed` round may be a failed turn, an unresolved source, or a deadline), else the state."""
    words = _error_words(error)
    for n in range(len(words), 0, -1):
        hit = ERROR_CODE.get("_".join(words[:n]))
        if hit:
            return hit
    return STATE_CODE.get(state, "service_unavailable")


_EMITTED = False
_LAST = None        # the envelope this invocation wrote — or, while _EMITTED is False, tried to
_IN_CLI = False     # inside agent_cli: one line per invocation is enforced
_COMMITTED = None   # the rid a create/transfer/idempotent match just made durable, this invocation


def envelope(**fields) -> dict:
    """Print THE agent envelope line: ENVELOPE_KEYS first (defaults filled, detail ≤200 chars), then
    any verb-specific extras. ASCII-escaped, so no stdout encoding can make the one line fail. A
    no-op once this invocation's line is out: there is never a second one."""
    global _LAST
    env = {"schema": 1, "rid": None, "state": None, "code": "ok", "next_command": None,
           "human_action": None, "answer_path": None, "detail": None}
    env.update(fields)
    if env["detail"] is not None:
        env["detail"] = str(env["detail"])[:200]
    if _EMITTED and _IN_CLI:
        return env
    _LAST = env   # kept even if the write fails, so the fallback re-sends THIS verdict
    with _no_sigint():   # an interrupt is delivered after the write, never inside it
        _write_line(_line(env) + "\n")
    return env


def _write_line(line: str) -> None:
    """THE emit primitive. `_EMITTED` means the line was written, not that a write was tried:
    it is set only once every byte is out, or as soon as any byte is (a partial line is never
    followed by a second one). A failure before the first byte leaves it unset, so a caller may
    retry. Writes straight to stdout's fd, in a loop for partial writes, after flushing what the
    text layer buffered; a stdout without an fd (a test capture) gets one text write."""
    global _EMITTED
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        fd = sys.stdout.fileno()
    except Exception:
        fd = None
    if fd is None:
        sys.stdout.write(line)   # raises before anything is stored, or stores it whole
        _EMITTED = True
        with contextlib.suppress(Exception):
            sys.stdout.flush()
        return
    data, done = line.encode(), 0
    try:
        while done < len(data):
            done += os.write(fd, data[done:])
    except BaseException:
        if not done:
            raise
    _EMITTED = True


def _line(env: dict) -> str:
    return json.dumps(env)


@contextlib.contextmanager
def _no_sigint():
    """Hold SIGINT off for the one envelope write, so an interrupt can neither cut the line nor
    land between "emitted" and the write (where it would leave zero lines or provoke a second)."""
    import signal
    mask = getattr(signal, "pthread_sigmask", None)
    if mask is None:
        yield
        return
    old = mask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        mask(signal.SIG_SETMASK, old)


def emitted() -> bool:
    return _EMITTED


class _StderrTap:
    """sys.stderr, remembering the last `CGC_ERROR`/`CGC_BLOCKER` line and the last line overall —
    what a verb that refused in prose meant, recovered for its envelope."""
    def __init__(self, inner):
        self.inner, self.err, self.last, self._buf = inner, None, None, ""

    def write(self, text):
        self._buf += text
        *lines, self._buf = self._buf.split("\n")
        for ln in lines:
            ln = ln.strip()
            if ln:
                self.last = ln
                if ln.startswith(("CGC_ERROR", "CGC_BLOCKER")):
                    self.err = ln
        return self.inner.write(text)

    def flush(self):
        return self.inner.flush()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def agent_cli(call, rid_of=lambda: None) -> int:
    """Run one agent verb so stdout ENDS with exactly one envelope line, whatever happens: a verb
    that printed its own envelope is left alone; a prose refusal, an argparse error or `--help`, a
    SystemExit message, a crash or an interrupt gets one synthesized (from its last CGC_ERROR line
    when there is one). The emitted flag is scoped to this invocation."""
    import traceback
    global _EMITTED, _COMMITTED, _LAST, _IN_CLI
    outer, _EMITTED, _COMMITTED, _LAST = _EMITTED, False, None, None
    outer_cli, _IN_CLI = _IN_CLI, True
    tap = sys.stderr = _StderrTap(sys.stderr)
    crashed = interrupted = False
    try:
        try:
            rc = call()
        except SystemExit as e:
            if isinstance(e.code, str):
                sys.stderr.write(e.code + "\n")
                rc = 1
            else:
                rc = e.code or 0
        except KeyboardInterrupt:
            interrupted, rc = True, 130
        except Exception as e:
            traceback.print_exc()
            tap.err, crashed, rc = f"{type(e).__name__}: {e}", True, 1
    finally:
        sys.stderr = tap.inner
    try:
        if _EMITTED:   # the line is out: a later failure never adds a second one or flips success
            return 0 if crashed and (_LAST or {}).get("code") == "ok" else rc
        rid = _COMMITTED or rid_of()
        for _ in range(3):
            try:
                if _EMITTED:
                    pass
                elif _LAST is not None:   # the verb's own verdict, whose write failed: send it
                    envelope(**_LAST)
                    if crashed:   # the verdict stands; only the write had failed
                        rc = 0 if _LAST.get("code") == "ok" else 1
                else:
                    _synthesize(rid, rc, tap, crashed, interrupted)
                break
            except KeyboardInterrupt:   # after the guarded write, or before it: emit if still owed
                interrupted, rc = True, 130
            except Exception as e:      # the envelope itself must not depend on anything failing
                tap.err, crashed = f"{type(e).__name__}: {e}", True
        return rc
    finally:
        _EMITTED, _IN_CLI = outer, outer_cli


def _synthesize(rid, rc, tap, crashed, interrupted):
    """The envelope for a verb that printed none."""
    if rid and rid == _COMMITTED:
        # The round is durably in the store: whatever failed came after, so the rid is the receipt.
        # Built from the rid alone — the failure may be the store itself.
        envelope(rid=rid, state=None, code="ok",
                 next_command=_cmd("cgc_spool.py", "await", "--rid", rid),
                 detail="the round is queued; this command failed after committing it: "
                        + str(tap.err or tap.last or "interrupted"))
    elif interrupted:
        # Only this observation stopped; the round (if any) lives on in the store.
        envelope(rid=rid, state="interrupted", code="observation_deadline",
                 detail="interrupted before an outcome; nothing about the round changed",
                 next_command=diagnose_cmd(rid) if rid else None)
    elif rc == 0:
        envelope(rid=rid, code="ok", detail=tap.last)
    else:
        why = tap.err or tap.last
        code = ("service_unavailable" if crashed else
                code_of(None, why) if tap.err else "invalid_request")
        envelope(rid=rid, state="refused", code=code, detail=why,
                 next_command=(diagnose_cmd(rid) if rid
                               and code not in ("not_found", "invalid_request") else None))


def _cmd(script: str, *args) -> str:
    """A copy-pasteable command line, shell-quoted (an install path may contain spaces)."""
    import shlex
    return shlex.join(["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), script),
                       *map(str, args)])


def diagnose_cmd(rid: str) -> str:
    return _cmd("cgc_spool.py", "diagnose", "--rid", rid)


def _retrieve_cmd(rid: str, conv) -> str:
    if conv:
        return _cmd("cgc_spool.py", "enqueue", "--kind", "retrieve", "--conversation", conv,
                    "--parent", rid)
    return _cmd("cdp_consult.py", "find-conversation", "--rid", rid)


def _answer_file(r: dict, out: str | None = None) -> str | None:
    """The answer file iff the round is COMPLETED and the file holds exactly the store's result —
    rebuilt from the store when it is missing or stale (the file is a derived view; the store is the
    authority). Never a path for a non-completed round: a leftover file there is not its answer."""
    import cgc_spool as _spool
    out = out or r.get("out_path") or os.path.abspath(_spool.default_out(r["rid"]))
    text = r.get("result_text") or ""
    if r["state"] not in _DONE_STATES or not out or not text:
        return None
    try:
        with open(out, encoding="utf-8") as f:
            if f.read() == text:
                return out
    except (OSError, ValueError):   # missing, unreadable, or not UTF-8: rebuild it
        pass
    try:
        _materialize(out, text)
    except OSError:
        return None
    return out


_DONE_STATES = (store_mod.COMPLETED_VERIFIED, store_mod.COMPLETED_UNVERIFIED)
_PROVIDER_FINAL = re.compile(r"^(?:CGC_ERROR )?turn_failed(?![A-Za-z0-9_])")
# auto_continue_state values after which a failed turn is final: never claimed, or an outcome known.
_CONTINUE_FINAL = (None, "landed", "not_sent")


def decide(s, r: dict, *, daemon_up: bool = True, out: str | None = None) -> dict:
    """THE controller decision for a round — await, diagnose and status all print this, so the three
    never disagree. From the store alone: {code, next_command, human_action, retryable,
    answer_path, confidence}. Rules, in order:

      - completed: classified from the result and attribution, never a stale error_code. The answer
        path is returned only when the file matches the store's result (rebuilt if not).
      - in flight: re-arm await (daemon down → service_unavailable); a gated queued round → probe.
      - a retrieve sends nothing: recovery is always for its SOURCE round, never the observer.
      - proven not sent (_release_eligible) → refire. The ONLY path to a re-send.
      - ChatGPT declared the turn dead (turn_failed, after the one auto-continue) → a new fire.
      - anything else crossed the send fence without proof → retrieve, never re-send."""
    rid, state, kind = r["rid"], r["state"], r.get("kind")
    err = r.get("error_code")
    rearm = _cmd("cgc_spool.py", "await", "--rid", rid)
    d = {"code": code_of(state, err), "next_command": None, "human_action": None,
         "retryable": False, "answer_path": None, "confidence": None}
    if state in _DONE_STATES:
        d["code"] = STATE_CODE[state]
        d["answer_path"] = _answer_file(r, out)
        if d["answer_path"] is None:
            d.update(code="result_unverified", next_command=rearm,
                     human_action="the stored result is empty or could not be written; a human "
                                  "checks the ChatGPT thread")
            return d
        if state == store_mod.COMPLETED_UNVERIFIED:
            d.update(confidence="unverified",
                     human_action="verify the salvaged answer is complete and belongs to this round")
        else:
            d["confidence"] = "verified"
            if r.get("attribution") == store_mod.ATTR_MISMATCHED:
                d.update(code="human_required",
                         human_action=f"answer produced by {r.get('model_slug')!r}, which "
                                      f"{_ATTR_GATE_ENV} does not admit — judge it yourself before "
                                      "using it")
        return d
    gate = s.gated_by(r) if state == store_mod.QUEUED else {}
    if gate:
        d.update(code="ui_contract_broken", next_command=_cmd("cgc_probe.py", "--status"),
                 human_action=paused_text(gate), retryable=True)
        return d
    if state not in store_mod.TERMINAL and state != store_mod.POSSIBLY_ACCEPTED:
        d.update(code="ok" if daemon_up else "service_unavailable", next_command=rearm,
                 retryable=not daemon_up,
                 human_action=None if daemon_up else "run `cgc install-daemon` (the daemon is not "
                                                     "running)")
        return d
    if d["code"] == "ok":   # cancelled before send
        return d
    if kind == "retrieve":
        src = r.get("parent_rid") or _spec_of(r).get("parent_rid")
        if not src:
            d["human_action"] = "this retrieve names no source round; nothing to recover"
            return d
        src_conv = s.conversation_of(src) or s.conversation_of(rid)
        if state == store_mod.BLOCKED:
            d.update(next_command=_retrieve_cmd(src, src_conv),
                     human_action="clear the blocker in the ChatGPT window, then run next_command")
        else:
            d.update(next_command=_cmd("cdp_consult.py", "find-conversation", "--rid", src),
                     human_action="the source turn was not confirmed; check the ChatGPT window, "
                                  "never re-send")
        return d
    succ = _refired_to(s, rid)
    if succ:
        d.update(code="ok", next_command=_cmd("cgc_spool.py", "await", "--rid", succ),
                 human_action=f"refired as {succ}; await that round")
        return d
    if _release_eligible(s, r):
        if d["code"] == "policy_denied" or not r.get("rendered_prompt"):
            d["human_action"] = ("remove what the gate refused (`detail`), then fire again"
                                 if d["code"] == "policy_denied" else "nothing was sent; fire again")
            d["retryable"] = str(err or "").startswith("unverified:")
            return d
        why = ("fix the UI (`cgc probe` must pass)" if d["code"] == "ui_contract_broken" else
               "fix the cause in `detail`" if state == store_mod.BLOCKED else "if still needed")
        d.update(next_command=_cmd("cgc_spool.py", "refire", "--rid", rid), retryable=True,
                 human_action=f"nothing was sent: {why}, then run next_command")
        return d
    if (state == store_mod.FAILED and _PROVIDER_FINAL.match(str(err or ""))
            and s.auto_continue_state(rid) in _CONTINUE_FINAL):
        d.update(code="remote_failed",
                 human_action="ChatGPT failed the turn: re-fire with a NEW --request-key if still "
                              "needed")
        return d
    d.update(next_command=_retrieve_cmd(rid, s.conversation_of(rid)),
             human_action="the send may have landed: check the ChatGPT window; retrieve, never "
                          "re-send")
    if state == store_mod.FAILED and d["code"] == "remote_failed":
        d["code"] = "send_uncertain"
    return d


DIAGNOSE_MAX = 1500
_PHASE_EVENTS = ("created", "claimed", "begin_send", "accepted")


def _last_marker(path) -> str | None:
    """The log's last CGC_ERROR / CGC_BLOCKER line — the driver's own verdict — from its tail."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    hits = [ln.strip() for ln in lines if ln.startswith(("CGC_ERROR", "CGC_BLOCKER"))]
    return hits[-1] if hits else None


def _incident_bundle(rid: str, since) -> str | None:
    """The newest incidents/<id>/diagnosis.json (under the state or data dir) that names this rid or
    was written after its claim. Optional: no such dir means no bundle."""
    import glob
    import cgc_spool as _spool
    try:
        since_ts = datetime.datetime.fromisoformat(since).timestamp() if since else None
    except ValueError:
        since_ts = None
    best = None
    for root in {_spool.CGC_STATE_DIR, store_mod.data_dir()}:
        for p in glob.glob(os.path.join(root, "incidents", "*", "diagnosis.json")):
            try:
                mt = os.path.getmtime(p)
                hit = since_ts is not None and mt >= since_ts
                if not hit and os.path.getsize(p) < 262144:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        hit = rid in f.read()
            except OSError:
                continue
            if hit and (best is None or mt > best[0]):
                best = (mt, p)
    return best[1] if best else None


def _fit(env: dict, limit: int) -> dict:
    """Shrink `env` until its printed line is ≤ limit bytes: prose shortens, optional diagnostics
    drop. Machine fields (rid, code, next_command, answer_path, ...) are never altered — a cut path
    names no file. If they alone outgrow the limit (paths of ~600+ characters), the line is printed
    anyway with `truncated: true`: the bound covers everything except the decision itself."""
    size = lambda: len(_line(env).encode())   # noqa: E731
    for key, cap in (("last_marker", 160), ("human_action", 160), ("detail", 120),
                     ("last_marker", 60), ("detail", 60)):
        if size() <= limit:
            return env
        if env.get(key):
            env[key] = env[key][:cap]
    if size() > limit and env.get("ui", {}).get("broken"):
        env["ui"] = {"needs": env["ui"]["needs"], "broken": {k: "" for k in env["ui"]["broken"]}}
    for key in ("send", "last_marker", "phases", "ui", "bundle", "conversation", "kind"):
        if size() <= limit:
            return env
        env.pop(key, None)
    for key in ("detail", "human_action"):
        if size() > limit and env.get(key):
            env[key] = env[key][:max(0, len(env[key]) - (size() - limit))] or None
    if size() > limit:
        env["truncated"] = True
    return env


def diagnose(rid: str) -> int:
    """`diagnose --rid R`: what an agent needs to decide on a stuck or failed round, in one line of
    at most DIAGNOSE_MAX bytes, so it never reads the log or the driver. Read-only (apart from
    rebuilding a completed round's missing answer file from the store)."""
    import cgc_spool as _spool
    with store_mod.Store() as s:
        r = s.get_round(rid)
        if r is None:
            envelope(rid=rid, state="missing", code="not_found", detail="no such round in the store")
            return 1
        d = decide(s, r, daemon_up=_spool.daemon_alive())
        evs = s.events(rid)
        needs = list(store_mod.round_needs(r.get("kind") or "", r.get("spec_json")))
        broken = {k: (v.get("cause") or "")[:80] for k, v in s.gated_by(r).items()}
        conv = s.conversation_of(rid)
        attempts = s.attempt_count(rid)
        send = s.send_evidence(rid) or {}
    phases, claimed = {}, None
    for e in evs:
        if e["kind"] in _PHASE_EVENTS and (e["kind"] != "created" or "created" not in phases):
            phases[e["kind"]] = e["at"][:19]
            claimed = e["at"] if e["kind"] == "claimed" else claimed
        elif e["kind"] == "state" and "-> waiting" in (e.get("detail") or ""):
            phases["waiting"] = e["at"][:19]
    if r["state"] in store_mod.TERMINAL or r["state"] == store_mod.POSSIBLY_ACCEPTED:
        phases["finished"] = r["updated_at"][:19]
    bundle = _incident_bundle(rid, claimed)
    if bundle and d["code"] == "ui_contract_broken":
        d["human_action"] = f"{d['human_action'] or 'the UI contract is broken'}; see bundle"
    env = {"schema": 1, "rid": rid, "state": r["state"], "code": d["code"],
           "next_command": d["next_command"], "human_action": d["human_action"],
           "answer_path": d["answer_path"],
           "detail": None if r["state"] in _DONE_STATES else r.get("error_code"),
           "retryable": d["retryable"], "kind": r["kind"], "attempts": attempts,
           "conversation": conv, "answer_on_disk": d["answer_path"] is not None,
           "last_marker": _last_marker(_spool.log_path(rid)), "phases": phases,
           "ui": {"needs": needs, "broken": broken}, "bundle": bundle,
           "send": {k: str(send[k])[:60] for k in ("submitted", "reason", "phase") if k in send}
           or None}
    envelope(**_fit(env, DIAGNOSE_MAX))
    return 0


# ---- the outcome envelope ----------------------------------------------------
# Every await exit prints ONE machine-readable JSON line on stdout (prose stays on stderr): an
# agent should never have to infer state by scraping prose. schema=1 is the envelope's version.
def _emit(rid, state, *, retryable, human_action=None, next_command=None, answer_path=None,
          parent_rid=None, confidence=None, error=None, source_rid=None, observed_rid=None,
          model_badge=None, model_slug=None, attribution=None, code=None):
    import cgc_spool as _spool
    code = code or code_of(state, error)
    # A non-success outcome that names no move of its own points at `diagnose`, which decides from
    # the store (and the log's last verdict) so the agent never reads the log itself. A completed
    # answer is self-explanatory, and so is a rid the store has never seen.
    if next_command is None and code != "ok" and not answer_path and state != "missing":
        next_command = diagnose_cmd(rid)
    # The driver's typed outcome for this round's last send attempt, read here so EVERY envelope
    # carries it (None: no attempt, or one that predates typed outcomes).
    try:
        with store_mod.Store() as _s:
            send = _s.send_evidence(rid)
    except Exception:
        send = None
    # Envelope invariant: retryable=true must always name the retry — an agent told "retryable"
    # with no next_command/human_action has a verdict but no move. Enforced structurally so no
    # future branch can violate it.
    if retryable and not (next_command or human_action):
        human_action = "re-run the SAME fire (same --request-key) — the failure was transient"
    log = _spool.log_path(rid)
    try:
        has_log = os.path.exists(log)
    except OSError:
        has_log = False
    envelope(**{
        "rid": rid, "state": state, "code": code, "next_command": next_command,
        "human_action": human_action, "answer_path": answer_path, "detail": error,
        "parent_rid": parent_rid, "retryable": retryable, "log_path": log if has_log else None,
        "confidence": confidence, "error": error,
        # source_rid: for a retrieve round, the rid of the round it recovers (transparency — a
        # retrieve's own rid is fresh and means nothing on its own). observed_rid: on a rid-mismatch
        # failure, the rid the CDP layer actually found on the page instead, so a human/agent can
        # see WHAT it refused to substitute, not just that it refused.
        "source_rid": source_rid, "observed_rid": observed_rid,
        # THREE separate model facts, never collapsed (SHARED CONTRACT #4 in cdp_consult.py):
        #   model_badge — what the composer's switcher read BEFORE the send. Selection evidence.
        #   model_slug  — data-message-model-slug off the answer's own node. Producer attribution.
        #                 null = the provider stamped nothing; it is NOT a mismatch.
        #   attribution — the verdict recorded when the round completed: matched / mismatched /
        #                 unknown / unchecked, or null for a round that predates this field.
        # `confidence` remains strictly about the rid sentinel and says nothing about identity.
        "model_badge": model_badge, "model_slug": model_slug, "attribution": attribution,
        # send: the driver's typed outcome for the round's last send attempt (_send_evidence) —
        # submitted (true / false / null = unknown), reason, phase, drift, the contract it observed.
        "send": send,
    })


# ---- the UI contract: typed send outcomes and the capability gate -------------------------------
def driver_rev() -> str:
    """Which driver build produced a capability verdict: sha256[:12] of cdp_consult.py."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cdp_consult.py"), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        return "unknown"


_EVIDENCE_KEYS = ("submitted", "reason", "phase", "drift", "blocker", "adapter", "contract",
                  "conversation_id", "echoedRid", "wanted", "wantedFamily", "detail", "readyState")


def _send_evidence(res: dict) -> dict:
    """The driver's typed line for one send (its last stdout JSON), bounded: the named fields only,
    each string cut to 300 chars, plus the exit code and the CGC_ERROR line."""
    rec = res.get("outcome") or {}
    ev = {k: (v[:300] if isinstance(v, str) else v) for k in _EVIDENCE_KEYS
          if (v := rec.get(k)) not in (None, "") or (k == "submitted" and k in rec)}  # null = unknown
    ev["exit"] = res.get("code")
    err = _stderr_error_line(res.get("stderr") or "")
    if err:
        ev["error"] = err[:300]
    bundle = re.search(r"^CGC_DRIFT_BUNDLE \w+: (\S+/diagnosis\.json)", res.get("stderr") or "", re.M)
    if bundle:                              # the driver's drift-repair bundle (cgc_drift) for this send
        ev["diagnosis"] = bundle.group(1)
        sys.stderr.write(f"CGC_UI_DIAGNOSIS {ev.get('reason') or '?'}: {bundle.group(1)}\n")
    return ev


def _project_send(store, attempt: str, rid: str, res: dict) -> str | None:
    """Record the send's typed outcome on its attempt, and mark `reader` broken when the driver says
    the send landed and its reply is unreadable (selector_drift on a page showing no login, challenge
    or rate limit — the driver's `drift`). That is the one failure a real round may turn into a gate,
    because it is the one that costs a message: every further send would spend another into the same
    blind reader. A pre-click composer or mention failure costs nothing and is ambiguous from one
    round (a deleted project, a stale frozen family, an unconnected app), so only the probe — which
    checks the configured project, model and app — records those capabilities.

    Called AFTER the round's durable state and address are written, and fail-soft: a diagnostics
    failure is logged and swallowed, so it can never move a round or lose its conversation."""
    try:
        return _project_send_unchecked(store, attempt, rid, res)
    except Exception as e:
        sys.stderr.write(f"CGC_WARN {rid}: send outcome not recorded ({type(e).__name__}: {e})\n")
        return None


def _project_send_unchecked(store, attempt, rid, res):
    ev = _send_evidence(res)
    store.record_attempt_evidence(attempt, json.dumps(ev, sort_keys=True))
    cap = ev.get("drift")
    if cap != "reader":
        return None
    cause = ev.get("error") or ev.get("reason") or "driver reported drift"
    # Only gate what a probe can re-prove. With no finished round to read, no probe could ever clear
    # this, and every later send would wait behind it forever — so the drift is recorded for diagnosis
    # and the gate is left open; the next round that lands either proves the reader or breaks it.
    import cgc_probe
    if cgc_probe.pick_reader_round(store) is None:
        store.record_event("ui_drift_ungated", rid=rid,
                           detail=json.dumps({cap: cause, "why": "no reader fixture"}))
        sys.stderr.write(f"CGC_UI_DRIFT {cap}: {cause} (round {rid}) — NOT gated: no finished round "
                         "exists for a probe to re-prove the reader on.\n")
        return None
    store.mark_broken(cap, phase=f"send:{ev.get('phase') or '?'}", cause=cause,
                      ref=rid, driver_rev=driver_rev())
    sys.stderr.write(f"CGC_UI_BROKEN {cap}: {ev.get('error') or ev.get('reason')} (round {rid}) — "
                     "rounds that need it stay queued until a probe passes.\n")
    return cap


def paused_text(gate: dict) -> str:
    """The human_action of a round held by the gate."""
    why = "; ".join(f"{k}: {(v.get('cause') or v.get('phase') or 'broken')[:200]}"
                    for k, v in sorted(gate.items()))
    return (f"ChatGPT UI changed ({why}) — sends paused; driver needs a fix; queued rounds are intact. "
            "`cgc probe` re-checks it and resumes them")


# How long `await` lets a gate stand before it reports the pause: one probe run, which a break
# schedules at once (unless one ran in the last 30 min), takes about two minutes.
GATE_GRACE_S = 180


_OBSERVED_RID_RE = re.compile(r"observed_rid=(\S+)")


def _parse_observed_rid(error_code: str | None) -> str | None:
    """Pull the 'observed_rid=<rid>' token cdp_consult embeds in a retrieve's rid_absent/
    rid_superseded stderr line (see cdp_consult._locate_source_turn) back out of the error_code
    the round stored it under, for the outcome envelope. 'none' means the CDP layer itself found
    nothing to report."""
    if not error_code:
        return None
    m = _OBSERVED_RID_RE.search(error_code)
    if not m or m.group(1) in ("none", "None"):
        return None
    return m.group(1)


def _stderr_error_line(stderr: str) -> str | None:
    """The last CGC_ERROR line in a CDP subprocess's stderr — the actionable verdict (rid_absent,
    rid_superseded, ...), not the CGC_WAIT heartbeat noise around it. None if the subprocess never
    printed one (e.g. a plain timeout)."""
    lines = [ln for ln in (stderr or "").splitlines() if ln.startswith("CGC_ERROR")]
    return lines[-1] if lines else None


# ---- await -------------------------------------------------------------------
# Only a SENTINEL-VERIFIED answer is automatic success. completed_unverified (the model skipped the
# BEGIN/END_RESPONSE:<rid> wrapper, so the answer was salvaged by size/position) is NOT auto-success:
# the diff-review showed its salvage can attribute a DIFFERENT round's answer to this rid under
# concurrent same-thread sends, and handing automation a silent "success" there can corrupt
# downstream autonomous work. Unverified answers are materialized for a human, but await returns
# review-required rather than success.
_TERMINAL_OK = (store_mod.COMPLETED_VERIFIED,)


def await_round(a) -> int:
    """The public await entry. EVERY termination — including a store that cannot open
    (SchemaTooNew, a corrupt file) or an answer that cannot materialize (disk full) — emits exactly
    one JSON envelope on stdout; the traceback goes to stderr. 'Every await exit has an envelope'
    is a contract, not a happy-path property."""
    global _EMITTED
    before, _EMITTED = _EMITTED, False
    try:
        return _await_round(a)
    except Exception as e:
        import traceback
        traceback.print_exc()
        if _EMITTED:   # the outcome is already out; a late failure never re-reports it
            return 0 if (_LAST or {}).get("code") == "ok" else 1
        try:
            _emit(a.rid, "error", retryable=False,
                  human_action="await itself failed before reaching a round outcome — read the "
                               "traceback on stderr",
                  error=f"{type(e).__name__}: {e}")
        except Exception:
            if _EMITTED:   # _emit got its line out before failing
                return 1
            envelope(rid=getattr(a, "rid", None), state="error", code="service_unavailable",
                     human_action="read the traceback on stderr", detail=f"{type(e).__name__}: {e}",
                     retryable=False, error=f"{type(e).__name__}: {e}")
        return 1
    finally:
        _EMITTED = _EMITTED or before


def _await_round(a) -> int:
    """Poll the round until there is something to act on, then print `decide`'s verdict — the same
    one diagnose and status print — and route the exit code from it (0 answer / 3 a human must act /
    1 broken). This function only decides WHEN to stop watching: at a terminal or uncertain state,
    when a dead daemon outlasts its grace, or when the UI gate has held the round past a probe run.

    Liveness is checked in EVERY non-terminal state: the round only advances while a daemon is
    alive to advance it, so a dead daemon must surface promptly — not as 90 silent minutes of
    polling a row nobody is working. "Still running" is never an outcome."""
    import cgc_spool as _spool
    deadline = time.time() + a.timeout
    down_since = gated_since = None
    while time.time() < deadline:
        with store_mod.Store() as s:
            r = s.get_round(a.rid)
            if r is None:
                sys.stderr.write(f"CGC_BROKEN {a.rid}: no such round in the store.\n")
                _emit(a.rid, "missing", retryable=False, error="no such round in the store")
                return 1
            state = r["state"]
            settled = state in store_mod.TERMINAL or state == store_mod.POSSIBLY_ACCEPTED
            up = _spool.daemon_alive()
            stop = settled
            if not settled:
                if up:
                    down_since = None
                elif down_since is None:
                    down_since = time.time()
                    sys.stderr.write("CGC_WAIT no live daemon; holding briefly in case it is "
                                     "restarting…\n")
                elif time.time() - down_since > _spool.DAEMON_GRACE_S:
                    stop = True
                gate = s.gated_by(r) if state == store_mod.QUEUED else {}
                if not gate:
                    gated_since = None
                elif gated_since is None:
                    gated_since = time.time()
                elif time.time() - gated_since >= GATE_GRACE_S:
                    stop = True
            if stop:
                # The round's own address (chosen at creation) unless --out asks for a second copy.
                d = decide(s, r, daemon_up=up or not settled and down_since is None,
                           out=os.path.abspath(a.out) if getattr(a, "out", None) else None)
        if stop:
            return _report(a.rid, r, d)
        time.sleep(getattr(a, "poll", None) or 20)
    sys.stderr.write(f"CGC_STUCK {a.rid}: no terminal state within {a.timeout}s — run: "
                     f"{diagnose_cmd(a.rid)}\n")
    _emit(a.rid, "stuck", retryable=False, error=f"no terminal state within {a.timeout}s",
          human_action="the consult is broken, not slow — run next_command (diagnose)")
    return 1


def _report(rid: str, r: dict, d: dict) -> int:
    """Print `decide`'s verdict as the await envelope plus one prose line on stderr, and map it to
    the exit code: 0 a usable answer, 3 a human must act (or may act: an uncertain send, a review),
    1 broken."""
    state, err = r["state"], r.get("error_code")
    src = r.get("parent_rid") if r.get("kind") == "retrieve" else None
    answer = d["answer_path"]
    if answer and d["code"] == "ok":
        sys.stderr.write(
            f"CGC_DONE {rid}: answer ready ({len((r.get('result_text') or '').encode())} bytes). "
            f"READ IT AT:\n  {answer}\n"
            + (f"Producer attribution: {r.get('model_slug')} (selection evidence: "
               f"{r.get('model_badge') or 'none'}).\n" if r.get("model_slug") else
               "Producer attribution: UNKNOWN — the provider stamped no model on this answer.\n")
            + "This round is closed. Read the answer and decide from it alone whether anything "
              "further is warranted.\n")
        rc = 0
    else:
        tag = {"human_required": "CGC_BLOCKER", "send_uncertain": "CGC_UNCERTAIN",
               "result_unverified": "CGC_REVIEW_REQUIRED",
               "ui_contract_broken": "CGC_PAUSED"}.get(d["code"], "CGC_BROKEN")
        if state == store_mod.BLOCKED:
            tag += (" (pre-send) Nothing was sent; Do NOT retrieve, there is no answer to find —"
                    if d["retryable"] else " (post-send) the send may have landed; never re-send —")
        sys.stderr.write(f"{tag} {rid}: {err or d['human_action'] or state}\n"
                         + (f"  next: {d['next_command']}\n" if d["next_command"] else "")
                         + (f"  human: {d['human_action']}\n" if d["human_action"] else ""))
        broken = (state in (store_mod.FAILED, store_mod.GATE_REJECTED)
                  or d["code"] == "service_unavailable"
                  or (d["code"] == "result_unverified" and not answer))
        rc = 1 if broken else 3
    _emit(rid, state, retryable=d["retryable"], parent_rid=r.get("parent_rid"),
          human_action=d["human_action"], next_command=d["next_command"], answer_path=answer,
          confidence=d["confidence"], error=err if state not in _DONE_STATES else None,
          source_rid=src, observed_rid=_parse_observed_rid(err) if src else None,
          model_badge=r.get("model_badge"), model_slug=r.get("model_slug"),
          attribution=r.get("attribution"), code=d["code"])
    return rc


def _materialize(out: str, text: str) -> None:
    """Write the store's authoritative result to the derived answer file atomically (0600). The file
    is a rematerializable view of the store, so 'done but file missing' is repairable, not a
    contradiction.

    The temp is UNIQUE per call (mkstemp, 0600, same directory so the replace stays atomic): the
    producer and an `await` can materialize the same round at once, and a shared `<out>.tmp` let one
    writer truncate the file the other was about to rename into place."""
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=os.path.basename(out) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, out)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def _publish_answer(store, rid: str, text: str) -> None:
    """Write a just-completed answer to the address the round has been promising since it was created.

    THE PRODUCER MATERIALIZES. The answer file used to be written only by `await`, which made the
    artifact's existence depend on a notifier being alive at the instant the daemon committed the
    round — and it is not: a detached waiter was stopped, the daemon finished the round seconds
    later, and the answer existed only inside SQLite while the caller polled the promised path
    forever. Nothing was lost (the store is the authority) but nothing arrived either, and the path
    every receipt had printed was a promise no writer was keeping.

    The file stays a derived view, so a failed write must never fail a round whose answer is already
    durably committed: log it and leave it for `await`/`status`, which still repair on demand."""
    import cgc_spool as _spool
    try:
        r = store.get_round(rid)
        out = (r or {}).get("out_path") or _spool.default_out(rid)
        _materialize(out, text)
        sys.stderr.write(f"CGC_ANSWER {rid} ({len(text.encode('utf-8'))} bytes) -> {out}\n")
    except Exception as e:
        sys.stderr.write(f"CGC_WARN materialize_failed {rid}: {type(e).__name__}: {e} — the answer "
                         "IS committed in the store; `await --rid` writes it on demand.\n")


# ---- the worker --------------------------------------------------------------
_expired = store_mod.past
_use_by = store_mod.use_by_of


def _use_by_passed(spec: dict) -> bool:
    return store_mod.past(_use_by(spec))


def _expire_if_due(store, rid: str, spec: dict) -> bool:
    """`fire --use-by T`: a round whose answer is useless after T never sends after it. Fails a READY
    round past its deadline and returns True. Called at entry AND again immediately before each
    begin_send, because the gate's gh calls and the conversation-lease wait both sit in between and
    can run past T. Every call site is strictly pre-send (no attempts row yet), so the failure is
    pre-send by construction: stamped not_sent_proven, which keeps it out of the message ledger and
    releases its request-key."""
    use_by = _use_by(spec)
    if not (use_by and _expired(use_by)):
        return False
    fields = store_mod._expired_fields(use_by)
    store.set_state(rid, fields.pop("state"), expect=store_mod.READY, **fields)
    return True


def process_round(store, r: dict, run_cdp, *, daemon_instance_id: str, validate) -> str:
    """Drive one claimed (`ready`) round to a terminal or uncertain state. `run_cdp` is injected (the
    daemon wraps cdp_consult subprocesses; tests stub it); `validate(prompt) -> (ok, reason)` is the
    egress gate. Returns the final round state (for logging/tests)."""
    rid = r["rid"]
    prompt = r["rendered_prompt"] or ""
    spec = json.loads(r["spec_json"]) if r.get("spec_json") else {}

    # The one dispatch point every send passes through (a fresh claim, a gate-backoff requeue, a
    # ready-orphan); re-checked right before each begin_send below.
    if _expire_if_due(store, rid, spec):
        return store_mod.FAILED

    # retrieve: attach to an existing conversation and wait — no gate, no send (nothing leaves).
    # It pins the SOURCE round's rid (spec["parent_rid"], from --parent at enqueue) and hands it to
    # the waiter as the exact turn to verify + extract. Auto-adopting whatever the conversation's
    # LATEST turn happens to be (the earlier design here) silently commits a later same-thread
    # send's answer as this recovery's result if one lands before/during it — per-rid leases don't
    # serialize a conversation, and an explicit-parent caller can legitimately fire more than one
    # round on the same thread. Fail closed instead: no source_rid, no retrieve.
    if r["kind"] == "retrieve":
        conv = spec.get("conversation")
        if not conv or conv == "auto":
            store.finish(rid, store_mod.FAILED, error_code="retrieve needs an explicit conversation id")
            return store_mod.FAILED
        source_rid = spec.get("parent_rid") or r.get("parent_rid")
        if not source_rid:
            store.finish(rid, store_mod.FAILED,
                        error_code="retrieve needs an explicit source_rid (--parent <rid>) — the "
                                  "recovery path must never auto-adopt the conversation's latest turn")
            return store_mod.FAILED
        store.set_state(rid, store_mod.WAITING, expect=store_mod.READY)
        return _wait_phase(store, rid, conv, spec, run_cdp, wait_rid=source_rid, is_retrieve=True)

    # followup: CONTINUE the same thread — attach to its conversation and send there, never open a
    # new one (a fresh submit would silently lose the thread's context, the worst kind of bug).
    if r["kind"] == "followup":
        conv = spec.get("conversation")
        if not conv or conv in ("auto", "last"):
            # enqueue pins a concrete conversation (from --parent, or the last completed thread).
            # Only fall back to THIS round's own pinned thread — never a process-time GLOBAL resolve:
            # the diff-review flagged that a global "latest completed" at process time can attach to
            # an unrelated conversation that finished after enqueue. Ambiguity refuses, never guesses.
            conv = _thread_conv(store, r)
        if not conv:
            store.finish(rid, store_mod.FAILED,
                         error_code="followup has no pinned thread to continue — pass --parent <rid> "
                                    "or --conversation <id>")
            return store_mod.FAILED
        import cgc_spool as _spool
        # S3-B worker boundary: a pre-release queued row may carry a rid-shaped conversation. The lock
        # key MUST be canonical; a non-canonical resolved conversation is a PRE-SEND terminal failure
        # (nothing has touched the browser here), never a mis-keyed lock or a send.
        if not _spool.is_canonical_conversation(conv):
            store.finish(rid, store_mod.FAILED,
                         error_code=(f"followup conversation is not a canonical id ({conv!r}) — "
                                     "re-enqueue with --parent <rid> or a bare conversation id"))
            return store_mod.FAILED
        gated = _gate(store, rid, prompt, validate)
        if gated is not None:
            return gated
        # Per-conversation exclusive mutator lease. The gate above is store-only (no browser), so the
        # mutating region is exactly begin_send + the follow-up CDP subprocess (attach -> clear ->
        # paste -> verify -> click -> landing-rid). Acquire AFTER the conversation is concrete and
        # BEFORE that subprocess; hold until it returns. A second same-conversation follow-up cannot
        # enter this region — it refuses (the round is still READY, redispatched later) rather than
        # racing W1 onto the one shared composer and corrupting the pre-click not-sent proof.
        conv_lease = _spool.acquire_conversation_lease(conv)
        if conv_lease is None:
            raise ConversationLeaseRefused(conv)
        try:
            if _expire_if_due(store, rid, spec):   # the lease may have been contended for a while
                return store_mod.FAILED
            attempt = store.begin_send(rid, prompt, store_mod.sha256(prompt),
                                       daemon_instance_id=daemon_instance_id)
            # SEND only (not send+wait): so the round reaches `waiting` promptly and is reattach-able on
            # a restart, exactly like submit. A combined followup --watch would leave it `sending` for
            # the whole ~25-min answer, where a restart would strand it as possibly_accepted.
            # model/model_family come from the SPEC, frozen at enqueue — not from the daemon's
            # ambient environment, which is what a queued round used to be re-pinned by at send
            # time. The followup path passed NEITHER before, so both are named here.
            send = run_cdp("followup", rid=rid, conversation=conv, prompt=prompt,
                           model=spec.get("model") or "Pro",
                           model_family=spec.get("model_family") or "GPT-6",
                           mentions=spec.get("mentions") or [], not_after=_use_by(spec))
        finally:
            if hasattr(conv_lease, "close"):
                conv_lease.close()  # mutating region done — the read-only wait below needs no lease
        if send.get("code") == 0:
            # same thread, confirmed — with the composer reading it selected before this send
            store.mark_accepted(attempt, conv, model_badge=send.get("model_badge"))
            store.mark_waiting(rid)
            _project_send(store, attempt, rid, send)    # after the state + address; fail-soft
            return _wait_phase(store, rid, conv, spec, run_cdp, may_continue=True)
        # Every other outcome: its durable state FIRST, then the driver's typed outcome (the
        # `finally`, after whichever branch ran). _project_send is diagnostics and fail-soft, so it
        # can never move the round or lose its address.
        try:
            if send.get("code") == _EXIT_NOT_SENT_PRECLICK:
                store.mark_send_not_sent(attempt, f"followup failed before the click (exit "
                                                  f"{_EXIT_NOT_SENT_PRECLICK}): {(send.get('stderr') or '').strip()[:300]}")
                return store_mod.FAILED
            if _not_sent_marker(send, _NOT_SENT_BLOCK):
                # Same proof shape as _NOT_SENT_RETRY below (module docstring, _NOT_SENT_BLOCK comment
                # above): login_needed/captcha/rate_limit fire from the preflight probe or
                # _composer_failure, both strictly before the click, in this send-phase classifier only
                # (the wait-phase blocker at _wait_phase's `code == 3` branch is a DIFFERENT invocation
                # and is deliberately never stamped — that one follows a confirmed send). Stamped so an
                # expired-login BLOCKED round — the most common real case of this class — can release
                # its request-key once the human logs back in, instead of the model_not_selectable trap
                # this round fixed for _NOT_SENT_RETRY recurring here.
                store.set_state(rid, store_mod.BLOCKED, expect=store_mod.SENDING,
                                send_disposition=store_mod.NOT_SENT_PROVEN,
                                error_code=_block_code(send.get("stderr")))
                return store_mod.BLOCKED
            if _not_sent_marker(send, _NOT_SENT_RETRY):
                # Same evidence, same verdict as a submit's. These markers are emitted FAIL-CLOSED
                # before the click, so the follow-up provably did not send and re-queuing duplicates
                # nothing. Omitting this check here (the submit branch always had it) filed a proven
                # not-sent as UNCERTAIN, which is the costliest possible misfiling: it blocks the free
                # automatic retry, burns a full auto-retrieve and then a manual one hunting an answer
                # that was never asked for, and leaves a human staring at "may have sent" with only a
                # resend left to try — under exactly the uncertainty the invariant exists to prevent.
                return _requeue_or_block(store, rid, _not_sent_marker(send, _NOT_SENT_RETRY))
            store.mark_possibly_accepted(attempt, f"followup send failed (exit {send.get('code')}"
                                                  f"{_why_unconfirmed(send)}) — retrieve, don't resend")
            return store_mod.POSSIBLY_ACCEPTED
        finally:
            _project_send(store, attempt, rid, send)

    gated = _gate(store, rid, prompt, validate)
    if gated is not None:
        return gated

    if _expire_if_due(store, rid, spec):           # the gate's gh calls may have run past use-by
        return store_mod.FAILED
    attempt = store.begin_send(rid, prompt, store_mod.sha256(prompt),
                               daemon_instance_id=daemon_instance_id)
    sub = run_cdp("submit", rid=rid, prompt=prompt,
                  project_url=spec.get("project_url") or "https://chatgpt.com/",
                  model=spec.get("model") or "Pro",
                  model_family=spec.get("model_family") or "GPT-6",
                  mentions=spec.get("mentions") or [], not_after=_use_by(spec))
    conv = sub.get("conversation")

    if conv and sub.get("code") == 0:
        store.mark_accepted(attempt, conv, model_badge=sub.get("model_badge"))
        store.mark_waiting(rid)
        _project_send(store, attempt, rid, sub)        # after the state + address; fail-soft
        return _wait_phase(store, rid, conv, spec, run_cdp, may_continue=True)

    # Every other outcome: its durable state and address FIRST, then the driver's typed outcome (the
    # `finally`). _project_send is diagnostics and fail-soft: it can never move the round or lose
    # the conversation that makes an uncertain send retrievable.
    try:
        if sub.get("code") == _EXIT_NOT_SENT_PRECLICK:
            store.mark_send_not_sent(attempt, f"submit failed before the click (exit "
                                              f"{_EXIT_NOT_SENT_PRECLICK}): {(sub.get('stderr') or '').strip()[:300]}")
            return store_mod.FAILED

        # The click did not confirm. Distinguish PROVEN-not-sent from uncertain.
        if _not_sent_marker(sub, _NOT_SENT_BLOCK):
            # Both call sites of this branch run in the send-phase classifier (expect=SENDING, against
            # stderr _run scopes to this one invocation) — login_needed/captcha are raised before the
            # composer is even populated, so this is pre-click proof exactly like _NOT_SENT_RETRY
            # below, and gets the same stamp. (`"usage"` also matches argparse's own `usage:` banner
            # on a CLI misuse of cdp_consult.py — still a pre-click condition, so the stamp is correct
            # there too; noted here so it doesn't read as an accidental substring match.)
            store.set_state(rid, store_mod.BLOCKED, expect=store_mod.SENDING,
                            send_disposition=store_mod.NOT_SENT_PROVEN,
                            error_code=_block_code(sub.get("stderr")))
            return store_mod.BLOCKED
        if _not_sent_marker(sub, _NOT_SENT_RETRY):
            # provably not sent → safe to re-queue (this is NOT resending a possible send), bounded
            return _requeue_or_block(store, rid, _not_sent_marker(sub, _NOT_SENT_RETRY))
        # UNCERTAIN. A conversation reported by a FAILING submit is an address, not a confirmation
        # (the driver reports one whenever the post-click tab sits in a thread, including a reused tab
        # it was already in), so the state stays possibly_accepted — but recording it is what makes
        # this round retrievable read-only (recover()'s `retrievable` bucket, verified by its own
        # END_RESPONSE sentinel) instead of the dead end that forced a human to copy the answer out of
        # the window by hand. Linked ONLY here: on the proven-not-sent branches above the round is
        # re-queued, and a thread pinned from a send that never happened would follow it into its
        # next attempt.
        if conv:
            store.link_conversation(rid, conv)
        store.mark_possibly_accepted(
            attempt,
            f"submit did not confirm the send (exit {sub.get('code')}{_why_unconfirmed(sub)})"
            + (f"; conversation {conv} recorded for read-only retrieval" if conv else
               "; no conversation captured"))
        return store_mod.POSSIBLY_ACCEPTED
    finally:
        _project_send(store, attempt, rid, sub)


def _why_unconfirmed(res: dict) -> str:
    """', <reason>: submitted=<true|false|unknown>' from the driver's typed line, so the uncertain
    round's error names what the driver saw (selector_drift: sent, unreadable; unknown_send: no rid
    seen) instead of only an exit code. Empty when the driver printed no typed line."""
    rec = res.get("outcome") or {}
    if not rec.get("reason"):
        return ""
    sub = {True: "true", False: "false"}.get(rec.get("submitted"), "unknown")
    return f", {rec['reason']}: submitted={sub}"


def resume_round(store, r: dict, run_cdp) -> str:
    """Reattach to a round and resume polling its existing conversation — the store peer of the
    spool's orphan recovery. It sends NOTHING — not the prompt, and not the automatic "continue" either:
    that is a new paid message, and only the original send worker's first wait may spend it (recovery
    has no send authority; a failed turn found here ends FAILED, telling the agent to re-fire). Attach
    + wait is read-only; the waiter's rid-sentinel check means it completes only if THIS round's own
    answer is on the thread, and it watches only what is left of the send's observation deadline.
    Handles three inputs:
      - accepted / waiting          — a worker died mid-poll; resume it.
      - possibly_accepted + conv    — a ONE-SHOT auto-retrieve (recover() only offers these once): the
                                      send may have landed; read-only attach recovers it if so, and
                                      falls back to uncertain (for a human) if the sentinel is absent.
    A round with no addressable conversation cannot be resumed and is left uncertain."""
    rid = r["rid"]
    spec = json.loads(r["spec_json"]) if r.get("spec_json") else {}
    conv = store.conversation_of(rid)
    if not conv:
        # no conversation recorded → we cannot address it; do NOT resend, leave uncertain.
        if r["state"] != store_mod.POSSIBLY_ACCEPTED:
            store.set_state(rid, store_mod.POSSIBLY_ACCEPTED,
                            error_code="accepted but no conversation to reattach — retrieve manually")
        return store_mod.POSSIBLY_ACCEPTED
    if r["state"] == store_mod.POSSIBLY_ACCEPTED:
        # Claim the one-shot auto-retrieve ATOMICALLY (marker + possibly_accepted->waiting in one
        # transaction). If we lose the claim (already consumed, or no longer eligible), leave it for
        # a human rather than racing another worker onto the same read-only retrieve.
        if not store.claim_auto_retrieve(rid):
            return store_mod.POSSIBLY_ACCEPTED
        return _wait_phase(store, rid, conv, spec, run_cdp, final_look=True)
    if r["state"] == store_mod.ACCEPTED:
        store.mark_waiting(rid)
    return _wait_phase(store, rid, conv, spec, run_cdp)


def _thread_conv(store, r) -> str | None:
    tid = r.get("thread_id")
    if not tid:
        return None
    row = store.db.execute("SELECT conversation_id FROM threads WHERE thread_id=?", (tid,)).fetchone()
    return row["conversation_id"] if row else None


def _read_answer(path) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _no_live_worker(rid: str) -> bool:
    """Is nobody else working this round? The retrieve holds only its OWN rid lease, so the round it
    is about to resolve is unfenced: the daemon's one-shot auto-retrieve can be mid-wait on the very
    round a human-enqueued retrieve just recovered. Terminalising it under that worker turns its own
    `finish` into an IllegalTransition traceback — the data stays right, the log looks like
    corruption. Probing the lease is what makes the two paths mutually exclusive."""
    try:
        import cgc_spool as _spool
        return _spool.rid_lease_free(rid)
    except Exception:
        return False   # cannot prove it is free → do not touch it


# ---- THE attribution gate (one gate, one place) -------------------------------
# The policy is an explicit allow-list of PRODUCER SLUGS, CGC_MODEL_SLUG, matched case-insensitively
# with fnmatch globs (e.g. "gpt-6-*,gpt-5-6-pro"). Empty (the default) means the slug is recorded
# and reported but nothing judges it — `unchecked`.
#
# It is deliberately NOT derived from CGC_MODEL / CGC_MODEL_FAMILY. Those are the composer's UI
# labels ("Pro", "GPT-6", "GPT-5.6 Sol"); a slug is the provider's own identifier ("gpt-6-pro",
# "gpt-5-6-thinking"). Nothing local knows the mapping between them, OpenAI changes both sides
# without notice, and a label->slug table would be a rot machine that fabricates a verdict from a
# guess — which is the exact failure this whole change exists to remove. So the installation
# declares which producers it accepts, or it declares nothing and gets an honest `unchecked`.
_ATTR_GATE_ENV = "CGC_MODEL_SLUG"


def _attribution_policy() -> list[str]:
    raw = os.environ.get(_ATTR_GATE_ENV, "") or ""
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def _attribution_verdict(slug):
    """(verdict, detail) for one round's producer attribution. detail is None unless a human needs
    a sentence. Never raises, and ABSENCE is never a failure: a consult that otherwise succeeded
    must not be broken by the provider declining to stamp its own answer."""
    import fnmatch
    policy = _attribution_policy()
    slug = (slug or "").strip()
    if not slug:
        return store_mod.ATTR_UNKNOWN, (
            "the answer node carried no data-message-model-slug — which model served this answer "
            "is unknown (not wrong, not missing an answer: simply not stated by the provider)")
    if not policy:
        return store_mod.ATTR_UNCHECKED, None
    low = slug.lower()
    if any(fnmatch.fnmatchcase(low, pat) for pat in policy):
        return store_mod.ATTR_MATCHED, None
    return store_mod.ATTR_MISMATCHED, (
        f"the provider attributes this answer to {slug!r}, which no {_ATTR_GATE_ENV} pattern "
        f"({', '.join(policy)}) admits — the answer is intact and its rid sentinel verified, but "
        "it was NOT produced by a model this installation accepts")


RETRIEVE_IDLE_EXIT_S = 180


def _spool_stuck_after() -> int:
    import cgc_spool as _spool
    return _spool.STUCK_AFTER_S
CONTINUE_LEASE_WAIT_S = 60


def _continue_prompt(rid: str) -> str:
    """The one automatic retry after ChatGPT marks a round's turn failed. It carries the round's OWN
    rid in the standard wrapper, so the continue turn is that rid's latest canonical turn and the
    round's wait reads the answer from it (the "legitimate resend" _locate_source_turn keeps)."""
    from consult import RESPONSE_WRAP
    return ("Continuing this consult.\n\n"
            "Your reply to my previous message failed before it produced an answer. Please answer "
            "that same request now, in full.\n\n" + RESPONSE_WRAP.format(rid=rid))


def _newer_send(store, rid, conv):
    """A submit/followup round in the same thread, created after `rid`, that has reached (or passed)
    begin_send. Its turn would sit after this round's, making "answer that same request" ambiguous."""
    return store.db.execute(
        "SELECT r.rid FROM rounds r WHERE r.thread_id=? AND r.rid<>? "
        "AND r.kind IN ('submit','followup') "
        "AND r.created_at>(SELECT created_at FROM rounds WHERE rid=?) "
        "AND EXISTS (SELECT 1 FROM attempts a WHERE a.rid=r.rid) LIMIT 1",
        (conv, rid, rid)).fetchone()


def _past_deadline(rid, deadline, where) -> bool:
    """A continue whose answer nobody has time left to watch is a message paid for nothing."""
    if time.time() < deadline:
        return False
    sys.stderr.write(f"CGC_WARN {rid}: auto-continue skipped — the observation deadline passed {where}.\n")
    return True


def _click_not_after(spec, deadline) -> str:
    """The instant the continue's click must not pass: the earlier of the round's use-by and its
    observation deadline. The driver re-checks it at the click, so a slow attach/paste that crosses
    the deadline ends provably not sent (its pre-click exit) instead of sending unwatched."""
    obs = datetime.datetime.fromtimestamp(deadline, datetime.timezone.utc)
    try:
        use_by = datetime.datetime.fromisoformat(_use_by(spec))
        if use_by.tzinfo is None:
            use_by = use_by.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return obs.isoformat()
    return min(use_by, obs).isoformat()


def _auto_continue(store, rid, conv, spec, run_cdp, deadline) -> str:
    """Send the one automatic continue for a round whose turn ChatGPT marked failed. Returns (and,
    once claimed, durably records) the outcome:
      "landed"   the continue provably landed — re-wait on the same rid;
      "not_sent" nothing left, provably (checks refused, no lease, or a fail-closed pre-click exit);
      "unsure"   the click may have happened (timeout, crash, unconfirmed echo)."""
    import cgc_spool as _spool
    prompt = _continue_prompt(rid)
    if _past_deadline(rid, deadline, "before it was considered"):
        return "not_sent"
    if _use_by_passed(spec):
        # The answer is worthless past use-by, and a continue is a second paid message for it.
        # Declined BEFORE the claim, so no auto_continue event exists and nothing is charged; the
        # round then ends turn_failed exactly as any declined continue does (see _wait_phase).
        sys.stderr.write(f"CGC_WARN {rid}: auto-continue skipped — use-by {_use_by(spec)} passed.\n")
        return "not_sent"
    if not _spool.is_canonical_conversation(conv):
        return "not_sent"
    ok, why = _spool.validate_prompt(prompt)   # the same egress gate every send passes
    if not ok:
        sys.stderr.write(f"CGC_WARN {rid}: auto-continue refused by the egress gate ({why}).\n")
        return "not_sent"
    # "Answer that same request" must mean THIS round's request. If another round has been sent into
    # the thread since, a continue would be read as a retry of THAT one, yet wrapped in this rid and
    # verified as this round's answer. Refuse rather than guess.
    newer = _newer_send(store, rid, conv)
    if newer:
        sys.stderr.write(f"CGC_WARN {rid}: auto-continue skipped — {newer['rid']} was sent into "
                         f"{conv} after it, so 'answer that same request' would be ambiguous.\n")
        return "not_sent"
    if _past_deadline(rid, deadline, "while the continue was validated"):
        return "not_sent"                             # before the claim: nothing spent
    if not store.claim_auto_continue(rid, store_mod.sha256(prompt)):
        return "not_sent"                             # already spent — at most one per round
    outcome = _send_continue(store, rid, conv, spec, run_cdp, prompt, deadline)
    store.record_auto_continue(rid, outcome)
    return outcome


def _send_continue(store, rid, conv, spec, run_cdp, prompt, deadline) -> str:
    import cgc_spool as _spool
    lease, end = None, min(time.time() + CONTINUE_LEASE_WAIT_S, deadline)
    while lease is None and time.time() < end:
        lease = _spool.acquire_conversation_lease(conv)
        if lease is None:
            time.sleep(2)
    if lease is None:
        sys.stderr.write(f"CGC_WARN {rid}: auto-continue skipped — another send held conversation "
                         f"{conv} until the lease wait or the observation deadline ran out.\n")
        return "not_sent"
    try:
        if _past_deadline(rid, deadline, "while waiting for the conversation"):
            return "not_sent"
        if _use_by_passed(spec):                 # the lease wait (up to 60 s) may have crossed it
            sys.stderr.write(f"CGC_WARN {rid}: auto-continue skipped — use-by {_use_by(spec)} "
                             "passed while waiting for the conversation.\n")
            return "not_sent"
        newer = _newer_send(store, rid, conv)    # again: a sibling may have sent while we waited
        if newer:
            sys.stderr.write(f"CGC_WARN {rid}: auto-continue skipped — {newer['rid']} was sent into "
                             f"{conv} while waiting for the conversation.\n")
            return "not_sent"
        try:
            sent = run_cdp("followup", rid=rid, conversation=conv, prompt=prompt,
                           model=spec.get("model") or "Pro",
                           model_family=spec.get("model_family") or "GPT-6",
                           mentions=spec.get("mentions") or [],
                           not_after=_click_not_after(spec, deadline))
        except Exception as e:                        # noqa: BLE001 — a crash may follow the click
            sys.stderr.write(f"CGC_WARN {rid}: auto-continue crashed ({type(e).__name__}: {e}).\n")
            return "unsure"
    finally:
        if hasattr(lease, "close"):
            lease.close()
    if sent.get("code") != 0:
        proven = (sent.get("code") == _EXIT_NOT_SENT_PRECLICK
                  or _not_sent_marker(sent, _NOT_SENT_BLOCK + _NOT_SENT_RETRY) is not None)
        sys.stderr.write(f"CGC_WARN {rid}: auto-continue did not confirm (exit {sent.get('code')}, "
                         f"{'provably not sent' if proven else 'may have been sent'}).\n")
        return "not_sent" if proven else "unsure"
    sys.stderr.write(f"CGC_CONTINUED {rid}: ChatGPT failed the turn; sent one continue in {conv}.\n")
    return "landed"


def _wait_phase(store, rid, conv, spec, run_cdp, wait_rid=None, *, is_retrieve=False,
                may_continue=False, final_look=False) -> str:
    """Wait for the round's answer and commit what the waiter saw.

    `may_continue` is SEND AUTHORITY, and only the worker that just sent the round holds it: a failed
    turn there earns the one automatic continue. Every other caller — a reattach, the auto-retrieve,
    a retrieve — is recovery, and recovery is read-only, so it defaults off.

    The budget is the send's, not this call's: the round's observation_deadline (begin_send + its
    timeout) bounds this wait and the continue's re-wait alike, so neither a restart nor a continue
    can extend it. At the deadline the round ends uncertain (retrieve, don't resend), never extended.
    The one exception is `final_look`, the one-shot auto-retrieve: past the deadline it still gets ONE
    short read-only look (RETRIEVE_IDLE_EXIT_S, idle-bounded), because its whole purpose is the answer
    that finished while nobody was watching — the waiter that died at minute 89. One-shot by its event
    marker, so the bound is budget + RETRIEVE_IDLE_EXIT_S, once."""
    out_tmp = os.path.join(store_mod.CGC_STATE_DIR, f"_wait_{rid}.txt")
    # Clear any stale answer + .raw sidecar from a PRIOR wait on this rid (e.g. a timed-out first
    # wait, before an auto-retrieve). _wait_phase decides completed_verified vs _unverified purely
    # from the .raw sidecar's existence, so a leftover .raw would downgrade a later CLEAN sentinel
    # answer to unverified (diff-review S2). Start each wait from a blank slate.
    for _p in (out_tmp, out_tmp + ".raw"):
        try:
            os.remove(_p)
        except OSError:
            pass
    # A recovery (retrieve) looks at a turn sent long ago, so an idle turn there is final, not a
    # thinking pause; bound it instead of holding one of the daemon's few worker slots for the full
    # 90-minute budget (three stuck retrieves starved a new send for ~40 min, 2026-09-25).
    budget = int(spec.get("timeout") or _spool_stuck_after())
    deadline = (None if is_retrieve else store.observation_deadline(rid, budget)) \
        or time.time() + budget

    # The remainder is read ONCE per wait: the value checked is the value passed. Reading the clock
    # twice let a 1 s check become a 0 s argv, which the driver used to read as "no budget".
    left = int(deadline - time.time())
    last_look = final_look and left < RETRIEVE_IDLE_EXIT_S
    if left <= 0 and not last_look:
        return _deadline_passed(store, rid)
    res = run_cdp("wait", rid=wait_rid or rid, conversation=conv, out=out_tmp,
                  poll=spec.get("poll"), timeout=RETRIEVE_IDLE_EXIT_S if last_look else left,
                  idle_exit=RETRIEVE_IDLE_EXIT_S if is_retrieve or last_look else None,
                  # Granting an app access is a mutation like a send: only the worker that sent the
                  # round answers its approval card; recovery leaves the card for a human.
                  approve=may_continue)
    code = res.get("code")
    turn_failed = code == 4 and "turn_failed" in (res.get("stderr") or "")
    if turn_failed and not is_retrieve:
        prior = store.auto_continue_state(rid)
        if prior is None and not may_continue:
            # Recovery found the failed turn. The continue is a paid send and recovery has none to
            # spend: end FAILED below ("no automatic continue went out"), and the agent re-fires.
            sys.stderr.write(f"CGC_WARN {rid}: turn failed, seen by a recovery wait — no continue "
                             "is sent from recovery.\n")
            outcome = "not_sent"
        elif prior is None:
            outcome = _auto_continue(store, rid, conv, spec, run_cdp, deadline)
        elif prior in ("claimed", "unsure"):
            # An earlier continue may have landed (a worker died after its click, or it went
            # unconfirmed), yet this rid's latest turn is still the failed one. The one-shot read-only
            # retrieve settles it: before it has run, stay uncertain; on that retrieve pass, a freshly
            # opened thread still showing only the failed turn means the continue never landed.
            outcome = "not_sent" if store.was_auto_retrieved(rid) else "unsure"
        else:
            outcome = prior                       # landed → this is its failure; not_sent → nothing out
        if outcome == "unsure":
            # The continue may be generating right now. Uncertain, not failed: the one-shot read-only
            # auto-retrieve re-attaches, and since the continue carries this rid, it reads that
            # newer turn if it landed — or finds the old failed turn and closes the round then.
            store.set_state(rid, store_mod.POSSIBLY_ACCEPTED,
                            error_code="ChatGPT failed the turn; the automatic continue may have "
                                       "been sent but was not confirmed — retrieve, don't resend")
            return store_mod.POSSIBLY_ACCEPTED
        if outcome == "landed":
            # The continue carries this round's rid, so the same wait now reads the newer turn. Start
            # it from a blank slate too: a stub .raw from the failed wait would downgrade a clean
            # sentinel answer to unverified (the S2 defect the loop above exists for).
            for _p in (out_tmp, out_tmp + ".raw"):
                try:
                    os.remove(_p)
                except OSError:
                    pass
            left = int(deadline - time.time())
            if left <= 0:
                return _deadline_passed(store, rid)
            res = run_cdp("wait", rid=rid, conversation=conv, out=out_tmp,
                          poll=spec.get("poll"), timeout=left, approve=may_continue)
            code = res.get("code")
            turn_failed = code == 4 and "turn_failed" in (res.get("stderr") or "")
    answer_path = res.get("out") or out_tmp
    answer = _read_answer(answer_path)
    if code == 0 and answer.strip():
        unverified = os.path.exists(answer_path + ".raw")
        # Producer attribution, committed with the answer. The verdict is computed HERE, once, and
        # stored — never re-derived when the round is later read, because the policy can change and
        # a round's identity finding is a fact about the moment it ran. Rows written before this
        # existed keep NULL and stay valid; nothing reclassifies them.
        slug = res.get("model_slug")
        attribution, attr_detail = _attribution_verdict(slug)
        if attribution == store_mod.ATTR_MISMATCHED:
            sys.stderr.write(f"CGC_ATTRIBUTION_MISMATCH {rid}: {attr_detail}\n")
        store.finish(rid, store_mod.COMPLETED_UNVERIFIED if unverified else store_mod.COMPLETED_VERIFIED,
                     result_text=answer, model_slug=slug, attribution=attribution)
        # After the commit, never before: a file that appears ahead of the round's terminal state
        # would let a watcher read an answer for a round that then fails to commit.
        _publish_answer(store, rid, answer)
        if (is_retrieve and wait_rid and wait_rid != rid and not unverified
                and _no_live_worker(wait_rid)):
            # The waiter matched END_RESPONSE:<wait_rid> on the thread — proof that the SOURCE
            # round's send landed and that this is its answer. Close the source with it; leaving it
            # uncertain with its answer already in hand is a lie the next reader has to re-litigate
            # (and, since uncertainty holds the tab sweep, one that keeps costing). Only a verified
            # sentinel may do this: an unwrapped salvage cannot prove which turn it came from.
            if store.adopt_retrieved_answer(wait_rid, answer, conversation_id=conv,
                                            model_slug=slug, attribution=attribution):
                sys.stderr.write(f"CGC_RECONCILED {wait_rid}: resolved from retrieve {rid} "
                                 "(sentinel-verified on the thread).\n")
                # The SOURCE round completes here too, and its own waiter is long gone by
                # construction — this is the recovery path. It needs its answer on disk most of all.
                _publish_answer(store, wait_rid, answer)
        return store_mod.COMPLETED_UNVERIFIED if unverified else store_mod.COMPLETED_VERIFIED
    if code == 3:
        store.set_state(rid, store_mod.BLOCKED, error_code=(res.get("stderr") or "blocker")[:200])
        return store_mod.BLOCKED
    if turn_failed and not is_retrieve:
        # Not uncertain: the send landed (its turn is on the page) and ChatGPT itself declared that
        # turn will never answer, after the one automatic continue. possibly_accepted would only
        # queue a retrieve that cannot find anything.
        went_out = store.auto_continue_state(rid) == "landed"
        store.finish(rid, store_mod.FAILED,
                     error_code=(_stderr_error_line(res.get("stderr") or "") or "turn_failed")
                     + (" — the one automatic continue also failed" if went_out else
                        " — no automatic continue went out"
                        + (f" (expired: use-by {_use_by(spec)} passed)" if _use_by_passed(spec)
                           else ""))
                     + "; re-fire with a NEW --request-key only if the answer is still needed.")
        return store_mod.FAILED
    if is_retrieve:
        # retrieve never sends anything, so a wait that cannot confirm/attribute the SOURCE turn's
        # answer (rid_absent — the turn was never sent here; rid_superseded/ambiguous — the thread
        # has since advanced past it and no sentinel-wrapped answer anchors it) is a plain, retryable
        # FAILURE — not "possibly accepted", which would wrongly imply a click that might need
        # reconciling. cdp_consult's CGC_ERROR line (if any) carries the actionable detail, including
        # 'observed_rid=' — the rid it actually found instead, for the envelope.
        detail = _stderr_error_line(res.get("stderr") or "")
        store.finish(rid, store_mod.FAILED,
                    error_code=detail or f"retrieve could not confirm source_rid={wait_rid or rid} "
                                         f"(exit {code})")
        return store_mod.FAILED
    # A wait that returns nothing usable is not a clean failure: the answer may still exist in the
    # conversation. Leave it uncertain so it is retrieved, not resent — but do NOT overwrite WHY the
    # round became uncertain in the first place. This fallback is also where the one-shot
    # auto-retrieve of an already-uncertain round lands, and stamping "accepted but wait produced no
    # answer" there asserted a confirmation that never happened, erasing the real disposition (a
    # proven-not-sent `model_not_selectable`, in the observed case) that a human needs to judge
    # whether a resend would duplicate anything.
    prior_row = store.get_round(rid)
    prior = prior_row["error_code"] if prior_row else None
    store.set_state(rid, store_mod.POSSIBLY_ACCEPTED,
                    error_code=(f"wait produced no answer (exit {code}) — retrieve, don't resend"
                                + (f"; original disposition: {prior}" if prior else "")))
    return store_mod.POSSIBLY_ACCEPTED


def _deadline_passed(store, rid) -> str:
    """The send's observation budget is spent before this wait could start. Not "not sent", not "remote
    failed": the turn may still be answering, so the round ends uncertain for an explicit retrieve —
    the same disposition a wait that ran out of time gets — keeping why it was uncertain before."""
    prior_row = store.get_round(rid)
    prior = prior_row["error_code"] if prior_row else None
    store.set_state(rid, store_mod.POSSIBLY_ACCEPTED,
                    error_code="observation deadline passed — retrieve, don't resend"
                               + (f"; original disposition: {prior}" if prior else ""))
    return store_mod.POSSIBLY_ACCEPTED


def _block_code(raw_stderr: str) -> str:
    """error_code for a pre-send BLOCKED round. A missing @mention keeps its whole line: it names
    the apps the popup DID offer, which is the one fact the agent needs to re-fire correctly; an
    invalid contract keeps its whole line too: it names the contract ids to fix."""
    for line in (raw_stderr or "").splitlines():
        if "mention_not_found" in line or line.startswith("CGC_ERROR contract_invalid"):
            return line.replace("CGC_ERROR ", "").strip()[:400]
    return _not_sent_marker({"stderr": raw_stderr}, _NOT_SENT_BLOCK) or "not_sent"


def _not_sent_marker(res: dict, markers):
    """The marker proving a pre-click refusal, or None. A proof has to be a line the driver WROTE:
    `CGC_ERROR <marker>` (or `CGC_LOGIN needed`) at the start of a stderr line. A plain substring
    search let a timeout's own text prove it — `_run`'s exit-124 stderr embeds the full argv, so a
    `--mention "Usage Tracker"` read as "usage" (not sent) on a send that may well have landed. A
    killed subprocess (124) is never a proof of anything."""
    if res.get("code") == 124:
        return None
    err = res.get("stderr") or ""
    for m in markers:
        if m == "CGC_LOGIN":
            pat = r"^CGC_LOGIN needed\b"
        elif m == "usage":                  # argparse's own banner: a CLI misuse exits before anything
            pat = r"^usage: \S+\.py\b"
        else:                               # e.g. cdp_attach_failed, new_tab_failed, model_not_selectable
            pat = r"^CGC_ERROR (?:[a-z_]+_)?" + re.escape(m) + r"(?:_[a-z_]+)?\b"
        if re.search(pat, err, re.M | re.I):
            return m
    return None
