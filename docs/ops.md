# 运维与配置手册

本文件承接 README 中移出的运行、配置与验收细节。仓库设计背景见
[原生AI新媒体运营系统-开发文档.md](原生AI新媒体运营系统-开发文档.md)，平台能力见
[platforms.md](platforms.md)。

## 1. 本地开发

### 1.1 前置要求

- Docker 与 Docker Compose v2（`docker compose` 需支持 `up --wait`），且 Docker daemon 已启动。
- GNU Make（macOS 需 Xcode Command Line Tools，`xcode-select --install`）：下文所有命令均通过 `make` 调用。
- `uv`：Makefile 通过 `uv sync` / `uv run` 安装后端依赖；uv 会自行获取兼容的 Python 3.11+ 解释器。`requires-python` 为 `>=3.11`，仓库不提供 `.python-version` 固定版本。
- Node.js `^20.19.0 || >=22.12.0`（Vite 8 的引擎要求）与 npm。
- PostgreSQL / Redis 由 Compose 提供（宿主机端口 15432 / 16379），后端与前端在宿主机运行（8000 / 5173）：这四个端口需空闲。

### 1.2 启动

```bash
make setup       # 复制 .env（若不存在）并安装后端依赖
make up          # PostgreSQL + Redis（等待 db/redis 健康检查通过）
make migrate     # alembic upgrade head
make dev         # uvicorn app.main:app --reload --port 8000
```

另开终端：

```bash
cd web && npm install && npm run dev
```

打开 http://localhost:5173 。本地开发默认账号取自 `.env`，模板默认
`admin@local` / `admin12345`。这些默认值仅限本地开发：`ENV` 非 `dev` 时应用会拒绝占位符
`JWT_SECRET` / `MASTER_KEY` 并启动失败，默认管理员密码也必须修改。

`make down` 停止容器并保留 `pgdata` 卷。

### 1.3 worker

定时发布、媒体生成、Agent/工作流/团队运行都与 API 进程分离，需要 Redis 已启动：

```bash
make worker
```

等价于 `cd backend && uv run arq app.worker.WorkerSettings`。worker 负责：

- 每分钟扫描 `approved` 且到期的发布任务并执行（公众号图文、抖音视频、头条视频）；执行前对账卡在 `publishing` 的陈旧任务。
- 消费 `process_media_job`，执行短视频/配音生成。
- 消费 `run_agent_job` / `run_workflow_job`，执行 Agent、协作团队与工作流运行。
- 每分钟对账心跳超时的运行；每天 03:15 归档过期审计日志。

未启动 worker 时，媒体、Agent、工作流与定时发布任务会停留在 `queued`。生产编排中 worker
是独立容器（见第 9 节）。

## 2. 数据导入与内容日历

`POST /api/analytics/import` 接收 UTF-8 CSV（上限 5MB）。必需列为
`content_id,platform,captured_at`，六项指标列可留空（按 0 处理）；额外列会被忽略：

```text
content_id,platform,captured_at,views,likes,comments,shares,saves,followers_delta
```

`captured_at` 必须为 `YYYY-MM-DD`；`platform` 必须与内容自身平台一致。同一内容、平台、日期重复导入会覆盖指标而不是新增行。

`GET /api/calendar?start=<ISO>&end=<ISO>` 聚合指定时间范围内有定时时间或发布时间、且未取消的发布任务；`start`/`end` 必须带时区，范围最长 90 天。前端 `/calendar` 提供周一开始的月视图，已发布、待审核/已通过、失败任务分色显示。

## 3. 对象存储

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

## 4. 画像记忆向量检索（pgvector）

`docker-compose.yml` 的数据库镜像为 `pgvector/pgvector:pg16`；`make migrate` 会自动启用 `vector` 扩展，并为 `profile_memories.embedding` 建立 1024 维 HNSW 余弦索引。

```text
EMBEDDING_MODEL=text-embedding-v3
```

