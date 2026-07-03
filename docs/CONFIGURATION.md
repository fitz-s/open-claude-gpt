# Configuration

Everything host- or preference-specific is read from the **environment**, so the
public skill ships no hard-coded identity. Set variables in your shell, or copy
`.env.example` to `.env` and `source .env` before use.

```bash
cp .env.example .env
$EDITOR .env
set -a && source .env && set +a     # export everything in .env
bin/cgc config                       # print the effective configuration
```

## Setting your ChatGPT project — the easy way

Which ChatGPT **project** a consult opens is the one thing most people customize. You don't
have to edit a shell rc or Claude's `settings.json` — persist it with the CLI, which stores it
in the tool's own config file (`~/.config/cgc/config`, or `$CGC_CONFIG` / `$XDG_CONFIG_HOME`):

```bash
cgc set-project "https://chatgpt.com/g/g-p-<id>-<slug>/project"   # every consult now opens here
cgc get-project                                                   # show the stored URL
cgc set-project --clear                                           # back to a plain new chat
```

A real `CGC_PROJECT_URL` environment variable (below) still **overrides** the stored value for
that run, and every other `CGC_*` setting can also live in that config file (`KEY=value` lines).

## Environment variables

Any of these can be set in the environment (they win over the config file), in a `.env` you
`source`, or persisted in the config file above.

| Variable | Default | Purpose |
| --- | --- | --- |
| `CGC_PROJECT_URL` | `https://chatgpt.com/` (new chat) | URL a fresh consult opens. Set to **your** ChatGPT project (`…/g/g-p-<id>-<slug>/project`) to keep every consult grouped in one project. Easiest: `cgc set-project <url>` (above). |
| `CGC_CONFIG` | `~/.config/cgc/config` | Path to the persistent config file that `cgc set-project` writes and every script reads. |
| `CGC_AUTO_MODEL` | `1` | Auto model-selection toggle (see below). `1`/`true`/`on` = pick `CGC_MODEL` and fail closed if unavailable; `0`/`false`/`off` = don't touch the picker. |
| `CGC_MODEL` | `Pro` | Which tier auto-select targets (only used when `CGC_AUTO_MODEL` is on). A `Pro*` target is satisfied by any Pro tier ChatGPT offers (Pro / Pro Extended) but never by Instant/Medium/High. Per-consult override: `--model "High"` / `--model skip`. |
| `CGC_PORT` | `9333` | Remote-debugging port of the dedicated Chrome. Must be free. |
| `CGC_PROFILE` | `~/.cgc-chrome` | Dedicated Chrome profile dir. Kept separate from your normal Chrome (CDP is disallowed on the default profile since Chrome 136). |
| `CGC_CHROME` | auto-detect | Explicit browser binary. Auto-detected across Chrome/Chromium/Edge on macOS + Linux; set only if detection fails. |
| `CGC_STATE_DIR` | `/tmp/cgc` | **Tool-owned scratch** for prompt/refs/answer files — safe to `rm -rf` at any time. Not a durable store: don't rely on files here surviving a reboot or cleanup. If you want to keep an answer, copy it (or point `--out`) to a durable path like `./cgc_answers/`. |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | Where `install.sh` puts the skill. |

Any variable can also be overridden per-invocation with a flag, e.g. `--port`,
`--project-url`, `--model` on the relevant subcommand. The flag wins over the env
var; the env var wins over the built-in default.

### Auto model-selection (toggle)

ChatGPT sometimes auto-downgrades a chat to a lighter model; a consult would then
silently get a weaker answer. With `CGC_AUTO_MODEL=1` (default) the composer's
model tier is **selected before every send and fails closed if it can't be
picked** — so a send either uses the tier you asked for or is refused, instead of
silently downgrading. This is where you actually spend your ChatGPT Pro plan's
usage allowance: set `CGC_MODEL` to the strongest tier your plan includes, subject
to whatever limits/availability your plan has.

```bash
CGC_AUTO_MODEL=1  CGC_MODEL="Pro"   # on: enforce Pro (default)
CGC_AUTO_MODEL=1  CGC_MODEL="High"           # on: enforce a tier your plan has
CGC_AUTO_MODEL=0                             # off: send on whatever is shown
```

Off is equivalent to `--model skip`. The per-consult `--model` flag overrides both
for a single call. Turn it off if you don't have a Pro plan and don't want the
fail-closed guard, or if you manage the model manually in the ChatGPT UI.

### Finding your ChatGPT project URL

1. Open (or create) a project in ChatGPT.
2. Copy the browser URL — it looks like `https://chatgpt.com/g/g-p-<id>-<slug>/project`.
3. `export CGC_PROJECT_URL="…/project"` (keep the `/project` suffix).

