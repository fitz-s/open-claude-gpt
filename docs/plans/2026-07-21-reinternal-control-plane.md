# Plan: GROUND-UP-REINTERNAL of the control plane (SQLite send_round boundary)
> Created: 2026-07-21 | Status: IN PROGRESS

## Goal
Replace the file-spool/PID/rename/lock/active.json control plane with one SQLite transactional
store and a single daemon-owned `send_round` boundary, so every recovery path is a transition over
named states — closing the automatic-duplicate window and making the egress gate a real boundary —
while KEEPING the entire browser-facing substrate (CDP, DOM adapters, model selection, launcher).

## Context
- From the corrected first-principles consult (rid REQ-20260721-010230-962710, commit 74ed842):
  verdict **GROUND-UP-REINTERNAL, 0.86**. Browser-facing code is earned; the orchestration around it
  is "mechanically over-engineered but semantically under-modeled."
- The decisive defects (all cross-module): remote-acceptance ambiguity (auto-resend can duplicate a
  consult), fragmented authority (spool status / answer file / active.json can disagree), PID-only
  ownership, absent browser-epoch, gate-path multiplicity, first-END completion (already fixed
  separately: b6051ae).
- Already landed this session (bounded fixes, NOT the rebuild): 191522d no-code gate; 74ed842
  lifecycle_lock fail-closed; b6db01a argv receipts + structured restart reason + daemon singleton;
  b6051ae sentinel last-END anti-injection.
- **Constraints (givens, unchanged):** single-user single-host macOS/launchd; only already-public
  code leaves; never secrets/.env/keys; agent never types credentials; agent never makes the
  external send (auto-mode classifier); daemon is the fail-closed egress gate; CGC_GATE_PRIVATE_REPOS
  stays (user-controlled, agent never sets it).
- **A live daemon may be running with in-flight consults.** Nothing in Phase 1 wires into it, so it
  is unaffected. Later phases drain-then-migrate; never dual-authority; `possibly_accepted` is NEVER
  auto-replayed.

## Approach
Incremental, behind the current CLI, one authority at a time. Build the store as a standalone,
fully-unit-tested module first (zero wiring, zero risk to the running daemon). Then add the
`send_round` boundary wrapping the EXISTING CDP adapter. Then cut the daemon's dispatch over to the
store with a one-shot spool→store migration (queued migrated; processing drained; sent-without-conv
imported as `possibly_accepted`, never replayed). Only after the store is the sole authority do we
delete the file-spool lifecycle machinery. Tests stay green at every phase.

## Target design (from consult §5 — reference, not all in phase 1)
- **State model:** one SQLite DB (WAL, synchronous=FULL for send-critical commits, foreign_keys=ON,
  0600) as the sole authority. Diagnostic logs stay append-only files, not state.
- **Schema:** `threads`, `rounds`, `attempts`, `browser_state`, append-only `events` (diagnostic).
- **Round state machine:**
  `queued → gate_rejected | ready → sending → accepted | possibly_accepted → waiting →
   completed_verified | completed_unverified | blocked | failed`
  - `accepted` may temporarily lack a conversation_id (RID landed, URL not yet stable).
  - `possibly_accepted` is NOT a failure synonym — it is an explicit prohibition on automatic retry.
- **send_round(attempt_id):** the ONLY function that clicks/Enter. Durably commit `ready→sending`
  BEFORE the click; after, commit `accepted(evidence)` or leave `possibly_accepted`. Never
  auto-retry a `possibly_accepted`.
- **Concurrency (later phase):** one scheduler actor, send_concurrency=1, max_active_generations
  configurable (default 1 until a local benchmark justifies more); `browser_epoch` invalidates
  old CDP targets on every launch/restart.
- **Gate (later phase):** typed round spec, not an authorized prompt path; separate source
  authorization from secret screening; daemon is the SOLE send path.

## Tasks

- [x] 1. **Store module foundation** — `skill/scripts/cgc_store.py` (NEW, no wiring) — DONE (16 tests)
  - Schema DDL (threads, rounds, attempts, browser_state, events); WAL/synchronous/foreign_keys/0600;
    `schema_version` + a migrations hook.
  - Round state machine as explicit transition functions with a legal-transition table (illegal
    transition raises). CAS claim of a `queued`/`ready` round.
  - `begin_send(rid) -> attempt_id` commits `ready→sending` + stores rendered bytes + sha256.
  - `mark_accepted(attempt_id, conversation, evidence)`, `mark_possibly_accepted(attempt_id, reason)`,
    `mark_waiting/completed_verified/completed_unverified/blocked/failed`, `gate_reject(rid, reason)`.
  - `recover()` classifier: `sending`/`possibly_accepted` are NEVER returned as auto-resendable.
  - Files: `skill/scripts/cgc_store.py`, `tests/test_store.py`
  - What: pure module + exhaustive unit tests (transitions legal/illegal, CAS, recovery classes,
    concurrent claim via two connections, possibly_accepted-never-replayed).