启用后复用默认 OpenAI 兼容提供商的 `base_url` 与密钥调用 `POST {base_url}/embeddings`（固定 `dimensions=1024`，模型需支持该维度）。写入记忆时同步生成向量；对话、技能、一稿多改与复盘按查询文本做语义检索，调用失败时自动回退到权重排序。历史记忆可用 `make embed-backfill` 补齐；未设置 `EMBEDDING_MODEL` 时保持原有权重排序，不发起嵌入请求。

## 5. 媒体生成（短视频 / 口播配音）

内置 `short-video` 与 `tts-voiceover` 两个媒体技能：前者由 LLM 生成分镜文案，用 Pillow 渲染卡片、ffmpeg 合成竖屏 MP4，可选 edge-tts 配音与 SRT 字幕；后者清洗口播文本后生成 MP3 与 SRT。渲染结果由项目媒体接口导入项目存储（`media/<run_id>/`），并创建 `video`/`audio` 类型的内容与版本。

前置依赖：

- `ffmpeg` 与 `ffprobe` 需在 `PATH` 中（本地已用 Homebrew 版本验证；镜像内已安装）。
- edge-tts 依赖通过媒体可选依赖安装：`make media-setup`（等价于 `cd backend && uv sync --extra media`）。
- edge-tts 需要访问微软在线语音服务；网络不可达时任务失败并返回可读错误，不会生成半成品内容。

接口：

```text
POST /api/projects/{project_id}/media/short-video        # 202，返回媒体任务
POST /api/projects/{project_id}/media/tts-voiceover      # 202，返回媒体任务
GET  /api/projects/{project_id}/media/jobs/{job_id}      # 查询任务状态
GET  /api/projects/{project_id}/media/jobs/{job_id}/stream   # SSE 快照，终态关闭
POST /api/projects/{project_id}/media/jobs/{job_id}/retry
GET  /api/projects/{project_id}/media
GET  /api/projects/{project_id}/files/preview?path=media/<run_id>/video.mp4
```

生成请求先写入 `media_jobs` 并入队，API 立即返回 `202`；任务在 worker 中执行，状态依次为
`queued` → `running` → `succeeded`/`failed`。前端订阅 SSE 快照而非轮询，成功后自动刷新媒体列表，
失败时展示错误并提供重试按钮。队列不可用时接口返回 `503 queue_unavailable` 并把任务落为失败，便于稍后重试。

预览接口按扩展名白名单返回 `video/mp4`、`audio/mpeg`、图片或纯文本，响应带 `Content-Disposition: inline` 与 `X-Content-Type-Options: nosniff`。前端不能给 `<video>` 直接加 Bearer 头，因此媒体预览组件的流程是：先用带鉴权的 `fetchBlob` 取回二进制，再以 Object URL 播放；组件卸载时释放 URL。

生成请求字段：`topic`（短视频必填）或 `text`（配音必填）、`title`、`platform`、`duration_seconds`（8–180）、`voice`、`voice_name`、`theme`、`rate`、`instructions`。任务失败会记录结构化 `error_code`（如 `llm_rate_limited`、`llm_unavailable`），不会落库半成品。`tts-voiceover` 在 LLM 清洗失败时会降级为直接使用原始口播稿合成，音频元数据标记 `llm_fallback`，前端展示黄色提示。

## 6. AI 助理与 Agent 运行时（第 1 层）

