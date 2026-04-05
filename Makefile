.PHONY: dev test lint format migrate revision frontend-dev frontend-build install

PYTHON ?= python3
VENV ?= .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python
UVICORN := $(VENV)/bin/uvicorn
ALEMBIC := $(VENV)/bin/alembic
RUFF := $(VENV)/bin/ruff
MYPY := $(VENV)/bin/mypy
PYTEST := $(VENV)/bin/pytest

$(VENV)/bin/activate:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

install: $(VENV)/bin/activate
	$(PIP) install -e ".[dev]"

dev:
	$(UVICORN) app.main:app --reload --host 0.0.0.0 --port 8000 --app-dir backend

test:
	$(PYTEST)

lint:
	$(RUFF) check backend
	$(MYPY)

format:
	$(RUFF) format backend
	$(RUFF) check --fix backend

migrate:
	cd backend && ../$(ALEMBIC) upgrade head

revision:
	cd backend && ../$(ALEMBIC) revision --autogenerate -m "$(m)"

frontend-dev:
	cd frontend && npm run dev

frontend-build:
	cd frontend && npm run build
