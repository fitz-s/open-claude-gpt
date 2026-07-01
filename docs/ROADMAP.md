# Roadmap — deferred design work

Non-blocking, design-level suggestions that surfaced from deep review consults and
were consciously **deferred** past v0.1 (not dropped). Each carries the reason it can
wait and what would make it worth doing. This file exists so a good suggestion is
*recorded*, not silently discarded — see the skill's "disposition every finding" rule.

## Post-v0.1 candidates

### Alternative backend: typed API or browser-extension/native-messaging bridge
A typed OpenAI-API backend, or a narrow browser-extension / native-messaging bridge,
would dominate the current CDP approach on **structured output**, **replayable tests**,
and **auth isolation**.

- **Why deferred:** the entire v0.1 value proposition is *use the ChatGPT web session you
  already pay for, with no API key and no per-token bill*. A typed API backend is a
  different product with a different cost model; an extension bridge is a heavier install.
  The final review concluded the CDP path is "sufficiently fenced and fail-closed to ship
  as an explicitly experimental OSS release."
- **Revisit when:** structured-output fragility or test flakiness on the CDP path becomes a
  real maintenance drag, or users ask for an API-keyed mode as an *option* alongside the
  web-session default (not a replacement for it).

### Per-rid job registry (replace the single `active.json`)
A per-request job registry would be cleaner and more inspectable than the current
`active.json` + `recent[]` model for concurrent consults.

- **Why deferred:** review found **no correctness blocker** for 2–3 concurrent consults on
  the supported macOS/Linux/POSIX target — `_write_state` commits under `flock` via
  `os.replace`, and `_resolve_conv` refuses ambiguous auto-followup when multiple threads
  are active (callers pass the explicit `--conversation` printed by `submit`). It's an
  ergonomics/observability upgrade, not a bug fix.
- **Revisit when:** users routinely run many parallel consults and the ambiguity refusals /
  state inspection become a friction point.

### Cross-implementation parity test in CI (Node)
Run the shared sentinel-parser fixtures through **all four** implementations
(`_sentinel_parse` py, `_sentinel_js`, `retrieval_window.js` `extractAnswer`,
`consult.py` `poll_js`) plus a pure-unit test of the `/c/<id>` pathname matcher — as
fixtures, no live browser.

- **Why deferred:** the Python side already has strong sentinel fixtures
  (CRLF, whitespace, inline mentions, duplicate messages, fenced bare sentinels). Adding
  the JS mirror needs Node in CI.
- **Priority: NEXT** — this is the cheapest of the three and the most valuable, because the
  four parsers must stay behaviorally identical and only Python is currently guarded. Any
  future edit to one JS parser can silently drift. Worth adding early in the 0.x line.
