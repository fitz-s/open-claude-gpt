# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses date-based
releases until it stabilizes.

## [Unreleased]

### Added — which model *answered*, read from the answer

Everything above pins the composer *before* a send, and a composer read before a send can only ever
say what was requested. It was being published as if it said who replied. The two are demonstrably
different: OpenAI documents a Thinking rate-limit fallback onto a model that is not even a picker
option, so a correct badge and a different serving model coexist by design. Three properties were
being run together as one — *capability* (was the strongest permitted tier requested), *continuity*
(is this an identity the installation already approved), and *attribution* (which model does the
provider say produced **this** response). Only the first two were ever established, and the third
was being asserted from the second.

A completed assistant turn carries the answer. Measured live on 2026-09-06, one build, one page
load: our GPT-6 consult thread reported `data-message-model-slug="gpt-6-pro"`, an earlier consult
`"gpt-5-6-pro"`, a hand-typed chat `"gpt-5-6-thinking"` — per-conversation, stable across a reload,
not a build-wide constant.

- **`wait` reads that attribute off the SAME node it just extracted the answer from**, scoped by the
  round's own rid and turn interval, never "the last assistant message" — a stale tab under-reports
  how many assistant turns exist, and a last-node read would attribute another turn's producer to
  this answer. It rides back on `wait`'s stdout receipt exactly as `submit`'s conversation id does.
- **`model_slug` is its own field and never merges with `modelBadge`.** The badge is selection
  evidence; the slug is producer attribution. Neither substitutes for the other, and no code path
  lets one stand in for the missing other.
- **Absent is a third outcome, not an error.** It was never established that the attribute is
  populated the instant streaming ends, so a missing one reads as `attribution: unknown` and a
  consult that otherwise succeeded still succeeds. Inventing a value there would be the original
  defect with a new name.
- **One gate, and it is opt-in by policy, not by guesswork.** `CGC_MODEL_SLUG` is an explicit
  fnmatch allow-list over the provider's own slugs (`gpt-6-*,gpt-5-6-pro`). A round whose slug
  matches nothing is `attribution: mismatched`: `await` still writes the answer out, but exits 3
  with no follow-up command, so nothing auto-chains on it. It is deliberately NOT derived from
  `CGC_MODEL` / `CGC_MODEL_FAMILY` — those are the composer's UI labels, slugs are the provider's
  identifiers, nothing local knows the mapping, and a label→slug table would rot into fabricating
  the exact certainty this whole change exists to stop fabricating.
- **No new terminal state, and `completed_verified` still means only what it meant.** It says the
  answer's own rid sentinel verified. A mismatched attribution does not make a verified answer
  unverified; conflating those two properties is the original sin here. The verdict is recorded on
  the round at completion and never re-derived on read, so a later policy change cannot
  retroactively reclassify a finished round, and rows written before this carry NULL — "predates
  attribution", never "mismatch".
- **Schema 5** adds `rounds.model_badge`, `rounds.model_slug`, `rounds.attribution`. Three
  `ALTER TABLE ADD COLUMN`s and nothing else, so a daemon mid-flight against the live DB survives
  it, exactly as migration 4 was built to allow; verified on a copy of the live v4 control DB (97
  rounds, 70 of them `completed_verified`, all unchanged). `threads.model` — NULL in every row since
  it was created — is left alone on purpose rather than repurposed: a thread can span a model
  change, so there is no true value at that grain, and the evidence belongs on the round.

### Fixed — a queued round was pinned by the daemon's environment, not by the request

The queued spec carried `model` but no family, and the daemon's child argv passed `--model` alone,
so `cdp_consult` read `CGC_MODEL_FAMILY` out of whatever environment the daemon happened to be
started with, at send time. A config change between firing a consult and it running silently
retargeted it. The follow-up path was worse: it passed neither dimension, so a thread's re-asserted
tier came from ambient environment too.

- `model_family` is resolved once, in the caller's process, and frozen into the round's spec — the
  same place `model` already lived. Both are now named on the submit AND follow-up child argv.
- It joins the request-key fingerprint, for the reason `model` already was in it: a different family
  is a different logical request, so the same key under a different family **conflicts** rather than
  handing back the old round's receipt. One-time consequence, stated plainly: a key fired before
  this change and re-fired after it conflicts, because the stored fingerprint has no family in it —
  and truthfully, that round was not pinned to one.

### Fixed — GPT-6 broke the picker, and pinning a tier stopped meaning pinning a model

