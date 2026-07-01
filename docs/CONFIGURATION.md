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

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `CGC_PROJECT_URL` | `https://chatgpt.com/` (new chat) | URL a fresh consult opens. Set to **your** ChatGPT project (`…/g/g-p-<id>-<slug>/project`) to keep every consult grouped in one project. |
| `CGC_AUTO_MODEL` | `1` | Auto model-selection toggle (see below). `1`/`true`/`on` = pick `CGC_MODEL` and fail closed if unavailable; `0`/`false`/`off` = don't touch the picker. |
| `CGC_MODEL` | `Pro` | Which tier auto-select targets (only used when `CGC_AUTO_MODEL` is on). A `Pro*` target is satisfied by any Pro tier ChatGPT offers (Pro / Pro Extended) but never by Instant/Medium/High. Per-consult override: `--model "High"` / `--model skip`. |
| `CGC_PORT` | `9333` | Remote-debugging port of the dedicated Chrome. Must be free. |
| `CGC_PROFILE` | `~/.cgc-chrome` | Dedicated Chrome profile dir. Kept separate from your normal Chrome (CDP is disallowed on the default profile since Chrome 136). |
| `CGC_CHROME` | auto-detect | Explicit browser binary. Auto-detected across Chrome/Chromium/Edge on macOS + Linux; set only if detection fails. |
| `CGC_STATE_DIR` | `/tmp/cgc` | Scratch dir for prompt/refs/answer files. Cleaned with `rm -rf` on that path. |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | Where `install.sh` puts the skill. |

Any variable can also be overridden per-invocation with a flag, e.g. `--port`,
`--project-url`, `--model` on the relevant subcommand. The flag wins over the env
var; the env var wins over the built-in default.

### Auto model-selection (toggle)

ChatGPT sometimes auto-downgrades a chat to a lighter model; a consult would then
silently get a weaker answer. With `CGC_AUTO_MODEL=1` (default) the composer's
model tier is **selected before every send and fails closed if it can't be
picked** — you always get the tier you meant. This is where you actually spend
your ChatGPT Pro subscription: set `CGC_MODEL` to the strongest tier your plan
includes.

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
waiter detects completion by those two bare lines; removing or reformatting them
breaks answer retrieval.

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
