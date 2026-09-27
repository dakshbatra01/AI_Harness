# Evaluator contract: `make setup` then `make run` (AI_API_KEY exported). `make test` needs no API key.
.PHONY: setup run tui test smoke eval clean help

SHELL := /bin/bash
PY ?= $(shell command -v python3 2>/dev/null || command -v python 2>/dev/null)
export PYTHONDONTWRITEBYTECODE := 1

help:
	@echo "make setup                         verify toolchain (stdlib-only: nothing to install)"
	@echo "make run                           terminal dashboard; headless when issue input is supplied"
	@echo "make tui                           full-screen terminal dashboard directly"
	@echo "make run REPO=path ISSUE_FILE=f    headless (also ISSUE='text', ISSUE_URL=..., TESTS='id1,id2', stdin, ARGS='...')"
	@echo "make test                          unit + offline end-to-end tests (no API key needed)"
	@echo "make eval TASKS=eval/tasks.json    run the harness over a task set and grade it"

setup:
	@test -n "$(PY)" || (echo "python3 (>=3.9) is required" && exit 1)
	@$(PY) -c 'import sys; assert sys.version_info >= (3, 9), "Python >= 3.9 required"'
	@command -v git >/dev/null || (echo "git is required" && exit 1)
	@command -v rg >/dev/null || echo "note: ripgrep not found - search falls back to git grep"
	@mkdir -p runs
	@$(PY) -m harness self-check

run:
	@test -n "$$AI_API_KEY" || (echo "AI_API_KEY is not set (export AI_API_KEY=...)" && exit 1)
	@REPO="$(REPO)" ISSUE="$(ISSUE)" ISSUE_FILE="$(ISSUE_FILE)" ISSUE_URL="$(ISSUE_URL)" TESTS="$(TESTS)" COMMIT="$(COMMIT)" $(PY) -m harness run $(ARGS)

tui:
	@$(PY) -m harness tui $(ARGS)

test:
	@set -o pipefail; $(PY) -m unittest discover -s tests -t . -v 2>&1 | tail -n 60
	@$(PY) -m harness smoke --quiet

smoke:
	@$(PY) -m harness smoke

eval:
	@$(PY) -m harness.eval --tasks $(or $(TASKS),eval/tasks.json) $(ARGS)

clean:
	rm -rf runs/ workspaces/ .cache/
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
