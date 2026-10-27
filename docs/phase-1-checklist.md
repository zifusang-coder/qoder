# Phase 1 验收清单

验收日期：2026-10-27。环境：macOS 本地；后端 Python 3.11（uv）；测试使用内存 SQLite。本清单只勾选本次自动化测试或既有已记录验证能够证明的项；依赖真实外部服务、浏览器或 Docker/Redis 的项统一列在文末，不冒充已验证。

## 功能

- [x] 画像 6 维字段可创建、读取、编辑、删除（验证方式：自动化 `tests/test_profiles.py`）
- [x] 画像记忆可手工添加，复盘可自动写入并标注来源（验证方式：自动化 `tests/test_profiles.py`、`tests/test_postmortem.py`、`tests/test_e2e.py`）
- [x] 内置技能可从 `skills/` 加载（当前 12 个目录），可列表、启停，停用后执行返回 409（验证方式：自动化 `tests/test_builtin_skills.py`、`tests/test_skills_api.py`；真实模型质量未验收）
- [x] 技能执行写入 `skill_runs` 审计记录（耗时、token、状态、错误）（验证方式：自动化 `tests/test_skill_executor.py`）
- [x] 项目可创建，内容可创建多个版本并按版本号回溯（验证方式：自动化 `tests/test_projects.py`、`tests/test_e2e.py`）
- [x] 上传文件限制 10MB，路径穿越与非法文件名被拒绝（验证方式：自动化 `tests/test_repurpose.py`、`tests/test_projects.py`）
- [x] 一稿多改复用/创建目标平台内容并生成独立版本（验证方式：自动化 `tests/test_repurpose.py`、`tests/test_e2e.py`）
- [x] 对话可按关键词路由到技能，未命中时进入通用问答（验证方式：自动化 `tests/test_chat.py`；真实模型回答未验收）
- [x] 发布前检查定位禁用词字符位置，人设禁区仅告警不阻断（验证方式：自动化 `tests/test_precheck.py`）
- [x] 非法状态跃迁返回 409 `invalid_transition`（验证方式：自动化 `tests/test_publish.py`）
- [x] 公众号发布经审核后创建草稿并记录 `media_id`（验证方式：自动化 `tests/test_wechat_publish.py`，使用 respx 模拟微信接口；真实公众号未验收）
- [x] 小红书/知乎/快手/视频号导出 zip，包含 `manifest.json`、`content.md`、`publish_guide.md`（验证方式：自动化 `tests/test_export.py`、`tests/test_e2e.py`）
- [x] 定时任务只扫描到期的已审核公众号任务，执行幂等且不重复发布（验证方式：自动化 `tests/test_worker.py`；真实 Redis + arq 未验收）
- [x] CSV 重复导入同一日期按键 upsert，不产生重复行（验证方式：自动化 `tests/test_analytics.py`、`tests/test_e2e.py`）
- [x] 复盘结果写入画像记忆并记录来源 `postmortem`（验证方式：自动化 `tests/test_postmortem.py`、`tests/test_e2e.py`）

## 安全与合规

- [x] LLM 提供商与平台密钥加密存储，接口只返回掩码（验证方式：自动化 `tests/test_settings_api.py`、`tests/test_wechat_publish.py`）
- [x] 发布必须经过 `awaiting_review → approved`，导出不绕过人工审核（验证方式：自动化 `tests/test_publish.py`、`tests/test_export.py`、`tests/test_e2e.py`）
- [x] 后处理脚本无 shell、环境变量白名单、30 秒超时（验证方式：自动化 `tests/test_script_runner.py`）
- [x] 受保护接口未认证访问返回 401（验证方式：自动化 `tests/test_auth.py`、`tests/test_profiles.py` 等）
- [x] 业务查询按 `tenant_id` 过滤，跨租户资源不可见（验证方式：自动化 `tests/test_profiles.py`、`tests/test_publish.py`、`tests/test_analytics.py`、`tests/test_postmortem.py`）
- [x] 发布边界符合合规要求：公众号仅建草稿，其他平台导出人工上传，不使用逆向接口或自动化点击（验证方式：代码与自动化导出/公众号测试，详见 README「发布边界」）

## 工程

- [x] `alembic upgrade head` / `downgrade base` 可往返（验证方式：Phase 0 已在本地 PostgreSQL 验证，见 `docs/phase-0-checklist.md`；本次未启动 Docker 复跑）
- [x] 后端 `uv run pytest -q` 全绿：499 passed（本次运行）
- [x] 后端 `uv run ruff check .` 通过（本次运行）
- [x] 前端 `npm run test` 与 `npm run build` 通过（验证方式：Task 1.19 已记录 25 passed、`tsc -b && vite build` 成功；本次未复跑前端）
- [ ] `README` 可在新机器 30 分钟内复现（见下方人工验收）

## 待人工验收（本环境无凭据/无浏览器）

以下项目本次无法执行，原因：本环境没有 LLM 与微信公众号凭据，没有可用的浏览器后端栈（PostgreSQL/Redis/uvicorn/vite 未启动），且任务明确禁止启动 Docker/PostgreSQL/Redis 或发起真实微信/LLM 调用。

- [ ] 浏览器完整流程：登录 → 画像 → 项目 → 发布 → 数据 → 复盘
- [ ] 真实 LLM 冒烟（使用真实提供商跑通至少一次技能执行与复盘）
- [ ] 真实公众号草稿（真实 AppID/Secret 获取 access_token 并创建草稿）
- [ ] 真实 Redis + arq 定时发布（`make worker` 下到点执行且不重复）
- [ ] 新机器按 README 从零启动，在 30 分钟内跑通一次 MVP 闭环
