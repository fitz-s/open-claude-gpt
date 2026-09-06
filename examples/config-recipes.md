# Example: 💬 config recipes

Small, copy-paste configurations for common setups. All values are environment
variables — set them in your shell or in `.env` (see [`../.env.example`](../.env.example)).

## Spend your ChatGPT Pro tier (default, recommended)

Auto-select the strongest tier before every send, and fail closed if it isn't
available — so a consult never silently runs on a weaker model:

```bash
export CGC_AUTO_MODEL=1
export CGC_MODEL="Pro"
```

## On a plan that doesn't have Pro

Target whatever tier your plan includes:

```bash
export CGC_AUTO_MODEL=1
export CGC_MODEL="High"     # or "Pro"
```

## Turn auto model-selection OFF

Send on whatever the composer currently shows (you manage the model in the UI, or
you don't want the fail-closed guard):

```bash
export CGC_AUTO_MODEL=0
```

Per-consult, without changing the global setting: `bin/cgc fire … --model skip`.

## Group every consult in your ChatGPT project

Keeps consults in one place and lets project-level instructions/files in ChatGPT
apply to all of them:

```bash
export CGC_PROJECT_URL="https://chatgpt.com/g/g-p-<id>-<slug>/project"
```

Find the URL: open your project in ChatGPT, copy the browser URL, keep the
`/project` suffix.

## Run several consults at once (concurrency)

Each fired round gets its own daemon-driven tab, so 2–3 consults run in parallel
and the 10–30 min latency is free. A bare `fire --followup` resolves the last
completed thread and refuses ambiguously when several are live — pass `--parent
<rid>` (preferred, causal) or an explicit `--conversation <id>` to pick one:

```bash
bin/cgc fire --followup --parent <rid> --no-code --task "…"
```

## Use a specific browser / non-default port

```bash
export CGC_CHROME="/Applications/Chromium.app/Contents/MacOS/Chromium"
export CGC_PORT=9444           # if 9333 is taken
export CGC_PROFILE="$HOME/.cgc-chrome"
```

## Keep scratch somewhere other than /tmp

```bash
export CGC_STATE_DIR="$HOME/.cache/cgc"
```

## Check what's in effect

```bash
bin/cgc config
```
