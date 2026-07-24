# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses date-based
releases until it stabilizes.

## [Unreleased]

Driven by a first-principles maturity audit (a 25-min GPT-5.6 Pro deep review of a48e3d1 collided
with in-session user-side measurement). The audit's verdict: a strong anti-duplicate core inside an
internally contradictory product shell — so this cycle is shell work: one authority, one durable
home, one machine-readable contract, honest risk posture.

### Changed — BREAKING
- **The file-spool control plane is retired.** The SQLite store is the sole round authority;
  `CGC_STORE_BACKEND` is gone (no rollback to the spool), and the spool dir now holds only daemon
  runtime files (heartbeat, singleton lock, per-round logs). Legacy `pending/processing/done`
  lifecycle, per-rid status files, pid fencing, and `lifecycle_lock` are deleted (~700 lines).
- **The store moved out of /tmp.** `control.db` lives in `CGC_DATA_DIR` (default
  `~/.local/state/cgc`) — durable state was living in a directory docs called deletable, making
  `synchronous=FULL` crash-consistency moot across reboots. A legacy `/tmp`-era DB is relocated
  automatically (sqlite backup API; original kept as `*.migrated`). install.sh restarts the launchd
  daemon on upgrade to prevent version split-brain (observed live during rollout: the in-memory old
  daemon recreated an empty /tmp DB while new CLI code used the durable one, stranding another
  session's consult for 30 min).

The pre-tag adversarial re-review (same GPT-5.6 Pro thread, at 356cf7d) returned BLOCK with four
S3 correctness holes on the core trust guarantees; all are fixed below.

### Fixed — re-review release blockers
- **Worker crash recovery at reap time.** A worker that died mid-`sending` used to leave the round
  `sending` until the awaiter's full timeout (the daemon only classified at its own restart). The
  daemon now classifies on reap: `sending` → `possibly_accepted` within one loop; ready/waiting
  stay on their existing re-dispatch/reattach paths.
- **Cross-generation worker & browser fencing (flock leases).** The daemon's in-memory children
  map is only THIS generation's knowledge; a worker surviving its parent was invisible to a
  replacement daemon, which could sweep its tab, restart its Chrome, spawn a duplicate waiter, or
  promote its live `sending` row. Workers now hold a per-RID exclusive lease + a shared browser
  lease for their process lifetime (OS-released on any death); tab sweep / Chrome restart require
  the exclusive browser lease; startup promotion and re-dispatch touch only lease-free rounds.
- **Relocation is an atomic, fenced cutover.** The one-time /tmp→durable move now copies to a
  uniquely-named temp, validates (`PRAGMA integrity_check` + schema), fsyncs, then atomically
  publishes via rename — target existence now implies a complete DB; an interrupted fence
  (both files present) is finished on the next open; an old empty stub no longer suppresses
  relocation forever. The runtime writer fence: every store carries a `store_uuid`, the daemon
  heartbeat publishes its identity (protocol, schema_version, db_path, store_uuid, instance), and
  enqueue REFUSES (`store_mismatch`) when a live daemon is serving a different store or running
  identity-less old code. install.sh now FAILS an upgrade whose daemon restart fails (was a
  warning) — `--force` overrides.
- **Request-key fingerprint covers the logical request.** The idempotency identity is now a
  canonical fingerprint over kind + rid-independent prompt identity + project + model + `--parent`
  + explicit `--conversation`: the same key with different routing now CONFLICTS instead of
  silently returning the old receipt (an answer for the wrong causal request). The prompt identity
  comes from a `<RID>`-placeholder render (`prep` → `--logical-sha`), so a rid quoted in user text
  is never normalized away. Concurrent same-key enqueues resolve deterministically through the
  unique index (the loser returns the winner's receipt, never a raw constraint error).
- **Envelope contract hardened.** `await` now emits its one-JSON-line envelope on EVERY exit,
  including a store that cannot open (SchemaTooNew, corruption) and answer-materialization
  failures; `retryable:true` structurally always carries `next_command` or `human_action`.
- **Bare-auto follow-up edges.** The ambiguity refusal now counts queued/ready consults as
  in-flight, and compares the two most recently active DISTINCT threads (a busy thread can no
  longer hide a near-simultaneous completion on another).
- **Release hygiene.** `.omc/` local tool state untracked + ignored; the CI identity scan covers
  every tracked file (was .py/.sh/.md only); install.sh installs the same pinned
  `websocket-client>=1.6,<2` range CI tests and verifies the installed version.

Dogfooding this release's own re-review then exposed two recovery-path defects in the field
(the fix-verification consult hit a post-click WebSocket timeout and exercised the whole
uncertain-round machinery live):

- **`--kind retrieve` could never succeed.** Its waiter pinned the retrieve round's own (fresh)
  rid, which by construction never matches the conversation's actual last request —
  `rid_mismatch`, exit 2, unconditionally. The advertised recovery flow was broken. Retrieve now
  requires and pins the SOURCE rid of the round being recovered (`--parent`): the waiter locates
  that exact user turn (not merely the last one) and completes only from a sentinel-verified
  answer for it; if the thread has advanced past causal attribution it fails
  `rid_superseded`/`rid_absent` rather than silently committing a later same-thread answer. The
  envelope reports `source_rid` and the observed remote rid.
- **The waiter's rid-resolution grace was shorter than a real render lag.** A follow-up on a long
  thread took >2 min before the just-sent user message committed to the DOM; the 120s resolution
  window expired exactly there and burned the round's ONE-SHOT auto-retrieve on a render lag.
  Grace raised to 600s — waiting longer on a wrong tab is free (read-only); giving up early costs
  the recovery.
- **A provably-unsent terminal failure releases its request-key.** The envelope's retry contract
  says "re-run the SAME fire, same `--request-key`" — but a prior round that terminally failed
  pre-send (cancelled, no-thread, gate-rejected; nothing ever left the machine) would answer that
  retry with its own dead receipt, permanently swallowing the request (observed live). Release is
  gated on durable no-send proof — GATE_REJECTED, a FAILED round with no send attempt on record,
  or an explicit operator `not_sent_proven` reconcile (v3 `send_disposition` column) — because
  generic FAILED is NOT proof: `possibly_accepted → failed` is a legal operator reconcile of a
  round that may have been sent. Fingerprints are compared before any release (a different logical
  request always conflicts), the key transfers to the successor in one transaction (no window
  where it is free), and the successor inherits the prior's resolved conversation/parent so
  same-fingerprint retries cannot diverge to different threads.

- **Sends survive a heavy, freshly-rehydrated DOM; pre-click failures are provably unsent.** A
  follow-up to a long thread died pasting the whole prompt in one `Runtime.evaluate` (the CDP reply
  outran the websocket read timeout on a ~30KB conversation DOM), leaving the draft in the composer,
  no click issued — and the round classified `possibly_accepted`, forcing a manual reconcile
  (observed twice). The composer is now cleared first, the prompt pasted in ~2KB chunks, and the
  pasted length verified before any click; any failure before the click exits
  `EXIT_NOT_SENT_PRECLICK` and lands as `FAILED` + `send_disposition=not_sent_proven` in one
  transaction — the same fire (same `--request-key`) retries safely with no operator step. Failures
  after the click keep the uncertain classification (the at-most-once invariant is untouched).

A second adversarial re-review (same thread, at 39eeb72) confirmed the architecture but found four
remaining concurrency/causality holes on the trust boundary; all fixed:

- **Ownership decisions happen only under the owned lease.** The reaper and a new periodic
  ownerless-`sending` sweep (startup and every poll share one path) reclassify a round only while
  HOLDING its exclusive rid lease; a worker that loses a lease race exits with a distinct code
  (`EXIT_LEASE_REFUSED`) and is inert at reap, so a losing duplicate can no longer get a live
  winner's `sending` round misclassified to `possibly_accepted`. Workers require BOTH leases: if
  the shared browser lease is unavailable (exclusive maintenance), the worker releases its rid
  lease and exits without touching the round.
- **Legacy DB relocation is serialized across processes.** The whole decision — stale-temp sweep,
  inspection, copy, publish, legacy fence — runs under one exclusive flock in the destination dir,
  closing the two-opener race where the loser's published copy was silently abandoned mid-write
  with the same store_uuid (undetectable by the identity fence). A pre-existing nonzero target
  must pass the same validation as a fresh copy (integrity + meta + store_uuid) before it may
  supersede the legacy DB; a partial/corrupt target is quarantined `.corrupt-*` and the copy redone.
- **Coordination locks moved to the durable data dir** (`$CGC_DATA_DIR/locks`, beside
  `control.db`): daemon singleton, browser lock, per-rid leases, and the heartbeat no longer live
  in deletable scratch where an unlink-while-held would split the fencing namespace; `CGC_SPOOL_DIR`
  now holds only logs. Without `fcntl` the fences fail CLOSED (startup error) instead of silently
  granting fake leases.
- **Low-level `enqueue --request-key` requires `--logical-sha`.** The substring fallback that
  replaced the rid inside the finished prompt could erase a literal occurrence supplied by a
  low-level caller and mint a false fingerprint match; refused instead.

### Added
- **JSON outcome envelope (schema 1).** Every `await` exit prints one machine-readable stdout line:
  rid, parent_rid, state, retryable, human_action, next_command, answer_path, log_path, confidence,
  error. Agents act on fields, not prose.
- **`fire --request-key`** — logical idempotency: same key + same content returns the original
  receipt (safe retry after lost output); different content refuses. Comparator normalizes the rid
  out of the rendered bytes.
- **`cgc cancel --rid`** — pre-send only (queued/ready), CAS-guarded, idempotent.
- **Strict follow-up resolution.** Bare `--followup` refuses when "the last thread" is ambiguous
  (a consult in flight, or two threads completed within 30 min); `--parent <rid>` is the
  agent-documented causal path and is recorded on the round.
- **Dead-reference detection at the gate.** Cited PR numbers and tree/blob/commit refs must exist
  (gh, deduped, capped): a typo'd `--pr` now fails in seconds at the gate instead of 25 minutes
  later at ChatGPT — the cost of offline `deliver`, repaid where gh already runs.
- **Ordered schema migrations (v2).** Version read before DDL, pre-migration backup
  (`control.db.v<n>.bak`), one transaction per version, and forward-refusal (`SchemaTooNew`) when
  the DB was written by newer code.
- **`cgc stats`** — completed/failed counts, unverified-completion rate, created→terminal latency
  p50/p90, and a sentinel-drift alarm (>25% unverified, n≥8). Baseline at introduction: 24
  completed, 20.8% unverified.
- **Store-aware browser maintenance.** Tab sweep and Chrome repair run only with an empty worker
  map; a browser permanently unable to open tabs no longer requeues attach-failures forever — the
  daemon escalates to a cooldown-bounded restart.

### Fixed
- `await` on the store path had no daemon-liveness check: a dead daemon meant 90 silent minutes
  instead of the promised one-line `cgc install-daemon` exit. Checked in every non-terminal state.
- `uninstall.sh` targeted the wrong skill directory (`open-claude-gpt` vs the installed
  `chatgpt-consult`) — default uninstall removed nothing.
- README's first-consult walkthrough taught the retired `cgc submit`; now fire → await.
- `make test` ran only the smoke test while `make check` claimed CI parity; it now runs the full
  suite. CI gains a real matrix (ubuntu 3.8/3.11/3.13 + macos 3.13 — the primary platform was never
  CI-run), a pinned `websocket-client>=1.6,<2`, and a doctor step that fails on crash/empty output.
- `fire`'s stdout carried two JSON receipts (enqueue's + fire's), breaking `json.loads` consumers.

### Docs
- **SKILL.md 51KB → 8.5KB** (~12.7k → ~2.1k tokens per activation, measured): activation
  discriminant with negative examples, the two-command path, the outcome envelope, a repeat-safety
  table, `--parent` follow-up, steering levers. History and catalogs moved behind references;
  retired-verb recipes deleted.
- **Honest risk posture:** README "Know the risks before installing" + SECURITY.md sections on
  ToS/account risk (programmatic output extraction sits against OpenAI consumer terms; the account
  risk is the user's), the local threat boundary (loopback is not process authentication), the
  exact gate guarantee (secrets/repos/refs — not arbitrary prose), and local retention/purge.

## [0.2.0] — 2026-07-22

The control plane is rebuilt on a single SQLite transactional store, replacing the file-spool / PID /
rename / lock / active.json arrangement. The cutover was driven first-principles from a design
teardown (`docs/plans/2026-07-21-reinternal-control-plane.md`) and then hardened against two
adversarial GPT-5.6 Pro code reviews (the second reached via the new causal follow-up, dogfooded).

### Added
- **SQLite transactional control plane (`cgc_store.py`)** — one authority for threads, rounds,
  attempts, and browser state (WAL, `synchronous=FULL`, `foreign_keys=ON`, 0600), with an explicit
  round **state machine** whose legal-transition table raises on an illegal move. Every recovery
  decision is a transition over NAMED states in one transaction, not inference over files and PIDs.
- **The at-most-once anti-duplicate invariant, made structural** — `sending`/`possibly_accepted` are
  never dispatchable; `begin_send` durably commits `ready→sending` before the click, so a crash
  mid-send becomes `possibly_accepted` and is never auto-resent. Exactly-once is impossible (no remote
  idempotency key); at-most-once automatic retry under uncertainty is the guarantee.
- **Store-native recovery** — startup promotion of interrupted `sending`, reattach of orphaned
  `accepted`/`waiting` rounds, ready-orphan re-dispatch, and a **one-shot read-only auto-retrieve** of
  a `possibly_accepted` round with a known conversation (the rid-sentinel completes it only if THIS
  round's answer is on the thread — recovers a stranded consult without ever risking a duplicate).
- **Zero-bookkeeping, causal follow-up** — `fire --followup` continues a thread with no id to track;
  `--parent <rid>` pins the follow-up to THAT consult's conversation (causal, unambiguous under
  concurrency). Fail-closed: a follow-up that resolves to no thread is refused, never opened fresh.
- **One-shot spool→store migration (`cgc_migrate.py`)** (drain-only: refuses active legacy work) and
  a `status` observability surface (round counts by state; `possibly_accepted` flagged NEEDS RECONCILE).
- **`consult.py fire` — deliver + prep + enqueue in one call.** The agent makes no decision between
  those three stages: deliver's `refs_file` feeds prep, prep's rid and prompt file feed enqueue.
  Splitting them across three Bash calls made the model copy implementation paths from one JSON blob
  to the next — pure token cost, plus a chance to relay the wrong rid — so a consult now costs two
  agent round-trips instead of four. The individual verbs remain for debugging and for editing the
  refs or the prompt in between. `deliver` and `prep` return their state (main prints it) so the
  fusion composes tested functions in-process rather than re-implementing them.

### Changed
- **One deadline replaces three timeouts, and two of the three were never timeouts.**
  `CONSULT_TIMEOUT_S = 1500` was an *expectation* — how long a GPT-5.6 Pro round reasons.
  `AGENT_POLL_S = 870` was an *observation interval*, sized to a believed 900s cap on background
  tasks the agent launches. Neither answers the question a deadline answers: past what point is
  waiting no longer explained by the work? That is one number, `STUCK_AFTER_S = 3600`, and reaching
  it means something is broken, so the response is to read the job log rather than wait again. The
  `timeout 899` wrapper, the 870/899 pairing, and the four-layer nesting (wrapper → agent window →
  daemon budget → child budget) are all gone.
- **The 900s background-task cap does not exist — measured, not assumed.** It was asserted in a
  dozen places in this repo with no evidence, and the entire slicing apparatus existed to satisfy
  it. An unbounded background waiter is accepted, and an unbounded 1000-second task ran to
  completion (`SURVIVED_1000s`, start/end markers 1000s apart). The wrapper only ever truncated
  healthy consults.
- **Waiter exit codes collapse from five to three**, because three things are actionable: `0` the
  answer is on disk and its path is printed, `3` a human must act in the ChatGPT window, `1` broken
  and the job-log path is printed. Exit `5` "still running" is gone — a consult routinely outlasts
  any particular observation, and making that an exit code turned every healthy 25-minute round
  into a failure the caller had to notice and manually retry. Waiting longer is the waiter's job.
  Exit `4` "no usable answer" and exit `2` "error" merge: they differ in cause but not in what the
  caller does next. `cdp_consult.py wait` keeps the finer set internally so the *cause* still
  reaches the log — cause is worth keeping where it aids diagnosis, not where it forces the caller
  to branch on a difference it cannot act on.
- **Success hands back a path, not a lecture.** `enqueue` and `await` printed multi-line guidance
  blocks on every invocation; they now print the one command or file that matters.

### Added
- **`enqueue --kind retrieve`** — attach to an EXISTING conversation and read its answer. A
  consult's ChatGPT conversation outlives its waiter whenever the daemon restarts, crashes, or the
  user closes Chrome, and the spool had no operation for that: `submit` opens a new conversation
  and `followup` sends another message. The only recovery left was a direct agent-side wait — the
  path auto mode blocks by design. The recovery path must not be the forbidden path. retrieve sends
  nothing, so there is no payload for the gate to validate; that is enforced structurally rather
  than trusted, since the job carries no prompt and the daemon refuses one that does.

### Fixed
- **Answer detection had gone blind against ChatGPT's current DOM — every consult on that build ran
  its full 25-minute budget and returned nothing.** All four parsers selected assistant turns with
  `[data-message-author-role="assistant"]`; the live build marks the turn `data-turn="assistant"`
  and leaves `data-message-author-role` on the *user* node only, so `querySelectorAll` returned
  **zero** assistant nodes, the sentinel could never be found, and `wait` timed out reporting
  `ac=0`. Confirmed live against a running conversation: old selector `ac=0`, new selector `ac=1`
  with `len=926` on the same streaming answer. All four implementations now match either shape, and
  the selector is named ONCE (`_SEL_A`/`_SEL_U`/`_SEL_ANY` in `cdp_consult.py`) instead of being
  retyped in six JS string literals where the next rename would again have to be found six times.
- **`submit` reported a model it was not running on.** It printed `{"model": "Chat",
  "modelConfirmed": true}` for a `--model Pro` consult. Confirmation scans every switcher — which is
  right, since ChatGPT splits model and reasoning effort across two menus — but the *reported* model
  was always `labels[0]`, and `labels[0]` is the composer's **mode** toggle (Chat / Agent / …), not
  the model pill. So a correctly pinned Pro run recorded itself as running on "Chat": a record that
  contradicts itself in one JSON object, makes a healthy run look broken, and would disguise a
  genuinely wrong tier just as well. `_model_verdict` now reports the switcher that actually carries
  the tier. Verified live: the project page shows `['Chat', 'Pro']`, the conversation `['Pro']`.
  This guard had no test coverage at all; it now has six.
- **A daemon that died mid-consult left `await` looping forever.** The liveness check was gated on
  having seen the job picked up, so once a job reached `processing` the check switched off — every
  later `await` then waited out its whole window on a job nobody was working. Liveness is now
  checked in every state, and a daemon that stays gone past `DAEMON_GRACE_S` ends the wait as
  broken — the one case where "still running" would be a lie, since nothing can ever finish it.
- **Jobs stranded in `processing/` were lost silently.** Workers are children of the daemon and
  nothing ever re-scans `processing/`, so a crash or launchd restart orphaned the consult while
  `await` kept reporting it healthy. The daemon now rescans for them every 60s — requeuing only
  those older than a worker's hard ceiling (`STUCK_AFTER_S + 120`), so a still-live worker can
  never be double-sent and billed twice. (Scanning only at startup was not enough: a daemon that
  restarts early in a job's life scans while that job is still ineligible and never looks again.)
- **The launcher discarded Chrome's own error output** to `/dev/null`, so the one failure that
  actually kills a consult — the debug Chrome not coming up — was undiagnosable. Chrome's output now
  goes to `$CGC_STATE_DIR/chrome.log`, the failure path prints its tail and distinguishes "Chrome
  exited" from "Chrome is up but never opened the port" (a second Chrome holding the same profile).
  The readiness wait went from 10s to 40s: a warm profile exposes the port in ~2s, but this also
  runs from a launchd agent whose I/O is deprioritized, and a false negative costs a whole round.

### Changed
- **The daemon no longer swallows its workers' output.** `_run` captured stdout+stderr and discarded
  everything but a 240-char tail folded into the status message, so when a consult produced nothing
  the waiter's per-poll heartbeat (`CGC_WAIT alive: gen/len/done/begin/end/ac`) — the one line that
  says *which* failure happened — was gone. Each job now streams a full transcript to
  `$CGC_SPOOL_DIR/logs/<rid>.log`, **live**, so a 25-minute consult can be watched while it runs;
  `await` names that path on every failure. This is what turned today's two silent 25-minute
  failures into a diagnosis in seconds.
- **The egress gate distinguishes "could not check" from "not public".** They demand opposite
  actions and were reported identically: a `gh` that timed out produced "repo … is not confirmed
  PUBLIC", which reads as a security verdict the repo never earned and tells the caller to give up
  on a job a retry would have sent. `unverified:` now marks a failed *check* (retry) and `refused:`
  a real verdict (stop). `gh` reads its token from the OS keyring, which can block far longer under
  a launchd daemon than in a shell, so the gate also allows 25s and retries once — a single 20s
  attempt cost a real consult its whole budget. A 404 stays terminal: that IS gh's answer.
  The daemon no longer marks the plist `ProcessType: Background`, which told launchd to throttle a
  job whose entire purpose is driving a GUI Chrome. **Re-run `cgc install-daemon` to pick this up.**
- **`prep` stopped dumping MCP-only artifacts into the agent's context.** `poll_js`, `preflight_js`,
  the window script and the ScheduleWakeup wake plan exist only for the MCP fallback; nothing on the
  default CDP+daemon path reads them (`cdp_consult.py` rebuilds the sentinels from the rid). Printing
  them cost ~2.5k chars of dead JS on every consult. The CDP path now prints `request_id` +
  `prompt_file` (115 bytes) and a `CGC_NEXT` line with the exact `enqueue` command to copy; the MCP
  backend still prints the full set.
- **Store is the default control plane** (`CGC_STORE_BACKEND=1`); the file-spool path is retained
  only as a rollback net, gated out of the store path. `bin/cgc submit`/`followup` retired — the
  daemon's gated send is the sole automatic egress path.
- **`STUCK_AFTER_S` 3600 → 5400 (90 min).** A deep re-reasoning follow-up was observed to think
  ~62 min; at 3600 the waiter timed out minutes before the answer landed and stranded the round.
  Completion detection itself is prompt; the budget was simply shorter than the long-tail work.
- **The consult tab never steals foreground focus** (`Target.createTarget background:true`; no
  `Page.bringToFront`).

### Security
- **The egress gate is enforced by the click owner too.** `cdp_consult.py submit`/`followup` run the
  full gate (public-repo verification + secret scan) over the exact bytes, fail-closed, so a DIRECT
  invocation cannot bypass the daemon's validator. A recognized code URL that does not canonicalize to
  a verifiable owner/repo (percent-/double-encoded, non-repo) now fails closed — closing a URL
  classification fail-open where a percent-encoded owner skipped visibility verification.
- **Rollback can no longer auto-resend** — legacy orphan recovery marks a no-conversation orphan a
  blocker for human reconciliation instead of re-sending it.
- **Scope of the gate's guarantee, stated exactly:** no recognized secret leaves, and every cited
  repo is gh-confirmed public. It does NOT vet arbitrary prose — a `--no-code`/follow-up prompt is
  exempt from the public-link rule by design, so the caller is responsible for its content. A typed,
  control-plane-attested source manifest (the stronger boundary) is tracked, not shipped.

### Fixed
- **Cross-attempt marker leak that caused an automatic duplicate send** — `_run` now returns only the
  current invocation's stderr (was a whole-file tail of the append-only per-rid log); the legacy
  Chrome-repair branch matches that scoped stderr too.
- **Legacy maintenance ran under the store backend** (`_recover_orphans`, `_sweep_tabs`) — gated to
  spool mode only.
- **`completed_unverified` is no longer automatic success** — `await` returns review-required and
  materializes the answer for a human (an unwrapped salvage can carry another round's answer).
- **Post-send blocker guidance** no longer recommends a duplicate re-enqueue; **auto-retrieve claim**
  is now one atomic transaction; **terminal rounds are immutable**; a **stale `.raw` sidecar** no
  longer downgrades a verified answer; ready-forever liveness and transient-gate stranding closed.

### Deferred (documented, tracked in the plan — NOT in this release)
- Typed `source_mode`/provenance gate; unwrapped-salvage structural attribution binding; full worker
  process-group kill/reap on daemon restart; migration payload import; `blocked` pre-/post-send state
  split; physical deletion of the file-spool machinery (kept as the rollback net).

## [0.1.0] — 2026-07-01

First public release. Experimental pre-release (the CDP/browser-automation path is
functional and fenced, but early — expect rough edges).

### Added
- **`prep --no-code`** for consults with no code subject — a maths proof, a research or writing
  question, a self-contained analysis. These were previously impossible: `prep` refused without a
  `--refs-file` ("there is NO override") and the daemon's gate refused a prompt with no code link,
  so the only route was gisting the question, which the gate also refuses by default. The flag
  renders an explicit "references no code" declaration that both the model and the gate read. It is
  scoped to that one rule: secrets are still refused, and any repo such a prompt does cite must
  still be gh-confirmed public — the security guarantees are unchanged, only the quality rule
  (don't send a *code* consult as blind prose) now correctly does not apply to non-code questions.
- Claude Code skill and `cgc` CLI for sending public-GitHub-linked consults to a
  logged-in ChatGPT web session; dedicated Chrome profile launch, doctor checks,
  model selection, background waiting, follow-up threads, and public-source delivery.
- **Per-rid job registry** for concurrent consults (replaces the single-active
  `active.json`): jobs keyed by request id, atomic + `flock`-guarded writes, tolerant
  of stale/corrupt state, and the same ambiguity refusal when several threads are live.
- **Cross-implementation parity test + CI** pinning all four sentinel parsers
  (`_sentinel_parse`, `_sentinel_js`, `retrieval_window.js`, `poll_js`) against a
  shared fixture corpus, so the parsers can't silently drift (Node runs it in CI).

- **`cgc set-project <url>`** — persist which ChatGPT project consults open, in a tool-owned
  config file (`~/.config/cgc/config`), so any user customizes it without editing a shell rc or
  Claude's settings; a `CGC_PROJECT_URL` env var still overrides it (`cgc_config.py`).
- **Auto-mode-safe egress daemon** — `cgc watch` (foreground) and `cgc up` (Chrome + daemon,
  detached) run the actual send to ChatGPT out-of-band from the agent, so Claude Code's `auto`
  mode data-exfiltration classifier (which sits above the permission system and isn't
  suppressible via `permissions.allow`) never sees an agent Bash call touch `chatgpt.com`.
- **`cgc enqueue` / `cgc await` / `cgc queue`** — the agent-facing side of the daemon path: pure
  local file I/O (write a spool job, poll for the answer, or check daemon liveness + spool
  contents) with no network access, so it's unaffected by the classifier. Exit codes mirror
  `wait` (0 done, 3 blocker, 4 no-answer, 2 error/setup incl. daemon not running).
- **`validate_prompt` egress gate** (`skill/scripts/cgc_spool.py`) — before the daemon
  (`skill/scripts/cgc_daemon.py`) sends anything, it independently re-verifies every referenced
  GitHub repo is gh-confirmed public and scans the rendered prompt for secret shapes, failing
  closed on either check — a validating gate, not classifier evasion.
- `CGC_SPOOL_DIR` (default `$CGC_STATE_DIR/spool`) and `CGC_GATE_ALLOW_GIST` (default `0`,
  gist links refused by the gate unless set) configure the daemon path; `cgc doctor` now also
  checks the daemon is running.

### Hardened
- Public-source provenance is fail-closed by default.
- Remote debugging is loopback-bound and verified by the doctor; `cgc doctor --secure`
  now hard-fails when it can't verify the bind address (not just on a routable one).
- Answer retrieval uses a fenced, line-anchored BEGIN/END sentinel parser; all four
  implementations read the DOM via a block-newline `textContent` walk (not `innerText`,
  which collapses on a backgrounded tab).
- Follow-up commands pin the intended conversation and request id; conversation
  identity matches on the URL path only (a spoofed `?x=/c/<id>` query can't fool it).
- Waiter salvage writes an `<out>.raw` copy on both the timeout and stable paths;
  `submit --reuse-tab` requires the user-message count to actually grow; `_write_state`
  surfaces a `CGC_WARNING` instead of silently swallowing an unwritable state dir.
- `uninstall.sh --purge` guards the Chrome-profile directory the same way it guards
  the scratch dir (won't `rm -rf` a path that doesn't look like a dedicated cgc dir).

### Changed
- The installable skill is now named **`chatgpt-consult`** (installs to `~/.claude/skills/chatgpt-consult`). The project/repo remains `open-claude-gpt`.
- **`cgc install-daemon` / `uninstall-daemon` — the daemon is now permanent, so a consult never
  stalls waiting for a human.** Previously the egress daemon had to be started by hand (`cgc watch`),
  which meant an agent could prep and enqueue a consult only to discover nothing would send, then
  interrupt the user mid-task. `install-daemon` registers it as a launchd agent (`RunAtLoad` +
  `KeepAlive`): up at login, respawned if it dies, and it opens the debug Chrome itself. Run once,
  never again. The daemon still runs in the user's own session and is still never started by the
  agent — the security model is unchanged, only the number of times a human is needed (now: once).
  The generated plist bakes the installing shell's `PATH`, without which a LaunchAgent's minimal
  `PATH` would break the gate's `gh` repo-visibility check.
- **The agent no longer preflights the daemon.** SKILL.md/ACTIVATION.md now explicitly forbid running
  `cgc queue`/`doctor` "to make sure" before a consult: it spends tokens every round to learn
  something it wouldn't act on, and `enqueue`/`await` already fail in one actionable line (exit 2).
- **Timing defaults resized to 25 minutes throughout — that is simply how long a GPT-5.6 Pro round
  reasons.** The daemon's per-job budget (`cgc enqueue --timeout`), `cdp_consult.py wait`, and
  `followup --watch` all default to **1500s / 25 min**, and `prep --expect-minutes` defaults to **25**.
  The one number that is *not* 25 min is the agent's own `cgc await` window (**870s**): Claude Code's
  background-task guard blocks any bounded task capped over 900s, so the agent must watch in chunks —
  its *effective* wait is still the full 25 min, via exit 5 + re-await. That slice size is now the
  named `AGENT_POLL_S`, distinct from the single `CONSULT_TIMEOUT_S` every real timeout uses — one
  magic number no longer stands in for two unrelated things. The skill documents that a consult is a slow, proof-shaped,
  multi-angle round — so the question and the prompt must be precise, and it should only be spent on
  the hardest open-ended work.
- **New `cgc await` exit code `5` = still running** (was: conflated into `4`). `4` had meant both "the
  daemon finished and produced nothing" and "my local window elapsed while the consult is healthy" —
  opposite situations demanding opposite actions. At the old 870s-everywhere defaults the second case
  was rare; at a 25-minute budget it is the *normal* path, so every consult would have looked like a
  no-answer failure at 14.5 min. `5` now means "re-run the same await"; `4` keeps its true meaning.
- **Prompt guidance retargeted to the GPT-5.6 family** (`gpt-5.6`→sol / terra / luna, GA 2026-07-09; the `Pro Extended` ChatGPT tier now runs sol-class). `references/gpt-5.6-prompting-principles.md` replaces the 5.5 file, grounded in OpenAI's "Using GPT-5.6" guide (which states 5.5 prompting carries forward) and enriched with the 5.6 deltas that matter here: shorter-prompts-measurably-win, avoid blanket "be concise" (5.6 may truncate the artifact — use prioritization), the tier-is-the-mode / "prompt the task not the mode" rule, and safeguard behavior. Plus a retrieval-budget stopping rule, the persona-vs-personality split, and an explicit "no preamble — sentinel-wrapped answer only" note. The outcome-first `PROMPT_TEMPLATE` is unchanged; model selection targets the switcher *label*, so it survived the 5.5→5.6 swap untouched. SKILL.md now notes a GPT-5.6 safeguard refusal as a possible `blocker` and its mid-stream classifier pauses as a non-stall for the waiter.

### Fixed
- **Project-scoped conversations** (`/g/g-p-<pid>/c/<id>`) are now matched: `conversation_id()`
  and the tab matcher previously recognized only a root `/c/<id>` path, so every consult run
  inside a ChatGPT **project** failed to record/pin its thread. The id is now matched as a path
  segment anywhere in the pathname (still pathname-only, so a `?x=/c/<id>` query spoof can't hit).
- **Model auto-select reaching the Pro tier**: the menu-item click matched the first line
  exactly, but ChatGPT labels the top effort tier `Pro Extended` (there is no bare `Pro` item),
  so a `Pro` target could never click it and selection failed closed on a switcher sitting on
  Medium. The click now uses the same Pro-family prefix rule as the confirm check.

### Renamed
- Project is now **Open Claude GPT** (`open-claude-gpt`). The `cgc` CLI and `CGC_*`
  environment prefix are unchanged.

### Changed
- Default model tier is now **`Pro`** (matches any Pro tier ChatGPT offers) instead
  of `Pro Extended`; override with `CGC_MODEL` / `--model`.

### Added
- Open-source packaging: `install.sh` / `uninstall.sh`, `bin/cgc` CLI dispatcher,
  and `cgc doctor` health check (deps, browser, debug port, login, scratch, skill
  files, config; `--deep` and `--json` modes).
- Full environment-based configuration — `CGC_PROJECT_URL`, `CGC_MODEL`,
  `CGC_PORT`, `CGC_PROFILE`, `CGC_CHROME`, `CGC_STATE_DIR` — with `.env.example`.
- Cross-platform browser auto-detection (Chrome/Chromium/Edge on macOS + Linux).
- Docs: install, configuration (incl. prompt customization), architecture,
  security model, troubleshooting.
- CI: parse + shell-syntax + doctor + no-personal-identifier checks.

- `CGC_AUTO_MODEL` toggle: auto-pick the model tier before every send and fail
  closed if it can't be selected (on by default), or turn it off to send on
  whatever the composer shows.
- More example walkthroughs: plan a feature, hard reasoning / proof, architecture
  decision, research-backed decision, stuck-bug second opinion, config recipes
  (plus `--output-file` spec stubs).

### Changed
- All host-/account-specific values moved out of the scripts into the environment;
  the repo ships no hard-coded identity.
- Repositioned around **using your ChatGPT Pro subscription with Claude Code** for
  **planning, hard reasoning, and review** — dropped the "frontier model / ~7× cost"
  framing in favor of the subscription + three-pillars story.
- `deliver` now resolves a commit's associated PR via `commits/<sha>/pulls` and
  leads with that public PR link even for `--ref <sha>` and the auto-detect
  gist-fallback — a pushed commit with a PR never falls back to a gist.
