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

## `gh` warnings in doctor
`gh` is optional but recommended. Without it, `deliver` can't verify repo
visibility or resolve PR associations. Install from https://cli.github.com and
`gh auth login`.

## Nothing activates in Claude Code
Confirm the skill is installed at `~/.claude/skills/chatgpt-consult/SKILL.md`
(`bin/cgc doctor` checks this) and restart the Claude Code session so it re-scans
skills.
