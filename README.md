# 新媒体运营系统

原生 AI 新媒体运营系统（Phase 0/1 MVP）。设计见 [设计文档](docs/原生AI新媒体运营系统-开发文档.md)。

## 本地启动

### 前置要求

- Docker 与 Docker Compose v2（`docker compose` 需支持 `up --wait`），且 Docker daemon 已启动。
- GNU Make（macOS 需 Xcode Command Line Tools，`xcode-select --install`）：下文所有命令均通过 `make` 调用。
- `uv`：Makefile 通过 `uv sync` / `uv run` 安装后端依赖；uv 会自行获取兼容的 Python 3.11+ 解释器（本次验证使用 3.11.15）。`requires-python` 为 `>=3.11`，仓库不提供 `.python-version` 固定版本。
- Node.js `^20.19.0 || >=22.12.0`（Vite 8 的引擎要求）与 npm。
- PostgreSQL / Redis 由 Compose 提供（宿主机端口 15432 / 16379），后端与前端在宿主机运行（8000 / 5173）：这四个端口需空闲。

```bash
make setup       # 复制 .env（若不存在）并安装后端依赖
make up          # PostgreSQL + Redis（等待容器就绪，db 健康检查通过）
make migrate     # alembic upgrade head
make dev         # uvicorn app.main:app --reload --port 8000
```

另开终端：

```bash
cd web && npm install && npm run dev
```

打开 http://localhost:5173 ，使用本地开发默认账号登录：凭据取自已存在的 `.env`，若不存在则由 `make setup` 从 `.env.example` 复制，模板默认 `admin@local` / `admin12345`。这些默认值仅限本地开发：`ENV` 非 `dev` 时应用会拒绝占位符 `JWT_SECRET` / `MASTER_KEY` 并启动失败，默认管理员密码也必须修改。

Phase 0 的对话页为占位壳，尚无 `/api/chat` 接口。停止 uvicorn 与 vite 后，运行 `make down` 清理容器（`pgdata` 卷会保留）。

## 测试

前置：先安装前端依赖（`npm --prefix web install`）并启动 Docker 服务（`make up`），再运行下列测试与构建命令。

```bash
make test                   # 后端：uv run pytest -q
npm --prefix web run test   # 前端单测（vitest run）
npm --prefix web run build  # 前端类型检查 + 构建（tsc -b && vite build）
```

## 约定

- 发布默认人工审核；LLM 不直接触发发布。
- 平台密钥仅加密存储，接口只返回掩码。
- 产物写入 `outputs/<project_id>/`，不进版本库。
- 后处理脚本以独立子进程运行，工作目录限定为技能目录，环境变量白名单、30 秒超时、无 shell；这不是 OS 级隔离，生产环境应迁移到容器执行（Phase 2）。
