# chatgpt-consult

A Claude Code skill that lets Claude Code consult a **visible, logged-in ChatGPT Pro** web session for deep, high-stakes second opinions — then return to local execution. ChatGPT plans/reviews; Claude Code executes and verifies. It is the Claude Code answer to the (Codex-only) [codex-chatgpt-control](https://github.com/adamallcock/codex-chatgpt-control) SDK, rebuilt around Claude Code's own tools.

Model-invocable, and meant to be reached for proactively (a SessionStart hook injects an activation note; a large-PR hook nudges it). The agent-facing operating guide is **[SKILL.md](SKILL.md)** — read it before running.

## Why — an async cloud coprocessor

The core value is **offload + parallelism**, not just depth. Fire a deep, self-contained job to ChatGPT Pro, then **keep working locally while it runs in the cloud**; the detached waiter wakes Claude Code with the full result when it's done. It's an upgraded `ultraplan`/`ultrareview` that runs *off* local context, *in parallel*, on a *different model family* (independent blind spots) with a huge context window + long reasoning budget + browsing. The 10–30 min latency is free whenever you keep working — you're never idle waiting, and you can have 2–3 consults in flight at once. See SKILL.md → *"When to use it"* for the full playbook:

- High-risk **PR-merge gate** (adversarial review before merging to main)
- **Pre-refactor** architecture consult (ask before a large refactor)
- **Deep code review** of a whole module/subsystem
- **Broad-investigation** / research-backed plan
- Adjudicate a hard **design dispute** or a stuck bug
- **Long-context consistency audit** across spec + code
- **Devil's-advocate** before an irreversible action
- Design / DX / naming sounding board
- Multi-round **plan → execute → feed results back** loop

**Invariant:** ChatGPT advises; Claude Code executes and is the source of truth. Verify every claim locally — never merge/ship/declare-done on Pro's word alone.

## Prerequisites

- **Preferred (CDP backend):** `pip install websocket-client`; Google Chrome installed; a dedicated debug profile launched + logged into ChatGPT **Pro** once (`scripts/cdp_launch.sh` — `submit` auto-runs it; login persists across restarts). `gh` authenticated.
- **Fallback only (MCP backend):** Claude-in-Chrome MCP connected, with Chrome signed in to ChatGPT **Pro** (see [references/mcp-fallback.md](references/mcp-fallback.md)).
- For the gist delivery channel: `gh` authenticated, plus the `Bash(gh gist create:*)` + `Bash(gh api gists/*)` allow rules (already in this skill's `allowed-tools`; the **user** must add them to settings — the agent cannot self-grant permissions).
- The fixed ChatGPT project for consults: `$CGC_PROJECT_URL (your ChatGPT project, or a plain new chat)`.

## Preferred backend: CDP (external DevTools client)

The original path drives the ChatGPT tab via the Claude-in-Chrome MCP, which pays four taxes (page CSP, a privacy scanner on tool returns, a 50k-char output cap, and a full agent-context reload on every poll-wake). An **external Chrome DevTools Protocol client** is neither the page nor the MCP, so none apply. `scripts/cdp_consult.py` (`submit`/`wait`/`status`) drives a **dedicated** Chrome debug profile; `wait` runs as a detached background process that polls outside agent context and, on completion, extracts the full answer to a local file and exits — re-invoking the agent (the wake). Main agent context is touched twice total (submit + read).

One-time setup (`scripts/cdp_launch.sh`): launches Chrome with a remote debug port on a **separate** `--user-data-dir` (CDP is disallowed on Chrome's default profile since v136), loopback-scoped origin. **You log into ChatGPT Pro once** — the session persists across Chrome restarts, so later launches are already logged in (the launcher prints `CGC_LOGIN ok`/`needed`). The agent never types credentials. Verified end-to-end (submit → detached wait → auto-extract → exit-wake → read). The MCP + `ScheduleWakeup` path remains as a zero-setup fallback.

## How it works (the flow)

```
prep (consult.py)  → REQUEST_ID, prompt (title + steerable role + end-to-end depth mandate) + sentinels
deliver files      → GitHub links (PR/compare/tree/blob), or a secret gist of local files (NOT auto upload)
submit             → open a project chat, insert the ask, submit (no screenshots) → conversation_id
monitor            → CDP: detached `wait` re-invokes on done. MCP: ScheduleWakeup poll-loop; line-anchored done
retrieve           → CDP: full answer to a local file. MCP: retrieval_window.js → show(i) → get_page_text
use                → advisory only; Claude Code applies + runs tests locally
follow up          → report local results back into the SAME thread (prep --followup + cdp followup); ≤3 rounds
```

The fixed prompt carries the generic contract and an end-to-end depth mandate; the calling agent supplies the three steering levers — **`--title`** (headline), **`--role`** (job-matched persona), **`--task`** (the delta) — to drive a deep round rather than a shallow Q&A. A consult is a multi-round thread: `wait --keep-tab` keeps it alive so Claude Code can feed verification results back via `followup`.

### File delivery (ChatGPT has no local access)

The agent **cannot** auto-upload local files — `file_upload` is sandboxed to files the user attached via the UI (verified). Deliver by:
1. **GitHub raw URLs** (committed code) — fully automatic.
2. **Secret gist** of local/uncommitted files (`gh gist create`) — fully automatic given the allow rule.
3. **User-initiated upload** — the user drags files into the chat themselves.

Note: ChatGPT's browse tool collapses newlines reading gist *raw* URLs, so it may mis-flag valid files as malformed — prefer a real repo when ChatGPT must read source closely.

### The monitor (wake)

The default CDP backend uses a detached `wait` process (polls every ~20 s, re-invokes the agent on completion). The MCP fallback instead uses an agent-side **`ScheduleWakeup` poll loop** whose cadence comes from `prep`'s wake-plan (`--expect-minutes`, default 25 — sized for GPT-5.6 Pro's ~25-min reasoning — → first wake ≈21 min, then re-poll); `poll_js` is line-anchored (standalone `BEGIN_RESPONSE`/`END_RESPONSE` lines + not generating) and detects login/captcha/rate-limit blockers. (Authoritative wake numbers live in `prep`'s state output, not in prose.)

### Large answers (the 50 000-char tool-output cap)

`get_page_text` is the only content channel; it has no offset. `retrieval_window.js` renders the answer **one chunk-window at a time** into the page so `get_page_text` reads each window; the script returns metadata only (counts/indices), never content. Chunks reassemble verbatim; URL tokens are kept whole.

## Files

| File | Role |
|------|------|
| `SKILL.md` | Agent-facing operating guide (read first) |
| `scripts/consult.py` | `prep` (render prompt + retrieval window + poll JS) and `deliver` (resolve purpose-grouped GitHub refs) |
| `scripts/cdp_consult.py` | CDP backend: `submit` / `wait` / `status` (the preferred path) |
| `scripts/cdp_launch.sh` | One-time dedicated debug-Chrome launcher (login probe + loopback origin) |
| `scripts/retrieval_window.js` | MCP-fallback DOM-windowing state machine (`install`/`show(i)`/`status`/`restore`/`setTarget`) |
| `references/injection-and-prompting.md` | Full info-injection catalog + per-scenario prompt templates + output contract (composed by ChatGPT; read when building a non-trivial consult) |
| `references/gpt-5.6-prompting-principles.md` | OpenAI's official GPT-5.x prompt guidance distilled for the GPT-5.6 family — the outcome-first structure the live `PROMPT_TEMPLATE` follows |
| `README.md` | This file |

## Safety

Visible UI only. No hidden ChatGPT endpoints, no bypassing login/CAPTCHA/rate-limits. Never send secrets/`.env`/keys/tokens in a prompt, gist, or upload. Stop on login/CAPTCHA/rate-limit/selector-drift/extension-disconnect. ChatGPT's output is model judgment, not verified truth.
