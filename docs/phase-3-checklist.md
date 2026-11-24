# Phase 3 验收清单

验收日期：2026-11-11。环境：macOS 本地；自动化测试使用内存 SQLite；真实环境使用 Docker Compose 中的 PostgreSQL 16 + Redis 7、uvicorn、vite、arq worker，以及浏览器手工验收。

## 功能

- [x] 默认租户与平台管理员：迁移后存在 `tenants` 默认行、原管理员 `role=owner`；首次启动后 `ensure_admin` 授予首位平台管理员，`is_platform_admin=true`、`token_version=0`（验证：`tests/test_models_saas.py`、迁移后 SQL 查询 + 启动后 SQL 查询）
- [x] 登录 token 含 `tid`/`ver`；`/auth/me` 返回角色、租户、平台权限与强制改密标记（验证：`tests/test_auth_saas.py`）
- [x] 成员创建返回一次性临时密码，首次登录强制改密（验证：`tests/test_members.py`）
- [x] 角色矩阵：viewer 只读、editor 可写、admin 管理账号与发布审核、owner 管理成员（验证：`tests/test_permissions.py`）
- [x] 最后一名 owner 不可降级/停用；不能修改自己的角色或停用自己（验证：`tests/test_members.py`）
- [x] 平台管理员创建租户并同时创建 owner；slug/邮箱冲突返回 409（验证：`tests/test_tenants.py`）
- [x] 停用租户使该租户所有在线 token 失效，重新启用后可登录（验证：`tests/test_tenants.py`）
- [x] 停用租户的定时发布不被扫描；媒体任务立即失败为 `tenant_suspended`（验证：`tests/test_worker.py`）
- [x] 双租户隔离：跨租户读取/修改/删除返回 404，列表不泄漏（验证：`tests/test_isolation.py`）
- [x] 审计覆盖登录、改密、成员、租户、账号、设置、技能、发布、媒体事件；支持过滤与分页（验证：`tests/test_audit.py`、`tests/test_audit_events.py`）
- [x] 审计详情脱敏：password/token/secret/api_key/credential/authorization/cookie 字段被丢弃（验证：`tests/test_audit_service.py`、`tests/test_audit.py`）
- [x] 前端角色化导航与页面：团队（admin 读/owner 写）、审计（admin）、租户（平台管理员）、改密（强制跳转）（验证：`web/src/__tests__/*.test.ts` 与浏览器验收）
- [x] viewer 在业务页面看不到写入按钮（验证：浏览器验收）

## 安全与合规

- [x] 新增写接口使用角色依赖，未授权返回 403 `forbidden`（验证：`tests/test_permissions.py`）
- [x] 跨租户操作统一 404，不暴露资源存在性（验证：`tests/test_members.py`、`tests/test_isolation.py`）
- [x] `token_version` 在改密、改角色、停用用户、停用租户时递增，旧 token 返回 401 `token_revoked`（验证：`tests/test_auth_saas.py`、`tests/test_members.py`）
- [x] 审计只追加：无 PATCH/DELETE 接口；审计写入失败不阻断认证响应（验证：`tests/test_audit.py`、`tests/test_audit_service.py`）
- [x] 临时密码只返回一次，不写审计、不入日志（验证：代码审查 `app/api/members.py`、`app/api/tenants.py` 与 `tests/test_members.py`）
- [x] 平台接口只管理租户元数据，不读取其他租户业务数据（验证：`tests/test_tenants.py`、`tests/test_permissions.py`）

## 工程

- [x] `alembic upgrade head` 成功，当前 head `c3d4e5f6a7b8`（真实 PostgreSQL）
- [x] `cd backend && uv run ruff check .` 通过
- [x] `cd backend && uv run pytest -q` 全部通过
- [x] `npm --prefix web run test` 全部通过
- [x] `npm --prefix web run build` 通过
- [x] 验收结束后 `make down`，8000/5173/15432/16379 均无残留监听

## 真实环境验收（对应设计第 13 节）