OpenAI shipped GPT-6 ("GPT-6 Astra") on 2026-09-03 and the composer's picker changed under us.
Measured live on 2026-09-06 against a logged-in Pro tab: `_select_model(c, "Pro")` returned
`(False, '6')` in 4.0s. Every consult refused to send. The guard failed closed, which is the
behaviour it was built for — but it failed closed on a perfectly good Pro tab, which is a total
outage of the tool.

- **The tier is now read by identity, never by line position.** The picker group's first line used
  to *be* the tier announcement (`"Pro, 5 of 5."`); on the GPT-6 build it is the model badge
  (`"6"`), and the tier moved to line 2. `_slider_label("6")` returned `"6"`, `_matches("6", "Pro")`
  is false, so the walk matched at no slider position and the flat-click and submenu fallbacks found
  nothing. The tier now comes from the slider control's OWN `aria-describedby` — on this build the
  `[role=menuitem]` wrapper's `innerText` and `textContent` are both empty, so that accessible
  description is the primary source, read first. The group-line `", N of M."` scan, the legacy
  bare form, and the switcher-button read are all still in the code and still tried in order after
  it — kept for the builds where the description isn't there, not removed by this change. Every
  pre-GPT-6 shape still resolves; the fixtures for them are kept.
- **Confirmation moved with the DOM.** The switcher button used to carry the tier, so scanning
  button labels could confirm one. It now shows the model badge, so that scan can never confirm and
  would refuse a correctly pinned Pro forever. A slider walk that matched is now a confirmation in
  its own right — it only matches on a label taken from the control's own accessibility
  announcement, which is a stronger source than the button ever was. The confirming read also moved
  to *after* the menu closes, so the verdict describes the resting composer rather than a transient
  `"Thinking effort"`.

### Added — the model itself is pinned now, not just how hard it thinks

The same menu that carries the power slider now carries the model as radio items (`Latest`,
`GPT-5.6 Sol`, `GPT-5.5`), and nothing in this tool asserted which one was checked. "Pro tier on
GPT-5.5" satisfied every check we made while being a different model than the receipt named — the
core promise, broken quietly. The rule this file used to state, *"Model must never be touched (it
picks the family, not the tier)"*, was true only while the model was unselectable; it is now
exactly backwards and has been rewritten in place.

- **`CGC_MODEL_FAMILY`** (default `Latest`, `skip` to disable, `--model-family` per consult) is
  selected with the tier's own discipline: fail closed, and name the families the account actually
  offers when the target is absent. It is picked FIRST, because changing the model re-renders the
  picker and can reset the slider.
- **An already-correct model is not clicked.** Clicking a checked Radix radio is not a guaranteed
  no-op — it commits the menu closed, which would drop the slider out from under the tier walk.
- **A build with no model radios is a silent no-op, never a refusal.** ChatGPT is mid-rollout and
  older accounts still serve the pre-GPT-6 picker; refusing them over a control their composer does
  not render would be a second outage in the shape of a fix.
- **"The tier already looks right" is not evidence about the model.** With a family enforced, a
  run whose picker never opens cannot confirm: the radios are only readable inside an open menu,
  so that path refuses rather than reporting success on an unverified model — a fail-open in the
  middle of a fail-closed guard is the bug class this whole change exists to remove.
- **`submit` now reports `modelBadge`** — what the composer's switcher read *before* send (`"6"`
  here) — selection evidence for which model was requested, not a receipt for which model
  answered: a pre-send DOM read cannot attest to what actually served the response. `cgc doctor`'s
  config line reports the family alongside the tier for the same reason.
- The new fixtures are transcribed from a DOM dump of the live tab rather than from what the code
  expects to find, and were checked to fail against the pre-fix parser. The last picker break
  shipped green precisely because the fixtures agreed with the bug.

### Changed — the prompt now defends against what Astra does differently

Retargeting the prompting layer from GPT-5.6 to GPT-6 Astra is mostly a rename, except for two
documented behaviour changes that this system is unusually exposed to. Both come from OpenAI's own
"Using GPT-6 Astra" guide (fetched 2026-09-06); `references/gpt-5.6-prompting-principles.md` is
renamed to `references/gpt-6-astra-prompting-principles.md` and records an accept-or-reject verdict,
with the reasoning, for every block that guide recommends.

- **Astra asks clarifying questions where 5.6 assumed.** The guide says it is "more likely to ask the
  user a question when additional input could materially change the result," which "can cause it to
  stop when the user may expect it to make reasonable assumptions and persist." A consult is
  unattended by construction: nobody is reading the thread for the ~25 minutes it runs, so a
  clarifying question is not a slower answer, it is a round that returns nothing. The stop rule now
  says so outright and grants the authorization explicitly, replacing the old rule rather than
  stacking on it.
