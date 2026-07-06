.PHONY: venv test cov random parallel lint format typecheck check

# Assumes .venv may already be active; targets call plain pytest/ruff/mypy so
# they work the same whether invoked inside or outside the venv shell.
PYTHON ?= python3

venv:
	$(PYTHON) -m venv .venv
	.venv/bin/python -m pip install -r requirements-dev.txt

test:
	pytest tests/

cov:
	pytest tests/ --cov=lib --cov-report=term-missing

random:
	pytest tests/ -p randomly

parallel:
	pytest tests/ -n auto

lint:
	ruff check lib tests

format:
	ruff format lib tests

typecheck:
	mypy

check: lint typecheck test
