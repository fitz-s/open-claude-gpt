# Security model

`open-claude-gpt` automates a browser session **you** have already authenticated.
It is designed so the agent never touches your credentials and so consults ship
*links to already-public code*, not private content.

## Read this before installing: account and Terms-of-Service risk

This tool **programmatically drives your ChatGPT session and programmatically
reads its output**. OpenAI's consumer Terms of Use (Rest-of-World individual
terms at the time of writing) prohibit using the services to "automatically or
programmatically extract data or Output", and OpenAI reserves the right to
suspend or terminate accounts for terms violations. Different regional,
business, or negotiated agreements may apply to you; **this project takes no
position on whether your use is permitted** — that is between you and OpenAI.

What that means concretely:

- **The account risk is yours and is real.** The worst plausible outcome is
  suspension or loss of the ChatGPT account you log in with. If that account
  matters to you beyond this tool, weigh that before installing.
- The tool deliberately does nothing to hide itself: it drives a visible
  browser session at human-plausible interaction rates, never touches hidden
  endpoints, and never bypasses login, CAPTCHA, or rate limits. That limits the
  blast radius; it does not create permission.
- If OpenAI offers a plan or written permission that covers automation of your
  session, that changes this calculus — check what your agreement actually says.

## Local threat boundary — what loopback does and does not give you

The DevTools port is bound to loopback and origin-restricted, which keeps it
unreachable **off-box**. It is NOT process authentication: **any process running
as your user on this machine can attach to the debug port** and, through it,
drive the dedicated Chrome — including its logged-in ChatGPT session — with far
broader capability than "read one answer". The dedicated profile bounds what
that session can reach (it holds only the ChatGPT login you gave it, not your
normal browsing identity). If your threat model includes hostile same-user
processes, do not run this tool on that machine.

## What the egress gate does and does not guarantee

The daemon re-validates every prompt before sending, fail-closed, over the exact bytes that leave
(the click-owning process runs the same gate again, so a direct send cannot skip it). It checks:

- **The URL law.** The gate judges the URL a browser would actually request. The prompt is cut into
  whitespace-delimited runs, and every `http(s):` inside a run starts a token that runs to the end of
  that run: an inner scheme never truncates the outer URL (`.../ok/ok/https:/../../../other/private`
  is judged whole), and a link nested in a proxy or redirect URL is judged as its own URL. Quotes,
  brackets and parentheses stay inside the token; only trailing sentence punctuation is dropped. Each
  token is parsed once, and that one parse feeds the visibility check, the existence check and the
  gist rule, so no check can look at a different repo than the link reaches. A GitHub link
  (`github.com`, `raw.githubusercontent.com`, `gist.github.com`, `api.github.com`,
  `codeload.github.com`) must be written plainly or it is **refused, not normalized**: `https://`
  only; the host exactly, with no userinfo, port, escape, trailing dot or non-ASCII look-alike; no
  backslash; no `.` or `..` path segment, no percent-escape and no empty segment; owner and repo in
  GitHub's character sets; a path segment never ends in `.`. Any other host under `github.com`,
  `githubusercontent.com`, `github.io`, `githubassets.com` or `ghcr.io` (Pages, signed downloads,
  packages, assets) is refused outright, because the gate has no check for it.
- **Line breaks.** A URL parser drops TAB, CR and LF from anywhere in the string it is handed, so the
  text after a link can be joined onto it. The gate judges the link alone and joined with what
  follows. Across one line break, the joined reading must name the same repo as the link alone:
  `https://github.com/cli/cli` followed by a line `-private-zz/x` reaches `cli/cli-private-zz`, and
  is refused. The consequence you will notice: **a bare `owner/repo` link that ends a line and is
  followed directly by a line starting with a word, digit or `-` is refused**, because nothing
  distinguishes prose from a join. End the link with a space or a trailing slash, or leave a blank
  line. Links that end in a path (`/pull/12`), links followed by a space, and links in the `prep`
  refs list are unaffected. Across a blank line (a paragraph break nobody joins) only a joined
  reading with a dot segment, an escape or a backslash is refused, and a TAB or bare CR next to a
  link is refused always. `tests/test_gate_differential.py` checks all of this against node's WHATWG
  URL parser on a generated corpus.
  **Links to other sites are outside the gate**: they are part of the operator's free-text boundary
  below, like any other sentence in a `--task`.
