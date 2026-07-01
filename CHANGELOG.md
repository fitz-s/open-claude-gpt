# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); this project uses date-based
releases until it stabilizes.

## [Unreleased]

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

### Changed
- All host-/account-specific values moved out of the scripts into the environment;
  the repo ships no hard-coded identity.
- `deliver` now resolves a commit's associated PR via `commits/<sha>/pulls` and
  leads with that public PR link even for `--ref <sha>` and the auto-detect
  gist-fallback — a pushed commit with a PR never falls back to a gist.
