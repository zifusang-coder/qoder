# 云AI新媒体运营系统 变更日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的结构，版本号在进入正式发布流程后按语义化版本管理。

## [Unreleased]

### 新增

- **对象存储抽象**：本地与 S3 兼容后端（AWS S3 / MinIO / COS S3 网关）共用读写、列表接口；新增项目文件下载接口；S3 依赖通过 `uv sync --extra s3` 安装。
- **内容日历**：`GET /api/calendar` 按时间范围聚合未取消的定时/已发布任务；前端新增周一开始的月视图与导航入口。
- **pgvector 记忆检索**：`profile_memories` 新增 1024 维向量列与 HNSW 余弦索引；数据库镜像切换为 `pgvector/pgvector:pg16`；记忆写入时生成向量，对话/技能/改写/复盘按查询文本语义检索并回退权重排序；新增 `make embed-backfill`。
- **媒体技能**：新增 `short-video`（LLM 分镜 → Pillow 卡片 → ffmpeg 竖屏 MP4，可选 edge-tts 配音与 SRT）与 `tts-voiceover`（口播文本清洗 → MP3 + SRT）；共享 `media_common.py` / `tts_common.py`；后处理超时可配置；新增 `make media-setup` 安装 `media` 可选依赖。
- **项目媒体 API**：`POST /projects/{id}/media/{skill_id}` 生成并导入产物到 `media/<run_id>/`，创建 `video`/`audio` 内容与版本；`GET /projects/{id}/media` 聚合媒体产物；`GET /projects/{id}/files/preview` 按 MIME 白名单内联预览并带 `nosniff`。
- **异步媒体任务**：新增 `media_jobs` 表与迁移；媒体生成改为 `202` 入队，由 arq worker 的 `process_media_job` 执行；新增任务轮询与失败重试接口，队列不可用时返回 `503 queue_unavailable` 并将任务落为失败。
- **TTS 降级**：LLM 清洗失败（限流/不可用/超时等 `llm_*` 错误）时，`tts-voiceover` 直接使用原始口播稿合成，并在技能审计与音频元数据中记录 `llm_fallback`。
- **前端媒体体验**：项目详情新增短视频/口播配音生成表单、媒体产物列表、鉴权 blob 预览（video/audio/image/text）、字幕与下载入口；任务每 2 秒轮询，失败可重试，降级配音展示提示；新增媒体工具函数单测。
- **多租户 SaaS 基础**：新增 `tenants` 表与平台管理员权限位，支持租户创建、改名、停用/启用；创建租户时同步开通 owner 并返回一次性临时密码。
- **四级角色与鉴权**：新增 `viewer/editor/admin/owner` 角色层级与全接口依赖；JWT 增加 `ver`，改密、改角色、停用用户或租户时递增 `token_version` 使旧 token 立即失效。
- **成员管理**：新增成员创建、列表、改角色/显示名、停用/启用接口；临时密码仅返回一次，首次登录强制改密；保护最后一名 owner 与自我操作。
- **安全审计**：新增 `audit_logs` 表与 `/api/audit-logs` 查询接口，覆盖认证、成员、租户、账号、设置、技能、发布与媒体关键事件；审计只追加，敏感字段写库前脱敏。
- **前端 SaaS 页面**：新增改密、团队、审计、租户页面与角色化导航；只读角色隐藏写入控件。
- **多平台发布账号**：新增平台能力注册表，账号支持 9 个平台与同平台多账号。
- **账号 API 动态平台**：支持动态凭据字段、平台过滤、能力矩阵与按平台校验 meta。
- **发布适配器扩展**：适配器接收内容上下文与存储后端；新增抖音、头条视频发布，百家号占位。
- **按能力分流**：创建任务、手动执行与定时 worker 按「平台 + 内容类型」能力分流。
- **前端能力展示**：账号页动态渲染凭据表单，发布页与项目详情按能力显示操作。
- **待接入提示**：头条图文与百家号发布仍需官方接口文档，当前返回明确配置错误。
- **清爽主题**：全局改为白灰底、自然绿主色与酒红点缀；新增 `paper/surface/ink/sage/wine/warm` 设计令牌与 `card/input/btn-*` 组件类，移除旧的深色 slate/indigo 配色。
- **登录限流**：按邮箱与 IP 双维度固定窗口计数，默认 5 次 / 20 次每 5 分钟；计数存入 Redis 供多实例共享，超限返回 `429 too_many_attempts` 与 `Retry-After`；Redis 故障时放行并记录错误日志。
- **强制首次改密**：`must_change_password` 为真时，除 `/api/auth/me` 与 `/api/auth/change-password` 外的业务接口由后端返回 `403 password_change_required`。
- **最后 owner 并发保护**：成员变更统计 owner 前锁定租户行，避免并发降级绕过最后一名 owner 校验。
- **审计归档与保留**：worker 每天 03:15 把超过 `AUDIT_RETENTION_DAYS`（默认 180 天，`<=0` 关闭）的审计日志按批导出为 NDJSON 到对象存储 `_audit_archive` 后再删除；归档失败保留数据等待重试。
- **Agent 运行时**：新增 `agent_runs`/`agent_steps`/`agent_approvals`/`agent_policies` 四张表与迁移；新增 `app/agent/` 包（工具契约、注册表、策略、脱敏、运行时）与首批只读/低风险工具，基于 arq 执行，支持预算、循环检测、心跳对账、崩溃续跑与 `(run_id, step_index)` 幂等。
- **人工确认**：命中确认策略的工具挂起为 `waiting_approval`，由发起人之外的管理员审批后继续；支持驳回、过期与撤销，真实发布仍不提供工具。
- **Agent API 与页面**：新增 `/api/agent` 的运行、取消、重试、审批与策略接口；前端新增「AI 助理」页面，展示步骤时间线、预算进度与产物链接，活跃运行每 2 秒轮询。
- **LLM 工具调用**：网关新增 provider 无关的 `complete_tool_calls`，不改动既有 `complete`/`stream` 行为。
- **工作流引擎**：新增 `workflows`/`workflow_versions`/`workflow_schedules`/`workflow_runs`/`workflow_node_runs`/`workflow_approvals` 六张表与迁移，`agent_runs` 增加 `workflow_run_id`/`workflow_node_id`；新增 `app/workflow/` 包（图解析、发布校验、变量解析、引擎、审批、生命周期、定时与内置模板），支持并行分支、条件跳过、节点重试、时间片推进、崩溃续跑与从失败处续跑。
- **工作流 API**：新增 `/api/workflows` 的定义、版本、发布、恢复与模板实例化接口，`/api/workflows/{id}/schedules` 定时接口，`/api/workflows/{id}/runs` 与 `/api/workflow-runs/{id}` 运行/取消/续跑接口，`/api/workflow-approvals` 审批接口，以及供编辑器使用的 `GET /api/agent/tools`；草稿保存用 `revision` 乐观锁，发布失败返回 `422 workflow_invalid` 与 `detail.issues`。
- **工作流 worker**：新增 `run_workflow_job`、`fire_due_workflow_schedules`、`reconcile_workflow_runs` 三个任务与 cron；定时先 CAS 推进 `next_run_at` 再建运行，活跃运行跳过；第 1 层子运行审批接入父运行，父子不会并发执行。
- **工作流画布**：前端新增 React Flow 画布编辑器（节点面板、属性面板、连线校验、条件真假句柄、撤销/重做、草稿保存与发布校验抽屉）、工作流列表与模板实例化、运行详情页（节点状态、审批、产物、取消/续跑，活跃运行每 2 秒轮询）。
- **分级自治**：工作流复用第 1 层策略矩阵，租户只能收紧不能放宽；审核节点与 `confirm` 工具统一挂起审批，真实发布在工作流里结构上不可达。

