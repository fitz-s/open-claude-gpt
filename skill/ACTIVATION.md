[chatgpt-consult — ACTIVE this session]
A background channel to the user's ChatGPT Pro (GPT-6 Pro by default). It answers deep questions in about half an hour while you keep working, and the thread continues across rounds. The user wants the weekly allowance spent on real work, and under-use is the failure this note exists to prevent. A fire is one command and never blocks you, so load the `chatgpt-consult` skill on your own initiative, without being asked, whenever a consequential decision is still open when the answer lands and one of these holds:
- you are about to merge, or open a PR for, a non-trivial diff (fire alongside your usual reviewer, not instead of it);
- a change touches money, data integrity, concurrency, or security;
- a measured result contradicts a load-bearing assumption;
- the deciding evidence is in a local DB, logs, or private code that an independent analyst should query and re-run;
- a reversible but expensive design fork comes up before building;
- a maths or statistics property must be derived or broken;
- you are stuck: a bug survives two fix attempts, or a diagnosis is split between competing causes;
- you and a reviewer disagree on a consequential claim, or a fix has been sent back twice;
- an external contract must be researched before it is coded;
- substantive writing has competing framings.

Your usual reviewers stay for what you need now; Pro gets the deeper, slower question in parallel. Name the decision it can change, your current answer, and the check that will judge the result. Questions that can wait go to the backlog with `--not-before`.

A failed, refused, or silent round is a channel incident, not a verdict on the channel: act on the envelope's `code` (diagnose, retrieve, refire only where the envelope says so, relay `human_action`) and keep firing new questions. Never decide the consult is optional or stuck and stop using it.

Local evidence: add `--workspace <name> --mention "<app>"` (both listed by `consult.py apps`) and the remote model reads files, queries data, and runs checks inside the user's registered local workspaces. This is how private code and local evidence reach it; never paste them, and never paste secrets.

The agent path is local only: `consult.py fire`, then a detached `await`. The user's daemon does the send. Do not check the daemon first. If the classifier denies a command, relay it and stop. After acting on an answer, grade it with `cgc_spool.py feedback`; the grades tune these triggers.
