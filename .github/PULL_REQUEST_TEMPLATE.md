<!-- Thanks for contributing! Keep changes scoped; separate refactors from behavior changes. -->

## What & why

<!-- What does this change, and why? Link any issue. -->

## Checklist

- [ ] Scripts parse: `python3 -c "import ast,glob;[ast.parse(open(f).read()) for f in glob.glob('skill/scripts/*.py')]"`
- [ ] Shell valid: `for f in install.sh uninstall.sh bin/cgc skill/scripts/*.sh; do bash -n "$f"; done`
- [ ] Doctor passes: `CGC_HOME="$PWD/skill" bin/cgc doctor`
- [ ] Tests pass: `make test`
- [ ] No personal identity added (usernames, home paths, project ids — all via env)
- [ ] Sentinel contract (`BEGIN_RESPONSE`/`END_RESPONSE`) unchanged, or the change is explained
- [ ] Docs updated (README / docs / CHANGELOG) if behavior changed
