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
| `CGC_MODEL` | `Pro` | Which **reasoning tier** auto-select targets (only used when `CGC_AUTO_MODEL` is on). ChatGPT's composer offers `Instant / Medium / High / Extra High / Pro` on a power slider; a `Pro*` target is satisfied by any Pro tier the build offers (`Pro`, or `Pro Extended` on older builds) but never by a weaker tier. This is the tier only — since GPT-6 it no longer says *which model* answered; see `CGC_MODEL_FAMILY`. Per-consult override: `--model "High"` / `--model skip`. |
| `CGC_MODEL_FAMILY` | `GPT-6` | Which **model** to pin, a separate dimension from the tier. GPT-6's composer (shipped 2026-09-03) lists the model itself as radio items beside the power slider (`GPT-6` / `GPT-5.6 Sol` / `GPT-5.5`; the first was labelled `Latest` until 2026-10), so "Pro tier on GPT-5.5" is reachable and would otherwise satisfy every tier check while answering on a different model than the receipt names. Selected before the tier (changing the model re-renders the slider) and fails closed, naming the families the account actually offers — but only when the switcher's open menu carries family radios in the one layout the code recognizes; a build that renders them some other way is, like a build with none at all, silently not checked. Resolved and FROZEN into the round when you fire it, then passed to the browser driver on the argv: a queued job keeps the family you configured, not whatever the daemon's environment says by the time it runs. It is part of the request-key fingerprint too, so the same key under a different family conflicts instead of returning the earlier round's receipt. `skip` disables the check; per-consult override: `--model-family "GPT-5.6 Sol"` / `--model-family skip`. |
| `CGC_MODEL_SLUG` | *(empty)* | Which **producing models** you accept, as a comma-separated list of `fnmatch` globs over ChatGPT's own slugs — `gpt-6-*,gpt-5-6-pro`. This is the third dimension and the only one read *after* the answer exists: `wait` takes `data-message-model-slug` off the answer's own assistant node, and the round records it as `model_slug` beside the pre-send `model_badge` (selection evidence — the two are never merged). A round whose slug matches no pattern is `attribution: mismatched` and `await` hands it to a human instead of returning it as auto-consumable. Empty (the default) records the slug and judges nothing — `attribution: unchecked`. Deliberately NOT derived from `CGC_MODEL_FAMILY`: those are the composer's UI labels and these are the provider's identifiers, nothing local knows the mapping, and a guessed table would fabricate exactly the certainty this field exists to stop fabricating. When the attribute is absent the round is `attribution: unknown` — a real outcome, never a mismatch and never a failure. |
| `CGC_APPS` | `WebCodex Demo` | ChatGPT apps/plugins `fire --mention` knows, comma-separated. `consult.py apps` prints the list so an agent never has to guess what exists; a `--mention` that case-insensitively matches, or is a unique prefix of, one entry is rewritten to that entry's spelling. Unlisted names still go through — the composer's @ popup is the authority, and when it offers no match the round fails closed pre-send with `mention_not_found`, naming the apps the popup did offer. |
| `CGC_WORKSPACES` | *(empty)* | The local WebCodex workspaces you registered, comma-separated. An agent cannot see that registry, so this list is how it knows when `fire --workspace NAME --mention "WebCodex Demo"` applies. `consult.py apps` prints it as `"workspaces"` next to `"apps"`; a `--workspace` that case-insensitively matches, or is a unique prefix of, one entry is rewritten to that entry's spelling. **It is also the authority for the no-code-link exemption:** a `--workspace` round is queued, and later sent, only when its workspace is listed here and one of its `--mention` apps is listed in `CGC_APPS`. Empty means no workspace round can be sent. For these two keys, unlike every other `CGC_*` key, the gate reads the config file as it is at send time and the file wins over the environment: a key missing from the file means nothing is registered (`CGC_APPS`: the default), so editing or removing the entry takes effect in a running daemon without a restart. The environment is used only when no config file exists. A round whose workspace is not listed, or whose config file cannot be read, is held and retried (then blocked), not rejected for good. |
| `CGC_WEEKLY_BUDGET` | *(unset)* | Your weekly ChatGPT Pro message allowance, as a number. `stats --week` and the SessionStart line compare the last 7 days of *potentially spent* messages against it ("18 of 50 used · 32 available"). Unset, they show the spend and say "budget unset". Recording only: nothing is ever refused because of it. |
| `CGC_AUTO_APPROVE` | *(= `CGC_APPS`)* | ChatGPT apps whose approval card ("Allow file materialization?" — `[Deny] [Allow once]`) the waiter answers by itself. ChatGPT has no standing grant for these cards (a plugin's "Allow all tools" does not cover them), and an unanswered card stalls the round. The waiter already polls the consult's own tab, so it clicks "Allow once" there with a page-level `element.click()` over CDP: no mouse, no focus change, no other tab touched, so concurrent consults and your foreground work are undisturbed. A card is only answered when one line of its own text (≤400 chars, so the prompt cannot lend it a name) is exactly a listed app's name, case-insensitively — `WebCodex Demo Clone` never rides on `WebCodex Demo`; any other app's card, or one still showing after 5 clicks, ends the wait as `approval_needed` for a human. `off` disables it. |
| `CGC_PORT` | `9333` | Remote-debugging port of the dedicated Chrome. Must be free. |
| `CGC_PROFILE` | `~/.cgc-chrome` | Dedicated Chrome profile dir. Kept separate from your normal Chrome (CDP is disallowed on the default profile since Chrome 136). |
| `CGC_CHROME` | auto-detect | Explicit browser binary. Auto-detected across Chrome/Chromium/Edge on macOS + Linux; set only if detection fails. |
| `CGC_STATE_DIR` | `/tmp/cgc` | **Tool-owned scratch** for prompt/refs/answer files — safe to delete; everything here is re-derivable or copyable. Not durable across reboots: copy an answer you want to keep (or point `--out`) to your own path. |
| `CGC_DATA_DIR` | `$XDG_STATE_HOME/cgc` or `~/.local/state/cgc` | **Durable data dir** holding the consult store `control.db` (threads, rounds, results — what `fire --followup` continues) and `locks/` (daemon heartbeat, singleton lock, per-round leases — coordination state a live daemon or worker may be holding; NOT safe to delete while either could be running). Survives reboots; deleting it deletes consult history. A legacy `/tmp`-era `control.db` is relocated here automatically on first use (the old file is kept as `control.db.migrated`). |
| `CGC_STORE_DB` | `$CGC_DATA_DIR/control.db` | Explicit store DB path override (disables the automatic legacy relocation). |
| `CGC_SPOOL_DIR` | `$CGC_STATE_DIR/spool` | Per-round send+wait logs for the `enqueue`/`await`/`watch` daemon path. Tool-owned scratch, same rules as `CGC_STATE_DIR` — the round lifecycle itself lives in `control.db`, and the daemon heartbeat, singleton lock, and per-round leases live under `$CGC_DATA_DIR/locks` (NOT here — see below). |
| `CGC_GATE_ALLOW_GIST` | `0` | Let the daemon's egress gate accept gist links. Default off because a gist's visibility can't be cheaply proven public the way a repo can via `gh`. Only `1`, `true`, `yes` or `on` turns it on; any other value leaves it off. |
| `CGC_GATE_PRIVATE_REPOS` | *(empty)* | Comma-separated `owner/repo` list the gate may send **even though they are not public**, e.g. `acme/api,acme/infra`. Empty (the default) means public-only. `*` allows any repo your `gh` can resolve. Only set this if your ChatGPT account has a **GitHub connector** authorized — otherwise ChatGPT cannot open the link and answers blind. See "Private repos" below. |
| `CLAUDE_SKILLS_DIR` | `~/.claude/skills` | Where `install.sh` puts the skill. |

Any variable can also be overridden per-invocation with a flag, e.g. `--port`,
`--project-url`, `--model`, `--model-family` on the relevant subcommand. The flag wins over the env
var; the env var wins over the built-in default.

### Auto model-selection (toggle)

ChatGPT sometimes auto-downgrades a chat to a lighter model; a consult would then
silently get a weaker answer. With `CGC_AUTO_MODEL=1` (default) **both** halves of
"which model answered" are selected before every send and fail closed if they
can't be picked — so a send either runs on what you asked for or is refused,
instead of silently downgrading:

- the **reasoning tier** (`CGC_MODEL`, default `Pro`) — the power slider, and
- the **model** (`CGC_MODEL_FAMILY`, default `GPT-6`) — the radio items GPT-6's
  composer added beside that slider on 2026-09-03.

The family matters because the tier alone stopped identifying the model: `Pro` on
`GPT-5.5` passes every tier check while being a different model than the consult
claims to have used. The family is picked first (changing it re-renders the
slider), and a build that has no such radios simply ignores it.

This is where you actually spend your ChatGPT Pro plan's usage allowance: set
`CGC_MODEL` to the strongest tier your plan includes, subject to whatever
limits/availability your plan has.

```bash
CGC_AUTO_MODEL=1  CGC_MODEL="Pro"   CGC_MODEL_FAMILY="GPT-6"        # default: newest model, Pro tier
CGC_AUTO_MODEL=1  CGC_MODEL="High"                                  # a tier your plan has
CGC_AUTO_MODEL=1  CGC_MODEL_FAMILY="GPT-5.6 Sol"                    # pin an older model deliberately
CGC_AUTO_MODEL=1  CGC_MODEL_FAMILY=skip                             # tier enforced, model left alone
CGC_AUTO_MODEL=0                                                    # off: send on whatever is shown
```

Off is equivalent to `--model skip`, and disables the family check with it. The
per-consult `--model` / `--model-family` flags override both for a single call. Turn it off if you don't have a Pro plan and don't want the
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

- `--workspace NAME` — the evidence is in a local WebCodex workspace, not in a pushed repo. It stands in for the code link the source rule otherwise demands (so the round no longer has to claim `--no-code`), and **requires at least one `--mention`**, because the remote model reaches the workspace only through the app. The prompt gains one short block: ``Source: local workspace `NAME`, reachable through the "<app>" app's tools; state the revision/as-of time you read.`` Every secret-shape, gist and URL-provenance gate runs unchanged; `--workspace` alone, or with `--no-code`, is refused before anything is queued. The workspace must be in `CGC_WORKSPACES` and a mention in `CGC_APPS`: `fire` records the workspace in the round, and the daemon's gate exempts the missing link only for a stored round that records one. The `Source: local workspace` sentence is a label for the model; typing it into a `--task` or a hand-written prompt exempts nothing, and a bare `enqueue` cannot declare a workspace. Use `fire`. Add `--repo/--pr/--ref` too and the links ship beside it.

### Intent and schedule flags on `fire`

All optional, all local bookkeeping: none of it is rendered into the prompt.

| Flag | Meaning |
| --- | --- |
| `--origin {agent,user}` | Who decided to fire: the agent on its own initiative, or the user asked. |
| `--trigger {premise-conflict,workspace-evidence,design-fork,math,competing-causes,reviewer-disagreement,external-contract,writing,research,review,other}` | Which activation trigger applied. `stats --week` grades per trigger. |
| `--decision TEXT` | The action this answer can change. |
| `--baseline TEXT` | The current local or gpt-sol answer, frozen *before* the answer arrives. |
| `--check TEXT` | How the result will be judged. |
| `--not-before ISO8601\|+DURATION` | The daemon will not send before this time (`+90m`, `+2h`, `+1d`, `+1h30m`, or e.g. `2026-10-12T09:00:00+08:00`; a time without an offset is your local time). A complete question can wait in the backlog. To move it, `cancel` and re-fire. |
| `--use-by ISO8601\|+DURATION` | Past this time the round never sends. It is checked at every claim pass (including a round held by a paused capability), before `begin_send`, and by the driver immediately before the click. Past it, the round ends `failed` with `expired: use-by <t> passed before send`, stamped not-sent-proven, so it is not counted as a spent message and its `--request-key` is free again. A `refire` replays the intent but not the deadline. |

They are stored in the round's `spec_json` under `consult_meta`, together with `policy_rev`, added
automatically: the first 12 hex digits of the sha256 over `SKILL.md`, `ACTIVATION.md` and the prompt
templates as they were when you fired. The five intent flags join the `--request-key` fingerprint only
when set (so existing keys keep theirs); `policy_rev` and the schedule never do.

### Grading and the weekly view

```bash
cgc feedback --rid R --grade {-1,0,1,2} \
  --reason {useful,decision-changed,defect-caught,duplicate,late,refuted,unused,transport,missing-source,wrong} \
  --action "what you did with the answer" [--evidence "commit SHA / test / file / acceptance judgment"]
cgc feedback --policy "what rule changed and why"      # records that the weekly review happened
cgc stats --week                                       # the rolling 7-day view
```

`feedback` is local (no network, no browser) and writes a `consult_outcome` event; the latest event
for a round is the grade that stands. A grade of 1 or 2 **requires `--evidence`**; an unknown rid is
refused; a `retrieve` round's grade lands on the round it recovered. It never touches
`rounds.updated_at`, so latency stats are unaffected. `--policy` writes a `policy_update` event
stamped with the current `policy_rev` and takes no other flag.

`stats --week` covers the **last 7 days, rolling** (a message counts for 7 days after it was sent, so
the budget frees up continuously; there is no reset day): potentially spent messages vs
`CGC_WEEKLY_BUDGET`, completed answers with no grade (rid and title), the ready backlog and expired
rounds, grade counts by trigger and by with/without `--mention`, useful outcomes (grade 1 or 2) per
spent message, and whether a `policy_update` was recorded. Plain `stats` keeps its shape; its
`messages` block now reports `potentially_spent`, which counts one message per base send **plus one
per automatic continue that landed, was unsure, or never recorded a result** (a `not_sent` continue and
a `retrieve` count zero), and `free` is the number of rounds that spent nothing.

### The SessionStart line

`cgc activation-hook --install` points the hook at `cgc_activation.py snapshot`, which prints
`ACTIVATION.md` and then one line, e.g. `Consult budget: 18 of 50 used (rolling 7d) · 32 available · 2
ready in backlog · 3 answers awaiting grade (cgc feedback) · p50 28m · weekly review due`. It opens the
store read-only (`mode=ro`), makes no network or browser call, is cut off after 0.8 s, and on any error
prints the note alone (plus the visible `WARNING` if `ACTIVATION.md` is missing) and exits 0. Re-running
`--install` upgrades an already-installed hook in place.

### 2. Edit the templates (repo-wide default)

Two string templates drive every consult:

- `PROMPT_TEMPLATE` — the first-round prompt: `Role → Goal → Success criteria → refs → Constraints → Output → Stop rules → Final output format`.
- `FOLLOWUP_TEMPLATE` — the continuation prompt for `followup` rounds.
- `DEFAULT_OUTPUT_BODY` / `REPLACE_OUTPUT_CLOSE` — the default vs. custom Output-section body.

Edit these to change the house style (e.g. tighten the success criteria, change
the default severity scale, add a project convention). Keep the first line
`CGC_RID:{rid}` (`RID_MARKER`) too, ahead of everything else: ChatGPT collapses a
long prompt to its head, and the first such token is the only place a collapsed
turn still carries its rid. Keep the
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

**There is one deadline, and it is 90 minutes** (`STUCK_AFTER_S = 5400`, the default
`--timeout` on `cgc enqueue`, `cgc await`, `cdp_consult.py wait`, and `followup
--watch`).

It is not a budget for the consult. A GPT-6 Astra Pro round reasons for about 25 minutes;
that is how long the work *takes* — an expectation, not a deadline — and nothing is
killed for reaching it. The deadline answers a different question: past what point is
waiting no longer explained by the work? Beyond 90 minutes the answer is not late,
something is broken, and the right response is to read
`$CGC_SPOOL_DIR/logs/<rid>.log` rather than wait again. (An earlier 3600s value
stranded a deep re-reasoning follow-up that was observed to think ~62 minutes before
answering — 5400s covers that observed tail with margin.)

Earlier versions carried three numbers — a 1500s per-consult budget, an 870s agent
window, and a `timeout 899` wrapper — because two of them were mis-named. The 1500s was
the expectation above; the 870s existed only to stay under a believed 900s cap on
background tasks the agent launches. **That cap was measured and does not exist:** an
unbounded background waiter is accepted, and an unbounded 1000-second task ran to
completion. The wrapper only ever truncated healthy consults, so it is gone. Waiters
wait.

### 3. Reference material

Deeper prompt/injection guidance the templates are derived from lives in
[`skill/references/`](../skill/references/):

- `injection-and-prompting.md` — the URL catalog (every GitHub injection method, when to use it, failure modes) and file-selection rules.
- `gpt-6-astra-prompting-principles.md` — prompting principles for the current model (GPT-6 Astra).
- `deep-review-output.md` — the review output contract.

## Backends

The only backend is the **external CDP client** (`cdp_consult.py`) — no MCP scanner, no output cap, detached waiter. The former Claude-in-Chrome MCP fallback was retired in v0.6; the reasons it lost are in [ARCHITECTURE.md](ARCHITECTURE.md#why-an-external-cdp-client-not-the-mcp-browser-path).

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
`cgc enqueue` (writes a queued round to the SQLite store, `control.db`) and
`cgc await` (polls that round until it's terminal, then materializes the answer) —
no network access from the agent's Bash call, so the classifier never triggers.
`bin/cgc doctor` checks the daemon is running as part of its normal health check;
`bin/cgc queue` shows daemon liveness plus what's currently queued in the store.
See [docs/ARCHITECTURE.md](ARCHITECTURE.md) for the full data flow and the
validating egress gate the daemon runs before every send.

### When ChatGPT's page changes: `cgc probe`

The daemon checks, without sending anything, that ChatGPT's page still works the way the driver
reads it. It checks four capabilities: the composer (paste and read back), the model picker (for
the configured tier@family; a break holds only rounds frozen to that pair), the @mention picker,
and the reader (finding a finished turn and its answer). It runs these checks at start, before
queued work once the last check is 6 h old, and again (at most every 30 min) after one breaks.

When one breaks, rounds that need it **stay queued**: nothing is failed and nothing is spent.
`cgc await` reports `ChatGPT UI changed (<capability>: <cause>) — sends paused; driver needs a fix;
queued rounds are intact`, and `cgc queue` shows the same line. A retrieve is never paused.

```bash
bin/cgc probe                        # check now, then print each capability's state
bin/cgc probe --only reader
bin/cgc probe --mention "WebCodex Demo"   # the app the mention check inserts
bin/cgc probe --status               # print the recorded states; no browser
```

The probe opens its own background tabs, closes only those, and never sends. The composer check
selects `CGC_MODEL_FAMILY`/`CGC_MODEL` on a `CGC_PROJECT_URL` tab. The mention check uses an app
in this order: `--mention`, then the app a recorded mention failure names, then the first app in
`CGC_APPS`. With none of those, the probe says so. If the app is not connected on your account,
mention is reported "not established", which is not the same as broken. A check that fails for a
reason unrelated to the page changes nothing: Chrome down, a timeout, or a login, challenge or
rate-limit page, which is printed as `CGC_PROBE_BLOCKER` for you to clear. Exit codes: 0 all ok,
3 something is broken, 1 the probe was inconclusive.

## The dedicated Chrome, in one place

The launcher [`cdp_launch.sh`](../skill/scripts/cdp_launch.sh) reads
`CGC_PORT`, `CGC_PROFILE`, `CGC_CHROME`, and `CGC_PROJECT_URL`. Run it once via
`bin/cgc launch`; it starts Chrome if down, and in `--gate` mode reports login
state by exit code (0 ready / 2 login-needed / 1 chrome-fail) so `submit` can
self-heal setup with no LLM step.