- **入口**：前端 `/assistant`；后端 `/api/agent`。editor 及以上可发起并查看任务；viewer 仅能调用只读接口（前端入口对 editor 及以上可见）。
- **运行方式**：`POST /api/agent/runs` 返回 `202` 并入队 arq 的 `run_agent_job`；运行状态为 `queued → running → succeeded / failed / cancelled`，命中审批时为 `waiting_approval`。
- **流式**：对话 `POST /api/chat/stream` 按 token 流式输出并以 `[DONE]` 结束；Agent 运行另有 `/api/agent/runs/{id}/stream` 快照流。
- **预算**：请求可传 `max_steps` / `max_tokens` / `max_tool_calls` / `max_seconds`，服务端分别夹紧到 `AGENT_MAX_STEPS_CEILING`（默认 30）/ 200000 / 40 / 900；默认值见 `.env.example`。
- **工具**：只读工具与低风险写工具默认可自动执行；`publish.create_draft_job` 只创建 `status=draft`、`scheduled_at=None` 的任务，不触发真实发布；本期不注册任何高风险工具。
- **审批**：租户可把任意工具收紧为 `confirm` 或 `deny`（`PUT /api/agent/policy`），不能把默认确认的工具放宽。命中 `confirm` 时运行挂起，由发起人之外的管理员在 `/assistant` 或 `POST /api/agent/approvals/{id}/decide` 处理；超过 `AGENT_APPROVAL_TTL_HOURS` 自动过期并使运行失败。
- **恢复**：worker 每分钟对账心跳超时的运行，未超过 2 次尝试则重新入队；步骤与写工具以 `(run_id, step_index)` 幂等，重启不产生重复产物。
- **配额**：同一用户最多 2 个活跃运行；租户当日 token 超过 `AGENT_DAILY_TOKEN_QUOTA` 返回 `429 agent_daily_quota_exceeded`；队列不可用返回 `503 queue_unavailable`。

真实模型验收（前置条件：可连接的开发 PostgreSQL，且该租户已配置默认 LLM 提供商；缺失时用例报错而非跳过）：

```bash
cd backend && RUN_AGENT_E2E=1 uv run pytest tests/test_agent_e2e.py -q
```

## 7. 工作流（第 2 层）

- **入口**：`/workflows` 列表与内置模板；`/workflows/:id` 画布编辑器；`/workflows/:id/runs/:runId` 运行详情。admin/owner 可编辑与发布，editor 可运行已发布版本，viewer 只读。
- **节点**：输入、工具、Agent、条件、审核、输出六类；DAG 支持并行分支与汇合，条件节点用 `true`/`false` 两条出边，汇合节点在全部活跃分支结束后执行一次。
- **版本**：草稿自动保存（`draft_revision` 乐观锁），发布生成不可变 `workflow_versions`；编辑草稿不影响已发布版本与历史运行，可把任意历史版本恢复为草稿。
- **运行**：`POST /api/workflows/{id}/runs` 返回 `202` 并入队 arq 的 `run_workflow_job`；按 `WORKFLOW_TIME_SLICE_SECONDS` 分片推进，崩溃由心跳对账续跑，失败可从失败处续跑（`resume_count < 10`）。
- **观测**：`GET /api/workflow-runs/{id}/stream` 推送运行快照，终态自动关闭；前端不再轮询。
- **审批与发布边界**：命中租户策略 `confirm` 的工具或审核节点挂起为 `waiting_approval`，由非发起人的 admin/owner 决定；工作流只调用注册表里已有的工具，真实发布与排期仍无工具，只创建 `status=draft` 的发布任务。
- **定时**：`POST /api/workflows/{id}/schedules` 支持每日/每周（星期 1=周一 … 7=周日）+ 本地时间与时区；`(schedule_id, scheduled_for)` 唯一约束保证到点只触发一次，工作流已有活跃运行时跳过并写 `workflow.schedule.skipped` 审计。
- **配置**：6 个 `WORKFLOW_*` 见 `.env.example`。

真实模型验收：

```bash
cd backend && RUN_WORKFLOW_E2E=1 uv run pytest tests/test_workflow_e2e.py -q
```

## 8. 协作团队（第 3 层）

