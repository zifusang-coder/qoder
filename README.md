# 云AI新媒体运营系统

云AI新媒体运营系统（Phase 0–3：创作发布闭环 + 多租户 SaaS 基础）。设计见 [设计文档](docs/原生AI新媒体运营系统-开发文档.md)。

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

前端已接入对话页（非流式 `/api/chat`）、公众号账号管理、项目发布任务的检查/审核/驳回/取消/重试/定时重排、内容日历、项目内短视频/口播配音生成与媒体预览，以及发布数据导入与概览。停止 uvicorn 与 vite 后，运行 `make down` 清理容器（`pgdata` 卷会保留）。

### 定时发布与媒体任务 worker

定时发布和媒体生成都与 API 进程分离，需要 Redis 已由 Compose 启动：

```bash
make worker
```

等价于 `cd backend && uv run arq app.worker.WorkerSettings`。worker 每分钟扫描 `approved` 且到期的公众号任务并执行；执行前会先对账卡在 `publishing` 的陈旧任务。同时消费 `process_media_job` 队列，执行短视频/配音生成。worker 不在 docker-compose 中常驻；未启动 worker 时媒体任务会停留在 `queued`。

### 数据导入

`POST /api/analytics/import` 接收 UTF-8 CSV（上限 5MB）。必需列为 `content_id,platform,captured_at`，六项指标列可留空（按 0 处理）；额外列会被忽略。完整列如下：

```text
content_id,platform,captured_at,views,likes,comments,shares,saves,followers_delta
```

`captured_at` 必须为 `YYYY-MM-DD`；`platform` 必须与内容自身平台一致。同一内容、平台、日期重复导入会覆盖指标而不是新增行。

### 内容日历

`GET /api/calendar?start=<ISO>&end=<ISO>` 聚合指定时间范围内有定时时间或发布时间、且未取消的发布任务；`start`/`end` 必须带时区，范围最长 90 天。前端 `/calendar` 提供周一开始的月视图，已发布、待审核/已通过、失败任务分色显示。

### 对象存储

默认 `STORAGE_BACKEND=local`，产物写入 `OUTPUTS_DIR`。切换到 S3 兼容存储（AWS S3 / MinIO / 腾讯云 COS 的 S3 网关）时：

```bash
cd backend && uv sync --extra s3
```

```text
STORAGE_BACKEND=s3
STORAGE_S3_ENDPOINT=http://127.0.0.1:9000   # AWS S3 可留空
STORAGE_S3_BUCKET=newmedia
STORAGE_S3_ACCESS_KEY=...
STORAGE_S3_SECRET_KEY=...
STORAGE_S3_REGION=
STORAGE_S3_PREFIX=
STORAGE_S3_USE_SSL=false
STORAGE_S3_PATH_STYLE=true                  # MinIO 需要
```

项目文件仍通过相对路径暴露，下载接口为 `GET /api/projects/{project_id}/files/download?path=...`。对象存储凭据只从环境读取，不写入日志或接口响应。

### 画像记忆向量检索（pgvector）

`docker-compose.yml` 的数据库镜像为 `pgvector/pgvector:pg16`；`make migrate` 会自动启用 `vector` 扩展，并为 `profile_memories.embedding` 建立 1024 维 HNSW 余弦索引。

```text
EMBEDDING_MODEL=text-embedding-v3
```

启用后复用默认 OpenAI 兼容提供商的 `base_url` 与密钥调用 `POST {base_url}/embeddings`（固定 `dimensions=1024`，模型需支持该维度）。写入记忆时同步生成向量；对话、技能、一稿多改与复盘按查询文本做语义检索，调用失败时自动回退到权重排序。历史记忆可用 `make embed-backfill` 补齐；未设置 `EMBEDDING_MODEL` 时保持原有权重排序，不发起嵌入请求。

### 媒体生成（短视频 / 口播配音）

内置 `short-video` 与 `tts-voiceover` 两个媒体技能：前者由 LLM 生成分镜文案，用 Pillow 渲染卡片、ffmpeg 合成竖屏 MP4，可选 edge-tts 配音与 SRT 字幕；后者清洗口播文本后生成 MP3 与 SRT。渲染结果由项目媒体接口导入项目存储（`media/<run_id>/`），并创建 `video`/`audio` 类型的内容与版本。

前置依赖：

- `ffmpeg` 与 `ffprobe` 需在 `PATH` 中（本地已用 Homebrew 版本验证）。
- edge-tts 依赖通过媒体可选依赖安装：`make media-setup`（等价于 `cd backend && uv sync --extra media`）。
- edge-tts 需要访问微软在线语音服务；网络不可达时任务失败并返回可读错误，不会生成半成品内容。

接口：

```text
POST /api/projects/{project_id}/media/short-video        # 202，返回媒体任务
POST /api/projects/{project_id}/media/tts-voiceover      # 202，返回媒体任务
GET  /api/projects/{project_id}/media/jobs/{job_id}      # 轮询任务状态
POST /api/projects/{project_id}/media/jobs/{job_id}/retry
GET  /api/projects/{project_id}/media
GET  /api/projects/{project_id}/files/preview?path=media/<run_id>/video.mp4
```