### 验证

- 后端：`uv run pytest -q` 696 passed、2 skipped，`uv run ruff check .` 通过。
- 前端：`npm test` 127 passed，`npm run build` 通过。
- 上线加固真实环境验收：登录限流第 6 次返回 429 与 `Retry-After`；临时密码登录先 403 `password_change_required`、改密后放行；两名 owner 并发降级得到一个 200、一个 409 `last_owner`；过期审计日志归档为 NDJSON 后删除、近期日志保留。
- 真实 MinIO 联调：上传、列表、下载与导出 zip 均通过，对象键含项目隔离前缀。
- 真实千问 embedding：`text-embedding-v3` 返回 1024 维向量；pgvector 语义排序与回填已在本机 PostgreSQL 验证。
- 真实短视频链路：千问分镜 → edge-tts 配音 → ffmpeg 合成 → 媒体 API 导入 → 鉴权预览全部通过；产物为 1080×1920 H.264 + AAC，时长 25.968s。
- 媒体脚本测试：真实 ffmpeg 渲染与 `RUN_MEDIA_TTS=1` 的真实 edge-tts 合成均通过。
- 真实 PostgreSQL：迁移到 `c3d4e5f6a7b8`，默认租户与管理员回填正确。
- 真实环境验收：租户创建与强制改密、双租户隔离、角色矩阵、停用会话失效、租户停用、审计脱敏全部通过。
- 主题：前端 `npm test` 与 `npm run build` 通过；桌面与移动宽度下逐页视觉验收通过，无旧色板残留。
- Agent 运行时：`uv run pytest -q` 933 passed、3 skipped，`uv run ruff check .` 通过；`npm test` 156 passed、`npm run build` 通过。`RUN_AGENT_E2E=1` 真实模型用例通过：测试先种一条既有内容，模型为它调用 `content.create_version` 追加一版，再用 `publish.create_draft_job` 建草稿（项目由 `project.create` 另建）；共 7 步，产出项目、内容版本与发布草稿三类产物（草稿为 `draft` 且未排期），耗时 14.06s，远低于 10 分钟上限。
- 工作流：`uv run pytest -q` 1109 passed、4 skipped，`uv run ruff check .` 通过；`npm test` 181 passed、`npm run build` 通过。`RUN_WORKFLOW_E2E=1` 真实模型用例在「输入主题 → Agent → 输出」图上通过（1 passed，15.59s），产出项目、内容版本与 `draft` 发布任务（未排期）；浏览器验收走通创建 → 编辑 → 保存 → 发布 → 运行 → 审核通过 → 查看产物。

