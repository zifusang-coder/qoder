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

前端对话页仍为占位壳；后端已提供 `/api/chat` 编排接口（关键词路由到技能，未命中时通用问答）。停止 uvicorn 与 vite 后，运行 `make down` 清理容器（`pgdata` 卷会保留）。

### 定时发布 worker

定时发布与 API 进程分离，需要 Redis 已由 Compose 启动：

```bash
make worker
```

等价于 `cd backend && uv run arq app.worker.WorkerSettings`。worker 每分钟扫描 `approved` 且到期的公众号任务并执行；执行前会先对账卡在 `publishing` 的陈旧任务，不在 docker-compose 中常驻。

### 数据导入

`POST /api/analytics/import` 接收 UTF-8 CSV（上限 5MB）。必需列为 `content_id,platform,captured_at`，六项指标列可留空（按 0 处理）；额外列会被忽略。完整列如下：

```text
content_id,platform,captured_at,views,likes,comments,shares,saves,followers_delta
```

`captured_at` 必须为 `YYYY-MM-DD`；`platform` 必须与内容自身平台一致。同一内容、平台、日期重复导入会覆盖指标而不是新增行。

### 发布边界

- 公众号：仅通过官方接口创建草稿，群发需人工在公众号后台完成。
- 小红书/知乎/快手/视频号：导出 zip（manifest、正文、发布指南），由人工上传。
- 不使用任何逆向接口或自动化点击。

## 测试

后端测试使用内存 SQLite 与 respx 模拟外部接口，前端测试与构建同样不依赖 Docker；运行下列命令无需启动 PostgreSQL/Redis：

```bash
make test                   # 后端：uv run pytest -q
npm --prefix web run test   # 前端单测（vitest run）
npm --prefix web run build  # 前端类型检查 + 构建（tsc -b && vite build）
```

首次运行前安装依赖：`make setup`（后端）与 `npm --prefix web install`（前端）。只有本地联调与人工验收需要 `make up` 启动 PostgreSQL 和 Redis。

## Phase 1 已知限制

- 技能 `/api/skills/{id}/stream` 仅用于低延迟预览：不写 `skill_runs` 审计、不做输出校验与后处理、不统计 token；需要审计时调用 `/api/skills/{id}/run`。
- 公众号草稿需要先通过 `/api/accounts` 完成授权并传入 `account_id`；前端尚未提供账号管理与选择界面。
- 发布任务的驳回、取消、重试与定时重排目前仅提供后端 API，前端只覆盖创建、审核通过与导出/执行。

## 约定

- 发布默认人工审核；LLM 不直接触发发布。
- 平台密钥仅加密存储，接口只返回掩码。
- 产物写入 `outputs/<project_id>/`（项目内容）或 `outputs/adhoc/<tenant_id>/<run>/`（技能与对话临时产物），不进版本库。
- 后处理脚本以独立子进程运行，初始工作目录设为技能目录，环境变量白名单、30 秒超时、无 shell；这不是 OS 级隔离，生产环境应迁移到容器执行（Phase 2）。
