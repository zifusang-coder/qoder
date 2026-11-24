# 构建上下文是仓库根目录：镜像需要 backend/ 与 skills/
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=120 \
    PATH="/app/.venv/bin:$PATH" \
    SKILLS_DIR=/skills \
    OUTPUTS_DIR=/data/outputs

# ffmpeg 是短视频/配音技能的后处理依赖；非 root 用户写 /data/outputs
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data/outputs \
    && chown -R appuser:appuser /data/outputs

WORKDIR /app

# 先只装依赖，代码变更不会使依赖层失效
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --extra media --no-install-project

COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./
COPY skills /skills

RUN uv sync --frozen --no-dev --extra media \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
