# Example: review a pull request

End-to-end, by hand. In Claude Code the skill runs the same `fire` call for you —
this shows what it does.

## 0. One-time, once ever — not before every fire

```bash
bin/cgc install-daemon   # installs the egress daemon (launchd); starts at login, respawns if it dies
bin/cgc launch            # log into ChatGPT Pro in the window it opens, once — the session persists
```

This is setup, not a preflight: never run `doctor`/`queue`/`status` before firing
a consult. If the daemon is genuinely down, `fire`/`await` say so themselves in
one line.

## 1. Fire — resolve the code link, render the prompt, queue the round

`fire` is `deliver` + `prep` + `enqueue` in one process — there's no judgment call
between those three stages, so there's nothing to hand-edit in the normal case:

```bash
bin/cgc fire --repo owner/repo --pr 123 \
  --title "Review PR 123" \
  --role "senior reviewer auditing correctness, migration safety, and concurrency" \
  --task "Review this PR. Verify the approach before the diff; flag silent breakage, ordering/concurrency, and rollback. Recommend a simpler design if one dominates." \
  --output-file skill/references/deep-review-output.md --output-replace \
  --request-key pr-123-review
```

It leads with the **public PR link** (never a gist for pushed, public code) and
prints a JSON receipt — `rid`, `out`, `await` — for a round now queued in the
local store. No network call happens in this process; the user's launchd daemon
re-validates the code is public and performs the actual send.

## 2. Await — background, its exit is the wake

```bash
bin/cgc await --rid <rid>
```

In Claude Code this runs with `run_in_background: true`; the process exit wakes
the agent, which reads the `answer_path` off `await`'s JSON outcome envelope.

## 3. Verify, then follow up only if something is still unresolved

Read the answer and **verify each `verify locally: <check>` claim in your repo**.
A follow-up spends one of the account's few weekly Pro messages, so send one only
when a finding is still open after checking — not to report back that everything
was clean:

```bash
bin/cgc fire --followup --parent <rid> --no-code \
  --title "Round 2: local results" \
  --task "Confirmed findings 1-3 locally (tests pass). Finding 4 doesn't reproduce — here's why: … Re-assess." \
  --request-key pr-123-review-round2

bin/cgc await --rid <rid>
```