- **五角色**：Planner（Lead，决策与委派）、Scout（调研）、Creator（创作，必选）、Critic（审稿）、Publisher（交付）。角色是「系统提示词 + 工具子集 + 预算」的组合，复用第 1 层运行时 `run_agent` 串行执行，不引入第三方框架或新执行内核。租户角色配置只能收窄工具、调低预算，放宽返回 `422 policy_not_loosenable`；`planner`/`creator` 不可禁用。
- **黑板**：`agent_team_entries` 追加式共享上下文，编排器在每步结束后代表 Lead 追加条目（`goal/delegation/result/critic/final`），入库前脱敏与截断，运行时注入最近 `AGENT_TEAM_BLACKBOARD_INJECT`（默认 12）条。
- **Critic 返工**：Creator 交付后 Critic 强制审稿，不通过强制返工，上限 `AGENT_TEAM_REWORK_LIMIT`（默认 2）轮；超限落 `critic_rejected` 并保留草稿与问题清单。循环检测、预算耗尽（`team_budget_exhausted`）、角色失败（`role_failed`）等收敛为失败时，黑板与已产产物保留现场。
- **团队预算**：默认 120000 tokens / 40 工具调用 / 12 轮 / 900 秒；发起请求可上调至 `AGENT_TEAM_TOKEN_CEILING`（默认 200000）/ `AGENT_TEAM_SECONDS_CEILING`（默认 1800），其余 `AGENT_TEAM_*` 配置见 `.env.example`。运维注记：前端团队预算上限（`web/src/agentTeam.ts` TEAM_CEILINGS）为默认值常量，租户收窄与实际上限以服务端校验为准。
- **审批唤醒**：子运行命中 `confirm` 时父团队同步挂起为 `waiting_approval`，由发起人之外的 admin/owner 经 `POST /api/agent/approvals/{id}/decide` 批准后唤醒父团队续跑；驳回落 `approval_rejected`，超时过期失败。团队详情页审批卡带「团队 · 角色」标签，助理页审批卡展示团队上下文与详情链接。
- **双入口**：AI 助理页 `/assistant` 的「协作团队」页签直接发起，详情页 `/assistant/teams/:id` 提供角色时间线、黑板与产物；工作流画布新增 `team` 节点与内置「内容团队」模板（输入 → team → 输出），发布后可手动或定时运行，审批挂起与失败重试均从断点续跑。
- **观测**：`GET /api/agent/team/runs/{id}/stream` 推送团队快照，终态自动关闭。
- **权限矩阵**：admin/owner 可改角色配置、发起、审批（非发起人）与取消任意团队运行；editor 可发起、取消本人运行并查看配置与运行；viewer 只读，无发起/审批/取消入口。所有查询按租户过滤，跨租户返回 404。
- **发布边界**：注册表没有真发布、排期或导出工具，Publisher 只能调用 `publish.create_draft_job`，强制 `status=draft`、`scheduled_at=None`——真发布与排期结构上不可达。

真实模型验收：

```bash
cd backend && RUN_TEAM_E2E=1 uv run pytest tests/test_team_e2e.py -q
```

## 9. 生产部署

`docker-compose.yml` 中 `app` profile 提供 api / worker / migrate / web 四个服务，db 与 redis 复用开发编排（独立端口与卷不变）。

```bash
# 首次或配置变更后
make docker-build
make docker-up      # 自动执行迁移，等待全部健康检查通过
make docker-logs    # 跟踪 api / worker 日志
make docker-down    # 停止容器，保留 pgdata 与 outputs 卷
```

前置：`.env` 中的 `JWT_SECRET`、`MASTER_KEY`、`ADMIN_PASSWORD` 必须是真实值（占位符会让应用启动失败）；`ENV=production` 已在编排中设置。可选变量：

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `API_WORKERS` | uvicorn worker 进程数 | 2 |
| `METRICS_TOKEN` | `/metrics` 访问令牌，留空则端点 404 | 空 |
| `ALERT_WEBHOOK_URL` | 运行失败告警 Webhook | 空 |

编排要点：

