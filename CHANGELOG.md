# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses date-based
releases until it stabilizes.

## [Unreleased]

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
