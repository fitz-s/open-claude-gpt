# Troubleshooting

Run `bin/cgc doctor` (add `--deep` to include login state) first — it pinpoints
most problems and prints the fix. Common cases below.

## `CGC_ERROR missing_dep: pip install websocket-client`
The CDP client dep is missing or the wrong package is installed. Make sure it's
`websocket-client`, not `websocket`:
```bash
pip uninstall -y websocket websocket-client
pip install websocket-client
```

## `CGC_ERROR chrome_not_found`
No Chrome/Chromium/Edge on a known path or on `PATH`. Point at it explicitly:
```bash
export CGC_CHROME="/path/to/your/chrome"
```

## `CGC_ERROR chrome_unavailable` / debug Chrome not on the port
The dedicated Chrome isn't running (or another process holds the port).
```bash
bin/cgc launch                    # start it
lsof -i :9333                     # check what's on the port; change CGC_PORT if taken
tail -20 /tmp/cgc/chrome.log      # what Chrome itself said when it failed to start
```
If you already have a Chrome running on your default profile, that's fine — this
uses a separate profile and port and won't collide. But **only one Chrome may hold
`CGC_PROFILE` at a time**: if a previous debug Chrome is still alive, a second one
exits immediately instead of opening the port, and the launcher says so.

The launcher waits 40s for the port. That is generous on purpose — it also runs
from the launchd daemon, whose I/O launchd deprioritizes, and a false "Chrome
didn't start" throws away a whole 25-minute consult.

## The debug Chrome keeps dying, or stops being able to open tabs

Two different faults, often confused.

**"Chrome closed by itself", usually right after the daemon restarted.** It was not Chrome. A
browser launched by the daemon inherited the launchd job's process group at fork time — re-parenting
to init later does not change that — so `launchctl kickstart -k`, which kills the job by group, took
the browser with it. The launcher now starts Chrome in its own session (`start_new_session`), so it
outlives every daemon restart. Verify: `ps -o pid,pgid -p $(pgrep -f remote-debugging-port=9333)` —
pid and pgid should be equal, meaning Chrome leads its own group.

**Every consult fails to attach, while the browser looks fine.** ROOT CAUSE, proven: an
`http_proxy`/`HTTPS_PROXY` in the environment whose `no_proxy` does not exempt `127.0.0.1`. These
scripts reach the debug browser over loopback HTTP (`127.0.0.1:9333/json`), and `urllib` honours
`http_proxy` — so a local proxy (a model router, say) answered with its own HTML, `json.load` threw,
and the daemon concluded Chrome could not open a usable tab. It then restarted a perfectly healthy
browser, in a loop, killing whatever consults were in flight. Both scripts now install a
proxy-free opener, because loopback must never traverse a proxy. Reproduce the old failure with
`curl 127.0.0.1:9333/json` (proxy HTML) versus `curl --noproxy '*' 127.0.0.1:9333/json` (real JSON).

An earlier guess in this file blamed OS throttling of background renderers. **That was wrong** and
is recorded here because it was wrong for an instructive reason: two hypotheses were tested and
refuted (tab accumulation — 25 targets, new tabs still ready in 0.00s; leaked DevTools websockets —
60 open, same), and rather than keep digging, the remaining unexplained behaviour was attributed to
an untestable environmental cause. The anti-throttling Chrome flags stay because they are harmless
and appropriate for an unattended browser, but they were never the fix.

## A consult finished but produced nothing — where is the evidence?
Every job the daemon runs writes a full transcript of its send + wait to

    $CGC_SPOOL_DIR/logs/<rid>.log        # default /tmp/cgc/spool/logs/<rid>.log

`await` names that path on any failure. It streams **live**, so you can watch a
consult while it runs — the waiter's per-poll heartbeat is the line to read:

    CGC_WAIT alive: gen=… len=… done=… begin=… end=… ac=…

- `ac=0` for the whole run — ChatGPT never produced an assistant turn at all. The
  prompt was inserted but the send didn't take, or the session was rejected. Look
  at the debug Chrome window.
