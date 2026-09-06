# Example: 🧠 offload a hard reasoning problem

When the hard part is self-contained — a tricky algorithm, a proof, a heavy
calculation, a concurrency argument — hand it to ChatGPT Pro and keep Claude on
the main task. Claude then verifies the result locally (runs the code, checks the
math) before trusting it.

## A. A concurrency / correctness argument (code-grounded)

```bash
bin/cgc fire --repo owner/repo --ref main --files src/scheduler/queue.rs \
  --title "Is the lock-free queue actually linearizable?" \
  --role "concurrency theorist auditing a lock-free data structure" \
  --task "Prove or refute that the MPSC queue in src/scheduler/queue.rs is linearizable. Walk the interleavings that matter (concurrent push vs pop, the CAS on tail, ABA). If it's buggy, give the exact interleaving that violates it and a fix. Mark anything needing a runtime check 'verify locally'." \
  --request-key queue-linearizability

bin/cgc await --rid <rid>   # detached (run_in_background: true) — its exit is the wake
```

Then **verify locally**: Claude writes the stress test / loom model the answer
suggests and runs it — the proof is a hypothesis until the test passes.

## B. A self-contained math / calculation problem (no repo)

No code, no repo: put the whole problem in `--task` and skip the link with
`--no-code` — it's the one flag that legitimately needs no `--refs-file`.

```bash
bin/cgc fire --no-code \
  --title "Closed-form for the retry-budget expectation" \
  --role "applied mathematician" \
  --task "Derive the expected total retries under exponential backoff with jitter, cap C, base b, max attempts n. Show the derivation; give the closed form; sanity-check against n=1 and C→∞." \
  --request-key retry-budget-closed-form

bin/cgc await --rid <rid>
```

Verify locally: Claude runs a quick Monte-Carlo simulation and checks the closed
form matches the empirical mean.

> A problem statement too long for `--task`, and not already on GitHub, needs a
> link the agent can't create itself today — there's no `gh gist create`
> permission in this skill's `allowed-tools`, and the daemon's egress gate refuses
> any gist link unless the user has set `CGC_GATE_ALLOW_GIST=1`. Ask the user for
> a public URL instead, put it in a refs file by hand, then `bin/cgc prep
> --refs-file <that file> --title … --task …` and the `enqueue` line it prints —
> `fire` always builds its own refs file from `--repo`/`--ref` and can't take one.

> Rule of thumb: send the part that's **hard and self-contained**; keep the part
> that needs your repo, your data, or a running test on Claude's side — and always
> close the loop with a local check.
