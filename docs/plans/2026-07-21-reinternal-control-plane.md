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

- [x] 4. **Single egress path** — DONE
  - `store_enabled()` now DEFAULT ON (store is the control plane; `CGC_STORE_BACKEND=0` rolls back).
  - `bin/cgc submit/followup` retired — they refuse and point to `fire`/`enqueue`, so the store
    daemon's send_round is the SOLE send path and the gate can't be bypassed by which verb sent.
    `wait`/`status` kept as read-only diagnostics. MCP was already off the automatic path (explicit
    `--backend mcp` only), retained as a documented human fallback per the consult.

- [ ] 5. **Retire the file-spool lifecycle machinery** — HELD (dormant, rollback-only)
  - With store default-on, the spool machinery (worker-PID fencing, pending/processing/done as
    state, lifecycle_lock, active.json, orphan resubmission) is now UNREACHABLE on the default path
    — the correctness/security intent (nothing governed by PID/lock/dir-state) is already achieved.
  - Physically deleting it + the one-scheduler-actor / `browser_epoch` daemon rewrite is deliberately
    deferred until the store backend has proven itself in real use — deleting the rollback net the
    day of cutover is exactly what the consult's migration/rollback discipline warns against. Do it
    once the store has real mileage; nothing depends on it meanwhile.

- [ ] 6. **Test & verify** (every phase, not just the end)
  - Run: `python3 -m pytest tests/ -q` and `python3 -m py_compile skill/scripts/*.py && node -c skill/scripts/retrieval_window.js`
  - Expected: all green at each phase; no phase leaves the tree red or the running system broken.

## Diff-review follow-up (2026-07-21, GPT-5.6 Pro, conf 0.95)

A second consult diff-reviewed the wired implementation against this design. Verdict: fix-forward —
the store is the right architecture and implements the core no-auto-resend invariant, but the runtime
had one automatic-duplicate path plus several stranding paths. Acted on:

- [x] **S0 auto-resend duplicate (invariant violation) — FIXED** `033b036`. `_run` returned a
  whole-file tail of the append-only per-rid log, so attempt 1's fail-closed marker (e.g.
  `composer_not_ready`) could leak into attempt 2's classification → false proof-not-sent →
  SENDING→QUEUED → attempt 3 duplicated a possible send. `_run` now returns only the CURRENT
  invocation's stderr (log_start..EOF), so a proven-not-sent verdict is always the current attempt's
  own and the SENDING→QUEUED/BLOCKED edges are sound (markers are emitted pre-click).
- [x] **Legacy maintenance gated out of store mode — FIXED** `033b036`. `run_loop` ran
  `_recover_orphans` (rewrites the rollback spool) and `_sweep_tabs` (closes tabs by file-spool PID,
  blind to store rounds) before the store branch. Both now `if not _store_mode`.
- [x] **ready-orphan liveness — FIXED** `add1973`. `_dispatch_store` re-dispatches `ready` rounds
  with no live worker (begin_send's CAS fences double-dispatch); Popen guarded.
- [x] **transient gate → requeue — FIXED** `add1973`. `unverified:` gate failures requeue instead of
  terminal `gate_rejected`; `refused:` stays terminal.
- [x] **auto-retrieve stranded sends — FIXED** `add1973`. possibly_accepted + known conversation is
  re-attached READ-ONLY once (rid-sentinel completes only THIS round's answer; never re-sends),
  automating the manual recovery both of today's stranded rounds needed. `recover()` gains
  `retrievable`.

### Tier 2 — send adapter: RESOLVED via the review's pre-spawn branch (not the full refactor)
- [x] **Reliability captured cheaply; full refactor deliberately declined.** The review (at `b967363`,
  BEFORE Fix-1) pushed to wire `cgc_send.send_round` through an importable click-boundary adapter. Its
  stated URGENCY was that pre-spawn `begin_send` is "not safe IN COMBINATION with the text-based
  reverse transitions" — but that combination was unsafe ONLY because of the cumulative-log leak,
  which Fix-1 (`033b036`) closed. Post-Fix-1 the markers are reliable current-invocation proof, so
  pre-spawn commit + a COMPLETE marker list is invariant-safe and simpler. `add1973`+this commit
  extend `_NOT_SENT_RETRY` with the pre-click browser-failure family (attach_failed / new_tab* /
  wrong_page), which captures the adapter's actual reliability win (attach failures requeue instead
  of stranding). The click-boundary refactor's only residual gain is the negligible
  "subprocess-never-started" strand + architectural purity — not worth rewriting a live browser file
  (Occam). The review's own conditional applies: "if the project deliberately keeps pre-spawn
  commitment, delete cgc_send.py" — done (it was shadow-dead, no production import).
- [x] **Retired the one genuinely-dead file:** `cgc_send.py` + `test_send_round.py` deleted.

### Still open (larger — NOT invariant-critical; deferred with reasons)
- [ ] **File-spool machinery is NOT dead code — it is the rollback net.** The review's reachability
  correction: NONE of it is both provably unreachable AND safe to physically delete. Fix-2 gated it
  out of the store *path* (correctness/security intent already met: store mode is governed by no
  PID/lock/spool-dir). A full `run_loop` split + move-to-`cgc_legacy_*.py` is behavior-neutral
  reorganization that does NOT unlock deletion (still gated on: migration imports prompt/spec +
  repairs null-prompt rows; crash tests at every send boundary; store-native tab ownership +
  browser_epoch + drain; active.json → SQLite; `CGC_STORE_BACKEND=0` removed). Deleting the rollback
  net days after cutover is exactly what the migration discipline forbids. Physical retirement = task
  5, after the store earns mileage and the preconditions hold. Until then it stays, gated + documented.
- [ ] **Migration payload import (S1).** `import_round` cannot carry `rendered_prompt`/`spec_json`, so
  a future rollback+re-migrate would create empty-prompt rounds. Live cutover had zero in-flight, so
  unexercised; fix before relying on re-migration.
- [ ] **source_mode/provenance typed gate (S0 security-DESIGN, not a regression).** The gate still
  secret-scans + verifies public-repo on the exact stored bytes (no open exfil hole), but derives
  authority from prose, not a typed source manifest. Hardening, not a leak. Preserve
  `CGC_GATE_PRIVATE_REPOS` as the user-controlled private_connector mode.
- [ ] **Drain reaps worker process groups (S1); current_attempt_id CAS fencing (S2); completed_
  unverified distinct exit code (S2); active.json → SQLite diagnostics (S2).**

## Risks / Open Questions
- **Migration of a live system** is the highest risk. Rule: drain the old daemon, snapshot state,
  migrate in one transaction, never run both authorities, never replay `sending`/`possibly_accepted`.
- **No remote idempotency key exists** → exactly-once remote send is impossible; the achievable
  guarantee is at-most-once AUTOMATIC retry under uncertainty (possibly_accepted → human-authorized
  only). This is a deliberate availability sacrifice to avoid duplicate consults.
- **max_active_generations default** (1 vs 3) needs a local throughput benchmark (deferred; default 1).
- Sequencing: phases 1–2 are zero-risk (new files, no wiring); risk begins at phase 3 (the cutover).
```
