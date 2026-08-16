.PHONY: install test lint api worker web compose

install:
	uv sync --extra dev
	cd frontend && npm install

test:
	uv run pytest

lint:
	uv run ruff check backend
	cd frontend && npm run typecheck

api:
	uv run uvicorn app.main:app --app-dir backend --reload

worker:
	PYTHONPATH=backend uv run python -m app.worker --once

web:
	cd frontend && npm run dev

compose:
	docker compose up --build