- `begin=true end=false` — the model is writing the answer right now; wait.
- `len` growing, `gen=true` — alive, still reasoning. GPT-5.6 also pauses for
  several seconds mid-stream while its safeguard classifiers review output.

## `CGC_LOGIN needed` / `CGC_ERROR login_needed`
You're logged out in the dedicated profile. Run `bin/cgc launch`, log into ChatGPT
in the window that opens, leave it open, and retry. Verify with
`bin/cgc doctor --deep`.

## `CGC_ERROR no_code_source`
A non-followup consult was sent with no real code link. Run `deliver` first and
pass its refs file to `prep --refs-file`. A `--context-file` of prose does **not**
satisfy this — *prose about the code is not the code*. If the question has **no
code subject at all** (maths/research/writing), render it with `prep --no-code` —
that stamps a sentinel every gate honors, all the way through to the send. (This
error firing on a `--no-code` consult meant the send-time backstop had drifted out
of sync with the spool gate; the two now share one predicate.)

## `CGC_ERROR model_not_selectable`
Something the send was supposed to pin isn't offered in the composer for this
account/thread. Since GPT-6 that is **two** settings, both enforced, both able to
raise this error — read the sentence after the colon to see which one failed.

**The GPT-6 picker (2026-09-03 onward).** Opening the composer's switcher shows
one menu carrying both dimensions: a power slider for the reasoning tier
(`Instant / Medium / High / Extra High / Pro`) and, beside it, radio items for the
model itself (`Latest` / `GPT-5.6 Sol` / `GPT-5.5`). The switcher button no longer
reads a tier at all — it shows the model badge (`6`). Older accounts still get the
pre-GPT-6 forms (slider alone, an `Effort` submenu, or a flat menu), all of which
are still driven; a build with no model radios has the family check skipped rather
than failed.

**Tier miss** — `wanted 'Pro', switcher shows '<tier>' and it could not be
changed`. None of the picker forms carried the target tier:

- **Set a tier your account actually has:** `export CGC_MODEL="High"` (or
  whichever tier your plan offers).
- **Disable auto-selection entirely:** `export CGC_AUTO_MODEL=0` — sends on
  whatever tier is currently shown, no picking/enforcement.
- **Per-command override:** `--model skip` — skip model selection for just this
  one call, without changing your env config.

**Family miss** — `model family '<name>' is not offered in this picker (offered:
Latest, GPT-5.6 Sol, GPT-5.5)`. The radios are there and none of them is the model
you pinned — usually a rename on OpenAI's side, a spelling that doesn't match the
radio's own label exactly, or a plan that no longer lists that model. The error
always names what the account actually offers, so copy one of those verbatim:

- **Pin a model that is listed:** `export CGC_MODEL_FAMILY="GPT-5.6 Sol"`.
- **Stop enforcing the model, keep enforcing the tier:**
  `export CGC_MODEL_FAMILY=skip` (or `--model-family skip` for one call). Accepts
  the risk this check exists to remove: the answer may come from a different model
  than the receipt implies.

**Either miss** — **only when you deliberately want to proceed on a mismatch:**
`--allow-model-mismatch` sends on whatever is shown even though the requested
tier/model couldn't be selected.

The `modelBadge` field in `submit`'s JSON records what the composer's switcher
read *before* send (`"6"` on the GPT-6 build). That is selection evidence — which
model was **requested** — and it can never be a receipt for which model answered:
OpenAI's own release notes describe a Thinking-mode rate-limit fallback to a model
that isn't even a picker option, so selection and serving identity are demonstrably
different things.

Which model **answered** is read from the answer itself. When `wait` extracts a
round's reply it also reads `data-message-model-slug` off that same assistant node
— the provider's own attribution — and `await`'s JSON reports it:

| field | means |
|---|---|
| `model_badge` | the composer's pre-send reading. Selection evidence. |
| `model_slug` | the producing model per ChatGPT, e.g. `gpt-6-pro`. `null` = the provider stamped nothing. |
| `attribution` | `matched` / `mismatched` / `unknown` / `unchecked`; `null` on rounds that predate this field. |

