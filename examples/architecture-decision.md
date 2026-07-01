# Example: 🏗 weigh an architecture decision

Get an independent, code-grounded take on a design fork — with the strongest case
*against* your preferred option — before committing to it.

```bash
# whole-repo tree so it can see how the pieces fit today
REFS_FILE="$(bin/cgc deliver --repo owner/repo --ref main | python3 -c 'import json,sys; print(json.load(sys.stdin)["refs_file"])')"

bin/cgc prep \
  --title "Event sourcing vs. a state table for the ledger?" \
  --role "distributed-systems architect; adversarial, not agreeable" \
  --task "We're deciding between (A) event-sourcing the ledger and (B) a mutable balances table + audit log. Given the actual code, which dominates for our constraints (strong consistency, ~2k tx/s, must reconstruct any historical balance)? Argue BOTH sides from the code, name the failure modes of each (rebuild cost, snapshotting, schema evolution, concurrent writes), then recommend one — and give the strongest case against your own recommendation." \
  --refs-file "$REFS_FILE" \
  --output-file examples/_specs/decision-matrix.md \
  --output-replace

bin/cgc submit --rid <RID> --prompt-file /tmp/cgc/prompt_<RID>.md
# Copy the exact waiter command printed by submit — it includes the conversation id.
timeout 899 bin/cgc wait --rid <RID> --conversation <CONVERSATION_ID> --out ./cgc_answers/answer_<RID>.txt --poll 20 --timeout 870
```

A custom `--output-file` (with `--output-replace`) is useful here: define a
tradeoff matrix / decision-record shape instead of the default findings list.

## Follow up with a spike result

```bash
timeout 899 bin/cgc followup \
  --task "We prototyped option A. Rebuilding a balance from 2M events takes 6s uncached — too slow for the reconcile job. Does snapshotting every N events change your recommendation, and at what N?" \
  --title "Spike: rebuild latency" \
  --watch --conversation <CONVERSATION_ID> --out ./cgc_answers/answer_r2.txt --timeout 870
```

> For any decision with a design or a plan, tell it to **verify the approach
> first** and to name a superior alternative if one exists — before it evaluates
> the option you walked in with.