生成请求会先写入 `media_jobs` 并入队，API 立即返回 `202`；任务在 worker 中执行，状态依次为 `queued` → `running` → `succeeded`/`failed`。前端每 2 秒轮询一次，成功后自动刷新媒体列表，失败时展示错误并提供重试按钮。队列不可用时接口返回 `503 queue_unavailable` 并把任务落为失败，便于稍后重试。

预览接口按扩展名白名单返回 `video/mp4`、`audio/mpeg`、图片或纯文本，响应带 `Content-Disposition: inline` 与 `X-Content-Type-Options: nosniff`。前端不能给 `<video>` 直接加 Bearer 头，因此媒体预览组件的流程是：先用带鉴权的 `fetchBlob` 取回二进制，再以 Object URL 播放；组件卸载时释放 URL。

生成请求字段：`topic`（短视频必填）或 `text`（配音必填）、`title`、`platform`、`duration_seconds`（8–180）、`voice`、`voice_name`、`theme`、`rate`、`instructions`。任务失败会记录结构化 `error_code`（如 `llm_rate_limited`、`llm_unavailable`），不会落库半成品。`tts-voiceover` 在 LLM 清洗失败时会降级为直接使用原始口播稿合成，音频元数据标记 `llm_fallback`，前端展示黄色提示。

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

## Phase 2 工程基础

- 对象存储抽象已完成：本地与 S3 兼容后端共用读写/列表接口，文件下载同时支持两种后端；已用真实 MinIO 验证上传、列表、下载与导出链路。
- 内容日历已完成：后端按时间范围聚合任务，前端提供月视图。
- pgvector 记忆检索已完成：数据库镜像切换到 `pgvector/pgvector:pg16`，新增 1024 维向量列与 HNSW 余弦索引；写入时生成向量，检索失败回退权重排序，并提供 `make embed-backfill` 回填命令。
- 媒体技能已完成：`short-video` 与 `tts-voiceover` 产出 MP4/MP3/SRT，项目媒体接口导入存储并生成内容版本，前端提供生成表单与鉴权预览；生成改为 arq 后台任务 + 轮询，失败可重试，配音在 LLM 限流时降级为原始稿合成。
- 真实 LLM（千问）与公众号草稿创建已在本地完成冒烟；真实短视频链路（千问分镜 + edge-tts + ffmpeg + 存储 + 预览）已通过验收。COS 等其他 S3 兼容存储尚未逐家联调，生产部署前需按环境复核。

## Phase 3 多租户 SaaS 基础

- **租户生命周期**：新增 `tenants` 表与平台管理员权限位；平台管理员可创建/改名/停用/启用租户，创建时同步开通 owner 账号并返回一次性临时密码。不提供公开注册与租户删除。
- **四级角色**：`viewer < editor < admin < owner`。viewer 只读；editor 可创作、跑技能、生成媒体、导入数据、导出与创建发布任务；admin 可管理发布账号、审核/执行/排期发布、管理模型设置；owner 可管理成员与租户信息。平台管理员与业务角色解耦，即使角色为 viewer 仍可管理租户。
- **成员管理**：owner 可创建成员、改角色、停用/启用；临时密码只返回一次，首次登录强制改密；最后一名 owner 不可被降级或停用，不能修改自己的角色或停用自己。
- **会话失效**：JWT 增加 `ver` 声明并与 `users.token_version` 比对；改密、改角色、停用用户、停用租户都会使旧 token 立即失效。
- **审计**：新增 `audit_logs` 表，记录登录、改密、成员、租户、发布账号、模型提供商、技能启停、发布与媒体事件；审计按租户隔离、只追加，`password`/`token`/`secret`/`api_key` 等字段写库前脱敏。
- **前端**：新增 `/change-password`、`/team`、`/audit`、`/tenants`；导航按角色过滤，只读角色的写入表单与按钮隐藏。

升级现有数据库执行 `make migrate`。迁移会插入默认租户、把原管理员回填为 `owner` 并删除 `users.is_admin`；首次启动时 `ensure_admin` 将首位管理员提升为平台管理员。所有旧 token 均失效，升级后需要重新登录。

新增接口：

```text
GET/POST         /api/members
PATCH/DELETE     /api/members/{id}
GET/POST         /api/tenants
PATCH            /api/tenants/{id}
GET              /api/audit-logs
POST             /api/auth/change-password
```

Phase 3 已知限制：一人一租户；无公开注册、租户删除与计费；审计不自动归档；审计 IP 只取直连地址（不信任 `X-Forwarded-For`）。

## Phase 1 已知限制

- 对话接口为非流式；`/api/skills/{id}/stream` 的流式预览不写审计、不做输出校验与后处理。
- 公众号账号的 `meta`（封面素材 ID、作者、评论设置）仅在创建授权时写入，之后只能重命名或更新 AppID/AppSecret；需要调整 `meta` 时请停用后重新授权。
- 停用账号会立即销毁加密凭据且不可恢复；已有发布任务仍保留，但再次执行会因账号不可用而失败。

## 约定

- 发布默认人工审核；LLM 不直接触发发布。
- 平台密钥仅加密存储，接口只返回掩码。
- 产物写入 `outputs/<project_id>/`（项目内容）或 `outputs/adhoc/<tenant_id>/<run>/`（技能与对话临时产物），不进版本库。
- 后处理脚本以独立子进程运行，初始工作目录设为技能目录，环境变量白名单、30 秒超时、无 shell；这不是 OS 级隔离，生产环境应迁移到容器执行（Phase 2）。
