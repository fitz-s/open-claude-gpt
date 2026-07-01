<!-- Source: OpenAI official GPT-5.5 prompting guide, https://developers.openai.com/api/docs/guides/prompt-guidance (fetched 2026-06-14). The consult target is GPT-5.5 Pro Extended (a reasoning model). The PROMPT_TEMPLATE in scripts/consult.py is built to these principles — keep it aligned. -->

# GPT-5.5 prompting principles (authoritative)

The consult target is **GPT-5.5 Pro Extended** — a strong reasoning model. Prompt it the way OpenAI says to prompt 5.5, NOT the GPT-4-era process-heavy way.

## What changed vs older models (and what to STOP doing)
- **Outcome-first, not process-heavy.** Define the destination (goal, success criteria, constraints, available evidence, what the final answer must contain) and let the model choose the path. *Do not* spell out "first inspect A, then B, then think through every exception…". Over-specifying the process adds noise, narrows the search space, and yields mechanical answers.
- **Drop "think deeply / be exhaustive / go several levels deeper" coaching.** A reasoning model already does this; such lines are GPT-4-era residue. Depth comes from clear success criteria, not from telling it to breathe.
- **Stop the ALWAYS / NEVER / MUST spam.** Reserve absolutes for *true invariants* (safety, required output fields like the sentinel wrap, things that must never happen). For judgment calls (when to search, when to switch routes, when to ask), use **decision rules**, not screaming.
- **Use LESS formatting.** Plain paragraphs are the default for explanations/reports. Headers/bullets/tables sparingly — only when they aid comparison or scanning. Don't make the structure heavier than the content.
- **Drop the current date** (the model knows it). Don't bake "today is…" into the prompt.
- **Reasoning effort is a last-mile knob**, not the main quality lever; strong prompts + success criteria + light verification recover most of it.

## Suggested prompt structure (official)
```
Role: [1-2 sentences: function, context, job]
# Personality      [tone/collaboration — only if it matters]
# Goal             [user-visible outcome]
# Success criteria [what must be true before the final answer]
# Constraints      [policy, safety, evidence, side-effect limits]
# Output           [sections, length, tone]
# Stop rules       [when to retry, fallback, abstain, ask, or stop]
```

## Patterns we use
- **Outcome + stop rule**: "Resolve … end to end. Success means … If evidence is missing, ask for the smallest missing field." → our Goal + Success criteria + Stop rules.
- **Retrieval/route decision rule** (not an absolute): treat the given URLs as ground truth; if one fails, switch routes before reporting a gap; name exactly what's unreadable. (Replaces the old "BE RESOURCEFUL, NEVER decline" shouting.)
- **Grounding**: don't fabricate; absence of evidence ≠ a defect; label what needs local verification. (We're advisory; Claude Code verifies.)
- **Prompt it to check its work / hand back verification steps** — but as the few highest-value local checks, not a mechanical checklist.
- **Sentinel wrap** = a true invariant (required output field), so an absolute is correct there.

## The skill's live template
`scripts/consult.py` `PROMPT_TEMPLATE` implements this: Role → Goal(`{task}`) → Success criteria → Source to review(`{refs_block}`) → Context(`{context_block}`) → Constraints → Output(`{output_block}`) → Stop rules → sentinel. Keep edits outcome-first; resist re-adding process coaching or ALWAYS/NEVER stacks.
