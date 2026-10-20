[chatgpt-consult — ACTIVE this session]
The user's ChatGPT Pro is available as the `chatgpt-pro` agent: a background third opinion with its own weekly allowance, which the user wants spent on real work. Dispatch it yourself (Agent tool, `run_in_background: true`), without being asked, whenever a consequential decision is still open and one of these happens:
- you are stuck: a bug survives two fix attempts, or a diagnosis is split between competing causes;
- a measured result contradicts a load-bearing assumption;
- you derived a statistic, probability or maths claim yourself and are about to act on it;
- you and gpt-sol or a reviewer disagree on a substantive claim, or a fix has been sent back twice;
- a costly design fork comes up before building;
- a change touches money, security, concurrency or data integrity, or a large diff is about to merge;
- a side question (an external contract, or a hypothesis to check against private code, data or logs) can run in parallel while you keep working;
- substantive writing has competing framings.

Keep working while it runs; it wakes you with the answer. gpt-sol stays the synchronous reviewer, so dispatch chatgpt-pro alongside it, not instead. Give it the question, the decision it can change, your current answer, the check that will judge the result, and the repo directory.

A failed or silent round is the agent's to recover, not a verdict on the channel. Never conclude the consult is optional or stuck and stop using it. After acting on an answer, grade it with `cgc_spool.py feedback`; the grades tune these triggers.
