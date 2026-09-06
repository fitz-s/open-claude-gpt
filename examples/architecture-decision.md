# Example: 🏗 weigh an architecture decision

Get an independent, code-grounded take on a design fork — with the strongest case
*against* your preferred option — before committing to it.

```bash
bin/cgc fire --repo owner/repo --ref main \
  --title "Event sourcing vs. a state table for the ledger?" \
  --role "distributed-systems architect; adversarial, not agreeable" \
  --task "We're deciding between (A) event-sourcing the ledger and (B) a mutable balances table + audit log. Given the actual code, which dominates for our constraints (strong consistency, ~2k tx/s, must reconstruct any historical balance)? Argue BOTH sides from the code, name the failure modes of each (rebuild cost, snapshotting, schema evolution, concurrent writes), then recommend one — and give the strongest case against your own recommendation." \
  --output-file examples/_specs/decision-matrix.md --output-replace \
  --request-key ledger-design-fork
```

`fire` resolves the link, renders the prompt, and queues the round in one call —
all local writes. It prints a JSON receipt with `rid` and a ready-to-run `await`
line; copy the `rid`:

```bash
bin/cgc await --rid <rid>   # detached (run_in_background: true) — its exit is the wake
```

A whole-repo `--ref` (not `--files`) is right here: the model needs to see how the
pieces fit today, not one file. `--output-file` + `--output-replace` swaps the
default findings list for a tradeoff matrix / decision-record shape.

## Follow up — only once the case actually changed

Worth a second round here: a real spike number can flip the recommendation, which
the first answer couldn't have known.

```bash
bin/cgc fire --followup --parent <rid> --no-code \
  --title "Spike: rebuild latency" \
  --task "We prototyped option A. Rebuilding a balance from 2M events takes 6s uncached — too slow for the reconcile job. Does snapshotting every N events change your recommendation, and at what N?" \
  --request-key ledger-design-fork-spike

bin/cgc await --rid <rid>
```

> For any decision with a design or a plan, tell it to **verify the approach
> first** and to name a superior alternative if one exists — before it evaluates
> the option you walked in with.
