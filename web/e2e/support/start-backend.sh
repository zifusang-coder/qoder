#!/usr/bin/env bash
# 冒烟测试专用后端：独立数据库与端口，避免污染开发数据。
set -euo pipefail
cd "$(dirname "$0")/../../../backend"

API_PORT="${E2E_API_PORT:-8001}"
PG_PORT="${E2E_PG_PORT:-15432}"
export DATABASE_URL="${E2E_DATABASE_URL:-postgresql+asyncpg://app:app@127.0.0.1:${PG_PORT}/newmedia_e2e}"
export REDIS_URL="${E2E_REDIS_URL:-redis://127.0.0.1:${E2E_REDIS_PORT:-16379}/0}"

# 幂等创建 e2e 数据库：CI 直接复用服务容器里的 newmedia 库（E2E_CREATE_DB=0），
# 本机默认建独立库，通常只能通过项目自带的 db 容器。
if [ "${E2E_CREATE_DB:-1}" = "1" ]; then
  if command -v psql >/dev/null 2>&1; then
    PGPASSWORD=app psql -h 127.0.0.1 -p "$PG_PORT" -U app -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='newmedia_e2e'" | grep -q 1 || \
    PGPASSWORD=app psql -h 127.0.0.1 -p "$PG_PORT" -U app -d postgres \
      -c 'CREATE DATABASE newmedia_e2e'
  elif command -v docker >/dev/null 2>&1; then
    docker exec newmedia-db-1 psql -U app -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='newmedia_e2e'" | grep -q 1 || \
    docker exec newmedia-db-1 psql -U app -d postgres \
      -c 'CREATE DATABASE newmedia_e2e'
  fi
fi

uv run alembic upgrade head
exec uv run uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT"
