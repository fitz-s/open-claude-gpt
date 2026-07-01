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
bin/cgc launch          # start it
lsof -i :9333           # check what's on the port; change CGC_PORT if taken
```
If you already have a Chrome running on your default profile, that's fine — this
uses a separate profile and port and won't collide.

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
account/thread. Either set a tier you have (`export CGC_MODEL="High"` or `skip`),
or pass `--allow-model-mismatch` to send on whatever is shown.

## `CGC_ERROR ambiguous_followup`
Several consults are active and `--conversation auto` can't pick. Pass the exact
one it lists: `followup --conversation <id>` (or `--rid <rid>`).

## The waiter returns but the answer file is empty / truncated
Usually a virtualized DOM on a backgrounded tab. The waiter force-renders before
reading; if you hit this, make sure the tab wasn't manually closed and that the
consult actually completed (`bin/cgc status --rid <RID>`). Increase `--timeout`
for very long consults (the bound is a safety cap, not the expected duration).

## Waiter exit codes
- **`0`** — the wrapped answer was retrieved (clean `BEGIN_RESPONSE:<rid>` /
  `END_RESPONSE:<rid>` extraction), **or** a best-effort salvage at timeout (see
  below). Both cases write the answer to `--out`.
- **`4`** — genuine no-answer timeout: nothing usable was present when the waiter
  gave up.
- **`3`** — a blocker was detected (login, CAPTCHA, or rate-limit).
- **`2`** — usage error (bad flags/arguments).

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

## `gh` warnings in doctor
`gh` is optional but recommended. Without it, `deliver` can't verify repo
visibility or resolve PR associations. Install from https://cli.github.com and
`gh auth login`.

## Nothing activates in Claude Code
Confirm the skill is installed at `~/.claude/skills/open-claude-gpt/SKILL.md`
(`bin/cgc doctor` checks this) and restart the Claude Code session so it re-scans
skills.
