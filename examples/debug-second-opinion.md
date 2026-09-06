# Example: 🐞 second opinion on a stuck bug

When you're deep in a bug and out of fresh ideas, offload a ranked-hypotheses
second opinion. Give it the code link **plus** a `--context-file` with the failure
evidence (logs, stack trace, what you already ruled out).

```bash
# 1. bundle the evidence Claude has gathered locally
cat > /tmp/cgc/bug-context.md <<'EOF'
## Symptom
Intermittent 500s on POST /orders, ~1 in 300, only under load.

## Evidence
- stack trace (attached below) points at OrderRepo.save → connection pool
- happens only when concurrency > ~50; never reproduces single-threaded
- pool size 20, timeout 30s; DB CPU is flat (not the DB)
- added logging: the failing requests all waited >29s for a connection

## Already ruled out
- not a slow query (all queries <10ms in the slow log)
- not the DB (flat CPU, no lock waits)
- reverting commit abc123 does NOT fix it

<stack trace here>
EOF

# 2. fire: the code link + the evidence bundle, in one call
bin/cgc fire --repo owner/repo --ref main --files src/orders/repo.py src/db/pool.py \
  --title "Intermittent 500s under load — connection pool starvation?" \
  --role "debugger doing root-cause analysis; competing hypotheses, evidence for/against each" \
  --task "Given the code and the failure evidence, rank the likely root causes most-to-least probable, each with the evidence for/against and the ONE cheapest probe that would confirm or kill it. Focus on the pool-wait >29s signal. Don't propose a fix until the top hypothesis is nailed." \
  --context-file /tmp/cgc/bug-context.md \
  --request-key orders-pool-starvation

bin/cgc await --rid <rid>   # detached (run_in_background: true) — its exit is the wake
```

## Close the loop — worth it because the probe result can confirm or kill the top hypothesis

```bash
bin/cgc fire --followup --parent <rid> --no-code \
  --title "Probe #1 result: leaked connections" \
  --task "Ran your probe #1: 3 connections per failing request are never returned — a missing close() on the error path in repo.py:88. That matches. Confirm this fully explains the >29s waits, and check whether the retry wrapper double-acquires." \
  --request-key orders-pool-starvation-probe1

bin/cgc await --rid <rid>
```

> `--context-file` is for **supplementary** evidence (logs, traces, what you ruled
> out). It never replaces the code link — ChatGPT still needs to read the real code.
