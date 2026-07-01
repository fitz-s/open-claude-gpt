# Security model

`open-claude-gpt` automates a browser session **you** have already authenticated.
It is designed so the agent never touches your credentials and so consults ship
*links to already-public code*, not private content.

## What it does and does not do

- **Does:** open a tab in a Chrome profile you logged into, type a prompt, read
  the answer text back, and write it to a local file you own.
- **Does not:** handle your ChatGPT password or session cookies, read cross-site
  data, call any private/hidden ChatGPT API, or bypass login, CAPTCHA, or rate
  limits.

## Credentials

- **You log in once, by hand,** in the dedicated Chrome window. The agent has no
  credential path — it drives an already-authenticated session.
- The login persists in the dedicated profile (`CGC_PROFILE`, default
  `~/.cgc-chrome`), isolated from your normal Chrome.
- Remote debugging is scoped to loopback in two layers: the debug Chrome is
  launched with `--remote-debugging-address=127.0.0.1`, so the DevTools socket
  itself never binds to a routable interface, plus `--remote-allow-origins=http://127.0.0.1:<port>`
  (not `*`), so only a same-origin local client on that port can attach. `bin/cgc
  doctor` verifies the port is actually listening on loopback only, not just that
  the allow-origins flag was passed.

## Don't send secrets

The skill is built to **not** exfiltrate private content, but you own the inputs:

- **Never** put `.env` files, API keys, tokens, credentials, log output, or any
  private/customer/proprietary data in a prompt, gist, context file, or link —
  including inside a **public** gist. A public gist is still public: anything
  pasted into one is world-readable the moment it's created, same as a public
  repo link.
- `deliver` checks repo visibility (`gh`) and stamps the payload: a **public**
  repo's links are declared world-readable; a **private** repo is flagged as
  exfiltration and points you at pushing to a public repo or keeping it local.
- **Prefer links to pushed, public code.** A pushed commit with an associated PR
  always resolves to that public PR link. A **public gist** is a last resort for
  genuinely unpushed/private-inaccessible state — never a private gist, and never
  a substitute for keeping secrets out in the first place. Gisting content that
  is *already* on GitHub is explicitly disallowed by the skill's rules: the
  model gets a link to the existing public code, not a copy.
- **What must never go in a gist (public or otherwise):** `.env` files or any
  file containing secrets, API keys/tokens/credentials, session cookies, log
  files that may contain internal hostnames or user data, and any
  customer/proprietary data. If content doesn't belong in a public repo, it
  doesn't belong in a gist either.

## Fail-closed guarantees

- **No code source → no send.** `prep`/`submit` refuse a non-followup consult
  with no real code link (`CGC_ERROR no_code_source`). Prose about code is not a
  substitute for the code.
- **Wrong model → no send.** `submit`/`followup` refuse to send if the target
  model tier can't be selected, rather than silently degrading (override only
  with an explicit `--allow-model-mismatch`).

## Trust boundary

**ChatGPT is a strong advisor; your local agent applies its work.** Lean on the
consult — it does real reasoning worth acting on. Give the load-bearing claims a
quick local look (the `verify locally: <check>` tags point at what's worth a
glance) before shipping; that's a sanity-check, not a line-by-line re-audit.

## Reporting a vulnerability

Open a GitHub issue for non-sensitive reports, or use private disclosure for
anything exploitable. Please include repro steps and the affected script/version.
