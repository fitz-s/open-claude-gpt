<!-- Source: OpenAI "Using GPT-5.6" model guide (developers.openai.com/api/docs/guides/latest-model?model=gpt-5.6) + the GPT-5.5 prompting guide (/prompt-guidance), as of 2026-07-10. The 5.6 guide states verbatim: "Prompting guidance applicable to GPT-5.5 remains applicable to GPT-5.6" — so the 5.5 outcome-first principles distilled below ARE authoritative for 5.6, with the 5.6-specific deltas added in their own section. Model family: the `gpt-5.6` alias routes to `gpt-5.6-sol` (flagship); `gpt-5.6-terra` (balanced/cheaper); `gpt-5.6-luna` (efficient/high-volume). We drive ChatGPT web at the highest-reasoning tier ("Pro Extended" switcher item), which post-GA (2026-07-09) runs sol-class 5.6. The PROMPT_TEMPLATE in scripts/consult.py implements these — keep it aligned. -->

# GPT-5.6 prompting principles (authoritative)

The consult target is the **GPT-5.6 family** — a strong reasoning model (`Sol` flagship / `Terra` / `Luna`). We drive it through ChatGPT web at the highest-reasoning tier (the `Pro Extended` switcher item; `--model Pro` selects it). Prompt it the way OpenAI says to prompt 5.x reasoning models — outcome-first — NOT the GPT-4-era process-heavy way. 5.6 is more token-efficient and "efficient by default, maximum performance on demand" than 5.5, which only sharpens these rules: a tight outcome-first prompt gets more per token, and padding costs more.

## What changed vs older models (and what to STOP doing)
- **Outcome-first, not process-heavy.** Define the destination (goal, success criteria, constraints, available evidence, what the final answer must contain) and let the model choose the path. *Do not* spell out "first inspect A, then B, then think through every exception…". Over-specifying the process adds noise, narrows the search space, and yields mechanical answers.
- **Drop "think deeply / be exhaustive / go several levels deeper" coaching.** A reasoning model already does this; such lines are GPT-4-era residue. Depth comes from clear success criteria, not from telling it to breathe.
- **Stop the ALWAYS / NEVER / MUST spam.** Reserve absolutes for *true invariants* (safety, required output fields like the sentinel wrap, things that must never happen). For judgment calls (when to search, when to switch routes, when to ask), use **decision rules**, not screaming.
- **Use LESS formatting.** Plain paragraphs are the default for explanations/reports. Headers/bullets/tables sparingly — only when they aid comparison or scanning. Don't make the structure heavier than the content.
- **Drop the current date** (the model knows it). Don't bake "today is…" into the prompt.
- **Reasoning effort is a last-mile knob**, not the main quality lever; strong prompts + success criteria + light verification recover most of it. The API default moved to `medium` at 5.5, and **higher effort is not automatically better** — on a prompt with weak stopping criteria or open-ended search, more effort buys *overthinking* and needless searching, not quality. For us the "effort knob" is *tier selection* — we already pin the `Pro Extended` (5.6 Sol-class) tier, so the lever is spent; the win is in the prompt. This is exactly why the template's crisp **Stop rules + retrieval budget** carry the weight they do: they're what keeps a high-reasoning tier from spiraling.

