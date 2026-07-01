# Output spec: architecture decision record

Use ONE consistent structure (this replaces the default findings list):

1. **Recommendation** — one line + confidence (high/medium/low).
2. **Options** — a table: option × [fit for constraints, failure modes, migration cost, operational cost, schema/evolution]. Ground each cell in the actual code.
3. **The case against the recommendation** — the strongest argument for the option you did NOT pick, stated fairly.
4. **What would change the call** — the single fact/result (e.g. a benchmark number) that would flip the recommendation.
5. **First step** — the cheapest spike that de-risks the choice.

Tag anything needing a local run "verify locally: <check>".
