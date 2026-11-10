# Keepsake for iCloud. `make check` is the gate.
PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
export PYTHONPATH := src

.PHONY: help setup run test lint scrub diagram check ui-setup ui-test screenshots docker-build docker-smoke hooks

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

setup: ## create .venv (Python 3.11+) with the lint tool
	python3 -m venv .venv
	.venv/bin/pip install --quiet ruff==0.8.6

run: ## run natively on 127.0.0.1:8765 with ./data and no real folders (set KEEPSAKE_* to use real ones)
	KEEPSAKE_DATA=$${KEEPSAKE_DATA:-./data} $(PY) -m keepsake

test: ## unit and API tests (engine attack fixtures, service, REST API, scrub, docs truth)
	$(PY) -m unittest discover -s tests -t .

lint: ## ruff
	$(PY) -m ruff check src tests tools

scrub: ## fail on any private identifier in a tracked file
	$(PY) tools/scrub.py

diagram: ## fail if the architecture SVGs are stale
	$(PY) tools/gen_diagram.py --check

check: lint test scrub diagram ## the gate: lint, tests, scrub, diagram

hooks: ## arm the local pre-commit and commit-msg hooks
	./scripts/install-git-hooks.sh

ui-setup: ## install the headless-browser toolchain (playwright-core)
	cd tools/ui && npm install --no-audit --no-fund

ui-test: ## UI smoke test in headless Chromium; needs KEEPSAKE_CHROMIUM=<path to a Chromium binary>
	node tests/ui/smoke.mjs

screenshots: ## regenerate docs/assets/screenshots; needs KEEPSAKE_CHROMIUM
	node tools/ui/screenshots.mjs docs/assets/screenshots

docker-build: ## build the image as keepsake-for-icloud:local
	docker build -t keepsake-for-icloud:local .

docker-smoke: ## build a throwaway image, run it on a temp fixture, prove the safety behaviours, clean up
	./scripts/docker-smoke.sh