## GPT-5.6 deltas (on top of the 5.5 principles above)
OpenAI confirms 5.5 prompting carries forward to 5.6; these are the few things 5.6 changes, and all four already point the same way our template does.
- **Shorter prompts measurably win.** In OpenAI's evals, replacing long explicit prompts with minimal ones improved scores ~10–15% while cutting tokens 41–66% and cost 33–67% — because heavier prompts push 5.6 into extra exploration and repeated validation. Many old instructions are now *default behavior*; keep only what the model won't do on its own. → This is why `--task` must be pure *delta*: cut any sentence that restates the template's contract, and don't pad. (Was a style rule; on 5.6 it's a measured quality lever.)
- **Don't use blanket brevity instructions.** 5.6 is already biased toward compression and is *sensitive* to "be concise / keep it short / minimal text" — such a line can make it substitute a shorter artifact for the full one you asked for (e.g. truncating the analysis). Never tell a consult to be brief. Steer with **prioritization** instead — lead with the verdict, keep every required fact/caveat/next-step, trim intros and repetition — which the template's "open with the answer" + findings shape already do.
- **The tier IS the mode; prompt the task, not the mode.** Our pinned "Pro Extended" web tier is the analog of 5.6's API pro mode (`reasoning.mode:"pro"` — more model work, one final answer; effort still defaults to `medium`). OpenAI: enable it in the request, *not* the prompt — don't ask it to "use pro mode", "think harder", or emit several candidates; give the same outcome-first prompt. Reinforces the "drop think-deeply coaching" rule above.
- **Safeguards can pause or block mid-stream.** 5.6 runs synchronous cyber/bio misuse classifiers as it generates: expect occasional multi-second pauses (the detached waiter must stay patient — a pause is not a stall, don't kill it) and, rarely, an intervention on *legitimate* dual-use work — code review, vuln research, patch dev, security education, defensive testing (exactly this skill's use cases). A safeguard refusal presents like a blocker: surface it to the user, don't blind-retry.
- **~25 min of reasoning makes the QUESTION the expensive part.** Depth amplifies whatever you aimed it at, so a fuzzy ask returns 25 minutes of rigorous work on the wrong question — discovered only on arrival. Name the actual decision, the bar for "good", and labelled sub-questions (a/b/c) so the budget is spent per-risk. Precision and length are opposites here: depth comes from success criteria, not words.

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
- **Persona, not personality padding**: `--role` supplies the expert persona (5.6 splits *personality* = how it sounds from *collaboration style* = how it works). Keep it to the one sentence that steers where depth lands; don't spend it on warmth we don't need — a background consult has no conversational UX to tune.
- **Retrieval budget** (a stopping rule for search, not just a route rule): treat the supplied URLs as ground truth; start from them, and search the web again only when the given sources don't answer the core question, a required fact/owner/date/id is missing, or the ask is explicitly exhaustive. Don't re-search to improve phrasing or pad examples. Name exactly what's unreadable if a link fails, and switch routes before reporting a gap. (Replaces the old "BE RESOURCEFUL, NEVER decline" shouting; matches 5.6's efficient-by-default bias.)
- **Grounding**: don't fabricate; absence of evidence ≠ a defect; label what needs local verification; distinguish source-backed facts from reasoned inference. (We're advisory; Claude Code verifies.)
- **Prompt it to check its work / hand back verification steps** — but as the few highest-value local checks (`verify locally: <check>`), not a mechanical checklist.
- **No preamble / streaming updates.** 5.6 supports time-to-first-token preambles for interactive UIs — we deliberately do NOT use them. The detached waiter reads ONLY the sentinel-wrapped final answer (`BEGIN_RESPONSE`/`END_RESPONSE`); a preamble or mid-stream "commentary" message is not the answer and would break the contract. Never add preamble/`phase` prompting here.
- **Sentinel wrap** = a true invariant (required output field), so an absolute is correct there.

## The skill's live template
`scripts/consult.py` `PROMPT_TEMPLATE` implements this: Role → Goal(`{task}`) → Success criteria → Source to review(`{refs_block}`) → Context(`{context_block}`) → Constraints → Output(`{output_block}`) → Stop rules → sentinel. Keep edits outcome-first; resist re-adding process coaching or ALWAYS/NEVER stacks.

## Tier note (verify if the web UI renamed things)
The model selection in `cdp_consult.py` targets ChatGPT's **switcher labels** (`Pro` / `Pro Extended`), not a model-version string, so it survived the 5.5→5.6 swap unchanged — `Pro Extended` simply runs 5.6 now. If a future ChatGPT web build renames the top reasoning tier (e.g. surfaces `Sol` / `GPT-5.6 Pro` directly), the label match may miss and `submit` will fail-closed with `model_not_selectable`; fix by setting `CGC_MODEL` / `--model` to the new label. This is the one 5.6 detail that can't be confirmed without driving the live UI — a test `submit` (check `modelConfirmed`) or `cgc doctor` verifies it.
