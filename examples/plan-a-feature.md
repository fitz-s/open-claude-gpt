# Example: 🧭 plan a feature before Claude builds it

Use ChatGPT Pro to produce an ordered, critiqued plan — and to challenge whether
the approach is even right — before Claude writes code. Then Claude executes and
feeds results back into the same thread.

## 1. Deliver the repo as a browsable link

```bash
# whole-repo tree at a pinned commit — no need to pick files for a design/plan consult
bin/cgc deliver --repo owner/repo --ref main
```

## 2. Prep — ask for a plan, and make it challenge the premise

```bash
bin/cgc prep \
  --title "Plan: add offline sync to the notes app" \
  --role "staff engineer planning a feature; verify the approach before detailing steps" \
  --task "Plan adding offline-first sync. FIRST decide if the proposed approach (local SQLite + CRDT merge on reconnect) is right, or if a simpler design dominates. Then give an ordered plan: phases, the risky/uncertain steps, migration + rollback, and what to prototype first. Name the single assumption that, if wrong, breaks the plan." \
  --refs-file /tmp/cgc/refs_*.md \
  --output-file examples/_specs/plan-output.md   # optional: define the plan's shape
```

> Tip: for a plan, ask for **phases + risks + the one load-bearing assumption**,
> and explicitly invite it to reject your approach. Its biggest wins come from
> disagreeing with your framing, not rubber-stamping it.

## 3. Submit + wait (background)

```bash
bin/cgc submit --rid <RID> --prompt-file /tmp/cgc/prompt_<RID>.md
timeout 900 bin/cgc wait --rid <RID> --out /tmp/cgc/answer_<RID>.txt --timeout 870
```

## 4. Execute locally, then follow up with what actually happened

Claude implements phase 1, runs the tests, and reports back — so the plan adapts
to reality instead of staying theoretical:

```bash
timeout 900 bin/cgc followup \
  --task "Phase 1 done. CRDT merge works, but the migration step you proposed can't run online — the table lock blocks writes for ~40s on our data size. Re-plan phases 2-3 around a zero-downtime migration." \
  --title "Phase 1 results + migration constraint" \
  --watch --out /tmp/cgc/answer_r2.txt --timeout 870
```

Loop until the plan is solid, then let Claude finish executing it.
