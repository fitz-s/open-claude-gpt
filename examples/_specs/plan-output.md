# Output spec: implementation plan

Structure the answer as:

1. **Verdict** — one line: is the proposed approach right, or does an alternative dominate? Confidence (high/medium/low).
2. **Approach check** — if you'd change the approach, say what to instead and why it dominates. Otherwise, why the proposed one holds.
3. **Phases** — ordered. For each: goal, the concrete steps, and whether it's low-risk or uncertain.
4. **Risks & mitigations** — the steps most likely to go wrong; migration + rollback.
5. **Prototype first** — the single riskiest thing to spike before committing.
6. **Load-bearing assumption** — the one assumption that, if wrong, breaks the plan.

Tag anything needing a local run "verify locally: <check>".
