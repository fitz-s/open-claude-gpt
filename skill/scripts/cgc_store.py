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

SCHEMA_VERSION = 3

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
}

# The only value ever stored in rounds.send_disposition: an explicit operator reconcile proving the
# round's send never reached ChatGPT (e.g. browser inspection found no such message).
NOT_SENT_PROVEN = "not_sent_proven"


class SchemaTooNew(Exception):
    """The DB was written by a NEWER version of this tool. Refuse rather than guess: running old
    code against a newer schema risks silent corruption of durable consult state."""

# ---- the round state machine -------------------------------------------------
# Terminal states have no outgoing transitions. `sending` can ONLY go to accepted/possibly_accepted:
# once the click may have happened, "it failed" is not knowable, so there is no sending→failed edge —
# an abnormal exit becomes possibly_accepted. Pre-send failures happen while still `ready`.
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
                     state: str = QUEUED) -> None:
        """Insert a new round. `prompt` (the exact rendered bytes to send) is stored on the round at
        enqueue, so the CLI receipt is just the rid and there is no prompt PATHNAME whose contents
        could drift before the gate reads them — the store row is the one copy. `request_key` is the
        caller's logical-request identity (unique where set — the idempotency anchor); `parent_rid`
        records which consult a follow-up causally continues."""
        if state not in _LEGAL:
            raise ValueError(f"unknown initial state {state!r}")
        now = _now()
        psha = sha256(prompt) if prompt is not None else None
        with self._tx():
            if thread_id is not None:
                self.db.execute(
                    "INSERT OR IGNORE INTO threads(thread_id,created_at,updated_at) VALUES(?,?,?)",
                    (thread_id, now, now))
            self.db.execute(
                "INSERT INTO rounds(rid,thread_id,kind,source_mode,spec_json,rendered_prompt,"
                "prompt_sha256,out_path,request_key,parent_rid,state,created_at,updated_at,"
                "schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, thread_id, kind, source_mode, spec_json, prompt, psha, out_path,
                 request_key, parent_rid, state, now, now, SCHEMA_VERSION))
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
        ChatGPT (e.g. browser inspection found no such message). This explicit act — and ONLY this —
        qualifies a post-send-uncertain FAILED round to later release its request-key; a generic
        FAILED never does, because possibly_accepted->failed and waiting->failed are legal reconciles
        that do not themselves prove no-send. Drives a still-reconcilable round to FAILED as part of
        the act; on an already-FAILED round it just stamps the durable proof."""
        with self._tx():
            row = self.db.execute("SELECT state FROM rounds WHERE rid=?", (rid,)).fetchone()
            if row is None:
                raise KeyError(rid)
            cur = row["state"]
            if cur == FAILED:
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
                    "uncertain or already-failed round, not one still in flight")
            self._event("not_sent_proof", rid=rid, detail=evidence)

    def transfer_request_key(self, old_rid: str, *, rid: str, kind: str,
                             thread_id: str | None = None, out_path: str | None = None,
                             spec_json: str | None = None, prompt: str | None = None,
                             request_key: str, parent_rid: str | None = None,
                             state: str = QUEUED) -> None:
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
                self.db.execute(
                    "INSERT OR IGNORE INTO threads(thread_id,created_at,updated_at) VALUES(?,?,?)",
                    (thread_id, now, now))
            self.db.execute(
                "INSERT INTO rounds(rid,thread_id,kind,source_mode,spec_json,rendered_prompt,"
                "prompt_sha256,out_path,request_key,parent_rid,state,created_at,updated_at,"
                "schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, thread_id, kind, None, spec_json, prompt, psha, out_path,
                 request_key, parent_rid, state, now, now, SCHEMA_VERSION))
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
    def claim_ready(self, daemon_instance_id: str) -> dict | None:
        """Atomically pick ONE queued round and move it to `ready`, returning it — the store-native
        replacement for the pending→processing rename. IMMEDIATE transaction + single-writer means
        two daemons cannot both claim the same round (and phase 5 enforces one daemon anyway)."""
        with self._tx():
            row = self.db.execute(
                "SELECT rid FROM rounds WHERE state=? ORDER BY created_at LIMIT 1", (QUEUED,)
            ).fetchone()
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
            self._event("begin_send", rid=rid, attempt_id=attempt_id,
                        detail=f"epoch={browser_epoch} sha256={prompt_sha256[:16]}")
        return attempt_id

    def _attempt_round(self, attempt_id: str) -> str:
        row = self.db.execute("SELECT rid FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return row["rid"]

    def mark_accepted(self, attempt_id: str, conversation_id: str | None = None,
                      evidence_json: str | None = None) -> None:
        """The click landed: the RID (and ideally a conversation id) is observed. conversation_id may
        be None if the RID landed but the URL has not stabilised yet — `accepted` tolerates that."""
        rid = self._attempt_round(attempt_id)
        now = _now()
        with self._tx():
            r = self.db.execute("SELECT state,thread_id FROM rounds WHERE rid=?", (rid,)).fetchone()
            if r["state"] not in (SENDING, POSSIBLY_ACCEPTED):
                raise IllegalTransition(f"{rid}: mark_accepted from {r['state']!r}")
            self.db.execute(
                "UPDATE attempts SET phase=?,last_progress_at=?,remote_evidence_json=? WHERE attempt_id=?",
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
            self._apply_round_fields(rid, state=ACCEPTED)
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

    def mark_waiting(self, rid: str) -> None:
        self.set_state(rid, WAITING, expect=None)

    def finish(self, rid: str, state: str, *, result_text: str | None = None,
               error_code: str | None = None) -> None:
        """Terminal commit. A completed_verified/unverified MUST carry its result text in the same
        transaction — status and result are one fact, never two files that can disagree."""
        if state not in (COMPLETED_VERIFIED, COMPLETED_UNVERIFIED, BLOCKED, FAILED):
            raise ValueError(f"{state!r} is not a terminal finish state")
        confidence = ("verified" if state == COMPLETED_VERIFIED
                      else "unverified" if state == COMPLETED_UNVERIFIED else None)
        self.set_state(rid, state, result_text=result_text, error_code=error_code,
                       completion_confidence=confidence)

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
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  schema_version        INTEGER NOT NULL DEFAULT 1
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
