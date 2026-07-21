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
# ":memory:" is honoured for tests; the real store lives at a fixed path under the private state dir.
DB_PATH = os.environ.get("CGC_STORE_DB", os.path.join(CGC_STATE_DIR, "control.db"))

SCHEMA_VERSION = 1

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


def _now() -> str:
    """UTC ISO-8601. Durable recovery timestamps must be wall-clock (monotonic does not survive a
    restart); process-local elapsed-time budgets are a separate concern handled by the caller."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def store_enabled() -> bool:
    """The cutover flag. Off (default) → the file-spool path runs unchanged; on → enqueue/await/the
    daemon worker use this store. One switch, one authority — never both at once."""
    return os.environ.get("CGC_STORE_BACKEND", "").strip().lower() in ("1", "true", "yes", "on")


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
        self.path = path or DB_PATH
        if self.path != ":memory:":
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
        self.db.executescript(_DDL)
        cur = self.db.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                            (str(SCHEMA_VERSION),))
        # future: if int(row['value']) < SCHEMA_VERSION: run ordered migrations in one transaction.

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
                     state: str = QUEUED) -> None:
        """Insert a new round. `prompt` (the exact rendered bytes to send) is stored on the round at
        enqueue, so the CLI receipt is just the rid and there is no prompt PATHNAME whose contents
        could drift before the gate reads them — the store row is the one copy."""
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
                "prompt_sha256,out_path,state,created_at,updated_at,schema_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, thread_id, kind, source_mode, spec_json, prompt, psha, out_path, state,
                 now, now, SCHEMA_VERSION))
            self._event("created", rid=rid, detail=f"kind={kind} state={state}")

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
        out = {"dispatchable": [], "reattach": [], "uncertain": []}
        for row in self.db.execute("SELECT rid,state FROM rounds WHERE state NOT IN (%s)"
                                    % ",".join("?" * len(TERMINAL)), tuple(sorted(TERMINAL))):
            s = row["state"]
            if s in (QUEUED, READY):
                out["dispatchable"].append(row["rid"])
            elif s in (ACCEPTED, WAITING):
                out["reattach"].append(row["rid"])
            elif s in UNCERTAIN:
                out["uncertain"].append(row["rid"])
        return out

    def promote_sending_to_uncertain(self) -> list[str]:
        """Recovery step: a round left in `sending` when the daemon restarts means the worker died
        mid-click. Move it to `possibly_accepted` (durably, so it is never mistaken for dispatchable)
        and return the affected rids. Idempotent."""
        moved = []
        with self._tx():
            for row in self.db.execute("SELECT rid,current_attempt_id FROM rounds WHERE state=?",
                                       (SENDING,)):
                rid = row["rid"]
                if row["current_attempt_id"]:
                    self.db.execute("UPDATE attempts SET phase=? WHERE attempt_id=?",
                                    (POSSIBLY_ACCEPTED, row["current_attempt_id"]))
                self._apply_round_fields(rid, state=POSSIBLY_ACCEPTED,
                                         error_code="daemon died while sending — uncertain, not resent")
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
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  schema_version        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_rounds_state ON rounds(state);
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
