# Contributing

Thanks for helping improve `open-claude-gpt`.

## Ground rules

- **No personal identity in the repo.** Anything host- or account-specific goes
  through an environment variable (see `docs/CONFIGURATION.md`), never hard-coded.
  A ChatGPT project URL, port, model tier, path, or example repo must be generic
  (`owner/repo`, `$CGC_PROJECT_URL`, …).
- **Preserve the sentinel contract.** `BEGIN_RESPONSE:{rid}` / `END_RESPONSE:{rid}`
  in the prompt templates is load-bearing — the waiter detects completion by those
  bare lines. Don't reformat them.
- **Keep it fail-closed.** Don't relax the no-code-source or model-mismatch guards
  without an explicit opt-in flag.

## Dev setup

```bash
git clone https://github.com/fitz-s/open-claude-gpt
cd open-claude-gpt
./install.sh --link      # symlink so edits go live
pip install websocket-client
```

## Before opening a PR

```bash
# 1. scripts must parse
python3 -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('skill/scripts/*.py')]"

# 2. shell must be syntactically valid
for f in install.sh uninstall.sh bin/cgc skill/scripts/*.sh; do bash -n "$f"; done

# 3. doctor must pass against the repo
CGC_HOME="$PWD/skill" bin/cgc doctor

# 4. no personal identifiers leaked in (usernames, home paths, project ids) —
#    CI runs this scan; see .github/workflows/ci.yml for the exact pattern.
```

CI runs the same checks (`.github/workflows/ci.yml`).

## Style

- Match the surrounding code — terse comments that explain *why*, not *what*.
- Scripts carry the provenance header (`Created` / `Last audited` / `Authority basis`).
- Keep changes scoped; separate refactors from behavior changes.

## Reporting bugs

Include your OS, Chrome version, `bin/cgc doctor --json` output, and the exact
`CGC_ERROR …` line if any. Before posting, redact local usernames, home paths,
ChatGPT project URLs, conversation IDs, and any repo/customer names you don't
want public; never paste secrets or `.env` contents.
