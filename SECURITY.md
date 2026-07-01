# Security Policy

## Reporting a vulnerability

Please report anything exploitable **privately** — open a
[GitHub security advisory](../../security/advisories/new) rather than a public
issue. Include repro steps, the affected script/version, and impact.

For non-sensitive hardening ideas, a normal issue is fine.

## Scope & model

`open-claude-gpt` automates a browser session **you** have already authenticated.
It never handles your credentials, calls no private ChatGPT API, and is built to
ship *links to already-public code* rather than exfiltrate private content. The
full threat model, credential handling, and fail-closed guarantees are documented
in **[docs/SECURITY.md](docs/SECURITY.md)** — please read it before reporting, as
it explains what is intentional (e.g. remote-debugging scoped to loopback, the
dedicated Chrome profile, the no-code-source / model-mismatch guards).

## What to never put in a consult

Secrets, `.env` files, API keys, tokens, or unrelated personal data — in a prompt,
gist, context file, or link. Deliver public GitHub links, not private content.
