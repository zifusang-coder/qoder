.PHONY: setup up down dev migrate test fmt

setup:
	@test -f .env || cp .env.example .env
	cd backend && uv sync

up: setup
	docker compose up -d --wait
down:
	docker compose down
dev: setup
	cd backend && uv run uvicorn app.main:app --reload --port 8000
migrate: setup
	cd backend && uv run alembic upgrade head
test: setup
	cd backend && uv run pytest -q
fmt: setup
	cd backend && uv run ruff check --fix . || true
