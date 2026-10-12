#!/usr/bin/env python3
"""SQLite-backed transactional store for the consult control plane.

ONE authority for threads, rounds, attempts, and browser state. It replaces the file-spool / PID /
rename / lock / active.json arrangement, where a crash could expose combinations no state machine
permits (a `done` status with no answer file, a `processing` job whose worker is gone, two stores
disagreeing on the conversation id). Here every recovery decision is a transition over NAMED states,
committed in a single transaction, rather than inference over files and PIDs.

Design (from the first-principles teardown — docs/plans/2026-07-21-reinternal-control-plane.md):
- WAL journal: concurrent readers + one writer on one host, which is exactly this deployment.
- synchronous=FULL: a durable `sending` must survive power loss, or the whole point is lost.
- foreign_keys=ON; the DB file is 0600; one `meta.schema_version` row + a migration hook.
- The round state machine is EXPLICIT: an illegal transition raises, it is never silently ignored.
- `possibly_accepted` is NOT a synonym for failure. It is an explicit prohibition on automatic
  retry — the single most important invariant here. A browser click cannot be made atomic with a
  local commit, so any abnormal exit while `sending` becomes `possibly_accepted`, and the recovery
  path will NEVER auto-resend it (that is what prevents a crash-after-send from duplicating a
  consult). At-most-once automatic retry under uncertainty is the achievable guarantee; exactly-once
  would need a remote idempotency key the web product does not expose.

Phase 1 (this module): the store + state machine + recovery classification, standalone and fully
unit-tested. It is NOT yet wired into the daemon; the running system is unaffected. Later phases add
the `send_round` boundary, cut the daemon over with a one-shot spool→store migration, and retire the
file-spool lifecycle machinery.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import sqlite3
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from cgc_config import load_config as _cgc_load_config
    _cgc_load_config()
except Exception:
    pass

CGC_STATE_DIR = os.environ.get("CGC_STATE_DIR", "/tmp/cgc")


def data_dir() -> str:
    """Durable data dir. The authority (threads, rounds, results) must not live in the scratch dir:
    CGC_STATE_DIR defaults to /tmp, which reboots wipe and docs call deletable. Scratch files
    (prompt/refs/answer) stay under CGC_STATE_DIR; the DB lives here. Resolved from the environment
    at CALL time (not import time) so tests and late-loaded config are honoured."""
    explicit = os.environ.get("CGC_DATA_DIR")
    if explicit:
        return explicit
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "cgc")


def db_path() -> str:
    """Effective store DB path. ":memory:" is honoured for tests."""
    return os.environ.get("CGC_STORE_DB", os.path.join(data_dir(), "control.db"))


def _legacy_db() -> str:
    return os.path.join(os.environ.get("CGC_STATE_DIR", "/tmp/cgc"), "control.db")


class _RelocationValidationError(Exception):
    """A candidate DB file is not a complete, trustworthy store — partial, truncated, or foreign."""


def _validate_relocated_db(conn: sqlite3.Connection) -> None:
    """Raise _RelocationValidationError unless `conn` points at a complete store DB: integrity check
    passes, the meta table exists, and store_uuid is present and non-empty. Shared by the
    post-copy check and the pre-existing-target check — the same bar either way, because either
    file is about to become (or remain) the one authority every later opener trusts."""
    check = conn.execute("PRAGMA integrity_check").fetchone()
    check = check[0] if check else None
    has_meta = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    store_uuid = None
    if has_meta:
        row = conn.execute("SELECT value FROM meta WHERE key='store_uuid'").fetchone()
        store_uuid = row[0] if row else None
    if check != "ok" or not has_meta or not store_uuid:
        raise _RelocationValidationError(
            f"relocation candidate failed validation (integrity={check!r}, "
            f"meta={bool(has_meta)}, store_uuid={bool(store_uuid)})")


def _validate_target_db(path: str) -> None:
    """Open `path` fresh and run the same validation a relocated copy must pass. A file that isn't
    even a readable SQLite database (truncated/partial, e.g. left by an older relocation
    implementation) raises sqlite3.Error before PRAGMA runs at all — that counts as invalid too."""
    try:
        conn = sqlite3.connect(path)
        try:
            _validate_relocated_db(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise _RelocationValidationError(f"{path}: {exc}") from exc


def _acquire_relocation_lock(d: str) -> int:
    """Exclusive, blocking flock on a dedicated file in the destination data dir (`d`), meant to be
    held for the WHOLE relocation decision — sweep, inspection, copy, publish, and legacy fence.
    This is the ONLY inter-process serialization relocation has: without it, two processes can each
    pass the "legacy exists, target doesn't" check, each copy to a different temp, and race to
    publish over `target` — the loser's copy is silently abandoned mid-write by every later opener.
    A blocking acquire is fine; relocation is rare and finishes in well under a second.

    fcntl is POSIX-only. Rather than run unserialized on a platform without it, fail closed: an
    unserialized relocation is exactly the bug this function exists to prevent."""
    try:
        import fcntl
    except ImportError as exc:
        raise RuntimeError(
            "legacy DB relocation requires fcntl for inter-process serialization; this platform "
            "has none, so relocation is refused rather than run unserialized") from exc
    lock_path = os.path.join(d, ".relocation.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _relocate_legacy_db(target: str) -> None:
    """One-time ATOMIC cutover of the store out of the non-durable state dir into the durable data
    dir. Runs only when the caller is using the DEFAULT path (no CGC_STORE_DB override).

    Every step below — the stale-temp sweep, inspecting either DB, copying, publishing, and fencing
    the legacy file — happens under a single exclusive flock (`_acquire_relocation_lock`), so at
    most one process is ever inside this function's body at a time. That is what makes a stale-temp
    sweep safe (nothing else can be mid-copy) and what prevents two processes from independently
    publishing over `target`. State is re-read after the lock is acquired, since a peer may have
    finished the whole relocation while this process was blocked waiting for it.

    Publication protocol (crash-safe at every boundary):
      1. sqlite-backup the legacy DB into a UNIQUE temp file in the destination dir (never the
         final name — the final name must only ever hold a COMPLETE, validated copy).
      2. Validate the copy (PRAGMA integrity_check + meta table + store_uuid), fsync it.
      3. os.replace(temp, target) — the atomic publication. Target existence now IMPLIES a
         complete, validated DB; a crash earlier leaves only a uniquely-named temp (cleaned up on
         the next run), never a half-written target that would suppress retry.
      4. Rename the legacy file *.migrated — the writer fence. A crash between 3 and 4 leaves two
         complete files; the next run detects (target AND legacy both present) and finishes the
         fence instead of returning early with two apparent authorities.
    A pre-existing nonzero target is validated the same way before it is trusted to supersede an
    intact legacy DB: a partial/corrupt target (e.g. left by an older relocation implementation) is
    quarantined aside, never fenced against, and the copy is redone from legacy.
    The sqlite backup gives a consistent snapshot but is NOT a writer fence for an already-running
    old daemon holding the legacy inode — that is what the heartbeat identity check (store_uuid /
    db_path, enforced fail-closed at enqueue) plus install.sh's mandatory daemon restart close."""
    if "CGC_STORE_DB" in os.environ:
        return
    legacy = _legacy_db()
    if target == legacy:
        return
    # Fast path: no legacy file means no relocation work is possible (a loser of the publication
    # race never even starts a copy, so a fenced legacy leaves no temps behind either) — skip the
    # per-open flock. Racy by itself, but the locked body re-checks everything under the lock.
    if not os.path.exists(legacy):
        return
    d = os.path.dirname(target) or "."
    os.makedirs(d, mode=0o700, exist_ok=True)
    lock_fd = _acquire_relocation_lock(d)
    try:
        _relocate_legacy_db_locked(target, legacy, d)
    finally:
        # Release the flock by closing the fd. The lock FILE itself is never unlinked: flock's
        # exclusion is tied to the underlying inode via the open file description, so unlinking it
        # while a peer holds (or is about to open) that same path would let a third process create
        # a fresh inode of the same name and acquire an independent, non-serializing lock.
        os.close(lock_fd)


def _relocate_legacy_db_locked(target: str, legacy: str, d: str) -> None:
    # Sweep runs only under the lock: no other process can be mid-copy right now, so any
    # `.relocating-*` temp found here is provably abandoned, never a live peer file.
    for stale in os.listdir(d):
        if stale.startswith(".control.db.relocating-"):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(d, stale))

    # Re-check now that the lock is held: a peer may have relocated (and fenced legacy) while this
    # process was blocked waiting.
    if not os.path.exists(legacy):
        return

    if os.path.exists(target):
        if os.path.getsize(target) == 0:
            # A pre-cutover install crashed after pre-creating the final name empty; that stub
            # must not suppress relocation forever. Only an EMPTY file is provably not a DB.
            os.remove(target)
        else:
            try:
                _validate_target_db(target)
            except _RelocationValidationError:
                # A nonempty target that fails validation is a partial/corrupt file — quarantine it
                # rather than either trusting it or deleting it, then fall through and republish
                # from the still-intact legacy DB below.
                os.replace(target, os.path.join(d, f".corrupt-{uuid.uuid4().hex}"))
            else:
                os.replace(legacy, legacy + ".migrated")  # finish/redo the writer fence
                return

    tmp = os.path.join(d, f".control.db.relocating-{uuid.uuid4().hex}")
    os.close(os.open(tmp, os.O_CREAT | os.O_WRONLY, 0o600))
    src = sqlite3.connect(legacy)
    dst = sqlite3.connect(tmp)
    try:
        with dst:
            src.backup(dst)
        _validate_relocated_db(dst)
    except BaseException:
        dst.close()
        src.close()
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise
    dst.close()
    src.close()
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, target)
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    os.replace(legacy, legacy + ".migrated")

