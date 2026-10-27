.PHONY: up down dev migrate test fmt

up:
	docker compose up -d
down:
	docker compose down
dev:
	cd backend && uv run uvicorn app.main:app --reload --port 8000
migrate:
	cd backend && uv run alembic upgrade head
test:
	cd backend && uv run pytest -q
fmt:
	cd backend && uv run ruff check --fix . || true