- `migrate` 一次性执行 `alembic upgrade head`，`api`/`worker` 等它成功后再启动；`api` 健康检查通过后 `web` 才启动。
- api 只绑定 `127.0.0.1:8010`，web 绑定 `127.0.0.1:8080`；对外入口统一走 web 的 nginx。本机抓取指标用 `http://127.0.0.1:8010/metrics`。
- nginx 反代 `/api/` 时关闭 `proxy_buffering`、读超时 3600s，对话与运行 SSE 才能实时下发；同时生成 `X-Request-ID`。
- api 与 worker 共享名为 `outputs` 的卷（容器内 `/data/outputs`），本地对象存储的产物与导出对两侧都可见。
- 后端镜像以非 root 用户运行，内置 ffmpeg 与 edge-tts；前端镜像为多阶段构建 + nginx 静态托管。

排障：若 `make docker-build` 报 buildx 会话错误（如 `x-docker-expose-session-sharedkey` 含非打印字符），属本机 Docker Desktop builder 状态问题而非镜像缺陷，可重启 Docker Desktop，或临时用经典构建器逐镜像构建：`DOCKER_BUILDKIT=0 docker build -t newmedia-api:verify -f backend/Dockerfile .`（web 同理，上下文为 `web/`）。

## 10. 可观测性

- **请求 ID**：入口读取 `X-Request-ID`（缺失则生成），响应头回显同一值；日志格式 `时间 级别 [request_id] logger: 消息`，访问日志只包含方法、路由模板、状态码与耗时，不含查询串。
- **指标**：`GET /metrics` 输出 Prometheus 文本格式，含 `newmedia_http_requests_total{method,path,status}` 与 `newmedia_run_failures_total{kind,code}`；`path` 为路由模板，基数有界。启用时设置 `METRICS_TOKEN`，请求带 `Authorization: Bearer <token>` 或 `X-Metrics-Token`。
- **失败告警**：每次运行终态失败（Agent/团队/工作流/媒体）累加计数；配置 `ALERT_WEBHOOK_URL` 后尽力投递 JSON（3 秒超时，失败只记日志）。告警体只含应用名、事件、类型、资源 ID、租户 ID 与错误码，不含提示词、消息正文或凭据。
- **健康检查**：`GET /health`；compose 中 api 用该端点，worker 用 `arq --check`，web 用 nginx `location = /healthz`。

## 11. 测试与验收

后端测试使用内存 SQLite 与 respx 模拟外部接口，前端测试与构建同样不依赖 Docker：

```bash
make test                   # 后端：uv run pytest -q
npm --prefix web run test   # 前端单测（vitest run）
npm --prefix web run build  # 前端类型检查 + 构建（tsc -b && vite build）
```

首次运行前安装依赖：`make setup`（后端）与 `npm --prefix web install`（前端）。只有本地联调与人工验收需要 `make up` 启动 PostgreSQL 和 Redis。

端到端冒烟（Playwright）覆盖登录、创建画像、创建项目、助手会话与工作流/AI 助理入口：

```bash
npm --prefix web run test:e2e
```

该命令自动在 8001/5321 端口拉起独立后端与前端：后端使用 `newmedia_e2e` 数据库（本机自动通过 `newmedia-db-1` 创建，CI 复用服务容器），不会污染开发数据。首次运行需 `npx --prefix web playwright install chromium`。

真实模型用例默认跳过：`RUN_AGENT_E2E=1`、`RUN_WORKFLOW_E2E=1`、`RUN_TEAM_E2E=1` 分别启用 Agent、工作流与团队验收。

## 12. Phase 2 / Phase 3 记录

### Phase 2 工程基础

- 对象存储抽象已完成：本地与 S3 兼容后端共用读写/列表接口，文件下载同时支持两种后端；已用真实 MinIO 验证上传、列表、下载与导出链路。
- 内容日历已完成：后端按时间范围聚合任务，前端提供月视图。
- pgvector 记忆检索已完成：数据库镜像切换到 `pgvector/pgvector:pg16`，新增 1024 维向量列与 HNSW 余弦索引；写入时生成向量，检索失败回退权重排序，并提供 `make embed-backfill` 回填命令。
- 媒体技能已完成：`short-video` 与 `tts-voiceover` 产出 MP4/MP3/SRT，项目媒体接口导入存储并生成内容版本，前端提供生成表单与鉴权预览；生成改为 arq 后台任务 + SSE 订阅，失败可重试，配音在 LLM 限流时降级为原始稿合成。
- 真实 LLM（千问）与公众号草稿创建已在本地完成冒烟；真实短视频链路（千问分镜 + edge-tts + ffmpeg + 存储 + 预览）已通过验收。COS 等其他 S3 兼容存储尚未逐家联调，生产部署前需按环境复核。