- [x] 2. **send_round boundary** wrapping the existing CDP adapter — DONE (7 tests; `cgc_send.py`)
  - Files: `skill/scripts/cgc_daemon.py` (or a new `cgc_send.py`), `tests/test_send_round.py`
  - What: `send_round(store, attempt_id, cdp_invoke)` — commit `sending` before invoking CDP submit;
    map CDP `unknown_send` → `possibly_accepted`; success → `accepted(conversation)`; pre-send fail →
    retryable. Test with a stubbed cdp_invoke covering success / unknown_send / pre-send-fail / crash.

- [x] 3a. **Spool→store migration** — `skill/scripts/cgc_migrate.py` + `Store.import_round` — DONE
  - What: read-only spool→store mapping (queued→queued; processing+conv→waiting;
    processing-no-conv→**possibly_accepted, never queued**; done+answer→completed_verified /
    completed_unverified if `.raw` salvage; blocker→blocked; else→failed). Pure `plan_migration()` +
    `migrate_spool_to_store()`; idempotent; never mutates the spool. 5 tests against a synthetic spool.

- [x] 3b. **Daemon dispatch + CLI over the store (the LIVE cutover)** — DONE + verified live
  - `cgc_backend.py` (enqueue_round/await_round/process_round/resume_round); daemon
    run_worker_store + _dispatch_store (claim ready + reattach accepted/waiting, never resend) +
    startup recovery of interrupted `sending`→possibly_accepted; cmd_enqueue/cmd_await delegate
    behind `CGC_STORE_BACKEND`. 11 lifecycle tests with a stubbed CDP.
  - **Cutover executed**: flag persisted in `~/.config/cgc/config`; migration imported 13 rounds
    (0 in-flight); daemon restarted in store mode. **Verified end-to-end on the live system**: a real
    consult flowed queued→ready→sending→accepted→waiting→completed_verified and `await` materialized
    a real 1327-byte answer. Live verification caught + fixed a real bug (conversation id dropped for
    a thread-less submit → mark_accepted now creates/links the thread).
  - Rollback: remove the `CGC_STORE_BACKEND` line from `~/.config/cgc/config` and restart the daemon
    — the file-spool path is unchanged and still present.

- [ ] 4. **Single egress path**
  - Files: `bin/cgc`, `skill/SKILL.md`, `docs/`
  - What: `bin/cgc submit/followup` enqueue to the daemon rather than calling CDP send directly;
    remove the MCP fallback from the automatic send surface (keep as documented human-only note).
    send_round becomes the sole click path.

- [ ] 5. **Retire the file-spool lifecycle machinery**
  - Files: `skill/scripts/cgc_spool.py`, `skill/scripts/cgc_daemon.py`, `skill/scripts/cdp_consult.py`
  - What: once the store is the sole authority — delete worker-PID fencing, pending/processing/done
    directory-as-state, lifecycle_lock, active.json, automatic orphan resubmission. Introduce the
    one-scheduler-actor model (send_concurrency=1) + `browser_epoch`.

- [ ] 6. **Test & verify** (every phase, not just the end)
  - Run: `python3 -m pytest tests/ -q` and `python3 -m py_compile skill/scripts/*.py && node -c skill/scripts/retrieval_window.js`
  - Expected: all green at each phase; no phase leaves the tree red or the running system broken.

## Risks / Open Questions
- **Migration of a live system** is the highest risk. Rule: drain the old daemon, snapshot state,
  migrate in one transaction, never run both authorities, never replay `sending`/`possibly_accepted`.
- **No remote idempotency key exists** → exactly-once remote send is impossible; the achievable
  guarantee is at-most-once AUTOMATIC retry under uncertainty (possibly_accepted → human-authorized
  only). This is a deliberate availability sacrifice to avoid duplicate consults.
- **max_active_generations default** (1 vs 3) needs a local throughput benchmark (deferred; default 1).
- Sequencing: phases 1–2 are zero-risk (new files, no wiring); risk begins at phase 3 (the cutover).
```
