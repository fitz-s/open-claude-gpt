# Example: 🧠 offload a hard reasoning problem

When the hard part is self-contained — a tricky algorithm, a proof, a heavy
calculation, a concurrency argument — hand it to ChatGPT Pro and keep Claude on
the main task. Claude then verifies the result locally (runs the code, checks the
math) before trusting it.

## A. A concurrency / correctness argument (code-grounded)

```bash
bin/cgc deliver --repo owner/repo --ref main --files src/scheduler/queue.rs

bin/cgc prep \
  --title "Is the lock-free queue actually linearizable?" \
  --role "concurrency theorist auditing a lock-free data structure" \
  --task "Prove or refute that the MPSC queue in src/scheduler/queue.rs is linearizable. Walk the interleavings that matter (concurrent push vs pop, the CAS on tail, ABA). If it's buggy, give the exact interleaving that violates it and a fix. Mark anything needing a runtime check 'verify locally'." \
  --refs-file /tmp/cgc/refs_*.md

bin/cgc submit --rid <RID> --prompt-file /tmp/cgc/prompt_<RID>.md
timeout 900 bin/cgc wait --rid <RID> --out /tmp/cgc/answer_<RID>.txt --timeout 870
```

Then **verify locally**: Claude writes the stress test / loom model the answer
suggests and runs it — the proof is a hypothesis until the test passes.

## B. A self-contained math / calculation problem (no repo)

No code source? A design doc or the problem statement itself can be the source via
a gist:

```bash
gh gist create problem.md          # only for content genuinely not already on GitHub
bin/cgc prep \
  --title "Closed-form for the retry-budget expectation" \
  --role "applied mathematician" \
  --task "Derive the expected total retries under exponential backoff with jitter, cap C, base b, max attempts n. Show the derivation; give the closed form; sanity-check against n=1 and C→∞." \
  --refs-file <gist-url-refs-file>
```

Verify locally: Claude runs a quick Monte-Carlo simulation and checks the closed
form matches the empirical mean.

> Rule of thumb: send the part that's **hard and self-contained**; keep the part
> that needs your repo, your data, or a running test on Claude's side — and always
> close the loop with a local check.