`unknown` is a real outcome, not an error: the attribute is sometimes simply not
there, and a consult that otherwise succeeded is not failed over it. `unchecked`
means you have set no `CGC_MODEL_SLUG` policy, so nothing judged the slug.

## `CGC_ATTRIBUTION_MISMATCH`
The answer verified against its own `END_RESPONSE:<rid>` sentinel — it is complete
and it is this round's — but ChatGPT attributes it to a model no `CGC_MODEL_SLUG`
pattern admits. `await` writes the answer out and exits 3 (human-review) with no
follow-up command, so nothing auto-chains on it. Read it yourself, or re-fire.
Two honest fixes: widen `CGC_MODEL_SLUG` if the producer is one you actually
accept, or find out why your account was served that model (a rate-limit fallback
is the documented cause). `completion_confidence` stays `verified` throughout —
"the sentinel checked out" and "the right model produced it" are separate
properties and this tool keeps them separate.

## `CGC_ERROR ambiguous_followup`
Several consults are active and `--conversation auto` can't pick. Pass the exact
one it lists: `followup --conversation <id>` (or `--rid <rid>`).

## The waiter returns but the answer file is empty / truncated
Usually a virtualized DOM on a backgrounded tab. The waiter force-renders before
reading; if you hit this, make sure the tab wasn't manually closed and that the
consult actually completed (`bin/cgc status --rid <RID>`). Increase `--timeout`
for very long consults (the bound is a safety cap, not the expected duration).

## Waiter exit codes

`cgc await` returns three things, because three things are actionable:

| code | means | what to do |
| --- | --- | --- |
| `0` | the answer is on disk | its path is printed — read it |
| `3` | a human must act in the ChatGPT window | login / captcha / rate limit / a GPT-5.6 safeguard refusal, then re-enqueue |
| `1` | broken | the job-log path is printed — read it |

There is deliberately no "still running" code. A consult routinely takes ~25 minutes
and `await` simply keeps waiting; an earlier version returned `5` for this, which made
every healthy round look like a failure that had to be manually retried. `await` stops
without an answer only after 90 minutes, and that is a malfunction rather than a slow
answer — waiting again cannot fix it.

`cdp_consult.py wait` (the daemon's own child, and the direct-path fallback) keeps a
finer-grained set — `0` retrieved, `3` blocker, `4` genuine no-answer, `2` usage — so
the *cause* survives in the log even though the agent-facing contract collapses `4`
and `2` into `1`. Cause is worth keeping where it aids diagnosis, not where it forces
the caller to branch on a difference it cannot act on.

## A round is stuck uncertain (`possibly_accepted`) or `blocked` with no way forward
A send whose outcome the driver could not confirm is left `possibly_accepted` on purpose — it is
NEVER auto-resent, because a duplicate send is worse than a stuck round. Recovery is addressed by
*conversation*, so first find which tab (if any) is carrying it:

    python3 skill/scripts/cdp_consult.py find-conversation --rid <rid>

- **Found in exactly one tab:** it prints the retrieve command to run — read-only, never resends.
- **Found nowhere:** its own message is explicit that this is NOT proof the prompt was never sent —
  a closed tab or a virtualized thread that scrolled the turn out of the DOM looks identical to a
  round that never sent at all. **Look yourself**, in ChatGPT's own conversation history, before
  concluding anything.
- **You looked and it genuinely never sent:** record that proof durably, so the round terminates and
  (if it holds a `--request-key`) that key releases to a fresh retry instead of staying locked to a
  dead round forever:

      python3 skill/scripts/cgc_spool.py reconcile --rid <rid> --not-sent --evidence "checked ChatGPT history for <date>, no matching prompt/thread exists"

  `--evidence` is required and must be non-empty — the proof is the point. This is also the fix for
  the daemon's `CGC_DAEMON tab sweep HELD` warning repeating every poll: that hold exists because an
  uncertain round's tab is the only evidence of whether it sent, and it will not release the tab (or
  stop warning) until the round is reconciled one way or the other.

