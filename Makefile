.PHONY: venv test cov random parallel lint format typecheck check

# PYTHON auto-selects .venv/bin/python when a venv exists (created by the
# `venv` target below or manually), else falls back to plain `python3` -
# so `make check`/`make test`/... work whether or not the venv is active.
# SYSTEM_PYTHON is the interpreter used to CREATE the venv, kept separate
# so it never gets shadowed by an already-selected venv PYTHON. VENV is
# the venv directory, overridable (e.g. `make venv VENV=.venv-3.8`).
SYSTEM_PYTHON ?= python3
VENV ?= .venv
ifeq ($(wildcard $(VENV)/bin/python),)
PYTHON := $(SYSTEM_PYTHON)
else
PYTHON := $(VENV)/bin/python
endif

venv:
	$(SYSTEM_PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest tests/

cov:
	$(PYTHON) -m pytest tests/ --cov --cov-report=term-missing

random:
	$(PYTHON) -m pytest tests/ -p randomly

parallel:
	$(PYTHON) -m pytest tests/ -n auto

lint:
	$(PYTHON) -m ruff check lib tests

format:
	$(PYTHON) -m ruff format lib tests

typecheck:
	$(PYTHON) -m mypy

check: lint typecheck test
