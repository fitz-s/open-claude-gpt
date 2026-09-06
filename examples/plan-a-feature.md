# Example: 🧭 plan a feature before Claude builds it

Use ChatGPT Pro to produce an ordered, critiqued plan — and to challenge whether
the approach is even right — before Claude writes code. Then Claude executes and
feeds results back into the same thread.

## 1. Fire — ask for a plan, and make it challenge the premise

```bash
bin/cgc fire --repo owner/repo --ref main \
  --title "Plan: add offline sync to the notes app" \
  --role "staff engineer planning a feature; verify the approach before detailing steps" \
  --task "Plan adding offline-first sync. FIRST decide if the proposed approach (local SQLite + CRDT merge on reconnect) is right, or if a simpler design dominates. Then give an ordered plan: phases, the risky/uncertain steps, migration + rollback, and what to prototype first. Name the single assumption that, if wrong, breaks the plan." \
  --output-file examples/_specs/plan-output.md --output-replace \
  --request-key offline-sync-plan
```

A whole-repo `--ref` (no `--files`) is right for a design/plan consult — no need
to pick files up front. `--output-replace` matters here: `plan-output.md` defines
its own shape (verdict/phases/risks/assumption), so without it the answer would
carry two competing findings formats back to back.

> Tip: for a plan, ask for **phases + risks + the one load-bearing assumption**,
> and explicitly invite it to reject your approach. Its biggest wins come from
> disagreeing with your framing, not rubber-stamping it.

## 2. Await, then execute locally

```bash
bin/cgc await --rid <rid>   # detached (run_in_background: true) — its exit is the wake
```

## 3. Follow up only if execution changes the plan

Claude implements phase 1 and hits a real constraint the plan didn't know about —
that's worth a round: it invalidates part of the plan, not just confirms it.

```bash
bin/cgc fire --followup --parent <rid> --no-code \
  --title "Phase 1 results + migration constraint" \
  --task "Phase 1 done. CRDT merge works, but the migration step you proposed can't run online — the table lock blocks writes for ~40s on our data size. Re-plan phases 2-3 around a zero-downtime migration." \
  --request-key offline-sync-plan-phase1

bin/cgc await --rid <rid>
```