### 已知限制

- COS 等其他 S3 兼容存储尚未逐家联调；生产部署前需按环境复核。
- 语义检索依赖默认 OpenAI 兼容提供商同时提供 `/embeddings`；不满足时自动回退权重排序。
- 媒体生成依赖宿主机 `ffmpeg`/`ffprobe` 与 edge-tts 在线服务；edge-tts 不可达时任务失败，不产出半成品。
- 媒体任务依赖 `make worker` 消费队列；只启动 API 时任务会停留在 `queued`。
- `tts-voiceover` 的 LLM 文本清洗依赖提供商配额；千问触发 `limit_requests` 时会自动降级为原始口播稿合成，并在产物中标记 `llm_fallback`。
- 媒体后处理超时默认 300s（短视频）/180s（配音）；长时长视频仍建议人工抽检首尾帧与字幕对齐。
- 一人一租户，跨租户成员与租户切换未实现。
- 无公开注册、租户删除与订阅计费。
- 审计日志不自动归档；部署在代理后时审计 IP 仅记录直连地址。

## [0.1.0] - 2026-10-27

Phase 0/1 MVP：创作、发布、归因最小闭环。对应提交范围 `02f8e61..938a461`（分支 `phase-0-1-mvp`）。

### 新增

- **工程基础**：FastAPI + SQLAlchemy 2.0 async + Alembic 后端；React 19 + Vite 8 + Tailwind CSS v4 前端；PostgreSQL 16 / Redis 7 本地编排与 Makefile 开发命令；`uv` 依赖管理。
- **认证与租户**：JWT 登录、argon2 密码哈希、默认管理员种子；所有业务查询按 `tenant_id` 隔离。
- **LLM 能力**：提供商配置（密钥 Fernet 加密、响应仅返回掩码、单租户唯一默认）、按 fast/standard/premium 分层的网关与 SSE 流式输出。
- **技能体系**：manifest 加载/校验/注册表；执行器写入 `skill_runs` 审计（状态、耗时、token、错误）；后处理脚本无 shell、环境变量白名单、30 秒超时并按进程组回收；12 个内置技能（选题、大纲、标题、正文、改写、封面、质量门、风险扫描、人设校验、复盘等）。
- **画像**：六维画像 CRUD、手工记忆、画像上下文片段注入与标记转义。
- **项目与内容**：项目、内容、多版本文档；安全文件存储与 10MB 上传限制；跨平台一稿多改。
- **对话编排**：关键词确定性路由到技能，未命中时进入通用问答；临时产物写入按租户隔离的 `outputs/adhoc/<tenant_id>/<run>/`。
- **发布**：发布任务状态机（审核通过前禁止执行）、幂等创建、尝试次数上限、发布前检查（禁用词定位、人设禁区仅告警）、公众号草稿适配器（账号凭据加密）、小红书/知乎/快手/视频号 zip 导出（manifest、正文、发布指南）。
- **定时发布**：arq worker 扫描到期任务、陈旧 `publishing` 对账、批次上限、锁内二次校验发布时间。
- **归因**：CSV 指标导入（按内容/平台/日期 upsert）与项目概览；复盘生成报告并回写画像记忆（并发去重）。
- **前端**：登录、设置、对话壳、画像、项目、项目详情、发布、数据概览页面；前端 API 客户端与 SSE 解析。
- **验收**：端到端 MVP 测试与 `docs/phase-1-checklist.md` 验收清单。