### Phase 3 多租户 SaaS 基础

- **租户生命周期**：新增 `tenants` 表与平台管理员权限位；平台管理员可创建/改名/停用/启用租户，创建时同步开通 owner 账号并返回一次性临时密码。不提供公开注册与租户删除。
- **四级角色**：`viewer < editor < admin < owner`。viewer 只读；editor 可创作、跑技能、生成媒体、导入数据、导出与创建发布任务；admin 可管理发布账号、审核/执行/排期发布、管理模型设置；owner 可管理成员与租户信息。平台管理员与业务角色解耦，即使角色为 viewer 仍可管理租户。
- **成员管理**：owner 可创建成员、改角色、停用/启用；临时密码只返回一次，首次登录强制改密；最后一名 owner 不可被降级或停用，不能修改自己的角色或停用自己。
- **会话失效**：JWT 增加 `ver` 声明并与 `users.token_version` 比对；改密、改角色、停用用户、停用租户都会使旧 token 立即失效。
- **审计**：新增 `audit_logs` 表，记录登录、改密、成员、租户、发布账号、模型提供商、技能启停、发布与媒体事件；审计按租户隔离、只追加，`password`/`token`/`secret`/`api_key` 等字段写库前脱敏。
- **登录限流**：同一邮箱与同一 IP 双维度计数，默认 5 次 / 20 次每 5 分钟；计数存放在 Redis，多实例共享，超限返回 `429 too_many_attempts` 与 `Retry-After`；Redis 不可用时放行登录，只记录错误日志。
- **强制首次改密**：`must_change_password` 为真时，除 `/api/auth/me` 与 `/api/auth/change-password` 外的业务接口统一返回 `403 password_change_required`，不再依赖前端拦截。
- **并发保护**：成员变更在统计 owner 前锁定租户行（`SELECT ... FOR UPDATE`），避免两个并发降级同时通过“最后一名 owner”校验。
- **审计归档**：worker 每天 03:15 把超过 `AUDIT_RETENTION_DAYS`（默认 180 天，`<=0` 关闭）的审计日志按批导出为 NDJSON 到对象存储 `_audit_archive` 命名空间后再删除；先归档后删除，失败保留数据等待次日重试。
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

Phase 3 已知限制：一人一租户；无公开注册、租户删除与计费；审计 IP 只取直连地址（不信任 `X-Forwarded-For`）；登录限流依赖 Redis，单机部署下 Redis 故障期间限流暂时失效。

## 13. 已知限制与约定

- 发布默认人工审核；LLM 不直接触发发布。
- 平台密钥仅加密存储，接口只返回掩码。
- 公众号账号的 `meta`（封面素材 ID、作者、评论设置）仅在创建授权时写入，之后只能重命名或更新 AppID/AppSecret；需要调整 `meta` 时请停用后重新授权。
- 停用账号会立即销毁加密凭据且不可恢复；已有发布任务仍保留，但再次执行会因账号不可用而失败。
- 产物写入 `outputs/<project_id>/`（项目内容）或 `outputs/adhoc/<tenant_id>/<run>/`（技能与对话临时产物），不进版本库。
- 后处理脚本以独立子进程运行，初始工作目录设为技能目录，环境变量白名单、默认 30 秒超时、无 shell；这不是 OS 级隔离，多租户生产环境应把执行放到容器或沙箱（见 [skills/README.md](../skills/README.md) 的治理清单）。
- 流式预览接口 `/api/skills/{id}/stream` 不写审计、不做输出校验与后处理；需要完整审计时使用非流式接口。