SCHEMA_VERSION = 5

# Ordered, transactional migrations: _MIGRATIONS[n] upgrades a version-(n-1) DB to version n.
# A fresh DB is created at SCHEMA_VERSION directly by _DDL, so each migration must produce exactly
# the same shape _DDL creates. Never edit a shipped migration — append the next version instead.
_MIGRATIONS = {
    2: [
        "ALTER TABLE rounds ADD COLUMN request_key TEXT",
        "ALTER TABLE rounds ADD COLUMN parent_rid TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_rounds_request_key "
        "ON rounds(request_key) WHERE request_key IS NOT NULL",
    ],
    # send_disposition: an operator's durable, explicit verdict about whether a round's send ever
    # reached ChatGPT. Only 'not_sent_proven' is written (by record_not_sent_proof); its ABSENCE is
    # not evidence of a send. It is the one thing that lets a post-send-uncertain FAILED round
    # release its request-key — a generic FAILED never can, because the state alone cannot prove
    # no-send (possibly_accepted->failed and waiting->failed are both legal reconciles).
    3: [
        "ALTER TABLE rounds ADD COLUMN send_disposition TEXT",
    ],
    # not_before / gate_retry_count: the backoff for a `ready` round requeued by a TRANSIENT egress-
    # gate failure (cgc_backend._gate's `unverified:` branch — gh missing/timed out/API error, not a
    # confirmed-private verdict). Before this, that requeue went straight back to `queued` with
    # nothing stopping it from being claimed again on the very next poll — one round hit it 2461
    # times in 91 minutes (~1 every 2.2s) before a human cancelled it. not_before is the "not
    # claimable before this instant" fence claim_ready's WHERE clause now honours; gate_retry_count
    # is how _gate tells a transient blip from a standing condition and bounds the retry.
    4: [
        "ALTER TABLE rounds ADD COLUMN not_before TEXT",
        "ALTER TABLE rounds ADD COLUMN gate_retry_count INTEGER NOT NULL DEFAULT 0",
    ],
    # model_badge / model_slug / attribution: the two model facts a round has, kept APART, plus the
    # one verdict over them. model_badge is what the composer's switcher read BEFORE the send —
    # selection evidence, "what was requested". model_slug is data-message-model-slug off the
    # answer's own assistant node — the provider's attribution, "what actually served this". They
    # were the same field in spirit until 2026-09-06, which is the whole defect: a pre-send read
    # was being published as a receipt for the producer, and OpenAI's own rate-limit fallback makes
    # those two demonstrably different. attribution is the verdict recorded AT THE TIME the round
    # completed (matched/mismatched/unknown/unchecked — see cgc_backend._attribution_verdict), not
    # re-derived on read: the policy can change, and a round's identity finding is a fact about the
    # moment it ran. Every column is NULL on rows written before this migration, and NULL reads as
    # "this round predates attribution" — never as a mismatch. Additive ALTERs only: a daemon
    # mid-flight against this DB keeps working, exactly as migration 4 was designed to allow.
    5: [
        "ALTER TABLE rounds ADD COLUMN model_badge TEXT",
        "ALTER TABLE rounds ADD COLUMN model_slug TEXT",
        "ALTER TABLE rounds ADD COLUMN attribution TEXT",
    ],
}

# The four values ever written to rounds.attribution. NULL is a fifth, implicit state that no code
# writes: "this round ran before attribution existed".
ATTR_MATCHED = "matched"        # a slug was read AND it satisfies the configured policy
ATTR_MISMATCHED = "mismatched"  # a slug was read AND it does NOT — the one gate (see cgc_backend)
ATTR_UNKNOWN = "unknown"        # no slug on the node: a real outcome, not an error, never a failure
ATTR_UNCHECKED = "unchecked"    # a slug was read but no policy is configured, so nothing judged it

# The only value ever stored in rounds.send_disposition: an explicit operator reconcile proving the
# round's send never reached ChatGPT (e.g. browser inspection found no such message).
NOT_SENT_PROVEN = "not_sent_proven"


class SchemaTooNew(Exception):
    """The DB was written by a NEWER version of this tool. Refuse rather than guess: running old
    code against a newer schema risks silent corruption of durable consult state."""

# ---- the round state machine -------------------------------------------------
# Terminal states have no outgoing transitions. `sending` can ONLY go to accepted/possibly_accepted
# via this table: once the click may have happened, "it failed" is not knowable through a mere state
# change, so there is no sending→failed edge IN _LEGAL — an abnormal exit becomes possibly_accepted.
# The one exception bypasses this table entirely rather than weakening it: mark_send_not_sent goes
# straight to `failed` (with send_disposition stamped in the same transaction) ONLY when the CDP
# driver's own pre-click boundary durably proves the click was never issued — a stronger guarantee
# than any state-machine edge could express, so it is enforced in code (mark_accepted's and
# mark_possibly_accepted's own state checks, not this dict) rather than added here as a generally
# reachable sending→failed edge. Pre-send failures happen while still `ready`.
QUEUED = "queued"
GATE_REJECTED = "gate_rejected"
READY = "ready"
SENDING = "sending"
ACCEPTED = "accepted"
POSSIBLY_ACCEPTED = "possibly_accepted"
WAITING = "waiting"
COMPLETED_VERIFIED = "completed_verified"
COMPLETED_UNVERIFIED = "completed_unverified"
BLOCKED = "blocked"
FAILED = "failed"

_LEGAL = {
    QUEUED: {READY, GATE_REJECTED, FAILED},
    # READY -> WAITING: a `retrieve` round attaches to an existing conversation and waits, it never
    # sends. GATE_REJECTED: the worker gates AFTER claiming (round is ready), so a rejection is a
    # ready-state transition. QUEUED: release a claimed-but-unsent round back.
    READY: {SENDING, WAITING, BLOCKED, FAILED, QUEUED, GATE_REJECTED},
    # sending -> accepted|possibly_accepted is the default. sending -> blocked|queued is permitted
    # ONLY for a submit that PROVABLY did not send (its fail-closed pre-click exits: login/captcha →
    # blocked; model-not-selectable/composer-not-ready → queued to retry). A CRASH leaves the round
    # in `sending` with no returned verdict, and recover() classifies that uncertain (→
    # possibly_accepted), never dispatchable — so the anti-duplicate invariant holds: only a proven
    # not-sent is ever re-queued, never a merely-possible send.
    SENDING: {ACCEPTED, POSSIBLY_ACCEPTED, BLOCKED, QUEUED},
    ACCEPTED: {WAITING, POSSIBLY_ACCEPTED},
    POSSIBLY_ACCEPTED: {WAITING, ACCEPTED, FAILED},   # ONLY via explicit recovery/human, never auto
    WAITING: {COMPLETED_VERIFIED, COMPLETED_UNVERIFIED, BLOCKED, FAILED, POSSIBLY_ACCEPTED},
    GATE_REJECTED: set(),
    COMPLETED_VERIFIED: set(),
    COMPLETED_UNVERIFIED: set(),
    BLOCKED: set(),
    FAILED: set(),
}
TERMINAL = {s for s, nxt in _LEGAL.items() if not nxt}
# States for which the click may already have reached ChatGPT — never automatically re-sent.
UNCERTAIN = {SENDING, POSSIBLY_ACCEPTED}


class IllegalTransition(Exception):
    """A state change the machine does not permit — a bug, surfaced instead of silently corrupting."""


class RequestKeyRaced(Exception):
    """A request-key transfer found the source row no longer holds the key — a concurrent retry moved
    it first. The caller re-resolves to the current owner (the winner) rather than creating a second
    successor, so exactly one successor ever owns a released key."""