Grouping consults in a project lets you attach project-level instructions/files
in ChatGPT that every consult inherits. If you don't set it, each consult opens a
plain new chat.

## Customizing prompts

The outgoing prompt is assembled from templates in
[`skill/scripts/consult.py`](../skill/scripts/consult.py). You can shape it three
ways, from least to most invasive:

### 1. Per-consult flags (no code edit)

- `--role "<persona>"` — sets the `Role:` line (e.g. *"distributed-systems architect auditing a migration"*). A sharp role measurably improves the answer.
- `--task "<the ask>"` — the Goal. Be specific about what "done" looks like.
- `--context-file <path>` — **supplementary** local context (failure bundles, perf numbers, an external design position). Not a substitute for a code link.
- `--output-file <path>` — a custom output contract for non-review consults (a plan's ordered phases, a design's tradeoff matrix, a bug's ranked hypotheses).
- `--output-replace` — when your `--output-file` defines its own findings shape/severity scale, this swaps out the default findings contract so the two don't collide.

### 2. Edit the templates (repo-wide default)

Two string templates drive every consult:

- `PROMPT_TEMPLATE` — the first-round prompt: `Role → Goal → Success criteria → refs → Constraints → Output → Stop rules → Final output format`.
- `FOLLOWUP_TEMPLATE` — the continuation prompt for `followup` rounds.
- `DEFAULT_OUTPUT_BODY` / `REPLACE_OUTPUT_CLOSE` — the default vs. custom Output-section body.

Edit these to change the house style (e.g. tighten the success criteria, change
the default severity scale, add a project convention). Keep the
`BEGIN_RESPONSE:{rid}` / `END_RESPONSE:{rid}` sentinel block **exactly** — the
parser is **line-anchored AND fence-aware**. Completion fires only when an
**unfenced** standalone trimmed line equals `BEGIN_RESPONSE:<rid>` and a later
**unfenced** standalone trimmed line equals `END_RESPONSE:<rid>`, with a
non-empty body between them. Lines inside fenced code blocks (delimited by
` ``` ` or `~~~`) are ignored, even if they contain a bare sentinel line.

**Accepted** — the sentinel lines stand alone, outside any fence:
```
BEGIN_RESPONSE:REQ-20260701-101500-ab12cd
...the answer body...
END_RESPONSE:REQ-20260701-101500-ab12cd
```

**Rejected** — a bare sentinel line sitting *inside* a fenced code block does
not count, even though the line itself matches exactly:
````
Here's the wrapper format for reference:
```
BEGIN_RESPONSE:REQ-20260701-101500-ab12cd
END_RESPONSE:REQ-20260701-101500-ab12cd
```
````
Because those lines are fenced, the parser ignores them and completion does not
fire. Removing or reformatting the sentinel block (so it's no longer an
unfenced standalone line) breaks answer retrieval.

### Waiter timeouts are a knob, not a fixed limit

The waiter runs as `timeout 899 … wait … --timeout 870` — an outer `timeout 899`
(bounded so the background-task guard doesn't block it; keep it ≤900) and an inner
`--timeout 870` (~15 minutes, ~29s under the outer bound so a timeout-rescue grab
still has time to complete and exit cleanly). That ~15-minute inner cap is not a
hard ceiling on consult length — raise **both** numbers together for a large
review (e.g. a big multi-hundred-file PR), keeping the outer at or under 900
because of the background-task guard.

### 3. Reference material

Deeper prompt/injection guidance the templates are derived from lives in
[`skill/references/`](../skill/references/):

- `injection-and-prompting.md` — the URL catalog (every GitHub injection method, when to use it, failure modes) and file-selection rules.
- `gpt-5.5-prompting-principles.md` — prompting principles for the current model.
- `deep-review-output.md` — the review output contract.
- `mcp-fallback.md` — the zero-setup MCP path (see below).

## Backends

The default and recommended backend is the **external CDP client** (`cdp_consult.py`)
— no MCP scanner, no output cap, detached waiter. A fallback **Claude-in-Chrome MCP**
path exists for zero-setup use; it pays three taxes (URL-scanned return values, a
50k-char page-text cap, and a context reload per poll). See
[`skill/references/mcp-fallback.md`](../skill/references/mcp-fallback.md).

## The dedicated Chrome, in one place

The launcher [`cdp_launch.sh`](../skill/scripts/cdp_launch.sh) reads
`CGC_PORT`, `CGC_PROFILE`, `CGC_CHROME`, and `CGC_PROJECT_URL`. Run it once via
`bin/cgc launch`; it starts Chrome if down, and in `--gate` mode reports login
state by exit code (0 ready / 2 login-needed / 1 chrome-fail) so `submit` can
self-heal setup with no LLM step.
