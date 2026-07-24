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
| `CGC_STATE_DIR` | `/tmp/cgc` | **Tool-owned scratch** for prompt/refs/answer files — safe to delete; everything here is re-derivable or copyable. Not durable across reboots: copy an answer you want to keep (or point `--out`) to your own path. |
| `CGC_DATA_DIR` | `$XDG_STATE_HOME/cgc` or `~/.local/state/cgc` | **Durable data dir** holding the consult store `control.db` (threads, rounds, results — what `fire --followup` continues). Survives reboots; deleting it deletes consult history. A legacy `/tmp`-era `control.db` is relocated here automatically on first use (the old file is kept as `control.db.migrated`). |
| `CGC_STORE_DB` | `$CGC_DATA_DIR/control.db` | Explicit store DB path override (disables the automatic legacy relocation). |
| `CGC_SPOOL_DIR` | `$CGC_STATE_DIR/spool` | Spool dir for the `enqueue`/`await`/`watch` daemon path (pending/processing/done job files + a daemon heartbeat). Tool-owned scratch, same rules as `CGC_STATE_DIR`. |
| `CGC_GATE_ALLOW_GIST` | `0` | Let the daemon's egress gate accept gist links. Default off because a gist's visibility can't be cheaply proven public the way a repo can via `gh`; set to `1` if you intend to deliver via gist. |
| `CGC_GATE_PRIVATE_REPOS` | *(empty)* | Comma-separated `owner/repo` list the gate may send **even though they are not public**, e.g. `acme/api,acme/infra`. Empty (the default) means public-only. `*` allows any repo your `gh` can resolve. Only set this if your ChatGPT account has a **GitHub connector** authorized — otherwise ChatGPT cannot open the link and answers blind. See "Private repos" below. |
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

### Private repos (only with a ChatGPT GitHub connector)

By default the gate sends links only to gh-confirmed **public** repos. Two separate reasons sat
behind that, and a GitHub connector removes exactly one of them:

- **Capability** — without a connector ChatGPT simply cannot open a private link, so the consult
  answers blind. A connector removes this.
- **Safety** — this gate is the mitigation for a *prompt-injected* agent exfiltrating private data.
  A connector does not remove this; if anything it raises the stakes, because the connector can
  reach every repo your account can.

So private repos are enabled by **naming them**, not by a switch:

```bash
cgc set-project ...                                   # unrelated, shown for shape
export CGC_GATE_PRIVATE_REPOS="acme/api,acme/infra"   # these two may be sent
export CGC_GATE_PRIVATE_REPOS="*"                     # anything gh can resolve — deliberate, broad
```

An allowlist bounds the blast radius to repos you chose. A boolean would let an injected prompt
name any private repo the connector can reach, which is the exact failure this gate exists to
prevent. Everything else still applies to an allowlisted repo: the secret scan runs, and a repo
`gh` cannot resolve at all is still refused, because an allowlist entry means "this repo of mine
may go", not "skip the check".

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

### The deadline

**There is one deadline, and it is 60 minutes** (`STUCK_AFTER_S = 3600`, the default
`--timeout` on `cgc enqueue`, `cgc await`, `cdp_consult.py wait`, and `followup
--watch`).

It is not a budget for the consult. A GPT-5.6 Pro round reasons for about 25 minutes;
that is how long the work *takes* — an expectation, not a deadline — and nothing is
killed for reaching it. The deadline answers a different question: past what point is
waiting no longer explained by the work? Beyond an hour the answer is not late,
something is broken, and the right response is to read
`$CGC_SPOOL_DIR/logs/<rid>.log` rather than wait again.

Earlier versions carried three numbers — a 1500s per-consult budget, an 870s agent
window, and a `timeout 899` wrapper — because two of them were mis-named. The 1500s was
the expectation above; the 870s existed only to stay under a believed 900s cap on
background tasks the agent launches. **That cap was measured and does not exist:** an
unbounded background waiter is accepted, and an unbounded 1000-second task ran to
completion. The wrapper only ever truncated healthy consults, so it is gone. Waiters
wait.

`prep --expect-minutes` (default 25) is unrelated to any of this — it only paces the
MCP fallback's wake plan.

### 3. Reference material

Deeper prompt/injection guidance the templates are derived from lives in
[`skill/references/`](../skill/references/):

- `injection-and-prompting.md` — the URL catalog (every GitHub injection method, when to use it, failure modes) and file-selection rules.
- `gpt-5.6-prompting-principles.md` — prompting principles for the current model (GPT-5.6 family).
- `deep-review-output.md` — the review output contract.
- `mcp-fallback.md` — the zero-setup MCP path (see below).

## Backends

The default and recommended backend is the **external CDP client** (`cdp_consult.py`)
— no MCP scanner, no output cap, detached waiter. A fallback **Claude-in-Chrome MCP**
path exists for zero-setup use; it pays three taxes (URL-scanned return values, a
50k-char page-text cap, and a context reload per poll). See
[`skill/references/mcp-fallback.md`](../skill/references/mcp-fallback.md).

## The egress daemon (auto mode)

Claude Code's `auto` permission mode blocks any agent Bash call that sends data to
an external host, `chatgpt.com` included — this classifier sits above the
permission system, so it isn't something an allowlist can turn off. To keep
consults working there, the actual send is done by a daemon the *user* starts,
not the agent:

```bash
bin/cgc install-daemon    # ONCE: launchd agent — starts at login, respawns if it dies
bin/cgc uninstall-daemon  # reverse it
bin/cgc watch             # foreground, for debugging or a one-off
```

With the daemon running, the agent's side of a consult is two local-only calls:
`cgc enqueue` (writes a job to `CGC_SPOOL_DIR`) and `cgc await` (polls the matching
answer file) — no network access from the agent's Bash call, so the classifier
never triggers. `bin/cgc doctor` checks the daemon is running as part of its normal
health check; `bin/cgc queue` shows daemon liveness plus what's currently in the
spool. See [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the full data flow and the
validating egress gate the daemon runs before every send.

## The dedicated Chrome, in one place

The launcher [`cdp_launch.sh`](../skill/scripts/cdp_launch.sh) reads
`CGC_PORT`, `CGC_PROFILE`, `CGC_CHROME`, and `CGC_PROJECT_URL`. Run it once via
`bin/cgc launch`; it starts Chrome if down, and in `--gate` mode reports login
state by exit code (0 ready / 2 login-needed / 1 chrome-fail) so `submit` can
self-heal setup with no LLM step.