1. [x] 平台管理员创建租户 B，返回 owner 临时密码；用该密码登录 B 的 owner，被强制改密。
2. [x] 租户 A 与 B 分别创建项目、内容、画像、发布任务；A 的用户访问 B 的资源全部 404，列表互不出现。
3. [x] 租户 A 的 owner 创建 editor 成员；editor 可创建内容与发布任务，访问发布审核/账号管理写入返回 403；admin 可审核但访问成员管理写入返回 403。
4. [x] 停用 editor 后，其旧 token 立即失效；重新登录返回 403 `account_disabled`。
5. [x] 平台管理员停用租户 B：B 的在线会话立即失效、登录返回 403 `tenant_suspended`、B 的定时发布不执行；重新启用后可登录。
6. [x] 修改密码后旧 token 失效，新 token 正常使用。
7. [x] 审计页面可查到登录、成员、发布、租户事件；任何审计 `detail` 中不出现密码、token 或平台凭据。
8. [x] 迁移成功；默认租户与现有管理员角色正确回填。

## 上线加固

- [x] 登录失败按邮箱与 IP 双维度限流，计数存 Redis 供多实例共享；超限返回 429 `too_many_attempts` 与 `Retry-After`；Redis 故障放行（验证：`tests/test_ratelimit.py`、真实 Redis 验收）
- [x] 首次登录未改密时，后端仅放行 `/auth/me` 与 `/auth/change-password`，其他业务接口返回 403 `password_change_required`（验证：`tests/test_password_gate.py`、真实 PostgreSQL 验收）
- [x] 成员变更前锁定租户行，避免并发降级同时通过最后 owner 校验（验证：`tests/test_members.py` 的 `FOR UPDATE` 编译断言、真实 PostgreSQL 并发验收）
- [x] 过期审计日志先按批归档为 NDJSON 到 `_audit_archive` 再删除；归档失败保留数据；每日 03:15 由 worker 执行（验证：`tests/test_audit_retention.py`、`tests/test_worker.py`、真实 PostgreSQL 验收）

## 本次结果（执行后填写）

- 后端测试：`cd backend && uv run pytest -q` → 696 passed, 2 skipped；`uv run ruff check .` → 通过。
- 前端测试：`npm --prefix web run test` → 14 个测试文件、127 个用例全部通过。
- 前端构建：`npm --prefix web run build`（`tsc -b && vite build`）→ 通过。
- 迁移：真实 PostgreSQL 16 上 `alembic upgrade head` 成功，当前 head `c3d4e5f6a7b8 (head)`；默认租户存在；`admin@local` 回填为 `owner`，`ensure_admin` 后 `is_platform_admin=t`、`token_version=0`；`audit_logs` 表与索引存在。
- 真实环境验收：第 1–8 项全部通过（API 脚本 + 浏览器）。补充记录：
  - 租户 B 通过 API、租户 C 通过浏览器创建；两者的一次性临时密码登录均被强制跳转 `/change-password`。
  - 双租户隔离：跨租户项目/内容访问返回 404，列表互不出现；viewer 仅见只读导航，业务页面的创建/导入/复盘/发布按钮均隐藏。
  - 角色矩阵：editor 可创建内容与发布任务，审核/账号/成员写入返回 403；admin 审核与账号写入成功，成员写入返回 403。
  - 会话失效：停用 editor 后旧 token 返回 403 `account_disabled`；重新启用后旧 token 返回 401 `token_revoked`；改密后旧 token 401，新 token 正常。
  - 租户停用：B 的登录返回 403 `tenant_suspended`；到期定时任务未被扫描（状态保持 `approved`，`publish.auto_execute` 计数为 0）；重新启用后可登录。
  - 审计：页面可查询 35 条事件，动作/状态/操作人/时间过滤正常；`tenant.create` 详情仅含 `slug` 与 `owner_email`，临时密码未出现在审计中。
  - 浏览器验收中发现登录成功后回跳 `/login` 的问题（`AuthProvider` 只在挂载时读取 token），已在 `web/src/pages/Login.tsx` 改为登录后整页跳转修复（提交 `ea08a16`）。
- 上线加固真实环境验收（PostgreSQL 16 + Redis 7 + uvicorn）：
  - 登录限流：同一未知邮箱连续 5 次错误密码返回 401，第 6 次返回 429 `too_many_attempts`，`Retry-After=63`；Redis 中存在邮箱与 IP 两类计数键。
  - 强制改密：临时密码登录后 `/api/projects` 返回 403 `password_change_required`，`/auth/me` 仍可访问；改密后新 token 访问 `/api/projects` 返回 200。
  - owner 并发：两名 owner 同时降级对方，结果为一个 200、一个 409 `last_owner`，成员列表仍保留 1 名 owner。
  - 审计归档：插入 200 天前的日志后执行归档，生成 `_audit_archive/<日期>/<id>.ndjson`，过期行被删除、近期行保留；验收文件已清理。
