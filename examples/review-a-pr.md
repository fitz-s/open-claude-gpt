# Example: review a pull request

End-to-end, by hand. In Claude Code the skill orchestrates all of this for you —
this shows what happens under the hood.

## 0. One-time setup

```bash
bin/cgc launch          # log into ChatGPT in the window, leave it open
bin/cgc doctor --deep   # confirm ready + logged in
```

## 1. Deliver — resolve the code links

Capture the refs file path from `deliver`'s JSON output — never a shell glob:

```bash
REFS_FILE="$(bin/cgc deliver --repo owner/repo --pr 123 | python3 -c 'import json,sys; print(json.load(sys.stdin)["refs_file"])')"
```

`deliver`'s output (trimmed) looks like:

```json
{
  "mode": "explicit",
  "slug": "owner/repo",
  "visibility": "public",
  "associated_pr": 123,
  "refs_file": "/tmp/cgc/refs_20260701-120000.md",
  "groups": {
    "intent": [["https://github.com/owner/repo/pull/123", "PR description, discussion, CI"]],
    "change_set": [["https://github.com/owner/repo/pull/123/files", "changed-file diff"]]
  }
}
```

Note it leads with the **public PR link** — never a gist for pushed, public code.
(`refs_file` itself lives under `/tmp/cgc` — tool-owned scratch — which is why
`$REFS_FILE` captures it rather than hardcoding the path.)

## 2. Prep — render the prompt

```bash
bin/cgc prep \
  --title "Review PR 123" \
  --role "senior reviewer auditing correctness, migration safety, and concurrency" \
  --task "Review this PR. Verify the approach before the diff; flag silent breakage, ordering/concurrency, and rollback. Recommend a simpler design if one dominates." \
  --refs-file "$REFS_FILE"
```

Prints a `request_id` (RID) and a `prompt_file` under `$CGC_STATE_DIR`.

## 3. Submit — open the chat and send

```bash
bin/cgc submit --rid <RID> --prompt-file /tmp/cgc/prompt_<RID>.md
```

It selects the model tier (`CGC_MODEL`), types, sends, and prints the exact
**detached waiter** command to run in the background.

## 4. Wait — background, its exit is the wake

```bash
# Copy the exact waiter command printed by submit — it includes the conversation id.
timeout 899 bin/cgc wait --rid <RID> --conversation <CONVERSATION_ID> --out ./cgc_answers/answer_<RID>.txt --poll 20 --timeout 870
```

(In Claude Code this runs with `run_in_background: true`; the process exit wakes
the agent, which then reads `--out`.)

## 5. Verify, then follow up

Read the answer, **verify each `verify locally: <check>` claim in your repo**,
then feed results back in the same thread:

```bash
timeout 899 bin/cgc followup \
  --task "Confirmed findings 1-3 locally (tests pass). Finding 4 doesn't reproduce — here's why: … Re-assess." \
  --title "Round 2: local results" \
  --watch --conversation <CONVERSATION_ID> --out ./cgc_answers/answer_r2.txt --timeout 870
```

Loop until the answer flags nothing new.