### 安全与合规

- 发布默认人工审核，LLM 不直接触发发布；不使用逆向接口或自动化点击，公众号仅创建草稿。
- 平台/模型密钥加密存储，接口不返回明文或密钥前缀；日志不记录完整带凭据 URL。
- Docker Compose 的 PostgreSQL/Redis 端口仅绑定 `127.0.0.1`。
- 未预期异常统一返回通用 500 错误，响应不泄露堆栈或内部路径。
- 技能产物路径在接口响应中转为 `outputs` 相对路径；`/skills/{id}/stream` 明确为不做审计与后处理的预览接口。

### 修复

- 加固发布状态机并发、导出 lease、worker 竞态与调度归一化。
- 修复画像记忆重复写入、复盘期间画像被删除导致的写入失败。
- 修复技能后处理超时后孙进程残留、SSE 结构化错误解析、密钥掩码前缀泄露。
- 修复上传文件名路径穿越、租户越权访问与参数校验边界问题。

### 变更

- `Makefile fmt` 不再吞掉 lint 失败。
- 测试与构建不再依赖 Docker；仅本地联调需要 PostgreSQL/Redis。
- README 补充测试前置、产物目录、发布边界与 Phase 1 已知限制。

### 验证

- 后端：`uv run pytest -q` 506 passed，`uv run ruff check .` 通过。
- 前端：`npm run test` 26 passed，`npm run build` 通过。

### 已知限制

- 前端暂未提供公众号账号管理与选择、驳回/取消/重试/定时重排界面，相关操作仅提供 API。
- 真实 LLM 冒烟、真实公众号草稿、真实 Redis + arq、PostgreSQL 迁移往返、浏览器端到端与干净机器复现仍需人工验收，详见 `docs/phase-1-checklist.md`。
