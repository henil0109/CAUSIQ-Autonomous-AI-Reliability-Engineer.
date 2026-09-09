# Causiq developer commands (macOS / Linux / Git Bash with make installed).
#
# Windows without make: use ./tasks.ps1 <target>, which runs the same commands.
#
# The `check` target is exactly what CI runs, so a green local run means a green
# CI run. Every target below works offline with no ANTHROPIC_API_KEY set (I6).
#
# `uv run --no-sync` is deliberate: the primary working copy lives in a
# OneDrive-synced folder, where uv's reinstall step intermittently fails on a
# locked dist-info directory. `make setup` performs the one sync that matters.

UV := uv run --no-sync

.PHONY: help setup lint format type test test-live cov check schemas clean

help:
	@echo "setup      - create the virtualenv and install dependencies"
	@echo "lint       - ruff check + format check"
	@echo "format     - ruff format (writes)"
	@echo "type       - mypy --strict"
	@echo "test       - offline test suite (no API key, no network)"
	@echo "cov        - offline suite with both coverage gates"
	@echo "test-live  - live-API suite (requires ANTHROPIC_API_KEY)"
	@echo "check      - lint + type + cov  (the CI gate)"
	@echo "schemas    - regenerate golden JSON schemas (intended changes only)"

setup:
	uv sync --extra dev

lint:
	$(UV) ruff check src tests scripts
	$(UV) ruff format --check src tests scripts

format:
	$(UV) ruff format src tests scripts

type:
	$(UV) mypy

test:
	$(UV) pytest -m "not live"

# Two gates, per Engineering Contract 10:
#   1. >= 85% across src/causiq
#   2. 100% on the invariant-critical modules (domain, evidence, authz, tools)
cov:
	$(UV) pytest -m "not live" --cov --cov-report=term-missing --cov-fail-under=85
	$(UV) pytest -m "not live" -q --cov=src/causiq/domain --cov=src/causiq/evidence \
		--cov=src/causiq/authz --cov=src/causiq/tools \
		--cov-report=term-missing --cov-fail-under=100

test-live:
	$(UV) pytest -m live

check: lint type cov

schemas:
	$(UV) python scripts/regenerate_golden_schemas.py

clean:
	rm -rf .mypy_cache .ruff_cache .coverage htmlcov var