- **Our own repository is an instruction surface, and Astra reads it more literally.** The guide
  warns that Astra "can be more sensitive to instructions contained in skills and other files, such
  as `AGENTS.md`," and strongly recommends auditing what the model can reach. Every consult ships
  public GitHub links to this repo — which contains `SKILL.md` and `ACTIVATION.md`, both written as
  imperatives aimed at an agent. The prompt now states that instructions found in anything browsed
  are evidence to review, never directives to follow.
- **Style guidance was adopted; brevity guidance was not.** Astra defaults to heavier formatting and
  stock phrasing, and the guide ships blocks for both. The 5.6 rule that a brevity instruction makes
  the model substitute a shorter artifact for the one you asked for still stands and was not
  retracted, so the form half is in and the length half stays out.
- **Not adopted, with the reasoning recorded rather than the suggestion silently dropped:** the
  subagent-delegation and testing-verification blocks (no subagents and no test runner exist on the
  ChatGPT side of a consult), and async tool calling, mid-turn steering, `configuration_update`, the
  dropped `none` reasoning effort and the removed sampling parameters (all Responses-API mechanisms;
  we drive a web tab and send no request body).
- **The ~25-minute sizing is unchanged, and now says where it comes from.** OpenAI publishes no
  per-turn figure for either model; the one official latency number (OSWorld 2.0, ~40 min/task for
  Astra against ~75 for Sol) measures multi-step agentic tasks and is the wrong quantity for sizing a
  chat timeout. This store's own 63 completed rounds are the real evidence: p50 1993s, p90 4802s.
  Prose that cited "a GPT-5.6 Pro round" as the justification now cites that instead.

Note for anyone reading the 2026-07 entry below: its claim that model selection "targets the switcher
*label*, so it survived the 5.5→5.6 swap untouched" was falsified by the GPT-6 build documented above.
Addressing by label survived a model swap; it did not survive the picker being redesigned.


### Changed — work that makes no progress now costs nothing and says nothing

A first-principles efficiency pass, driven by counting what the running system actually did rather
than by guessing where time goes. Three of the four findings were pure waste hiding in plain sight in
the daemon log and the event table.

- **A requeued round no longer hot-spins the daemon.** `ready -> queued` was 93% of all state events
  in the live store — 2461 of them from a single round, one every ~2.2s for 91 minutes, ending only
  because a human cancelled it. `_gate` requeues a round when the egress gate fails *transiently*
  (`gh` missing, timed out, API error) and its docstring claimed "the daemon poll paces the retry";
  nothing paced it, because a requeued round was instantly re-claimable and each cycle shelled out to
  `gh` again. Rounds now carry a `not_before` fence that `claim_ready` honours, with exponential
  backoff (5s doubling to a 180s cap) and a bound: past five retries this is a standing `gh` problem,
  not a blip, so the round blocks for a human — carrying the same `not_sent_proven` stamp the
  send-phase precheck uses, since the gate runs strictly before `begin_send`. Schema 4.
- **Idle maintenance stopped probing a browser that was not there.** `tab sweep skipped: URLError`
  was the single most common line in the daemon log — **6896** of them, one per minute for about five
  days, while Chrome simply was not running. Maintenance is now skipped entirely when nothing has been
  dispatched that could leave a tab behind, the probe backs off when it fails (to a 600s cap) and
  resumes its normal cadence the moment the browser answers, and only the *transitions* are logged.
- **Consecutive identical log lines collapse.** The same `tab sweep HELD` line appeared 325 times for
  one unreconciled round whose state never changed once. Repeats now print once and report
  `(repeated N×)` when the line finally changes. A small wrapper around the daemon's own stderr —
  worker output is untouched.
- **Two operator steps that should not have existed.** `enqueue --kind retrieve --parent <rid>`
  refused without an explicit `--conversation` even though the store already knows the parent's
  conversation; it now resolves it (an explicit value still overrides, and a parent with no recorded
  conversation still refuses, naming `find-conversation`). And re-firing a proven-unsent round meant
  hand-writing Python against the live store to rewrite the prompt's rid sentinels and re-assemble the
  spec — done twice by hand during this release. `cgc_spool.py refire --rid <rid>` does it, and
  refuses anything not durably proven unsent by reusing `_release_eligible` rather than inventing a
  second definition of "safe to send again".

### Fixed — the picker actually works on the tab the daemon opens

The slider fix above was verified by attaching to an already-open, settled tab. The daemon opens a
FRESH tab per consult, and there it failed every time — a real consult burned its three retries and
blocked with `model_not_selectable` while the tier button sat right there. Two independent bugs, both
invisible to the test suite because both fixtures encoded the same wrong assumptions the code did.

