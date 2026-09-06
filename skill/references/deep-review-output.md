# Required output — a DEEP review, in this order (do NOT reorder; §1 before any file)

## 1. APPROACH / PLAN VERDICT (do this FIRST, before any file)
Is the core design/plan even correct — not "is the code clean", but "is this the right approach at all"?
State a verdict: CORRECT-AS-IS / CORRECT-BUT-SUBOPTIMAL / WRONG, with the reasoning. If a SUPERIOR
architecture, formulation, or decomposition exists (a different model, method, or structure), name it and
say why it dominates. This is the highest-value section.

## 2. CORE CORRECTNESS (the part that decides if it actually works)
The load-bearing logic / math / algorithm: is each key step sound? Where a better estimator, method, or
data structure exists, name it. Where is it most likely wrong, and under what input?

## 3. VALIDATION SOUNDNESS
Is the evidence it works (tests / replay / benchmark / proof) a SOUND demonstration, or does it have holes
or coverage gaps? Name the single load-bearing claim that is asserted but not actually proven. (Omit this
section if the change has no validation artifact to assess.)

## 4. PER-FILE / PER-STAGE FINDINGS (every file in the change)
Cover every changed file — do not stop early, and sweep security, migration/rollback and
concurrency/ordering across the change as a whole (§2 owns correctness itself and the inputs that
break it). One finding per line, ONE severity scale throughout:
`[SEVERITY] category — file:path:line — impact (one sentence) — concrete fix — verify locally: <check>`
Call out whether the tests are genuinely adversarial (fail on a real regression) or pass trivially.

## 5. IDEAL-vs-ACTUAL DIFFERENCE + SUPERIOR REALIZATIONS (ranked by leverage)
For each place the implementation differs from the ideal realization, state the difference and the upgrade.
Rank by leverage/impact, highest first.

## 6. GO / NO-GO
Is it safe + correct enough to ship / act on now, or is there a BLOCKER that must be fixed first? List the
blockers explicitly, each with the smallest fix that clears it.