Retry-exhausted `model_not_selectable`/`composer_not_ready` rounds land `blocked` automatically with
their not-sent proof already stamped (the driver's own fail-closed pre-click boundary IS the proof,
same as exit 6 — no operator step needed there); `reconcile --not-sent` is for the case that proof
was never automatic: a round left uncertain post-send that you had to go verify by hand.

## `sentinel_missing` / `CGC_UNWRAPPED` / `.raw`
If the model never emits a clean `BEGIN_RESPONSE:<rid>` / `END_RESPONSE:<rid>`
pair before the inner `--timeout` expires, the waiter does one last best-effort
salvage instead of failing outright: if a substantial answer is present in the
tab (even without the wrapper), it writes that text to `--out` **and** to a
sibling `<out>.raw` file, and logs `CGC_UNWRAPPED` to the task output. This still
exits `0` — the consult produced *something*, just not through the sentinel
contract — whereas exit `4` is reserved for the case where nothing usable was
present at all.

**Do not fully trust unwrapped output.** Because it was taken without the
sentinel boundary, there's no guarantee the model was actually finished — the
salvage can be a mid-stream snapshot or otherwise cut off. Before acting on it:
check whether it reads as a complete answer (ends on a natural conclusion, not
mid-sentence/mid-list), and prefer following up (`followup --task "did that
answer finish? re-send the rest"`) if it looks truncated. The `.raw` sibling file
preserves exactly what was salvaged, in case the primary `--out` file gets
overwritten by a later step.

## `await` says `daemon_not_running`
The egress daemon isn't up. Install it permanently — once, on this machine:

    cgc install-daemon

That registers it as a launchd agent (`RunAtLoad` + `KeepAlive`), so it starts at
login and respawns if it ever dies. After this, neither you nor the agent has to
start or check it again, and the agent never starts it itself. `cgc uninstall-daemon`
reverses it. For a one-off or to watch it work, `cgc watch` still runs it in the
foreground.

## The daemon refused my job (`gate` error / GATE REFUSED)
The daemon's egress gate re-checks every job before sending and refuses
fail-closed. Read the first word of the reason — it tells you which of two very
different things happened:

- **`refused: …`** — a verdict. The gate checked and the answer was no. Fix the
  input; re-enqueueing the same job cannot help.
- **`unverified: …`** — the *check* failed, not the input. `gh` timed out or
  couldn't run, so nothing is known about the repo and nothing was sent. Re-enqueue
  to retry. (`gh` reads its token from the OS keyring, which can block much longer
  under the launchd daemon than in your shell; the gate already retries once.)

Common `refused:` causes:
- The referenced repo isn't gh-confirmed **public** — only public repos pass.
- The delivery is a **gist link** — gists are refused by default because
  visibility can't be cheaply proven the way a repo's can; set
  `CGC_GATE_ALLOW_GIST=1` if you deliberately intend to send one.
- A **secret-shaped string** was detected in the rendered prompt — remove it;
  don't put `.env`/keys/tokens in `--task` or `--context-file`.

## Consults hang forever in auto mode
Under Claude Code's `auto` permission mode, the direct `submit`/`wait` path is
blocked by the data-exfiltration classifier and the agent falls back to
`enqueue`/`await`, which needs the egress daemon running to make progress. Check
`bin/cgc queue` — if it shows the daemon **DOWN**, run `cgc install-daemon` once;
nothing will complete until it's up, and after that install it always is.

## `gh` warnings in doctor
`gh` is optional but recommended. Without it, `deliver` can't verify repo
visibility or resolve PR associations. Install from https://cli.github.com and
`gh auth login`.

## Nothing activates in Claude Code
Confirm the skill is installed at `~/.claude/skills/chatgpt-consult/SKILL.md`
(`bin/cgc doctor` checks this) and restart the Claude Code session so it re-scans
skills.
