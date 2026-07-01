.PHONY: help install install-link uninstall doctor test lint check clean

help:            ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:         ## install the skill into ~/.claude/skills
	./install.sh

install-link:    ## symlink the skill (dev — edits go live)
	./install.sh --link

uninstall:       ## remove the installed skill
	./uninstall.sh

doctor:          ## run the health check against this repo
	CGC_HOME="$(PWD)/skill" python3 skill/scripts/cgc_doctor.py

test:            ## run offline smoke tests
	python3 tests/test_smoke.py

lint:            ## parse + shell-syntax + no-personal-identity checks
	@python3 -c "import ast,glob;[ast.parse(open(f).read()) for f in glob.glob('skill/scripts/*.py')];print('py parse ok')"
	@for f in install.sh uninstall.sh bin/cgc skill/scripts/*.sh; do bash -n $$f && echo "sh ok: $$f"; done
	@if grep -rniE "g-p-[0-9a-f]{20}|fitz-s" --include='*.py' --include='*.sh' --include='*.md' . ; then \
	  echo "personal identifier leaked"; exit 1; else echo "identity clean"; fi

check: lint test doctor  ## everything CI runs

clean:           ## remove scratch + pycache
	rm -rf "$${CGC_STATE_DIR:-/tmp/cgc}" skill/scripts/__pycache__ tests/__pycache__
