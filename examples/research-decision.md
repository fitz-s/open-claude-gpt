# Example: 🔬 a research-backed decision (with web browsing)

ChatGPT Pro can browse the web. Use it for a library/approach/prior-art question
where the answer depends on current external information — while Claude keeps
building. Claude then verifies the recommendation against your actual repo.

```bash
bin/cgc fire --repo owner/repo --ref main --files src/http/client.py \
  --title "Replace our hand-rolled HTTP retry layer — with what?" \
  --role "senior engineer doing a build-vs-adopt evaluation with live web research" \
  --task "We maintain a custom retry/backoff/circuit-breaker layer in src/http/client.py. Research current options (tenacity, urllib3 Retry, httpx transports, resilience libs) as of now. Compare on: async support, jitter/backoff control, circuit breaking, maintenance health, and migration cost FROM our current code. Recommend one (or 'keep ours' with why). Cite sources inline; flag anything that needs a local benchmark." \
  --request-key http-retry-library-choice

bin/cgc await --rid <rid>   # detached (run_in_background: true) — its exit is the wake
```

**Verify locally:** Claude reads the recommended library's real API, checks it
against `src/http/client.py`'s call sites, and writes a tiny migration spike
before anyone commits to the switch. External research is a starting point, not
the decision.

> Because it browses, give it a **current** question and ask for **inline
> citations** — then verify the load-bearing claims yourself.