def _now() -> str:
    """UTC ISO-8601. Durable recovery timestamps must be wall-clock (monotonic does not survive a
    restart); process-local elapsed-time budgets are a separate concern handled by the caller."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def spent_messages(db) -> list:
    """Every outgoing paid message that may have been spent, as (rid, kind, at). `db` is any sqlite3
    connection onto the store, including a read-only one — the SessionStart snapshot has no Store.

    One round can send more than once: its base send, and the single automatic "continue" a
    ChatGPT-failed turn gets under the SAME rid. So the unit is the message, not the round.
      - base: the round crossed the send fence (an `attempts` row, begin_send being its only writer)
        and carries no durable not-sent proof. The proof covers every attempt so far: it is stamped
        on a proven not-sent requeue or terminal and cleared by the next begin_send, so a requeued
        round that never sent is free and one that later sent is charged once. An UNCERTAIN send is
        charged: the click may have landed. `at` is the last attempt's start.
      - continuation: a claimed `auto_continue` whose recorded result is landed, unsure, or absent
        (the worker died between claim and result, possibly after the click). `not_sent` is free.
    A `retrieve` never calls begin_send and never claims a continue, so it contributes nothing."""
    base = db.execute(
        "SELECT r.rid, r.kind, (SELECT max(a.started_at) FROM attempts a WHERE a.rid=r.rid) "
        "FROM rounds r WHERE EXISTS(SELECT 1 FROM attempts a WHERE a.rid=r.rid) "
        "AND (r.send_disposition IS NULL OR r.send_disposition<>?)", (NOT_SENT_PROVEN,)).fetchall()
    cont = db.execute(
        "SELECT r.rid, r.kind, e.at FROM rounds r JOIN events e "
        "ON e.rid=r.rid AND e.kind='auto_continue' "
        "WHERE COALESCE((SELECT x.detail FROM events x WHERE x.rid=r.rid "
        "AND x.kind='auto_continue_result' ORDER BY x.id DESC LIMIT 1), 'claimed')<>'not_sent'"
    ).fetchall()
    return [tuple(r) for r in (*base, *cont)]


WEEK_S = 7 * 86400   # the rolling window every weekly figure uses; no reset day to configure
_DONE = (COMPLETED_VERIFIED, COMPLETED_UNVERIFIED)


def _title(prompt) -> str:
    """The round's headline: the first `# <title>` line of the rendered prompt. The templates open with
    the `CGC_RID:<rid>` marker line, so the title is the first heading, not the first line."""
    for ln in (prompt or "").splitlines():
        if ln.startswith("# "):
            return ln[2:].strip()[:80]
    return ""


def _spec(raw) -> dict:
    try:
        v = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return v if isinstance(v, dict) else {}


def week_view(db, now=None) -> dict:
    """The rolling-7-day picture the weekly review and the SessionStart line both read. ONE
    definition, over any sqlite3 connection onto the store (a read-only one is fine), so the two
    surfaces cannot disagree. Pure reads: nothing here writes, migrates, or touches updated_at.

    Windowed by instant, never by calendar: a message counts for 7 days after it was sent, so the
    budget frees up continuously and there is no reset day to configure. Timestamps are all written
    by _now() (UTC isoformat), so string comparison orders them."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    t0 = (now - datetime.timedelta(seconds=WEEK_S)).isoformat()
    t1 = now.isoformat()
    q = lambda sql, *args: db.execute(sql, args).fetchall()  # noqa: E731

    spent = [m for m in spent_messages(db) if m[2] and m[2] >= t0]
    ungraded = [{"rid": r[0], "title": _title(r[1]), "completed_at": r[2]} for r in q(
        "SELECT r.rid, r.rendered_prompt, r.updated_at FROM rounds r "
        "WHERE r.state IN (?,?) AND r.kind<>'retrieve' AND r.updated_at>=? "
        "AND NOT EXISTS (SELECT 1 FROM events e WHERE e.rid=r.rid AND e.kind='consult_outcome') "
        "ORDER BY r.updated_at", *_DONE, t0)]
    # A scheduled round waits with a future not_before and no gate retries; a gate-backoff requeue
    # (gate_retry_count>0) also has a future not_before but is a transient retry, not backlog.
    backlog = [{"rid": r[0], "title": _title(r[1]), "not_before": r[2],
                "use_by": _spec(r[3]).get("consult_meta", {}).get("use_by")} for r in q(
        "SELECT rid, rendered_prompt, not_before, spec_json FROM rounds "
        "WHERE state=? AND not_before>? AND gate_retry_count=0 ORDER BY not_before", QUEUED, t1)]
    expired = [{"rid": r[0], "title": _title(r[1]), "error": r[2]} for r in q(
        "SELECT rid, rendered_prompt, error_code FROM rounds "
        "WHERE state=? AND error_code LIKE 'expired: use-by%' AND updated_at>=? ORDER BY updated_at",
        FAILED, t0)]

    by_trigger, by_mention, useful = {}, {}, 0
    for rid, detail, spec in q(
            "SELECT e.rid, e.detail, r.spec_json FROM events e JOIN rounds r ON r.rid=e.rid "
            "WHERE e.kind='consult_outcome' AND e.at>=? AND e.id=(SELECT max(x.id) FROM events x "
            "WHERE x.rid=e.rid AND x.kind='consult_outcome')", t0):
        sp = _spec(spec)
        g = str(_spec(detail).get("grade"))
        trig = (sp.get("consult_meta") or {}).get("trigger") or "none"
        ment = "with_mention" if sp.get("mentions") else "without_mention"
        for tally, k in ((by_trigger, trig), (by_mention, ment)):
            tally.setdefault(k, {})[g] = tally.setdefault(k, {}).get(g, 0) + 1
        useful += g in ("1", "2")

    pol = q("SELECT detail, at FROM events WHERE kind='policy_update' AND at>=? "
            "ORDER BY id DESC LIMIT 1", t0)
    durs = []
    for c, u in q("SELECT created_at, updated_at FROM rounds WHERE state IN (?,?)", *_DONE):
        try:
            d = (datetime.datetime.fromisoformat(u) - datetime.datetime.fromisoformat(c)).total_seconds()
        except (TypeError, ValueError):
            continue
        if d > 0:
            durs.append(d)
    durs.sort()
    try:
        budget = int(os.environ.get("CGC_WEEKLY_BUDGET", "").strip())
    except ValueError:
        budget = None
    return {
        "window": {"from": t0, "to": t1, "days": 7},
        "budget": {"weekly": budget, "spent": len(spent),
                   "available": max(0, budget - len(spent)) if budget is not None else None,
                   **({} if budget is not None else {"note": "budget unset (set CGC_WEEKLY_BUDGET)"})},
        "ungraded": ungraded, "backlog": backlog, "expired": expired,
        "grades": {"by_trigger": by_trigger, "by_mention": by_mention},
        "useful": {"outcomes": useful,
                   "per_spent_message": round(useful / len(spent), 3) if spent else None},
        "policy_update": ({"happened": True, "at": pol[0][1], **_spec(pol[0][0])} if pol
                          else {"happened": False}),
        "latency_p50_s": durs[len(durs) // 2] if durs else None,
    }


# ---- the UI contract: what a send needs from ChatGPT's page ---------------------------------
# ChatGPT's web UI is an unversioned contract the driver reads by structure, and it moved four times
# in three weeks. Each move was found one paid round at a time. A capability is one part of that
# contract, recorded in `meta` under `ui.<name>` as JSON {ok, epoch, phase, cause,
# rid_or_conversation, checked_at, driver_rev}. ABSENT means never established, which never gates:
# only a recorded failure does. `epoch` grows on every write; a probe reads it before it starts, and
# its pass may clear a failure only if no write happened since (Store.mark_passed).
CAPABILITIES = ("composer", "model", "mention", "reader")


# ---- use-by: `fire --use-by T` — a round whose answer is useless after T never sends after it --------
def past(instant) -> bool:
    """True iff the ISO-8601 `instant` has passed. None or unparseable -> False: a deadline is
    validated at fire time, and a malformed one must never silently eat a paid round. The one
    definition: the claim sweep, the worker's pre-send checks and the driver's click-time check."""
    try:
        t = datetime.datetime.fromisoformat(instant)
    except (TypeError, ValueError):
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t <= datetime.datetime.now(datetime.timezone.utc)


def use_by_of(spec) -> str | None:
    """A round's use-by from its spec (a dict, or spec_json text)."""
    spec = spec if isinstance(spec, dict) else _spec(spec)
    return (spec.get("consult_meta") or {}).get("use_by")


def _expired_fields(use_by: str) -> dict:
    """An expired round's terminal fields: pre-send by construction (every caller runs before
    begin_send), so not-sent-proven — never charged, and its request-key releases."""
    return {"state": FAILED, "send_disposition": NOT_SENT_PROVEN,
            "error_code": f"expired: use-by {use_by} passed before send"}


def round_needs(kind: str, spec_json) -> tuple:
    """The capabilities a round's send depends on. A retrieve sends nothing, so it needs none and is
    never gated. Every send needs the composer (paste, read it back) and the reader (prove the turn
    landed, read the answer). `model` is what the probe checks: selecting the configured tier on the
    configured family, so only a round that selects that same pair needs it; a `skip` round, or one
    pinned to another family, does not. A round with an @mention also needs the mention picker."""
    if kind == "retrieve":
        return ()
    spec = _spec(spec_json)
    auto = os.environ.get("CGC_AUTO_MODEL", "1").strip().lower() not in ("0", "false", "no", "off", "")
    configured = ((os.environ.get("CGC_MODEL", "Pro"), os.environ.get("CGC_MODEL_FAMILY", "GPT-6"))
                  if auto else ("skip", "skip"))
    picks = (spec.get("model") or "Pro", spec.get("model_family") or "GPT-6")
    model = picks[0].lower() != "skip" and picks == configured
    return (("composer", "reader") + (("model",) if model else ())
            + (("mention",) if spec.get("mentions") else ()))


def new_daemon_instance_id() -> str:
    """A fresh identity per daemon process — recorded on attempts so ownership is the daemon
    INSTANCE, not a reusable OS pid."""
    return _new_id()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Store:
    """A connection to the control DB. One Store per process/thread (sqlite3 connections are not
    shared across threads by default). Every mutating method commits its own transaction."""

    def __init__(self, path: str | None = None):
        self.path = path or db_path()
        if self.path != ":memory:":
            _relocate_legacy_db(self.path)
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, mode=0o700, exist_ok=True)
            # create the file 0600 before sqlite opens it, so secrets in rendered prompts are never
            # world-readable even for an instant.
            if not os.path.exists(self.path):
                os.close(os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.db = sqlite3.connect(self.path, isolation_level=None)  # autocommit off via explicit BEGIN
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    # ---- schema / migration --------------------------------------------------
    def _migrate(self) -> None:
        """Bring the DB to SCHEMA_VERSION. Fresh DB: _DDL creates the current shape and the version
        row is stamped directly. Older DB: the file is backed up (control.db.v<n>.bak, via the
        sqlite backup API — consistent even under WAL), then each pending migration runs in ONE
        transaction with its version bump, so a crash mid-migration leaves a coherent version-(n-1)
        DB plus a backup, never a half-migrated hybrid. Newer DB: refuse (SchemaTooNew)."""
        # Read the version BEFORE running the current DDL: on an older DB the DDL's newer indexes
        # reference columns only a migration adds, so DDL-first would fail on the very DB that
        # needs migrating.
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.executescript(_DDL)
            self.db.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                            (str(SCHEMA_VERSION),))
            self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('store_uuid',?)",
                            (_new_id(),))
            return
        have = int(row["value"])
        if have > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"store schema is v{have}, this code understands up to v{SCHEMA_VERSION} — "
                f"the DB was written by a newer install. Upgrade the skill (re-run install.sh) "
                f"instead of running old code against it.")
        # Older DBs predate the store identity; mint it once (no-op when already present).
        self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('store_uuid',?)",
                        (_new_id(),))
        if have == SCHEMA_VERSION:
            return
        self._backup(f".v{have}.bak")
        for target in range(have + 1, SCHEMA_VERSION + 1):
            with self._tx():
                for stmt in _MIGRATIONS[target]:
                    self.db.execute(stmt)
                self.db.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(target),))

    def _backup(self, suffix: str) -> None:
        """Consistent point-in-time copy of the DB file (skipped for :memory:)."""
        if self.path == ":memory:":
            return
        dst_path = self.path + suffix
        dst = sqlite3.connect(dst_path)
        try:
            with dst:
                self.db.backup(dst)
        finally:
            dst.close()
        with contextlib.suppress(OSError):
            os.chmod(dst_path, 0o600)

    def store_uuid(self) -> str:
        """The stable identity of THIS database file, minted once at creation/migration. The daemon
        publishes it in its heartbeat; enqueue refuses when the live daemon's store identity is not
        the one the CLI opened — the runtime writer fence the relocation copy cannot provide."""
        row = self.db.execute("SELECT value FROM meta WHERE key='store_uuid'").fetchone()
        return row["value"] if row else ""

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @contextlib.contextmanager
    def _tx(self):
        """One IMMEDIATE transaction — takes the write lock up front so a concurrent writer conflicts
        now (busy_timeout) rather than mid-statement."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    # ---- rounds --------------------------------------------------------------
    def create_round(self, rid: str, kind: str, *, thread_id: str | None = None,
                     source_mode: str | None = None, out_path: str | None = None,
                     spec_json: str | None = None, prompt: str | None = None,
                     request_key: str | None = None, parent_rid: str | None = None,
                     state: str = QUEUED, not_before: str | None = None) -> None:
        """Insert a new round. `prompt` (the exact rendered bytes to send) is stored on the round at
        enqueue, so the CLI receipt is just the rid and there is no prompt PATHNAME whose contents
        could drift before the gate reads them — the store row is the one copy. `request_key` is the
        caller's logical-request identity (unique where set — the idempotency anchor); `parent_rid`
        records which consult a follow-up causally continues. `not_before` (UTC ISO-8601) schedules the
        round: claim_ready will not pick it before that instant."""
        if state not in _LEGAL:
            raise ValueError(f"unknown initial state {state!r}")
        now = _now()
        psha = sha256(prompt) if prompt is not None else None
        with self._tx():
            if thread_id is not None:
                # thread_id IS the conversation id at every call site (enqueue passes the
                # resolved `conv`). Leaving the column NULL made the row a thread nobody could
                # address: conversation_of() returned None, so a completed round could not be
                # continued with --parent and vanished from `latest_conversation` — a follow-up
                # then silently degraded into a fresh consult that threw the thread's context away.
                self.db.execute(
                    "INSERT OR IGNORE INTO threads(thread_id,conversation_id,created_at,updated_at) "
                    "VALUES(?,?,?,?)", (thread_id, thread_id, now, now))
            self.db.execute(
                "INSERT INTO rounds(rid,thread_id,kind,source_mode,spec_json,rendered_prompt,"
                "prompt_sha256,out_path,request_key,parent_rid,state,created_at,updated_at,"
                "schema_version,not_before) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, thread_id, kind, source_mode, spec_json, prompt, psha, out_path,
                 request_key, parent_rid, state, now, now, SCHEMA_VERSION, not_before))
            self._event("created", rid=rid, detail=f"kind={kind} state={state}")

    def round_by_request_key(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM rounds WHERE request_key=?", (key,)).fetchone()
        return dict(row) if row else None

    def has_send_attempt(self, rid: str) -> bool:
        """True iff the round durably crossed the send fence at least once — begin_send is the ONLY
        writer of the attempts table and the ONLY path into `sending`, so an attempt row is exact
        proof the round entered `sending` (the click may have reached ChatGPT). Its ABSENCE is the
        durable proof that nothing was ever sent — the condition under which a terminally-FAILED
        round may release its request-key to a retry without risking a duplicate send."""
        return self.db.execute(
            "SELECT 1 FROM attempts WHERE rid=? LIMIT 1", (rid,)).fetchone() is not None

    def record_not_sent_proof(self, rid: str, evidence: str) -> None:
        """Operator reconcile: durably record that a round's send was PROVEN never to have reached
        ChatGPT (e.g. browser inspection found no such message, or ChatGPT's own history confirmed
        it). This explicit act — and ONLY this — qualifies a post-send-uncertain FAILED round, or an
        as-yet-unstamped BLOCKED round, to later release its request-key; a generic FAILED/BLOCKED
        never does, because possibly_accepted->failed, waiting->failed, and a blocker hit post-send
        while `waiting` are all legal reconciles that do not themselves prove no-send. Drives a
        still-reconcilable round (POSSIBLY_ACCEPTED/WAITING/READY) to FAILED as part of the act; on
        an already-FAILED or already-BLOCKED round it just stamps the durable proof IN PLACE — BOTH
        are already terminal, so this is a stamp, not a state transition, and BLOCKED must stay
        unreachable as a _LEGAL target from here (it never becomes one). This is also how a round
        BLOCKED before this operator command existed — carrying no stamp because the automatic
        stamping at its blocking site did not exist yet — gets reconciled after the fact."""
        with self._tx():
            row = self.db.execute("SELECT state FROM rounds WHERE rid=?", (rid,)).fetchone()
            if row is None:
                raise KeyError(rid)
            cur = row["state"]
            if cur in (FAILED, BLOCKED):
                self.db.execute("UPDATE rounds SET send_disposition=?,updated_at=? WHERE rid=?",
                                (NOT_SENT_PROVEN, _now(), rid))
            elif FAILED in _LEGAL.get(cur, set()):
                self._apply_round_fields(
                    rid, state=FAILED, send_disposition=NOT_SENT_PROVEN,
                    error_code=f"reconciled not-sent (proven): {evidence}")
                self._event("state", rid=rid, detail=f"{cur} -> {FAILED} (not-sent proof)")
            else:
                raise IllegalTransition(
                    f"{rid}: cannot record not-sent proof from {cur!r} — the reconcile applies to an "
                    "uncertain or already-terminal (failed/blocked) round, not one still in flight")
            self._event("not_sent_proof", rid=rid, detail=evidence)

    def transfer_request_key(self, old_rid: str, *, rid: str, kind: str,
                             thread_id: str | None = None, out_path: str | None = None,
                             spec_json: str | None = None, prompt: str | None = None,
                             request_key: str, parent_rid: str | None = None,
                             state: str = QUEUED, not_before: str | None = None) -> None:
        """Atomically MOVE a request-key from a terminally-failed `old_rid` to a fresh successor
        round, in ONE transaction: clear the old row's key and INSERT the new row already holding it,
        so the UNIQUE key is never momentarily free for a concurrent retry to seize (the separate
        clear-then-create the audit flagged left exactly such a window). Guards that `old_rid` still
        holds the key; if a concurrent transfer already moved it, raises RequestKeyRaced so the caller
        re-resolves to the winner instead of minting a second successor."""
        if state not in _LEGAL:
            raise ValueError(f"unknown initial state {state!r}")
        now = _now()
        psha = sha256(prompt) if prompt is not None else None
        with self._tx():
            row = self.db.execute("SELECT request_key FROM rounds WHERE rid=?", (old_rid,)).fetchone()
            if row is None or row["request_key"] != request_key:
                raise RequestKeyRaced(old_rid)
            self.db.execute("UPDATE rounds SET request_key=NULL,updated_at=? WHERE rid=?",
                            (now, old_rid))
            self._event("key_released", rid=old_rid,
                        detail=f"request-key transferred to {rid}: prior proven not sent")
            if thread_id is not None:
                # thread_id IS the conversation id at every call site (enqueue passes the
                # resolved `conv`). Leaving the column NULL made the row a thread nobody could
                # address: conversation_of() returned None, so a completed round could not be
                # continued with --parent and vanished from `latest_conversation` — a follow-up
                # then silently degraded into a fresh consult that threw the thread's context away.
                self.db.execute(
                    "INSERT OR IGNORE INTO threads(thread_id,conversation_id,created_at,updated_at) "
                    "VALUES(?,?,?,?)", (thread_id, thread_id, now, now))
            self.db.execute(
                "INSERT INTO rounds(rid,thread_id,kind,source_mode,spec_json,rendered_prompt,"
                "prompt_sha256,out_path,request_key,parent_rid,state,created_at,updated_at,"
                "schema_version,not_before) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, thread_id, kind, None, spec_json, prompt, psha, out_path,
                 request_key, parent_rid, state, now, now, SCHEMA_VERSION, not_before))
            self._event("created", rid=rid,
                        detail=f"kind={kind} state={state} (request-key transfer from {old_rid})")

    def import_round(self, rid: str, kind: str, state: str, *, thread_id: str | None = None,
                     conversation_id: str | None = None, source_mode: str | None = None,
                     out_path: str | None = None, result_text: str | None = None,
                     error_code: str | None = None, completion_confidence: str | None = None) -> None:
        """Insert a round DIRECTLY in a target state — for the one-shot spool→store migration ONLY.
        It is an import, not a transition (there is no prior state), so it bypasses the transition
        table; the migrator is responsible for choosing a legal target state per the documented
        mapping (notably: a sent-but-unconfirmed job maps to possibly_accepted, never queued)."""
        if state not in _LEGAL:
            raise ValueError(f"unknown state {state!r}")
        now = _now()
        with self._tx():
            if thread_id is not None:
                self.db.execute(
                    "INSERT OR IGNORE INTO threads(thread_id,conversation_id,created_at,updated_at) "
                    "VALUES(?,?,?,?)", (thread_id, conversation_id, now, now))
                if conversation_id is not None:
                    self.db.execute("UPDATE threads SET conversation_id=?,updated_at=? WHERE thread_id=?",
                                    (conversation_id, now, thread_id))
            self.db.execute(
                "INSERT INTO rounds(rid,thread_id,kind,source_mode,state,result_text,error_code,"
                "completion_confidence,out_path,created_at,updated_at,schema_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, thread_id, kind, source_mode, state, result_text, error_code,
                 completion_confidence, out_path, now, now, SCHEMA_VERSION))
            self._event("imported", rid=rid, detail=f"state={state}")

    def get_round(self, rid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM rounds WHERE rid=?", (rid,)).fetchone()
        return dict(row) if row else None

    def set_state(self, rid: str, new_state: str, *, expect: str | None = None, **fields) -> None:
        """Transition `rid` to `new_state`, enforcing the legal-transition table. `expect` is a CAS
        guard: if given, the current state must equal it or the call raises (lost-update safe).
        Extra keyword fields (result_text, error_code, conversation_id via thread, etc.) are written
        on the round row in the same transaction."""
        with self._tx():
            row = self.db.execute("SELECT state FROM rounds WHERE rid=?", (rid,)).fetchone()
            if row is None:
                raise KeyError(rid)
            cur = row["state"]
            if expect is not None and cur != expect:
                raise IllegalTransition(f"{rid}: expected state {expect!r}, found {cur!r}")
            # Terminals are IMMUTABLE. A same-state write (new_state == cur) would otherwise bypass
            # the transition table, letting a second/racing callback overwrite a terminal round's
            # result_text/error_code — the diff-review's worker-overlap "answer replacement" path.
            # A completed/blocked/failed/gate_rejected round is final; nothing may rewrite it.
            if cur in TERMINAL:
                raise IllegalTransition(f"{rid}: {cur!r} is terminal — no further writes")
            if new_state != cur and new_state not in _LEGAL.get(cur, set()):
                raise IllegalTransition(f"{rid}: {cur!r} -> {new_state!r} is not a legal transition")
            self._apply_round_fields(rid, state=new_state, **fields)
            self._event("state", rid=rid, detail=f"{cur} -> {new_state}")

    def _apply_round_fields(self, rid: str, **fields) -> None:
        cols = {k: v for k, v in fields.items() if v is not None}
        cols["updated_at"] = _now()
        assigns = ",".join(f"{k}=?" for k in cols)
        self.db.execute(f"UPDATE rounds SET {assigns} WHERE rid=?", (*cols.values(), rid))

    # ---- claiming ------------------------------------------------------------
    def claim_ready(self, daemon_instance_id: str, retrieve_only: bool = False) -> dict | None:
        """Atomically pick ONE queued round and move it to `ready`, returning it — the store-native
        replacement for the pending→processing rename. IMMEDIATE transaction + single-writer means
        two daemons cannot both claim the same round (and phase 5 enforces one daemon anyway).

        ONE IN-FLIGHT SEND PER CONVERSATION. A follow-up whose thread already has a live round is
        not claimed at all — a thread is a single shared composer and a single ordered transcript,
        so two overlapping sends into it are two writers on one mutable resource. The per-conversation
        mutator lease alone does not cover this: it is released once the send LANDS, leaving the whole
        answer wait unguarded, and a second follow-up sent then interleaves its turn into a thread the
        first round is still being answered from. Enforced here rather than by holding that lease
        longer, because refusing at claim time spawns no worker and therefore cannot spin: the round
        stays QUEUED and is picked up the moment the thread frees. Scoped to follow-ups — a `retrieve`
        is read-only and IS the recovery path, so it must never be fenced out of a busy thread — and
        to genuinely live states, since an unreconciled `possibly_accepted` would otherwise block its
        thread forever.

        `not_before`: a round requeued with a backoff (cgc_backend._gate_requeue_or_block) is not
        eligible until that instant — this is the ONE place that fence has to be enforced, since it
        is claiming, not requeuing, that makes a round hot-spinnable again.

        THE UI-CONTRACT GATE, for the same reason: a round that needs a capability recorded broken
        is skipped, not failed. It stays QUEUED and intact, and is claimed once a probe clears it.
        Read inside this transaction, so a capability recorded broken before the claim is seen.
        `retrieve_only` (a UI probe is running): only read-only retrieves are claimed; sends wait.

        USE-BY, swept here first: a queued round past its use-by fails in this transaction, whether or
        not the gate holds it. A gated round never reaches the worker's own use-by check, so without
        this it would sit queued past its deadline — refire refusing it, a same-key retry getting it
        back. (A claimed round is re-checked by the worker before begin_send and by the driver at the
        click.)"""
        now = _now()
        with self._tx():
            for q in self.db.execute("SELECT rid,spec_json FROM rounds WHERE state=? "
                                     "AND spec_json LIKE '%\"use_by\"%'", (QUEUED,)).fetchall():
                use_by = use_by_of(q["spec_json"])
                if use_by and past(use_by):
                    self._apply_round_fields(q["rid"], **_expired_fields(use_by))
                    self._event("state", rid=q["rid"], detail=f"{QUEUED} -> {FAILED} (use-by passed)")
            broken = self.broken_capabilities().keys()
            # Nothing broken (the common case): the first eligible row, as before. Otherwise walk the
            # cursor in order and stop at the first round that needs nothing broken.
            cur = self.db.execute(
                "SELECT rid,kind,spec_json FROM rounds r WHERE r.state=? "
                + ("AND r.kind='retrieve' " if retrieve_only else "") +
                "AND (r.not_before IS NULL OR r.not_before<=?) "
                "AND ("
                "  r.kind!='followup' OR r.thread_id IS NULL OR NOT EXISTS ("
                "    SELECT 1 FROM rounds o WHERE o.thread_id=r.thread_id AND o.rid!=r.rid"
                f"     AND o.state IN (?,?,?)))"
                " ORDER BY r.created_at" + ("" if broken else " LIMIT 1"),
                (QUEUED, now, SENDING, ACCEPTED, WAITING))
            row = next((c for c in cur
                        if not broken & set(round_needs(c["kind"], c["spec_json"]))), None)
            cur.close()
            if row is None:
                return None
            rid = row["rid"]
            self._apply_round_fields(rid, state=READY)
            self._event("claimed", rid=rid, detail=f"daemon={daemon_instance_id}")
        return self.get_round(rid)

    # ---- the send lifecycle (attempts) --------------------------------------
    def begin_send(self, rid: str, rendered_prompt: str, prompt_sha256: str, *,
                   daemon_instance_id: str, browser_epoch: int | None = None) -> str:
        """Durably commit `ready -> sending` and record the exact bytes about to leave, RETURNING a
        fresh attempt_id. The caller performs the browser click ONLY after this returns — so a crash
        between here and the click leaves a durable `sending`, which recovery treats as uncertain and
        never auto-resends. Storing the rendered bytes + hash makes the sent payload auditable and
        lets a superseded attempt be fenced by identity later."""
        attempt_id = _new_id()
        now = _now()
        with self._tx():
            row = self.db.execute("SELECT state FROM rounds WHERE rid=?", (rid,)).fetchone()
            if row is None:
                raise KeyError(rid)
            if row["state"] != READY:
                raise IllegalTransition(f"{rid}: begin_send requires state 'ready', found {row['state']!r}")
            self.db.execute(
                "INSERT INTO attempts(attempt_id,rid,daemon_instance_id,browser_epoch,phase,"
                "started_at,last_progress_at) VALUES(?,?,?,?,?,?,?)",
                (attempt_id, rid, daemon_instance_id, browser_epoch, SENDING, now, now))
            self._apply_round_fields(rid, state=SENDING, rendered_prompt=rendered_prompt,
                                     prompt_sha256=prompt_sha256, current_attempt_id=attempt_id)
            # A not-sent proof covers the attempts before this one (a proven not-sent requeue);
            # this attempt may click, so the round no longer carries it.
            self.db.execute("UPDATE rounds SET send_disposition=NULL WHERE rid=?", (rid,))
            self._event("begin_send", rid=rid, attempt_id=attempt_id,
                        detail=f"epoch={browser_epoch} sha256={prompt_sha256[:16]}")
        return attempt_id

    def _attempt_round(self, attempt_id: str) -> str:
        row = self.db.execute("SELECT rid FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return row["rid"]

    def mark_accepted(self, attempt_id: str, conversation_id: str | None = None,
                      evidence_json: str | None = None, model_badge: str | None = None) -> None:
        """The click landed: the RID (and ideally a conversation id) is observed. conversation_id may
        be None if the RID landed but the URL has not stabilised yet — `accepted` tolerates that.

        model_badge is the composer switcher's PRE-SEND reading, committed in the same transaction
        as the accept because that is the only moment it is true: it describes the send that just
        happened, and the composer has moved on by the time the answer arrives. It is selection
        evidence and nothing more — the producer is recorded separately, at finish()."""
        rid = self._attempt_round(attempt_id)
        now = _now()
        with self._tx():
            r = self.db.execute("SELECT state,thread_id FROM rounds WHERE rid=?", (rid,)).fetchone()
            if r["state"] not in (SENDING, POSSIBLY_ACCEPTED):
                raise IllegalTransition(f"{rid}: mark_accepted from {r['state']!r}")
            self.db.execute(
                "UPDATE attempts SET phase=?,last_progress_at=?,"
                "remote_evidence_json=COALESCE(?,remote_evidence_json) WHERE attempt_id=?",
                (ACCEPTED, now, evidence_json, attempt_id))
            if conversation_id:
                # A fresh submit has no thread until its conversation is first known (here). Create
                # and link one keyed by the conversation id, so the id is never lost — followup and
                # retrieve depend on it. An existing thread just gets its conversation set/updated.
                tid = r["thread_id"] or conversation_id
                self.db.execute(
                    "INSERT OR IGNORE INTO threads(thread_id,conversation_id,created_at,updated_at) "
                    "VALUES(?,?,?,?)", (tid, conversation_id, now, now))
                self.db.execute("UPDATE threads SET conversation_id=?,updated_at=? WHERE thread_id=?",
                                (conversation_id, now, tid))
                if not r["thread_id"]:
                    self.db.execute("UPDATE rounds SET thread_id=? WHERE rid=?", (tid, rid))
            self._apply_round_fields(rid, state=ACCEPTED, model_badge=model_badge)
            self._event("accepted", rid=rid, attempt_id=attempt_id,
                        detail=f"conv={conversation_id or 'pending'}")

    def mark_possibly_accepted(self, attempt_id: str, reason: str) -> None:
        """An abnormal exit while `sending` (or a wait against an accepted round that lost its
        session). The send MAY have reached ChatGPT — recovery will NOT auto-resend; a human or an
        explicit reconciliation must resolve it. This is the anti-duplicate invariant."""
        rid = self._attempt_round(attempt_id)
        with self._tx():
            r = self.db.execute("SELECT state FROM rounds WHERE rid=?", (rid,)).fetchone()
            if r["state"] not in (SENDING, ACCEPTED, WAITING):
                raise IllegalTransition(f"{rid}: mark_possibly_accepted from {r['state']!r}")
            self.db.execute("UPDATE attempts SET phase=?,last_progress_at=? WHERE attempt_id=?",
                            (POSSIBLY_ACCEPTED, _now(), attempt_id))
            self._apply_round_fields(rid, state=POSSIBLY_ACCEPTED, error_code=reason)
            self._event("possibly_accepted", rid=rid, attempt_id=attempt_id, detail=reason)

    def mark_send_not_sent(self, attempt_id: str, evidence: str) -> None:
        """A pre-click failure the CDP driver's OWN fail-closed boundary proved preceded the send
        click (cdp_consult.py's EXIT_NOT_SENT_PRECLICK) — unlike mark_possibly_accepted, this is not
        uncertain: the driver's control flow guarantees the click was never issued for this attempt.
        Goes straight to FAILED (the one FAILED edge a `sending` attempt may take) and stamps
        send_disposition=NOT_SENT_PROVEN in the SAME transaction, so _release_eligible treats it as
        release-eligible immediately — the driver's proof already IS the reconcile, no operator step
        needed. Distinct from record_not_sent_proof, which is an OPERATOR act and deliberately refuses
        a still-`sending` round (it cannot itself prove no race with a live attempt); this method may
        only be called by the same call stack that owns `attempt_id`, so no such race exists here."""
        rid = self._attempt_round(attempt_id)
        with self._tx():
            r = self.db.execute("SELECT state FROM rounds WHERE rid=?", (rid,)).fetchone()
            if r["state"] != SENDING:
                raise IllegalTransition(f"{rid}: mark_send_not_sent from {r['state']!r}")
            self.db.execute("UPDATE attempts SET phase=?,last_progress_at=? WHERE attempt_id=?",
                            (FAILED, _now(), attempt_id))
            self._apply_round_fields(rid, state=FAILED, error_code=evidence,
                                     send_disposition=NOT_SENT_PROVEN)
            self._event("send_not_sent", rid=rid, attempt_id=attempt_id, detail=evidence)

    def mark_waiting(self, rid: str) -> None:
        self.set_state(rid, WAITING, expect=None)

    def finish(self, rid: str, state: str, *, result_text: str | None = None,
               error_code: str | None = None, model_slug: str | None = None,
               attribution: str | None = None) -> None:
        """Terminal commit. A completed_verified/unverified MUST carry its result text in the same
        transaction — status and result are one fact, never two files that can disagree.

        model_slug/attribution ride the SAME transaction for the same reason, and because a round
        is terminal-immutable: there is no later moment at which its identity evidence could be
        written. They are strictly ORTHOGONAL to `state` and `completion_confidence`, which keep
        meaning exactly what they meant before — whether the answer's own rid sentinel verified.
        A mismatched attribution does not make a verified answer unverified, and an unverified one
        is not redeemed by a matching slug; conflating those two properties is the defect this
        column exists to undo."""
        if state not in (COMPLETED_VERIFIED, COMPLETED_UNVERIFIED, BLOCKED, FAILED):
            raise ValueError(f"{state!r} is not a terminal finish state")
        confidence = ("verified" if state == COMPLETED_VERIFIED
                      else "unverified" if state == COMPLETED_UNVERIFIED else None)
        self.set_state(rid, state, result_text=result_text, error_code=error_code,
                       completion_confidence=confidence, model_slug=model_slug,
                       attribution=attribution)

    def adopt_retrieved_answer(self, rid: str, result_text: str,
                               conversation_id: str | None = None,
                               model_slug: str | None = None,
                               attribution: str | None = None) -> bool:
        """Close an uncertain round with the answer a VERIFIED retrieve just proved is its own.

        The retrieve waiter matched END_RESPONSE:<rid> on the thread, which settles both open
        questions at once: the send DID land, and this text is what it produced. Without carrying
        that proof back, a round stays uncertain forever with its answer already in hand — and an
        uncertain round is not free, it holds the tab sweep and keeps offering itself as unfinished
        business to every human who reads `status`. Returns False if the round is already terminal
        (someone resolved it first); a still-`sending` round is NOT adopted — its own live attempt
        owns the row.

        Only ever called with a sentinel-VERIFIED answer: an unwrapped salvage cannot prove which
        turn it came from, so it must not close a round that no worker confirmed.

        The attribution carries back with the answer for the same reason: the retrieve read the slug
        off THIS round's own turn, so it is this round's fact. Dropping it would leave the source
        NULL, and NULL means "predates attribution" — which would be a lie about a round whose
        producer we actually saw."""
        now = _now()
        with self._tx():
            row = self.db.execute("SELECT state,thread_id FROM rounds WHERE rid=?", (rid,)).fetchone()
            # SENDING and WAITING are the states a live worker drives; terminals are immutable.
            # Callers additionally probe the rid lease — this is the last fence, not the only one.
            if row is None or row["state"] in TERMINAL or row["state"] in (SENDING, WAITING):
                return False
            # Every state that reaches here (ready/accepted/possibly_accepted) has a legal edge to
            # WAITING — take it rather than inventing a shortcut edge into a terminal, so the
            # transition table stays the one description of the lifecycle.
            if WAITING not in _LEGAL.get(row["state"], set()):
                return False
            self._apply_round_fields(rid, state=WAITING)
            self._event("state", rid=rid,
                        detail=f"{row['state']} -> {WAITING} (retrieve proved the send landed)")
            if conversation_id:
                # The retrieve proved WHERE this round lives, and a completed round without that
                # address is not continuable: `--parent <rid>` refuses, `latest_conversation` skips
                # it, and the follow-up silently degrades into a fresh consult that throws the
                # thread's context away. Recovering the answer but not the thread is half a recovery.
                tid = row["thread_id"] or conversation_id
                self.db.execute(
                    "INSERT OR IGNORE INTO threads(thread_id,conversation_id,created_at,updated_at) "
                    "VALUES(?,?,?,?)", (tid, conversation_id, now, now))
                self.db.execute("UPDATE threads SET conversation_id=?,updated_at=? WHERE thread_id=?",
                                (conversation_id, now, tid))
                self.db.execute("UPDATE rounds SET thread_id=? WHERE rid=?", (tid, rid))
            self._apply_round_fields(rid, state=COMPLETED_VERIFIED, result_text=result_text,
                                     completion_confidence="verified", model_slug=model_slug,
                                     attribution=attribution)
            self._event("state", rid=rid, detail=f"-> {COMPLETED_VERIFIED} (adopted from retrieve)")
        return True

    def gate_reject(self, rid: str, reason: str) -> None:
        self.set_state(rid, GATE_REJECTED, error_code=reason)

    # ---- recovery ------------------------------------------------------------
    def recover(self) -> dict:
        """Classify every non-terminal round for the daemon, WITHOUT mutating anything. The one rule
        that matters: `sending` and `possibly_accepted` are never 'dispatchable' — they may already
        have reached ChatGPT, so re-sending could duplicate a consult.

        Returns {dispatchable, reattach, uncertain} lists of rid, where:
          - dispatchable: queued/ready — no side effect yet, safe to (re)send.
          - reattach:     accepted/waiting — a conversation exists or the RID landed; resume polling.
          - uncertain:    sending/possibly_accepted — needs explicit reconciliation, NEVER auto-send.
        """
        out = {"dispatchable": [], "reattach": [], "uncertain": [], "retrievable": []}
        for row in self.db.execute("SELECT rid,state FROM rounds WHERE state NOT IN (%s)"
                                    % ",".join("?" * len(TERMINAL)), tuple(sorted(TERMINAL))):
            s = row["state"]
            rid = row["rid"]
            if s in (QUEUED, READY):
                out["dispatchable"].append(rid)
            elif s in (ACCEPTED, WAITING):
                out["reattach"].append(rid)
            elif s in UNCERTAIN:
                out["uncertain"].append(rid)
                # A possibly_accepted round WITH a known conversation can be re-attached READ-ONLY:
                # the waiter verifies END_RESPONSE:<rid>, so it completes ONLY if THIS round's own
                # answer is actually present on the thread, and it NEVER re-sends. That makes one
                # automatic retrieve safe — it recovers the common "accepted, then the waiter died or
                # timed out" stranding (which otherwise sits uncertain until a human runs `wait` by
                # hand) without ever risking a duplicate. Bounded to a single auto-attempt by an event
                # marker, so a send that truly never landed (no matching sentinel) falls back to the
                # human after one try instead of looping a full wait budget forever.
                if s == POSSIBLY_ACCEPTED and self.conversation_of(rid) and not self.was_auto_retrieved(rid):
                    out["retrievable"].append(rid)
        return out

    def link_conversation(self, rid: str, conversation_id: str) -> None:
        """Record WHERE a round lives without claiming anything about whether its send landed.

        `mark_accepted` couples those two facts, but an uncertain send needs them apart: the click is
        unconfirmed while the conversation is known, and it is precisely that pairing which makes the
        round retrievable (read-only, verified by its own END_RESPONSE sentinel) instead of a dead end
        a human has to resolve by hand. State is untouched, so this can never launder an unconfirmed
        send into a confirmed one."""
        now = _now()
        with self._tx():
            r = self.db.execute("SELECT state,thread_id FROM rounds WHERE rid=?", (rid,)).fetchone()
            if r is None:
                raise IllegalTransition(f"{rid}: no such round")
            if r["state"] in TERMINAL:
                raise IllegalTransition(f"{rid}: {r['state']!r} is terminal — no further writes")
            tid = r["thread_id"] or conversation_id
            self.db.execute(
                "INSERT OR IGNORE INTO threads(thread_id,conversation_id,created_at,updated_at) "
                "VALUES(?,?,?,?)", (tid, conversation_id, now, now))
            self.db.execute("UPDATE threads SET conversation_id=?,updated_at=? WHERE thread_id=?",
                            (conversation_id, now, tid))
            if not r["thread_id"]:
                self.db.execute("UPDATE rounds SET thread_id=? WHERE rid=?", (tid, rid))
            self._event("conversation_linked", rid=rid, detail=conversation_id)

    def attempt_count(self, rid: str) -> int:
        """How many times a send has been BEGUN for this round. begin_send writes exactly one row per
        attempt, so this is the honest retry counter — the thing that tells a caller whether a
        'transient' failure is actually transient."""
        return self.db.execute("SELECT count(*) c FROM attempts WHERE rid=?", (rid,)).fetchone()["c"]

    def conversation_of(self, rid: str) -> str | None:
        """The ChatGPT conversation id addressable for this round via its thread, or None."""
        row = self.db.execute(
            "SELECT t.conversation_id c FROM rounds r JOIN threads t ON r.thread_id=t.thread_id "
            "WHERE r.rid=?", (rid,)).fetchone()
        return row["c"] if row and row["c"] else None

    def latest_conversation(self, exclude_rid: str | None = None) -> str | None:
        """The conversation of the most-recently-completed consult — the 'active thread' a follow-up
        with `--conversation auto`/`last` continues. This is what restores zero-bookkeeping follow-up
        in the store world: the store, not a conversation id the agent had to remember and pass,
        knows which thread you last got an answer from. Only COMPLETED rounds qualify — you cannot
        continue a thread that has not answered yet — and the new follow-up round excludes itself."""
        row = self.db.execute(
            "SELECT t.conversation_id c FROM rounds r JOIN threads t ON r.thread_id=t.thread_id "
            "WHERE r.state IN (?,?) AND t.conversation_id IS NOT NULL AND r.rid != ? "
            "ORDER BY r.updated_at DESC LIMIT 1",
            (COMPLETED_VERIFIED, COMPLETED_UNVERIFIED, exclude_rid or "")).fetchone()
        return row["c"] if row else None

    def latest_conversation_strict(self, exclude_rid: str | None = None):
        """The 'active thread' for a bare `--conversation auto` follow-up — but REFUSED when the
        causal target is genuinely ambiguous. Returns (conversation_id, reason): exactly one of the
        two is set.

        Ambiguity rules (an agent must then pass --parent <rid>, which is always unambiguous):
          - another consult is anywhere PRE-TERMINAL (queued/ready/sending/accepted/waiting/
            possibly_accepted): the not-yet-finished one may be the intended target and may
            complete at any moment — 'latest completed' is a race, not an identity. queued/ready
            count too: a consult waiting to start is exactly as much a competing target as one
            mid-send.
          - the two most recently active DISTINCT threads finished within 30 minutes of each
            other: two answers that close together make 'the last one' an accident of ordering,
            not an intent. Compared per-THREAD (MAX(updated_at) per conversation), so a thread
            with several recent rounds cannot hide a near-simultaneous completion on another."""
        inflight = self.db.execute(
            "SELECT count(*) c FROM rounds WHERE state IN (?,?,?,?,?,?) AND rid != ?",
            (QUEUED, READY, SENDING, ACCEPTED, WAITING, POSSIBLY_ACCEPTED,
             exclude_rid or "")).fetchone()["c"]
        if inflight:
            return None, (f"ambiguous: {inflight} consult(s) still in flight — 'the last thread' is "
                          "a race. Pass --parent <rid> to name the consult you are continuing.")
        rows = self.db.execute(
            "SELECT t.conversation_id c, MAX(r.updated_at) u FROM rounds r "
            "JOIN threads t ON r.thread_id=t.thread_id "
            "WHERE r.state IN (?,?) AND t.conversation_id IS NOT NULL AND r.rid != ? "
            "GROUP BY t.conversation_id ORDER BY u DESC LIMIT 2",
            (COMPLETED_VERIFIED, COMPLETED_UNVERIFIED, exclude_rid or "")).fetchall()
        if not rows:
            return None, "none: no completed consult exists to continue"
        if len(rows) == 2:
            try:
                dt = abs((datetime.datetime.fromisoformat(rows[0]["u"])
                          - datetime.datetime.fromisoformat(rows[1]["u"])).total_seconds())
            except ValueError:
                dt = 0.0  # unparsable timestamps: treat as close together, fail toward refusal
            if dt < 1800:
                return None, (f"ambiguous: the two most recently completed threads finished within "
                              f"{int(dt)}s of each other (conversations {rows[0]['c']}, "
                              f"{rows[1]['c']}). Pass --parent <rid>.")
        return rows[0]["c"], None

    def claim_auto_continue(self, rid: str, prompt_sha256: str) -> bool:
        """ATOMICALLY claim the one automatic "continue" a round gets after ChatGPT marked its turn
        failed. The marker is written BEFORE the send, so a crash anywhere after it can never lead to
        a second continue — at most one extra message per round, ever. The round stays `waiting`: the
        original send landed (its failed turn is on the page), and the continue carries the same rid,
        so the round's own wait simply moves to the newer turn."""
        with self._tx():
            row = self.db.execute("SELECT state FROM rounds WHERE rid=?", (rid,)).fetchone()
            if row is None or row["state"] != WAITING:
                return False
            if self.db.execute("SELECT 1 FROM events WHERE rid=? AND kind='auto_continue' LIMIT 1",
                               (rid,)).fetchone() is not None:
                return False
            self._event("auto_continue", rid=rid, detail=f"sha256={prompt_sha256[:16]}")
        return True

    def record_auto_continue(self, rid: str, outcome: str) -> None:
        """The claimed continue's send outcome: landed | not_sent | unsure. A claim with no outcome
        means the worker died somewhere between claim and result — i.e. possibly after the click."""
        with self._tx():
            self._event("auto_continue_result", rid=rid, detail=outcome)

    def auto_continue_state(self, rid: str) -> str | None:
        """None (never claimed) | claimed (no recorded outcome) | landed | not_sent | unsure."""
        if self.db.execute("SELECT 1 FROM events WHERE rid=? AND kind='auto_continue' LIMIT 1",
                           (rid,)).fetchone() is None:
            return None
        row = self.db.execute("SELECT detail FROM events WHERE rid=? AND kind='auto_continue_result' "
                              "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        return row["detail"] if row else "claimed"

    def was_auto_retrieved(self, rid: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM events WHERE rid=? AND kind='auto_retrieve' LIMIT 1", (rid,)).fetchone()
        return row is not None

    def claim_auto_retrieve(self, rid: str) -> bool:
        """ATOMICALLY claim the one-shot read-only retrieve of a possibly_accepted round: in a single
        transaction, verify it is still possibly_accepted and not already claimed, record the
        auto_retrieve marker, AND transition possibly_accepted -> waiting. Returns True if this caller
        won the claim. Doing the marker and the state change in ONE transaction fixes the diff-review
        race where a crash between two separate commits left the round possibly_accepted WITH the
        marker — permanently excluded from `retrievable`, its recovery silently lost. Either both land
        or neither does."""
        with self._tx():
            row = self.db.execute("SELECT state FROM rounds WHERE rid=?", (rid,)).fetchone()
            if row is None or row["state"] != POSSIBLY_ACCEPTED:
                return False
            if self.db.execute("SELECT 1 FROM events WHERE rid=? AND kind='auto_retrieve' LIMIT 1",
                               (rid,)).fetchone() is not None:
                return False
            self._event("auto_retrieve", rid=rid, detail="one-shot read-only reattach of possibly_accepted")
            self._apply_round_fields(rid, state=WAITING)
            self._event("state", rid=rid, detail=f"{POSSIBLY_ACCEPTED} -> {WAITING} (auto-retrieve)")
        return True

    def promote_sending_to_uncertain(self, rids: list[str] | None = None,
                                     reason: str | None = None) -> list[str]:
        """Recovery step: a round left in `sending` whose worker is DEAD is uncertain. Move it to
        `possibly_accepted` (durably, so it is never mistaken for dispatchable) and return the
        affected rids. Idempotent. `rids` narrows the promotion to rounds the caller has PROVEN
        ownerless (its own reaped child, or a lease-free round at startup) — a blanket promotion
        would mutate a `sending` row a surviving prior-generation worker still owns."""
        moved = []
        with self._tx():
            for row in self.db.execute("SELECT rid,current_attempt_id FROM rounds WHERE state=?",
                                       (SENDING,)):
                rid = row["rid"]
                if rids is not None and rid not in rids:
                    continue
                if row["current_attempt_id"]:
                    self.db.execute("UPDATE attempts SET phase=? WHERE attempt_id=?",
                                    (POSSIBLY_ACCEPTED, row["current_attempt_id"]))
                self._apply_round_fields(
                    rid, state=POSSIBLY_ACCEPTED,
                    error_code=reason or "daemon died while sending — uncertain, not resent")
                self._event("recover_sending", rid=rid, detail="sending -> possibly_accepted")
                moved.append(rid)
        return moved

    # ---- events (append-only diagnostic history) -----------------------------
    def _event(self, kind: str, *, rid: str | None = None, attempt_id: str | None = None,
               detail: str | None = None) -> None:
        self.db.execute("INSERT INTO events(rid,attempt_id,kind,detail,at) VALUES(?,?,?,?,?)",
                        (rid, attempt_id, kind, detail, _now()))

    def record_outcome(self, rid: str, outcome: dict) -> None:
        """A local grade of one round's answer. An event, never a column: grading must not move
        rounds.updated_at (completion-latency stats read it), needs no migration, and the LATEST
        consult_outcome event per rid is the grade that stands."""
        with self._tx():
            self._event("consult_outcome", rid=rid, detail=json.dumps(outcome, sort_keys=True))

    def record_policy_update(self, policy_rev: str, text: str) -> None:
        """The record that the weekly review happened and what it changed."""
        with self._tx():
            self._event("policy_update",
                        detail=json.dumps({"policy_rev": policy_rev, "text": text}, sort_keys=True))

    # ---- the UI contract (see CAPABILITIES) ------------------------------------
    def capabilities(self) -> dict:
        """{name: record} for every capability ever recorded; a never-probed one is absent."""
        return {r["key"][3:]: _spec(r["value"]) for r in self.db.execute(
            "SELECT key,value FROM meta WHERE key LIKE 'ui.%'") if r["key"][3:] in CAPABILITIES}

    def broken_capabilities(self) -> dict:
        return {k: v for k, v in self.capabilities().items() if v.get("ok") is False}

    def capability_epoch(self) -> int:
        """The counter every capability write advances. A probe reads it BEFORE it starts."""
        row = self.db.execute("SELECT value FROM meta WHERE key='ui.epoch'").fetchone()
        return int(row["value"]) if row else 0

    def _write_capability(self, name: str, ok: bool, *, phase: str, cause: str | None,
                          ref: str | None, driver_rev: str | None) -> dict:
        if name not in CAPABILITIES:
            raise ValueError(f"unknown capability {name!r}")
        epoch = self.capability_epoch() + 1
        rec = {"ok": ok, "epoch": epoch, "phase": phase, "cause": (cause or "")[:400] or None,
               "rid_or_conversation": ref, "checked_at": _now(), "driver_rev": driver_rev}
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('ui.epoch',?)", (str(epoch),))
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        ("ui." + name, json.dumps(rec, sort_keys=True)))
        self._event("ui_" + ("pass" if ok else "broken"),
                    detail=json.dumps({name: rec}, sort_keys=True))
        return rec

    def mark_broken(self, name: str, *, phase: str, cause: str, ref: str | None = None,
                    driver_rev: str | None = None) -> dict:
        """Record that `name` no longer matches ChatGPT's page. Always written: a failure is never
        refused, whatever happened before it."""
        with self._tx():
            return self._write_capability(name, False, phase=phase, cause=cause, ref=ref,
                                          driver_rev=driver_rev)

    def mark_passed(self, name: str, *, started_epoch: int, phase: str, cause: str | None = None,
                    ref: str | None = None, driver_rev: str | None = None) -> bool:
        """Record a probe's pass, unless `name` was written after the probe started (its record's
        epoch is past `started_epoch`): a real round's failure seen mid-probe is newer evidence than
        a pass that began before it, so the stale pass is dropped. Read and write are one
        transaction, so no failure can land between the comparison and the write. False when
        dropped."""
        with self._tx():
            cur = self.capabilities().get(name)
            if cur is not None and int(cur.get("epoch") or 0) > started_epoch:
                return False
            self._write_capability(name, True, phase=phase, cause=cause, ref=ref,
                                   driver_rev=driver_rev)
        return True

    def gated_by(self, r: dict) -> dict:
        """The broken capabilities this round's send needs ({} = not gated)."""
        need = set(round_needs(r.get("kind") or "", r.get("spec_json")))
        return {k: v for k, v in self.broken_capabilities().items() if k in need}

    def record_event(self, kind: str, *, rid: str | None = None, detail: str | None = None) -> None:
        """A diagnostic event, in its own transaction; moves no round."""
        with self._tx():
            self._event(kind, rid=rid, detail=detail)

    def record_attempt_evidence(self, attempt_id: str, evidence_json: str) -> None:
        """The driver's typed outcome for one send attempt (cgc_backend._project_send)."""
        with self._tx():
            self.db.execute("UPDATE attempts SET remote_evidence_json=? WHERE attempt_id=?",
                            (evidence_json, attempt_id))

    def send_evidence(self, rid: str) -> dict | None:
        """The current attempt's recorded driver outcome, or None (no attempt, or none recorded)."""
        row = self.db.execute(
            "SELECT a.remote_evidence_json v FROM rounds r JOIN attempts a "
            "ON a.attempt_id=r.current_attempt_id WHERE r.rid=?", (rid,)).fetchone()
        return (_spec(row["v"]) or None) if row and row["v"] else None

    def events(self, rid: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM events WHERE rid=? ORDER BY id", (rid,))]


_DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS threads (
  thread_id       TEXT PRIMARY KEY,
  conversation_id TEXT,
  -- DELIBERATELY STILL UNWRITTEN, and not repurposed for the model evidence added in schema 5.
  -- A thread is not a model: it can span a picker change between rounds, and — as the 2026-09-06
  -- attribution work established — a single thread can be served by different models on different
  -- turns without anything local changing at all. So there is no true value to put here; a
  -- "thread's model" could only ever be one round's fact wearing the wrong grain. The evidence
  -- lives on rounds (model_badge / model_slug / attribution), where it is true. Left in place
  -- rather than dropped: it is NULL in every row, costs nothing, and dropping a column from a
  -- live DB under a running daemon buys less than it risks.
  model           TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
  rid                   TEXT PRIMARY KEY,
  thread_id             TEXT REFERENCES threads(thread_id),
  kind                  TEXT NOT NULL,
  source_mode           TEXT,
  spec_json             TEXT,
  rendered_prompt       TEXT,
  prompt_sha256         TEXT,
  state                 TEXT NOT NULL,
  result_text           TEXT,
  error_code            TEXT,
  completion_confidence TEXT,
  current_attempt_id    TEXT,
  out_path              TEXT,
  request_key           TEXT,
  parent_rid            TEXT,
  send_disposition      TEXT,
  not_before            TEXT,
  gate_retry_count      INTEGER NOT NULL DEFAULT 0,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  schema_version        INTEGER NOT NULL DEFAULT 1,
  -- Schema 5. Declared LAST because ALTER TABLE ADD COLUMN appends, and a fresh DB must have the
  -- identical column ORDER a migrated one gets — same shape, not merely the same column names.
  model_badge           TEXT,   -- pre-send selection evidence (what was REQUESTED)
  model_slug            TEXT,   -- provider attribution off the answer node (what SERVED it)
  attribution           TEXT    -- the verdict at completion time; see _MIGRATIONS[5]
);
CREATE INDEX IF NOT EXISTS idx_rounds_state ON rounds(state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rounds_request_key
  ON rounds(request_key) WHERE request_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS attempts (
  attempt_id          TEXT PRIMARY KEY,
  rid                 TEXT NOT NULL REFERENCES rounds(rid),
  daemon_instance_id  TEXT,
  browser_epoch       INTEGER,
  phase               TEXT NOT NULL,
  started_at          TEXT NOT NULL,
  last_progress_at    TEXT,
  remote_evidence_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_rid ON attempts(rid);
CREATE TABLE IF NOT EXISTS browser_state (
  singleton_key INTEGER PRIMARY KEY CHECK (singleton_key = 1),
  epoch         INTEGER NOT NULL,
  chrome_pid    INTEGER,
  health        TEXT,
  restart_reason TEXT,
  updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  rid        TEXT,
  attempt_id TEXT,
  kind       TEXT NOT NULL,
  detail     TEXT,
  at         TEXT NOT NULL
);
"""