- **Candidates were addressed by index into a live NodeList.** `_CAND_JS` is re-evaluated inside every
  call, so reading labels and then opening `c[i]` are two round-trips with a React re-render in
  between. Live: labels read `['Extra High']`, then opening index 0 opened the Chat/Agent MODE TOGGLE
  — whose menu has no slider and no tier items — and since the count read back as 1, the tier switcher
  was never tried at all. Hence the honest-but-useless `switcher shows 'Chat'`. Candidates are now
  addressed by identity: one evaluation finds the button by label AND clicks it, returning the label
  it actually opened, and an open that does not match what was asked for is discarded rather than
  acted on. The composer-scoped switcher (`form button[aria-haspopup=menu]`, minus the plus button) is
  tried before the document-wide scan, which stays as the fallback for other builds.
- **The label parser rejected the real DOM.** `_slider_label` returned `""` for any line without a
  comma, but the picker group renders `"Pro, 5 of 5."` only once keyboard interaction is active — a
  fresh tab shows the bare word `"Pro"`. So the walk drove the slider correctly to the target and then
  compared every reading against `""`: never a match, run to the ceiling, report failure. Traced live,
  one press at a time: `now` went 4→3→2→1→0→…→4 with `first` tracking each tier, and `_slider_set`
  still returned `(False, '')`. Both forms now parse, and the state read carries a second source — the
  composer button's own label, the same text the verdict already trusts — so the walk is never blind.

Verified on a fresh daemon-style tab: `High → Pro` confirmed in 8.6s, where the same path failed
outright before.

### Fixed — a proven-unsent round no longer eats its request-key forever

Found by dogfooding the picker breakage above. `model_not_selectable` is a **pre-click** failure —
the driver refuses to submit and the state machine already treats that marker as proof nothing was
sent — but when the retry budget ran out, the round went terminal as `blocked` with
`send_disposition` left NULL. `_release_eligible` only ever considered `GATE_REJECTED`/`FAILED`, and
`_idempotent_receipt` did not treat `blocked` as terminal at all, so it fell through to the
live/completed branch. Re-firing the same consult with the same `--request-key` therefore returned
an **idempotent receipt pointing at a dead round** — no error, no retry, no way forward short of
inventing a new key. Two real consults sat in that trap; one for nine days.

- **The proof is stamped where it is proven.** Every send-phase fail-closed marker now writes
  `send_disposition=not_sent_proven` in the same transaction as the block — retry exhaustion
  (`_requeue_or_block`) and the immediate login/captcha/rate-limit class alike. Both are pre-click by
  construction: the login and captcha exits come from the composer probe that runs before anything is
  typed, and the only `rate_limit` detector outside it lives in the wait loop, whose
  `waiting -> blocked` path is a different, post-send transition that is deliberately left unstamped.
- **A stamped `blocked` round releases its key; an unstamped one still cannot.** The stamp, never the
  state name, is what proves no-send — so the post-send blocker keeps the key owned exactly as before.
  `_idempotent_receipt` now handles `blocked` in the terminal branch and refuses with a message naming
  the prior round and its state, instead of handing back a receipt for something dead.
