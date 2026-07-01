# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses date-based
releases until it stabilizes.

## [Unreleased] — pre-0.1 draft (work in progress; not yet a release)

- Consult-driven hardening converged to SHIP (4 rounds): follow-up thread integrity
  (pathname-exact conv match + hostname-exact tab filter), legacy rid-guard, fence-aware
  sentinel parser across all 4 impls, submit/state/concurrency, loopback bind + doctor,
  public-source fail-closed. Remaining items are non-blocking (LOW/NIT).

### Renamed
- Project is now **Open Claude GPT** (`open-claude-gpt`). The `cgc` CLI and `CGC_*`
  environment prefix are unchanged.

### Changed
- Default model tier is now **`Pro`** (matches any Pro tier ChatGPT offers) instead
  of `Pro Extended`; override with `CGC_MODEL` / `--model`.

### Added
- Open-source packaging: `install.sh` / `uninstall.sh`, `bin/cgc` CLI dispatcher,
  and `cgc doctor` health check (deps, browser, debug port, login, scratch, skill
  files, config; `--deep` and `--json` modes).
- Full environment-based configuration — `CGC_PROJECT_URL`, `CGC_MODEL`,
  `CGC_PORT`, `CGC_PROFILE`, `CGC_CHROME`, `CGC_STATE_DIR` — with `.env.example`.
- Cross-platform browser auto-detection (Chrome/Chromium/Edge on macOS + Linux).
- Docs: install, configuration (incl. prompt customization), architecture,
  security model, troubleshooting.
- CI: parse + shell-syntax + doctor + no-personal-identifier checks.

- `CGC_AUTO_MODEL` toggle: auto-pick the model tier before every send and fail
  closed if it can't be selected (on by default), or turn it off to send on
  whatever the composer shows.
- More example walkthroughs: plan a feature, hard reasoning / proof, architecture
  decision, research-backed decision, stuck-bug second opinion, config recipes
  (plus `--output-file` spec stubs).

### Changed
- All host-/account-specific values moved out of the scripts into the environment;
  the repo ships no hard-coded identity.
- Repositioned around **using your ChatGPT Pro subscription with Claude Code** for
  **planning, hard reasoning, and review** — dropped the "frontier model / ~7× cost"
  framing in favor of the subscription + three-pillars story.
- `deliver` now resolves a commit's associated PR via `commits/<sha>/pulls` and
  leads with that public PR link even for `--ref <sha>` and the auto-detect
  gist-fallback — a pushed commit with a PR never falls back to a gist.
