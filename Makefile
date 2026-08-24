.PHONY: install test lint api worker web compose production-preflight production-deploy production-status production-backup release

install:
	uv sync --frozen --extra dev
	cd frontend && npm ci

test:
	uv run pytest

lint:
	uv run ruff check backend scripts
	cd frontend && npm run typecheck

api:
	uv run uvicorn app.main:app --app-dir backend --reload

worker:
	PYTHONPATH=backend uv run python -m app.worker --once

web:
	cd frontend && npm run dev

compose:
	docker compose up --build

production-preflight:
	python3 scripts/production-preflight.py --env-file .env.production --require-docker --check-dns

production-deploy:
	./scripts/deploy-production.sh .env.production

production-status:
	./scripts/production-status.sh .env.production

production-backup:
	./scripts/production-backup.sh .env.production

release:
	./scripts/build-release.sh