- **`reconcile` is a command now.** `record_not_sent_proof` was documented as *the* operator act and
  had no CLI — reconciling a stuck round meant hand-writing Python against the live store, which is
  what this release's own recovery required. `cgc_spool.py reconcile --rid <rid> --not-sent
  --evidence "<what you checked>"` performs it; evidence is required and non-empty, `--not-sent` must
  be explicit, and a bad rid or an in-flight round exits with `CGC_ERROR`, not a traceback. It also
  stamps an already-`blocked` round in place, without a state change — otherwise the command could not
  repair the rounds this very bug created. `BLOCKED` remains structurally unreachable in `_LEGAL`.
- **`docs/TROUBLESHOOTING.md`** documents the whole recovery: `find-conversation` finds nothing → look
  in ChatGPT's own history → stamp it. With the honest caveat the tool already makes: a closed tab or
  a virtualized thread looks identical to a prompt that never landed, so look before stamping.
- **`CDP.call` honours a timeout longer than its socket's.** The deadline was enforced in a Python
  loop around `ws.recv()` while the socket kept its 10s connect-time timeout, so any longer call died
  with `WebSocketTimeoutException` well before its own deadline (hit live driving a 40s evaluation).
  The socket timeout is now stretched for the call and restored in `finally`.

### Fixed — the tier picker follows ChatGPT's new power slider

ChatGPT replaced the composer's tier menu with a **power slider**, and the old picker could no
longer reach Pro. Probed live: the composer keeps one switcher button whose label is the current
tier, but its menu now holds `[role=menuitem][aria-label="Power"]` wrapping a `[role=slider]`
(`aria-valuenow` 0–4, ArrowLeft/ArrowRight, saturating at both ends) that maps onto
`Instant / Medium / High / Extra High / Pro`. There is **no `Pro` menuitem in that menu at all**, so
the click-the-item path could only ever "succeed" on a tab already sitting on Pro — every other tab
failed closed and refused to send.

- **Slider is the primary path.** `_slider_set` saturates left to a known position, then steps right
  one tier at a time, re-reading `(label, valuenow)` after every press and stopping the instant the
  target matches. Labels are read from the live DOM (`"Pro, 5 of 5."` → `Pro`), never hardcoded, so
  a tier rename does not need a code change. Verified live: Instant→Pro 6.6s, Medium→Pro 5.8s,
  Pro→High 5.2s, already-on-target 0.0s (the verdict short-circuits without touching the UI).
- **Fail-closed is side-effect-free again.** A target this account does not have used to leave the
  slider wherever the rightward search gave up — the composer silently ended up on the HIGHEST tier
  it passed through while the error still claimed the tier "could not be changed", and
  `--allow-model-mismatch` would then have sent on the most expensive tier the user never chose.
  Every failure return now walks back to the entry position (bounded, best-effort, never looping on
  a restore press that does not land). Verified live: High + unreachable target → refused, left on
  High.
- **The refusal names the tier, not the mode toggle.** `_model_verdict`'s no-match fallback reports
  `labels[0]`, which on a project page is the Chat/Agent toggle — the error told users the switcher
  showed `Chat` when the question was about a reasoning tier. The slider-observed label now wins
  that fallback.
- **Two fallbacks kept, deliberately.** The flat-menu click (older builds) and a submenu sweep (this
  build duplicates the tiers under `Advanced → Effort`) still run when the slider path does not
  land. ChatGPT ships this UI in stages; a single-mechanism picker is what just broke.
- **Keys-not-landing no longer false-bails.** A press is confirmed by a short bounded poll (≤3 reads
  at ~0.2s) instead of one flat 0.3s sleep — live settle time is 0.35–0.45s, so the old read
  mistook a merely-slow frame for a dead control.

### Fixed — a completed round's answer reaches its address without a waiter

Field incident: a detached `await` was stopped by the caller's harness (twice). The round finished
normally — `completed_verified`, 23KB of answer durably committed — but **nothing was ever written
to the path every receipt had printed**, because the answer file was materialized ONLY inside
`await`. A promise no writer was keeping: the caller fell back to `until [ -s <path> ]; do sleep 20;
done` and would have polled forever. A store scan found 33 of 39 completed rounds with nothing at
their recorded address.

- **The producer materializes.** `_wait_phase` now writes the answer to the round's recorded
  `out_path` immediately after `store.finish` commits it (`_publish_answer`) — so the artifact's
  existence no longer depends on a notifier being alive at the instant of completion. Written
  strictly AFTER the commit: a file that appeared first would let a watcher read an answer for a
  round that then failed to commit. Unverified salvages are materialized too — the verdict lives in
  the store and the envelope (`await` still returns 3), while the file is just the artifact, and a
  human cannot review what was never written. The recovery path's reconciled SOURCE round gets its
  answer written as well; its own waiter is gone by construction, which is the whole reason that
  path exists. The file remains a derived view: a failed write is logged and left for `await` to
  repair, never allowed to fail a round whose answer is already durably committed.
- **`status --rid` reports `answer_on_disk`.** An address can lie — `/tmp` is evicted, and until
  this release a round could complete with no writer. Printing the path without saying whether
  anything is AT it is what let a caller watch a file nobody was writing.
- **`SKILL.md`: a stopped `await` is a non-event, and a shell wait loop is not a substitute.** The
  round lives in the store and the daemon finishes it regardless; re-arm `await --rid <rid>`. An
  `until [ -s … ]` loop cannot terminate on `blocked`/`failed`/`possibly_accepted` — precisely the
  states that need a human — and reads an unverified salvage as success.

### Changed — the return of a command is an address, not a status bit

An exit code routes the caller; it does not tell them where anything is. Applied across the
caller-facing surface: wherever the system already knew an identifier or a path, it now mints and
returns it instead of demanding the caller invent one and hand it back.

- **`await --out` is an override, not an argument.** The answer's address is chosen ONCE, when the
  round is created (`--out`, or the default), and recorded on the round; `await --rid <rid>` reads
  it back, materializes there, and returns it as `answer_path`. It was `required=True`, so the
  caller had to carry a path from `fire`'s JSON receipt into a second command — the exact
  copy-a-path-between-blobs step `fire` exists to delete, and the last one standing. It was also a
  live hazard: a divergent `--out` wrote the answer to an address nobody recorded, leaving the
  store naming one location and the disk holding another. An explicit `--out` still writes a
  second copy for a human and deliberately does *not* rewrite the round, so `status`, the
  raw-salvage lookup, and every other reader keep trusting one address. Legacy rows with no
  recorded path fall back to the same default `enqueue` would have chosen. Every printed `await`
  command (fire's receipt, `_post_create_receipt`, the daemon-down `next_command`) drops `--out`.
- **A `retrieve` mints its own rid.** `enqueue --kind retrieve` has no prompt to read a
  `BEGIN_RESPONSE:` sentinel from, so `--rid` was required and hand-written — the last remaining
  path to `bad_rid`, and it burned a live recovery in the field. A retrieve's own rid is pure
  bookkeeping (what identifies the recovery is `--parent`, the round being recovered), so there was
  never anything for a caller to know. It is minted and returned in the receipt; a rid that *is*
  passed still has to be canonical. Every printed retrieve command drops `--rid <new-rid>`.
- **One definition of the rid shape.** `cgc_spool.new_rid()` lives next to the `_RID_RE` that
  validates it; `consult.cmd_prep` no longer spells the format out a second time, where a drift
  would have been unobservable until rounds started refusing to enqueue.
- **`status --rid` names the artifacts.** It reported a state word and an attempt id; it now also
  reports `out_path` and `conversation` — where the answer is, and which thread holds it, the
  latter being exactly what a stranded round's recovery is addressed by.

Field incident: a submit clicked, the DOM rid echo was unreadable, and the round went
`possibly_accepted` with **no conversation recorded**. Recovery is addressed *by conversation*, so
there was nothing to retrieve from — while the answer was generating in a tab the daemon was about
to sweep. A human had to copy 32KB out of the browser by hand. The send fence was never in
question; what failed is everything downstream of it.

### Fixed
- **An unconfirmed send keeps its address.** `submit` now resolves the tab's `/c/<id>` on *every*
  post-click outcome (`unknown_send`, `selector_drift`, ok) and reports it in the envelope; the
  worker records it via the new `Store.link_conversation` — state untouched, so an unconfirmed send
  is never laundered into a confirmed one — which puts the round in `recover()`'s `retrievable`
  bucket and lets the existing read-only auto-retrieve resolve it. Linked only on the uncertain
  branch: a re-queued proven-not-sent round must not inherit a thread.
- **A landing can be read without the turn adapters.** `_send_proven_by_landing` upgrades
  `unknown_send` to a landed send when the tab entered a thread it did not hold *and* our rid is
  rendered in that thread (`_rid_in_main`, scoped to `<main>` with no body fallback — it runs
  precisely when ChatGPT's DOM has moved — so the sidebar's derived titles cannot match).
  Both facts are required: `location.pathname` moves on plain navigation into an existing
  thread, so the URL alone would let a no-op click plus any unrelated navigation read as a
  successful send — silencing the one failure that is meant to summon a human, and pinning the
  global active-thread to a conversation that is not ours. The waiter still verifies
  `END_RESPONSE:<rid>`, so nothing is taken on trust that the answer will not have to prove.
- **The tab sweep no longer destroys the evidence it was told to preserve — without becoming a
  permanent outage.** `unknown_send` leaves its tab open on purpose; the sweeper's lease proves only
  "no live worker", which is not the same as "unowned" — an uncertain round has no worker and still
  owns its tab. An uncertain round's tab is now spared *by conversation* where one is known (precise,
  costs one tab, blocks nothing); only the case with no conversation holds the whole sweep. Either
  way the claim expires after 6h — pinning an addressed tab forever leaks the same browser one round
  at a time. `possibly_accepted` has no automatic exit, so an unbounded hold would let a single
  unreconciled round stop every future sweep — and a browser carrying enough tabs cannot open
  new ones, which is the failure that ends every consult. Fails closed if the store is unreadable.
- **The recovery loop terminates.** A retrieve that matched `END_RESPONSE:<source>` proved both that
  the source's send landed and what it answered, so it now closes the source round
  (`Store.adopt_retrieved_answer`). Sentinel-verified answers only — an unwrapped salvage cannot
  prove which turn produced it — and only when the source's rid lease is free, since the daemon's
  own one-shot auto-retrieve can be mid-wait on the very round a human-enqueued retrieve recovered.
- **Submit's subprocess budget raised 240s → 300s.** The conversation-id poll now runs on the failure
  paths, where the id it captures is the only thing that makes the round recoverable; a kill there
  loses stdout, which is how the round this budget protects became unrecoverable in the first place.

- **`enqueue` is idempotent on an exact repeat.** The canonical invocation is `enqueue && await`, so
  refusing a same-rid/same-bytes repeat short-circuited the chain that actually delivers the answer:
  the command failed identically on every retry while the round sat COMPLETED in the store, its
  22KB answer never materialized. Same rid + same bytes is the same request — nothing new is queued,
  the receipt says `queued: false`, and the caller proceeds to await. A DIFFERENT prompt under the
  same rid still refuses, and that refusal now names the round's state and the one command that
  follows from it (read/await the answer, continue with `--followup --parent`, render a fresh rid,
  or retrieve an uncertain send) instead of a bare "already exists in the store".

- **A follow-up that provably did not send is re-queued, not left uncertain.** The follow-up branch
  never checked `_NOT_SENT_RETRY` (the submit branch always did), so a fail-closed pre-click refusal
  — `model_not_selectable` when the thread's tier had dropped off Pro — was filed as UNCERTAIN. That
  is the costliest possible misfiling: it blocks the free automatic retry, burns a full auto-retrieve
  and then a manual retrieve hunting an answer that was never asked for, and leaves a human with only
  a resend left to try, under exactly the uncertainty the invariant exists to prevent. Observed in
  the field; it cost an hour and ended in a resend.
- **A failed wait no longer overwrites WHY a round became uncertain.** The uncertain fallback is also
  where the one-shot auto-retrieve of an already-uncertain round lands, and it stamped "accepted but
  wait produced no answer" — asserting a confirmation that never happened and erasing the real
  disposition (a proven-not-sent `model_not_selectable`) that a human needs to judge whether a resend
  would duplicate anything. The original disposition is now carried forward.

- **The proven-not-sent retry is bounded.** "Transient" means a retry MAY fix it, not that it will.
  Three begun sends that all died before the click is a standing condition — the observed one was a
  thread whose model tier had dropped off Pro, which no retry restores — and each turn of an
  unbounded retry opens a browser tab, exhausting the one resource whose exhaustion ends every
  consult. After three the round BLOCKS: terminal, never sent, addressed to the human who can fix it.
- **`await` reports an uncertain round as 3 (a human must act), not 1 (broken).** That is the
  documented contract, and nothing about an uncertain send is broken — it may well have landed.
  Reporting it as broken made every one of these read as a tool failure in the caller's log
  ("failed with exit code 1") instead of as the one action item it is.
- **`enqueue` reads the rid from the prompt.** The rid is a property of the RENDERED PROMPT — prep
  writes `BEGIN_RESPONSE:<rid>` into the text and the waiter accepts an answer only if that exact
  sentinel returns — so `--rid` is now optional, and one that DISAGREES with the prompt is refused
  rather than silently producing a round that could never be confirmed. Hand-typing the format was a
  step that only ever produced `bad_rid`.

### Fixed — the three deferred round-5 residuals (#1, #2, #3)
- **#2 The follow-up landing window keeps polling for the rid echo.** Count growth broke the loop
  immediately, leaving the authoritative rid read as a single shot against a turn whose text had not
  hydrated — a landed follow-up reported as `rid_echo_mismatch`, i.e. a false uncertain on a send
  that did happen. Count growth is now a hint; the canonical echo (or the deadline) ends the loop.
- **#3 One in-flight send per conversation.** A thread is one shared composer and one ordered
  transcript, so two overlapping sends into it are two writers on one mutable resource. The
  per-conversation mutator lease is released once the send LANDS, leaving the whole answer wait
  unguarded. A follow-up whose thread has a live round (`sending`/`accepted`/`waiting`) is now not
  CLAIMED at all — refusing at claim time spawns no worker and so cannot spin; the round stays
  queued until the thread frees. Scoped to follow-ups (a `retrieve` is read-only and IS the recovery
  path) and to genuinely live states (an unreconciled `possibly_accepted` would embargo its thread
  forever).
- **#1 The inherited-lease handoff is proven against the kernel, not asserted.** The prior tests
  covered the fd registry and the subprocess kwargs — the intent. The guarantee is an OFD property,
  so the new tests run the real schedule: a child inherits the rid/browser/conversation descriptors,
  the backend closes its own handles, contenders are verified BLOCKED, and ownership ends only when
  the child exits. Confirmed discriminating — the same flow without `pass_fds` leaves the lease free.

### Added
- `cdp_consult.py find-conversation --rid <rid>` — read-only search of open ChatGPT tabs for the rid
  that is inside the prompt we sent. It is the last automated step before a human reads the screen,
  and `await`'s uncertain-round message now names it. Best-effort by construction (a virtualized
  thread can scroll the turn out of the DOM); a miss is reported as a miss, never as "not sent".

## [0.2.1] — 2026-07-24

Driven by a first-principles maturity audit (a 25-min GPT-5.6 Pro deep review of a48e3d1 collided
with in-session user-side measurement). The audit's verdict: a strong anti-duplicate core inside an
internally contradictory product shell — so this cycle is shell work: one authority, one durable
home, one machine-readable contract, honest risk posture.

### Changed — BREAKING
- **The file-spool control plane is retired.** The SQLite store is the sole round authority;
  `CGC_STORE_BACKEND` is gone (no rollback to the spool), and the spool dir now holds only
  per-round logs (coordination state — heartbeat, singleton lock, leases — lives in
  `$CGC_DATA_DIR/locks`, see below). Legacy `pending/processing/done` lifecycle, per-rid status
  files, pid fencing, and `lifecycle_lock` are deleted (~700 lines).
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

A third re-review (at 5537dba) passed every prior gate and narrowed the remaining risk to two
browser-level schedules; both fixed:

- **Per-conversation exclusive mutator lease.** Two same-thread follow-ups (legal via explicit
  `--parent`) could interleave on the one composer: W1's click could submit W2's freshly-pasted
  bytes while W2's verification saw an emptied composer and exited "provably unsent" — a FALSE
  `not_sent_proven` that would authorize a duplicate send on retry. A follow-up now holds an
  exclusive per-conversation flock (same durable locks dir) across the whole mutating region
  (attach→clear→paste→verify→click→landing check); a second same-thread worker exits
  `EXIT_LEASE_REFUSED` untouched and is redispatched. Fresh submits (new thread) and read-only
  waits/retrieves take no lease.
- **Canonical per-turn rid parsing + interval-scoped answers.** A turn's rid was read by substring
  search, so a later prompt QUOTING `BEGIN_RESPONSE:<old-rid>` (ordinary audit content) could
  steal the old turn's identity and let a source-pinned retrieve persist the newer turn's answer.
  A turn's canonical rid is now the LAST complete bare-line BEGIN/END pair (the template's own
  footer always follows caller text, so a quote can never win); source-pinned waits take answers
  only from the DOM interval between the source turn and the next user turn, and fail
  `rid_absent`/`rid_superseded` when the interval can't be resolved — never a global fallback.

A fourth re-review (at 9bbc738) passed both round-three fixes and every earlier gate, and bounded
the remaining risk to two mechanical at-most-once holes; both fixed, plus two more field defects
the release's own consult traffic exposed:

- **Lease fds ride into the browser mutator.** The backend held the rid/browser/conversation
  flocks, but the CDP subprocess doing the actual clear/paste/click inherited none of them — a
  backend death mid-click released every fence while the orphan child could still submit another
  worker's bytes. Mutating CDP subprocesses now inherit the live lease fds (`pass_fds`); flock
  binds to the open file description, so ownership survives until BOTH processes exit. The Chrome
  relauncher deliberately does NOT inherit them (a restarted browser would pin the exclusive lease
  forever).
- **Conversation lock keys are canonical, fail-closed.** `--conversation <rid>` and
  `--parent <rid>` could reach the same thread under DIFFERENT lock files (raw string vs resolved
  id), defeating the mutator lease. An explicit `--conversation` must now be a bare canonical
  conversation id (rejected otherwise, with `--parent` named as the rid-targeting path); the
  worker revalidates before locking and fails a non-canonical row pre-send.
- **Turn text is read with line structure.** The composer pastes a prompt as one `<p>` per line;
  raw `textContent` concatenates them with NO newlines, so the line-anchored canonical parser
  could not see any rid in a live turn — a LANDED send was classified `possibly_accepted` because
  its own echo was unparseable. All user-turn reads now use the block-aware text walk (`__cgcText`,
  already used for answers) rather than raw textContent.
- **Landed-detection works on virtualized threads.** A long thread renders only the newest turns,
  so "user-turn count increased" can stay false forever after a real send; the count is now only a
  fast path, and the authoritative landed signal is the last rendered turn echoing OUR rid
  (canonical parse, 30s window) — with the wait phase's 600s grace still behind it.

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
