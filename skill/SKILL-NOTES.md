# chatgpt-consult — maintainer notes

A Claude Code skill that lets Claude Code consult a **visible, logged-in ChatGPT Pro** web session for
deep, high-stakes second opinions — then return to local execution. ChatGPT plans/reviews; Claude Code
executes and verifies. The agent-facing operating guide is **[SKILL.md](SKILL.md)**; the maintainer
rationale for the daemon/CDP/sentinel design is **[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)** —
read that before touching send-path code. This file is the fast orientation for the rest.

## Why — an async cloud coprocessor

Fire a deep, self-contained job to ChatGPT Pro, then keep working locally while it runs in the cloud;
the daemon sends it and a detached `await` wakes Claude Code with the full result. Independent blind
spots (a different model family), a huge context window, long reasoning, and browsing — for whatever
is worth the ~25 min and doesn't need Claude's local state to answer. See SKILL.md's frontmatter and
`## References` for the full playbook and when NOT to use it.

**Invariant:** ChatGPT advises; Claude Code executes and is the source of truth. Verify every claim
locally — never merge/ship/declare-done on Pro's word alone.

## The agent never sends anything — that's the whole point of this cycle's design

Claude Code's `auto` mode runs a data-exfiltration classifier above the permission system that
hard-denies any agent Bash call reaching an external host, `chatgpt.com` included — not a permission,
so nothing in `settings.json` can admit an exception. The agent's entire call path (`fire` →
`enqueue` → `await`) is therefore local file I/O only:

```
agent: fire (deliver+prep+enqueue, LOCAL)  →  queued round in the SQLite store
                                                        │
                                          user-owned, launchd-kept-alive
                                                        ▼
                                                  cgc_daemon.py
                                          (the egress gate: public repo, real
                                           refs, no secret shapes — fail-closed)
                                                        ▼
                                          cdp_consult.py submit/wait  →  ChatGPT Pro tab
                                                        ▼
                                            result committed to the store
                                                        │
agent: await (LOCAL read, polls the store) ◀───────────┘
```

`submit`/`wait`/`status` on `cdp_consult.py` are real, but they are the **daemon's** internal browser
adapter now, invoked by `cgc_daemon.py`'s worker — not a verb the agent runs directly. `bin/cgc wait`/
`status` still exist as read-only direct-poll diagnostics for a human debugging by hand; `bin/cgc
submit`/`followup` are retired outright (`bin/cgc submit` prints the retirement notice and exits 2)
because the store-backed daemon send is the sole path a bypass could otherwise route around.

## Prerequisites

- Google Chrome; `pip install websocket-client`. One-time, user-run: `cgc install-daemon` (installs
  the egress daemon as a launchd agent — starts at login, respawns if it dies) and `cgc launch` (opens
  the dedicated debug Chrome, log into ChatGPT **Pro** once — the session persists across restarts).
  Neither the agent nor a fresh session ever performs either step.
- **Fallback only (MCP backend):** Claude-in-Chrome MCP connected, Chrome signed into ChatGPT Pro —
  see [references/mcp-fallback.md](references/mcp-fallback.md).
- The fixed ChatGPT project for consults: `cgc set-project <url>` (or a plain new chat).

## File delivery (ChatGPT has no local access)

1. **GitHub links** (committed code) — fully automatic, and agent-safe: `deliver` builds them offline
   from local git, the daemon's gate re-verifies visibility before sending.
2. **Public gist** — human-initiated now, not automatic. This skill's `allowed-tools` in `SKILL.md`
   carries no `gh gist create` / `gh api gists` rule, so the agent cannot create one itself even if it
   wanted to (and creating one would itself be a write to an external host, the same class of call the
   classifier exists to catch). The egress gate also refuses any gist URL in a prompt unless the user
   has set `CGC_GATE_ALLOW_GIST=1` — it can't cheaply prove a secret gist is public. Ask the user for
   the URL and hand-build a refs file around it when there's no pushed repo to link instead.
3. **User-initiated upload** — the user drags files into the chat themselves.

Gist *raw* URLs may render with collapsed newlines in ChatGPT's browser tool; prefer a real repo or
the gist page when close reading matters.

## The wake

`await` polls the LOCAL store (no network) for the round's terminal state and prints a JSON outcome
envelope on its last stdout line; run it detached (`run_in_background: true`) so its exit is the wake.
The daemon's own worker is what actually watches the ChatGPT tab — `cdp_consult.py wait`'s
force-render-before-read, line-anchored/fence-aware `BEGIN_RESPONSE`/`END_RESPONSE` sentinel matching,
and reload-on-stale-DOM recovery all run there, out of agent context. See docs/ARCHITECTURE.md → "Why
sentinels" / "Why force-render before reading" for the mechanics.

## Files

| File | Role |
|------|------|
| `SKILL.md` | Agent-facing operating guide (read first) |
| `scripts/consult.py` | `fire` (deliver+prep+enqueue, the normal path), `deliver`, `prep` |
| `scripts/cgc_store.py` | The authority: SQLite round/thread/event store, explicit state machine |
| `scripts/cgc_backend.py` | Round logic: idempotent enqueue, await's outcome envelope, cancel, stats |
| `scripts/cgc_spool.py` | The egress gate + the agent-facing `enqueue`/`await`/`status`/`cancel` CLI |
| `scripts/cgc_daemon.py` | The egress daemon (launchd): claims rounds, runs the gate, drives CDP |
| `scripts/cdp_consult.py` | Browser adapter: `submit`/`followup`/`wait` — daemon-invoked |
| `scripts/cdp_launch.sh` | One-time dedicated debug-Chrome launcher (login probe, loopback origin) |
| `scripts/cgc_doctor.py` | Health check, for humans (never a pre-fire step for the agent) |
| `scripts/retrieval_window.js` | MCP-fallback DOM-windowing state machine |
| `bin/cgc` | Human CLI dispatcher over all of the above |
| `references/deep-review-output.md` | Default deep-review output contract |
| `references/injection-and-prompting.md` | Info-injection catalog + delivery rules |
| `references/gpt-6-astra-prompting-principles.md` | Prompting law for the pinned model family |
| `references/mcp-fallback.md` | Last-resort backend when the CDP debug profile can't exist |

## Safety

Visible UI only. No hidden ChatGPT endpoints, no bypassing login/CAPTCHA/rate-limits. Never send
secrets/`.env`/keys/tokens in a prompt, gist, or upload. Stop on login/CAPTCHA/rate-limit/
selector-drift/extension-disconnect. ChatGPT's output is model judgment, not verified truth.
