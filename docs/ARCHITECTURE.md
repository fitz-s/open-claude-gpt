# Architecture

## Components

| File | Role |
| --- | --- |
| `skill/scripts/consult.py` | **Prep plane.** `deliver` (resolve GitHub links + repo visibility + PR association) and `prep` (render the prompt from templates, add sentinels). Pure local, no browser. |
| `skill/scripts/cdp_consult.py` | **Control plane.** External CDP client: `submit`, `followup`, `wait`, `status`. Opens tabs, selects model, types + sends, polls to completion. |
| `skill/scripts/cdp_launch.sh` | **Setup plane.** Starts the dedicated debug Chrome, probes login, exit-code gate for self-healing. |
| `skill/scripts/cgc_doctor.py` | Health check / preflight. |
| `skill/scripts/retrieval_window.js` | Answer-windowing helper for the MCP fallback backend. |
| `bin/cgc` | Human CLI dispatcher over the above. |

## The flow

```
deliver ─▶ refs file (grouped GitHub links)
prep    ─▶ prompt file (Role/Goal/criteria/refs/output + BEGIN/END sentinels)
submit  ─▶ open chat ─▶ select model ─▶ type ─▶ send ─▶ print detached waiter cmd
wait    ─▶ (detached bg process) poll page ─▶ detect BEGIN/END ─▶ extract ─▶ write --out ─▶ exit = wake
verify  ─▶ local agent checks each claim
followup─▶ same thread, next round (keeps context + model) ─▶ wait ─▶ …
```

## Design decisions

### Why an external CDP client, not the MCP browser path
The Claude-in-Chrome MCP works with zero setup but pays three taxes: `javascript_tool`
return values are privacy-scanned (URLs blocked), `get_page_text` is capped at
50,000 chars (forcing DOM windowing), and every poll reloads the whole agent
context (cache miss). An external DevTools client is bound by none of these — the
page CSP only constrains the page's own JS, not a DevTools client; there is no
MCP scanner and no cap; and the wait loop runs in a detached shell holding no
agent context. The cost is a one-time dedicated-profile setup, which Chrome 136+
requires for CDP anyway.

### Why the debug Chrome is loopback-bound, not just origin-restricted
`cdp_launch.sh` starts the dedicated Chrome with both
`--remote-debugging-address=127.0.0.1` (binds the DevTools socket itself to
loopback, so it never listens on a routable interface) **and**
`--remote-allow-origins=http://127.0.0.1:<port>` (restricts which page origins may
open a WebSocket to it). The bind is the load-bearing control — origin restriction
alone still leaves the port reachable from other hosts on the network; binding to
`127.0.0.1` makes it unreachable off-box regardless of origin. `cgc_doctor.py`
verifies this at runtime: it inspects the actual listen address for `CGC_PORT` and
classifies it as loopback vs. routable, so a misconfigured or third-party Chrome
that opens the port on `0.0.0.0`/`*` is caught by the doctor rather than assumed.

### Why sentinels
The answer is wrapped in `BEGIN_RESPONSE:<rid>` / `END_RESPONSE:<rid>`. Completion
uses a **line-anchored parser, not substring/`lastIndexOf`**: it fires only when a
bare standalone line exactly equal to `BEGIN_RESPONSE:<rid>` (after trimming) is
followed by a later bare standalone line exactly equal to `END_RESPONSE:<rid>`; the
answer is the text strictly between them. A line that merely mentions or quotes the
sentinel tokens — in prose or inside a code block — does not trigger completion,
because it never exactly equals the bare token line.

- **Accepted:** the model's final lines are
  ```
  BEGIN_RESPONSE:REQ-20260701-101500-ab12cd
  ...the answer body...
  END_RESPONSE:REQ-20260701-101500-ab12cd
  ```
  Both sentinel lines stand alone with nothing else on them → completion fires.
- **Rejected:** the answer discusses the wrapper without emitting real bare
  sentinel lines, e.g. `` `The response should be wrapped like BEGIN_RESPONSE:REQ-...` `` inside prose or a fenced code block. That text contains the token but the line
  is never *exactly* `BEGIN_RESPONSE:<rid>` after trimming, so it does not match and
  completion does not fire.

`rid` (request id) also pins the answer to the right conversation.

### Why force-render before reading
ChatGPT virtualizes message nodes out of a **backgrounded** tab's DOM; a passive
`textContent` read would see only stubs. The waiter scrolls/`scrollIntoView`s to
materialize the answer node before every read, and brings the tab to front before
the timeout rescue. (This is why `innerText` is avoided — it collapses on a
backgrounded tab.)

### Why a detached, bounded waiter
`wait` is meant to run as a backgrounded shell process; its **exit** is the wake
signal to the agent. It is bounded (`timeout`/`--timeout`) so it can never hang a
turn, and it holds no agent context so polling is free.

### State + follow-up
`submit`/`followup` record the live conversation in `$CGC_STATE_DIR/active.json`
(with a recent-threads list) so `followup --conversation auto` resolves the right
thread with zero bookkeeping, and refuses ambiguously when several consults are
active rather than guessing.

### Link-first delivery
`deliver` resolves a commit to its associated PR (`commits/<sha>/pulls`) and leads
with that public PR link — carrying diff + intent + discussion + CI. A pushed
commit that has a PR never falls back to a gist. Tree/blob links are pinned to a
resolved SHA so a link stays immutable even if the branch moves during a long
consult.