- **Repos.** Every repo a link reaches must be gh-confirmed public. Gist links are refused.
- **References.** Every PR link and every tree / blob / commit / compare / raw link on an admitted
  repo is checked to exist, in prompt order, each gh path asked once. A ref containing `/` (the
  branch `feat/x`) is one ref: the shortest leading prefix that resolves and under which the rest of
  the link exists as a path is taken, so `/blob/main/missing.py` does not verify as `main`. A commit
  link is exactly one ref. PR numbers are canonicalized (`0012`, `12.diff` and `12.patch` are PR 12).
  More than 48 distinct gh
  probes is refused as `partial verification`, so the gate never says "refs exist" about a link it
  did not check. Issue and `/pulls` links and the sub-pages of a PR are not existence-checked.
- **Secret shapes.** A fixed list of patterns: private-key blocks, AWS access key ids and secret-key
  assignments, `sk-` keys, GitHub tokens (`ghp_`, `github_pat_`), Slack tokens, Google API keys, JWTs.

**What the secret scan is.** A detector for those specified patterns, and nothing more. It does not
prove that free text contains no secret. A `--task`, `--context-file` or `--no-code` consult can carry
any sentence the caller wrote: a password in prose, a customer name, an internal hostname, a token
in a format the list does not know. No pattern scan can prove a sentence is not sensitive. The
guarantee is exactly "no recognized secret shape, no non-public repo link, no dead PR or ref, and no
more references than the gate verified". It is not "arbitrary-content safe". The skill contract
forbids putting secrets in those fields; the scan is a backstop, not a reader.

### Operator opt-ins widen the boundary, on purpose

- `CGC_GATE_PRIVATE_REPOS` names repos the gate may send although they are not public (`*` means any
  repo your `gh` can resolve). `CGC_GATE_ALLOW_GIST=1` (also `true`, `yes`, `on`) accepts gist links.
  Both are your policy, honoured only when set: unset, empty or any other value leaves them off.
  Setting either replaces the "public repos only" guarantee with your own declaration, and the gate
  cannot judge what is inside a private repo or a gist. The secret scan and the reference checks still
  run. An allowlist entry that `gh` cannot reach at all (timeout, outage) is refused as `unverified`;
  one that `gh` answers 404 for is admitted, because your ChatGPT connector may read it under another
  account.
- **A registered WebCodex workspace is a channel you opened.** `fire --workspace NAME --mention APP`
  lets a round carry no public code link, because the remote model reads local files through the
  app. The gate does not look inside the workspace, and the prompt is still only scanned for secret
  shapes. The exemption from the public-source rule is granted by the round's recorded spec, checked
  against `CGC_WORKSPACES` and `CGC_APPS` when the round is enqueued and again when it is sent. At that
  moment the config file, when there is one, is the registry: a key missing from it means nothing is
  registered, whatever the long-running daemon remembers from its start. A file that cannot be read
  holds the round for a retry (it is never granted from the daemon's memory), and a workspace the
  file does not list is held the same way instead of being rejected for good. A
  sentence in the prompt (`Source: local workspace ...`) exempts nothing, and a workspace you did not
  register is refused. `CGC_WORKSPACES`, `CGC_APPS` and the store are files your user can write, so a
  hostile process running as you can register a workspace: that is the same-user boundary above, not
  something the gate can add.

## Local data retention

The store (`$CGC_DATA_DIR/control.db`, default `~/.local/state/cgc/`) durably
keeps every rendered prompt and every answer, 0600, indefinitely — that is what
makes follow-ups and crash recovery work. Answers are also materialized under
`$CGC_STATE_DIR` (default `/tmp/cgc`, wiped on reboot). If a consult's content
should not persist on disk, delete the store (`rm ~/.local/state/cgc/control.db`
— this also deletes all thread history) or point `CGC_DATA_DIR` at an encrypted
volume. Nothing is ever uploaded anywhere except the prompt you sent to ChatGPT.

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
  substitute for the code. The two declared exceptions are a `--no-code` question and
  a registered `--workspace` round (see above); the workspace one is read from the
  stored round, never from the prompt text.
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
