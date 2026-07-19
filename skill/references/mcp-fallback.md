# Backend B — MCP + ScheduleWakeup fallback

Use this ONLY when the dedicated debug-Chrome profile can't be set up (so the preferred CDP backend in SKILL.md → Pipeline A is unavailable). It drives the page through the Claude-in-Chrome MCP and pays all four platform taxes (see SKILL.md → "Hard platform facts"): page CSP, the `javascript_tool` privacy scanner, the 50000-char output cap (→ DOM windowing), and a full main-context reload per `ScheduleWakeup`. The CDP backend avoids all of them.

`SKILL` below = `~/.claude/skills/chatgpt-consult` (write paths in full per command — shell vars don't persist between Bash calls).

## 1. Prep
```bash
python3 ~/.claude/skills/chatgpt-consult/scripts/consult.py prep \
  --backend mcp --window-template ~/.claude/skills/chatgpt-consult/scripts/retrieval_window.js \
  --task "<the question / what you want ChatGPT to plan or review>" \
  --refs-file "<optional /tmp/cgc/refs_*.md from `deliver`>" \
  --context-file "<optional /tmp/context.md you packed>"
```
`--backend mcp` is required here (it renders the DOM-window script; the default `--backend cdp` skips it). All scratch goes under `/tmp/cgc/` — disposable. Record from the JSON: `request_id`, `prompt_file`, `window_js_file`, `sentinel_end`, `poll_js`, `preflight_js`, and the wake-plan (`first_wake_seconds`, `repoll_seconds`, `max_polls`) — `poll_js`/`preflight_js` are ready-to-paste JS with the rid baked in; never hand-edit a sentinel. Deliver code by link first (see SKILL.md → File delivery); use `--context-file` only for small inline context.

## 2. Browser preflight
- `list_connected_browsers` → `select_browser` (deviceId) if not already connected → `tabs_context_mcp`.
- `navigate` the tab to the project URL (SKILL.md → Fixed configuration). Navigating there presents a fresh "New chat in <project>" composer — there's no separate "New chat" button; use the composer.
- Run `preflight_js` via `javascript_tool` (returns `{isChatGPT,composer,loginLike,captchaLike}` — metadata only). Proceed when `isChatGPT` and `composer` are true; surface the blocker to the user if `loginLike`/`captchaLike` is true or `composer` is false.
- Confirm the model by script (scanner-safe `javascript_tool`): scan composer buttons for one whose first line is a Pro tier; if it's not Pro, click that switcher then the Pro item; if the React menu won't drive from JS, ask the user to set it.

## 3. Deliver + submit
- Deliver code by link/gist via SKILL.md → File delivery. Keep code out of the prompt; large pastes auto-file and are often unreadable.
- **Composer:** it's a `div[role="textbox"][contenteditable="true"]` with a sibling hidden `<textarea>`. `find` the composer to focus it, then `javascript_tool`: focus the contenteditable and `document.execCommand('insertText', false, <prompt_file text>)` — inserts the whole multi-line sentinel block at once (a typed newline submits early in this ProseMirror composer). Return `'ok'` only.
- **Verify (scanner-safe):** scan `[contenteditable="true"],[role="textbox"]`, take the largest-`innerText` one, confirm it contains `END_RESPONSE:<id>`. Then `computer key Return` (URL changes to a new `/c/...`).

## 4. Monitor (ScheduleWakeup poll-wake)
- **Cost model:** each wake = one full main-context reload (a `ScheduleWakeup` sleep >5 min misses the prompt cache), so cost ≈ `wake_count × context_size`. Waking early on an unfinished answer burns a reload; being late is free. Bias the schedule **long**; target ≤4 wakes.
- Use the wake plan from prep: `first_wake_seconds` (≈85% of expected latency) then `repoll_seconds`. `--expect-minutes` now defaults to **25** (GPT-5.6 Pro reasons that long), so the first wake already lands near completion — the biggest token lever. Raise it further only for a genuinely huge review. Let the wake chain run; don't poll early.
- Each wake: run `poll_js` verbatim via `javascript_tool` (returns `{generating,done,blocker,assistantCount,len}` — metadata only). `done` is line-anchored (standalone `BEGIN_RESPONSE:<id>` before standalone `END_RESPONSE:<id>`, not generating). The first 1–2 polls may show `len:0`/`done:false` while the node hydrates — keep polling, never resubmit.
  - `blocker` non-null (login/captcha/rate_limit) → stop, tell the user.
  - **Settled-without-sentinel:** `generating:false` AND `len>0` AND `done:false` AND `len` unchanged from the prior wake → it finished without `END_RESPONSE:<id>`. Read the last assistant message via `get_page_text`: real just-unwrapped answer → use it; junk/partial → re-submit. (Mirror of CDP exit 5.)
  - `done:false` and still generating / len growing → `ScheduleWakeup` again `delaySeconds: repoll_seconds`. Cap at `max_polls`; if exceeded, stop and tell the user.
  - `done:true` → step 5.

## 5. Retrieve via DOM windowing (content channel = get_page_text only)
- Inject the window script: read `window_js_file`, pass its contents to `javascript_tool` → JSON `{ok, chunkCount, totalChars}`.
- Loop `i = 1..chunkCount`: `javascript_tool: window.__cgcWin.show(i)` (integer, never bare `show()`) → `{ok,chunkIndex,chunkCount,chars}`; then `get_page_text` and slice between the exact marker lines `BEGIN_CHUNK:<request_id>:<i>/<chunkCount>` and `END_CHUNK:<request_id>:<i>/<chunkCount>`.
  - If a chunk lacks its `END_CHUNK` (truncated): `window.__cgcWin.setTarget(24000)` (then 16000) and restart the show-loop from chunk 1. Keep the same prep/rid — a new prep breaks sentinel matching against the already-submitted answer.
- Concatenate chunk bodies in order with no per-chunk trimming (so hard mid-token/URL splits rejoin losslessly).
- `window.__cgcWin.restore()` to reload the tab; if it lands on a login/error page, stop and tell the user.
- If install returns `no-answer` (sentinel missing): `get_page_text` once to inspect, or ask ChatGPT to regenerate with the sentinel.

## 6. Use + close the loop
- Treat the answer as advisory: apply locally, run tests locally, decide correctness.
- Follow-up round: pack a failure bundle (command, exit code, failure tail, diff) and consult again (new request_id). Max 3 rounds without user approval.
- Final report: what changed, tests run, whether ChatGPT's advice was used/partial/rejected, remaining risks. Never present its answer as verified unless local tests verified it.
