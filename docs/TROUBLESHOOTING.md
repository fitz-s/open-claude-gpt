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
satisfy this — *prose about the code is not the code*.

## `CGC_ERROR model_not_selectable`
The target model tier (`CGC_MODEL`) isn't offered in the composer for this
account/thread. Options, in order of preference:

- **Set a tier your account actually has:** `export CGC_MODEL="High"` (or
  whichever tier your plan offers).
- **Disable auto-selection entirely:** `export CGC_AUTO_MODEL=0` — sends on
  whatever tier is currently shown, no picking/enforcement.
- **Per-command override:** `--model skip` — skip model selection for just this
  one call, without changing your env config.
- **Only when you deliberately want to proceed on a mismatched tier:**
  `--allow-model-mismatch` — sends on whatever is shown even though the
  requested tier couldn't be selected.

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
without an answer only after 60 minutes, and that is a malfunction rather than a slow
answer — waiting again cannot fix it.

`cdp_consult.py wait` (the daemon's own child, and the direct-path fallback) keeps a
finer-grained set — `0` retrieved, `3` blocker, `4` genuine no-answer, `2` usage — so
the *cause* survives in the log even though the agent-facing contract collapses `4`
and `2` into `1`. Cause is worth keeping where it aids diagnosis, not where it forces
the caller to branch on a difference it cannot act on.

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
