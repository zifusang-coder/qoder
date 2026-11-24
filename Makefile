.PHONY: setup up down dev worker migrate test fmt embed-backfill media-setup \
	docker-build docker-up docker-down docker-logs

setup:
	@test -f .env || cp .env.example .env
	cd backend && uv sync

media-setup:
	@test -f .env || cp .env.example .env
	cd backend && uv sync --extra media

up: setup
	docker compose up -d --wait
down:
	docker compose down
dev: setup
	cd backend && uv run uvicorn app.main:app --reload --port 8000
worker: setup
	cd backend && uv run arq app.worker.WorkerSettings
migrate: setup
	cd backend && uv run alembic upgrade head
test: setup
	cd backend && uv run pytest -q
fmt: setup
	cd backend && uv run ruff check --fix .
embed-backfill: setup
	cd backend && uv run python -m app.scripts.backfill_embeddings

# 生产编排：需要 .env 中的 JWT_SECRET/MASTER_KEY/ADMIN_PASSWORD；down 不删数据卷
docker-build:
	docker compose --profile app build
docker-up:
	docker compose --profile app up -d --build --wait
docker-down:
	docker compose --profile app down
docker-logs:
	docker compose --profile app logs -f api worker
