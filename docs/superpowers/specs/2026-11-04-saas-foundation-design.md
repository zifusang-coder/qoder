# SaaS 基础（Phase 3）设计

**Goal:** 在现有 `tenant_id` 隔离之上补齐租户管理、四级角色权限、租户内成员管理与安全审计，使系统具备可运营的多租户基础。

**Architecture:** 新增 `tenants` 与 `audit_logs` 两张表，扩展 `users` 的角色与会话字段；认证层在 JWT 中绑定 `tenant_id` 与 `token_version`，所有请求实时校验用户、租户与会话状态；写接口按最小角色依赖鉴权，关键变更同事务写入审计；平台管理员通过独立权限位管理租户生命周期。

**Tech Stack:** FastAPI、SQLAlchemy 2.0 async、Alembic、PostgreSQL 16、arq、Pydantic v2、React 19 + TanStack Query、vitest。

**Status:** 已与用户确认，等待规格评审。

---

## 1. 背景

当前系统：

- 所有业务表带 `tenant_id`，查询均按租户过滤，但不存在 `tenants` 表，租户只是字符串。
- `users` 带 `tenant_id`，`email` 全局唯一；登录只凭邮箱与密码，JWT 载荷为 `sub`（邮箱）+ `tid`（租户）。
- 权限只有 `is_admin` 布尔值，且没有任何接口使用它。
- 审计只有 `skill_runs`（技能执行审计），没有登录、成员、凭据与发布操作审计。
- 前端没有团队、审计或租户管理入口。

Phase 3 需要让"多租户"从数据结构约定变成可管理、可审计、可验证的产品能力。

## 2. 范围

### 2.1 本期实现

1. `tenants` 表与平台级租户管理（创建、列表、改名、停用/启用）。
2. 四级固定角色 `owner > admin > editor > viewer` 与接口级鉴权。
3. 租户内成员管理（创建、改角色、改显示名、停用/启用）。
4. 密码修改与首次登录强制改密。
5. 关键事件审计（写入、查询、脱敏）。
6. 双租户隔离自动化测试与真实环境验收。
7. 前端团队、审计、租户、改密页面与角色化导航。

### 2.2 本期不做

- 订阅计费、套餐、账单、用量报表。
- 跨租户成员（一人属于多个租户）与租户切换。
- 邮件发送、邮件邀请、找回密码。
- SSO、MFA、OAuth。
- PostgreSQL 行级安全（RLS）、租户级数据库隔离。
- 租户配额（成员数、存储、调用次数上限）。
- 审计日志自动归档或清理策略。

### 2.3 明确的产品决策

| 决策 | 取值 | 理由 |
|---|---|---|
| 用户与租户关系 | 一人一租户 | 保持 `User.tenant_id` 与全局唯一邮箱，现有接口的租户来源不变；跨租户成员留待后续演进 |
| 平台管理员 | `users.is_platform_admin` | 与业务角色解耦；只有平台管理员能创建/停用租户 |
| 公开注册 | 不提供 | 租户由平台管理员开通，避免开放注册带来的滥用与计费前置依赖 |
| 成员开通方式 | owner 创建 + 一次性临时密码 | 当前无邮件服务；临时密码仅返回一次，首次登录强制改密 |
| 权限粒度 | 固定角色层级 | 不引入权限点清单，降低配置与测试成本；后续需要时再细化 |
| 审计范围 | 安全与业务关键事件 | 不记录普通读取，控制数据量与噪声 |
| 计费 | 延后 | 本期只做"可运营的多租户"，计费依赖套餐与支付渠道设计 |

## 3. 数据模型

### 3.1 `tenants`（新增）

| 列 | 类型 | 约束 |
|---|---|---|
| `id` | `String(36)` | 主键，等于业务表的 `tenant_id` |
| `name` | `String(100)` | 非空 |
| `slug` | `String(50)` | 非空、唯一，`^[a-z0-9][a-z0-9-]{1,49}$` |
| `status` | `String(16)` | 非空，默认 `active`；取值 `active` / `suspended` |
| `created_at` | `DateTime(timezone=True)` | 非空，服务端默认当前时间 |
| `updated_at` | `DateTime(timezone=True)` | 非空，更新时刷新 |

`tenants` 不继承 `TenantScoped`（它本身就是租户）。状态用字符串而非数据库枚举，便于后续增加 `pending` 等状态。

### 3.2 `users`（修改）

| 变更 | 说明 |
|---|---|
| 新增 `role` | `String(16)`，非空，默认 `editor`；取值 `owner` / `admin` / `editor` / `viewer` |
| 新增 `is_active` | `Boolean`，非空，默认 `true`；停用后无法登录，已有 token 失效 |
| 新增 `must_change_password` | `Boolean`，非空，默认 `false`；为 `true` 时前端强制跳转改密页 |
| 新增 `is_platform_admin` | `Boolean`，非空，默认 `false`；可创建/停用租户 |
| 新增 `token_version` | `Integer`，非空，默认 `0`；改密、改角色、停用、租户停用时递增 |
| 删除 `is_admin` | 角色取代布尔值；迁移时按旧值回填 |
| 保留 | `email` 全局唯一、`tenant_id`、`password_hash`、`display_name`、时间戳 |

角色使用字符串列，在 API 层用 `Literal` 校验；不增加数据库 `CHECK` 约束，保持与现有迁移风格一致。

### 3.3 `audit_logs`（新增）

| 列 | 类型 | 约束 |
|---|---|---|
| `id` | `Uuid` | 主键 |
| `tenant_id` | `String(36)` | 非空，索引 |
| `actor_id` | `String(36)` | 可空；系统事件为空 |
| `actor_email` | `String(255)` | 非空；系统事件为 `scheduler` 等固定标识 |
| `action` | `String(64)` | 非空，点号命名，如 `member.create` |
| `resource_type` | `String(32)` | 非空，默认空串 |
| `resource_id` | `String(64)` | 非空，默认空串 |
| `status` | `String(16)` | 非空，默认 `success`；取值 `success` / `failure` |
| `ip` | `String(64)` | 非空，默认空串 |
| `user_agent` | `String(300)` | 非空，默认空串 |
| `detail` | `JSONType` | 非空，默认 `{}`；只存已脱敏的非敏感字段 |
| `created_at` | `DateTime(timezone=True)` | 非空，服务端默认当前时间 |

索引：

- `ix_audit_logs_tenant_created`：`(tenant_id, created_at)`
- `ix_audit_logs_tenant_action`：`(tenant_id, action)`

审计表只提供插入与查询，不提供更新、删除接口。

### 3.4 迁移

新增迁移，`down_revision = 9a2f5c7d1b44`，步骤：

1. 创建 `tenants`，插入默认租户：
   - `id = 00000000-0000-0000-0000-000000000001`
   - `name = 默认租户`
   - `slug = default`
   - `status = active`
2. `users` 新增 `role`、`is_active`、`must_change_password`、`is_platform_admin`、`token_version`，均先以 `server_default` 写入非空默认值。
3. 回填角色：`role = 'owner' WHERE is_admin = true`，其余为 `editor`。
4. 删除 `users.is_admin`。
5. 创建 `audit_logs` 与上述索引。

downgrade：

1. 删除 `audit_logs`。
2. `users` 新增 `is_admin`，按 `role IN ('owner', 'admin')` 回填。
3. 删除新增的用户列。
4. 删除 `tenants`。

启动时 `ensure_admin` 额外保证：

- 默认租户行存在（若迁移已插入则跳过）。
- 环境变量管理员存在，`role = 'owner'`；若系统中没有任何 `is_platform_admin = true` 的用户，则将该管理员设为平台管理员。

迁移后旧 JWT 不含 `ver`，会被统一判为失效，所有用户需重新登录；上线说明中注明。

## 4. 认证与会话

### 4.1 JWT

载荷新增 `ver`（整数）并设为必需：

```json
{
  "sub": "user@example.com",
  "tid": "00000000-0000-0000-0000-000000000001",
  "ver": 0,
  "iat": "...",
  "exp": "..."
}
```

`decode_token` 的 `require` 列表加入 `ver`。`create_token(subject, tenant_id, token_version)` 随之调整。

### 4.2 登录

`POST /api/auth/login`：

1. 邮箱 `strip().lower()`。
2. 查用户；不存在时对固定 dummy hash 执行一次校验后返回 401 `invalid_credentials`。
3. 校验密码；失败返回 401 `invalid_credentials`，并对该用户所属租户写 `auth.login_failed` 审计（best-effort，失败只记服务端日志）。
4. 密码正确后依次校验：
   - 用户 `is_active = true`，否则 403 `account_disabled`（`账号已停用，请联系租户管理员`）。
   - 租户存在且 `status = active`，否则 403 `tenant_suspended`（`租户已停用，请联系平台管理员`）。
5. 成功返回 token，并写 `auth.login` 审计。

先校验密码再暴露停用状态，避免通过错误信息枚举账号状态。邮箱不存在时无法确定租户，不写审计，仅记录服务端日志。

### 4.3 请求鉴权

`get_current_user` 改为一次查询用户与租户（`LEFT OUTER JOIN tenants ON tenants.id = users.tenant_id`，以便区分"用户不存在"与"租户缺失"），依次校验：

| 条件 | 结果 |
|---|---|
| 缺少/非法 Bearer token | 401 `unauthorized` |
| 用户不存在 | 401 `unauthorized` |
| 用户 `is_active = false` | 403 `account_disabled` |
| 租户缺失或 `status != active` | 403 `tenant_suspended` |
| `ver != user.token_version` | 401 `token_revoked`（`登录状态已失效，请重新登录`） |

角色不写入 JWT，每次请求从数据库读取，确保角色调整立即生效。

### 4.4 修改密码

`POST /api/auth/change-password`（任意已登录用户）：

请求：

```json
{ "current_password": "...", "new_password": "..." }
```

规则：

- `new_password` 长度 8–128，且不能与 `current_password` 相同；不额外强制字符类型。
- 旧密码错误返回 400 `invalid_password`（`当前密码不正确`）。
- 成功后：更新 `password_hash`，`must_change_password = false`，`token_version += 1`，写 `auth.change_password` 审计。
- 由于 `token_version` 递增，当前 token 同时失效；响应返回新 token：

```json
{ "access_token": "...", "token_type": "bearer", "display_name": "..." }
```

前端收到后立即替换本地 token。

### 4.5 当前用户

`GET /api/auth/me` 扩展为：

```json
{
  "id": "uuid",
  "email": "user@example.com",
  "display_name": "运营同学",
  "role": "editor",
  "tenant_id": "uuid",
  "tenant_name": "默认租户",
  "is_platform_admin": false,
  "must_change_password": false
}
```

## 5. 权限模型

### 5.1 角色层级

`ROLE_RANK = {"viewer": 0, "editor": 1, "admin": 2, "owner": 3}`

| 角色 | 定位 |
|---|---|
| `viewer` | 只读成员，可查看业务数据（不含发布账号凭据与系统设置），不能做任何写入 |
| `editor` | 运营成员，可创作、跑技能、生成媒体、导入数据、导出、发起发布 |
| `admin` | 管理员，可管理发布账号、LLM 提供商、技能开关、审核/执行/排期发布 |
| `owner` | 租户所有者，可管理成员与租户信息，拥有 admin 全部能力 |

### 5.2 鉴权依赖

新增 `app/permissions.py`：

```python
def require_role(min_role: Role) -> Callable[..., Awaitable[User]]:
    async def dependency(user: User = Depends(get_current_user)) -> User:
        if ROLE_RANK[user.role] < ROLE_RANK[min_role]:
            raise AppError("forbidden", "权限不足", 403)
        return user
    return dependency


async def require_platform_admin(
    user: User = Depends(get_current_user),
) -> User:
    if not user.is_platform_admin:
        raise AppError("platform_admin_required", "需要平台管理员权限", 403)
    return user
```

所有接口显式声明最小角色；不再直接依赖 `get_current_user` 做写操作。读接口统一使用 `require_role("viewer")`，使权限声明可检索、可测试。

### 5.3 接口权限矩阵

| 方法 | 路径 | 最小角色 |
|---|---|---|
| POST | `/api/auth/login` | 公开 |
| GET | `/api/auth/me` | 已登录 |
| POST | `/api/auth/change-password` | 已登录 |
| GET | `/api/members` | admin |
| POST | `/api/members` | owner |
| PATCH | `/api/members/{id}` | owner |
| DELETE | `/api/members/{id}` | owner |
| GET | `/api/tenants` | 平台管理员 |
| POST | `/api/tenants` | 平台管理员 |
| PATCH | `/api/tenants/{id}` | 平台管理员 |
| GET | `/api/audit-logs` | admin |
| GET | `/api/accounts` | editor |
| POST | `/api/accounts` | admin |
| PATCH | `/api/accounts/{id}` | admin |
| DELETE | `/api/accounts/{id}` | admin |
| GET | `/api/settings/providers` | admin |
| POST | `/api/settings/providers` | admin |
| DELETE | `/api/settings/providers/{id}` | admin |
| GET | `/api/skills`、`/api/skills/{id}` | viewer |
| PATCH | `/api/skills/{id}/toggle` | admin |
| POST | `/api/skills/{id}/run`、`/api/skills/{id}/run/stream` | editor |
| POST | `/api/chat` | editor |
| GET | `/api/projects`、`/api/projects/{id}` | viewer |
| POST | `/api/projects` | editor |
| GET | `/api/projects/{id}/contents`、`/api/contents/{id}/versions` | viewer |
| POST | `/api/projects/{id}/contents`、`/api/contents/{id}/versions` | editor |
| GET | `/api/projects/{id}/files`、`/files/download`、`/files/preview` | viewer |
| POST | `/api/projects/{id}/files` | editor |
| GET | `/api/projects/{id}/media`、`/media/jobs/{job_id}` | viewer |
| POST | `/api/projects/{id}/media/{skill_id}`、`/media/jobs/{job_id}/retry` | editor |
| POST | `/api/projects/{id}/repurpose` | editor |
| GET | `/api/profiles`、`/api/profiles/{id}`、`/memories` | viewer |
| POST/PATCH/DELETE | `/api/profiles`、`/api/profiles/{id}`、`/memories` | editor |
| GET | `/api/analytics/overview` | viewer |
| POST | `/api/analytics/import`、`/api/analytics/contents/{id}/postmortem` | editor |
| GET | `/api/calendar` | viewer |
| GET | `/api/jobs`、`/api/jobs/{id}` | viewer |
| POST | `/api/jobs`、`/jobs/{id}/precheck`、`/jobs/{id}/export` | editor |
| POST | `/api/jobs/{id}/execute`、`/jobs/{id}/actions` | admin |
| PATCH | `/api/jobs/{id}/schedule` | admin |
| GET | `/health` | 公开 |

发布账号读取放宽到 `editor`，因为编辑器创建发布任务时需要选择账号；账号写入与凭据管理仍限 `admin`。

## 6. 成员管理

新增 `app/api/members.py`，前缀 `/api/members`。所有查询按 `tenant_id` 过滤。

### 6.1 数据结构

`MemberOut`：

```json
{
  "id": "uuid",
  "email": "member@example.com",
  "display_name": "成员",
  "role": "editor",
  "is_active": true,
  "created_at": "2026-11-04T10:00:00Z"
}
```

`MemberCreateIn`：

```json
{
  "email": "member@example.com",
  "display_name": "成员",
  "role": "editor"
}
```

- `email` 小写化后写入；全局唯一，重复返回 409 `email_exists`（`该邮箱已被使用`，不区分租户）。
- `display_name` 1–100 字符。
- `role` 取值 `owner/admin/editor/viewer`，非法值返回 422。
- 临时密码由服务端用 `secrets.token_urlsafe(12)` 生成（约 16 字符），只在创建响应中返回一次：`MemberCreatedOut = MemberOut + { "initial_password": "..." }`。
- 新用户 `must_change_password = true`。

`MemberUpdateIn`：

```json
{
  "display_name": "新名字",
  "role": "admin",
  "is_active": false
}
```

字段均可选，但至少提供一个。

### 6.2 保护规则

| 场景 | 结果 |
|---|---|
| 请求中包含 `role` 且目标是本人 | 409 `cannot_modify_self`（即使与当前角色相同） |
| 停用自己 | 409 `cannot_modify_self` |
| 降级或停用租户最后一个 `owner` | 409 `last_owner`（`租户必须保留至少一名所有者`） |
| 操作其他租户的成员 | 404 `not_found` |
| 重复邮箱 | 409 `email_exists` |

角色变更、停用、启用都递增目标用户的 `token_version`，使其现有会话立即失效。修改显示名不递增。通过 `PATCH` 修改 `is_active` 记 `member.update`；通过 `DELETE` 停用记 `member.disable`。

`DELETE /api/members/{id}` 等价于 `is_active = false` 的软停用，返回 204；不物理删除，保留审计与引用完整性。

## 7. 租户管理

新增 `app/api/tenants.py`，前缀 `/api/tenants`，全部使用 `require_platform_admin`。平台管理员只能管理租户元数据，不能读取其他租户的业务数据。

### 7.1 数据结构

`TenantOut`：

```json
{
  "id": "uuid",
  "name": "客户 A",
  "slug": "customer-a",
  "status": "active",
  "members_count": 3,
  "created_at": "2026-11-04T10:00:00Z"
}
```

`TenantCreateIn`：

```json
{
  "name": "客户 A",
  "slug": "customer-a",
  "owner_email": "owner@example.com",
  "owner_display_name": "客户 A 管理员"
}
```

- `name` 1–100 字符；`slug` 2–50，匹配 `^[a-z0-9][a-z0-9-]{1,49}$`，唯一，重复返回 409 `slug_exists`。
- `owner_email` 全局唯一，重复返回 409 `email_exists`。
- 创建事务内同时创建租户与 owner 账号，owner `role = owner`、`must_change_password = true`。
- 响应 `TenantCreatedOut = TenantOut + { "owner": MemberCreatedOut }`，其中包含一次性 `initial_password`。
- 新租户 `id = uuid4()` 字符串。

`TenantUpdateIn`：

```json
{ "name": "客户 A（改名）", "status": "suspended" }
```

字段可选，至少提供一个；`status` 仅允许 `active` / `suspended`。

### 7.2 生命周期规则

- 列表 `GET /api/tenants` 返回全部租户，按 `created_at` 倒序；`members_count` 用子查询聚合。
- 停用租户：
  - 拒绝停用自己的租户（409 `cannot_suspend_self`）。
  - 在事务内把该租户所有用户的 `token_version` 加一，使在线会话立即失效。
  - 该租户用户此后登录返回 403 `tenant_suspended`。
- 启用租户只恢复登录能力，不恢复旧 token。
- 平台管理员账号不能被降级为普通角色？不限制：角色与平台权限独立；即使角色为 `viewer`，平台管理员仍可管理租户。
- 不允许删除租户（本期无删除接口），避免误删业务数据。

## 8. 审计

### 8.1 写入

新增 `app/services/audit.py`：

```python
async def record(
    session: AsyncSession,
    *,
    tenant_id: str,
    action: str,
    actor: User | str,
    resource_type: str = "",
    resource_id: str = "",
    status: str = "success",
    detail: dict | None = None,
    request: Request | None = None,
) -> None:
    ...
```

- `actor` 为用户时记录 `actor_id` 与 `actor_email`；为字符串时（如 `scheduler`）`actor_id` 为空。
- `tenant_id` 一律取**操作发起方**的租户：平台管理员操作其他租户时，审计写入平台管理员自己的租户，目标租户 id 放在 `resource_id`；这样平台管理员能在自己的审计页看到这些操作，而目标租户不会因平台动作产生无归属记录。
- `ip` 取 `request.client.host`，不信任 `X-Forwarded-For`；`user_agent` 截断到 300 字符。
- `detail` 经过 `sanitize_detail`：只保留 `str/int/float/bool/None` 及一层嵌套 dict/list；字符串截断 200 字符；键名命中 `password`、`new_password`、`current_password`、`token`、`access_token`、`secret`、`api_key`、`credential`、`authorization`、`cookie` 的条目直接丢弃。
- 业务接口内的审计与业务写入使用同一事务提交，保证"操作成功即留痕"。
- 登录失败、登录成功等独立事件由调用方单独提交；审计写入异常只记录服务端日志，不影响认证响应。

### 8.2 事件清单

| action | 触发 | detail 示例 |
|---|---|---|
| `auth.login` | 登录成功 | `{}` |
| `auth.login_failed` | 密码错误（邮箱存在） | `{"reason": "bad_password"}` |
| `auth.change_password` | 修改密码成功 | `{}` |
| `member.create` | 创建成员 | `{"role": "editor"}` |
| `member.update` | 改角色/显示名/启停 | `{"role": "admin", "is_active": true}` |
| `member.disable` | DELETE 停用成员 | `{"is_active": false}` |
| `tenant.create` | 平台创建租户 | `{"slug": "customer-a", "owner_email": "owner@example.com"}` |
| `tenant.update` | 平台改名/停用/启用 | `{"status": "suspended"}` |
| `provider.create` | 新增 LLM 提供商 | `{"name": "千问"}` |
| `provider.delete` | 删除 LLM 提供商 | `{}` |
| `account.create` | 新增发布账号 | `{"platform": "wechat"}` |
| `account.update` | 修改发布账号 | `{}` |
| `account.revoke` | 停用发布账号 | `{}` |
| `skill.toggle` | 启用/停用技能 | `{"enabled": false}` |
| `publish.create` | 创建发布任务 | `{"platform": "wechat"}` |
| `publish.execute` | 立即发布 | `{}` |
| `publish.export` | 导出发布包 | `{"platform": "xiaohongshu"}` |
| `publish.schedule` | 调整定时 | `{"scheduled_at": "..."}` |
| `publish.action` | 审核/驳回/取消/重试 | `{"action": "approve"}` |
| `publish.auto_execute` | worker 定时发布 | `{}` |
| `publish.auto_fail` | worker 判定阻塞并失败 | `{"reason": "..."}` |
| `media.create` | 提交媒体任务 | `{"skill_id": "short-video"}` |
| `media.retry` | 重试媒体任务 | `{}` |

不记录普通读取、预览、列表查询。

### 8.3 查询

`GET /api/audit-logs`（`admin` 及以上）：

| 参数 | 说明 |
|---|---|
| `action` | 精确匹配，可选 |
| `status` | `success` / `failure`，可选 |
| `actor_email` | 精确匹配，可选 |
| `from` / `to` | ISO 8601 时间，闭区间过滤 `created_at`，可选 |
| `limit` | 1–200，默认 50 |
| `offset` | 默认 0 |

响应：

```json
{
  "items": [
    {
      "id": "uuid",
      "actor_email": "owner@example.com",
      "action": "member.create",
      "resource_type": "user",
      "resource_id": "uuid",
      "status": "success",
      "ip": "127.0.0.1",
      "user_agent": "Mozilla/5.0 ...",
      "detail": { "role": "editor" },
      "created_at": "2026-11-04T10:00:00Z"
    }
  ],
  "total": 42
}
```

固定按 `created_at DESC, id DESC` 排序；`total` 为同过滤条件计数。查询始终限定当前用户的 `tenant_id`。不提供删除或修改接口。

## 9. 后台任务

### 9.1 定时发布

`app/worker.py::list_due_jobs` 增加 `JOIN tenants`，条件 `tenants.status = 'active'`，停用租户的到期任务不会被扫描。任务在停用期间保持 `approved`，启用后按原时间补执行。

`publish_due_jobs` 在每个任务执行成功后写 `publish.auto_execute` 审计；`_fail_blocked_job` 与悬挂对账失败写 `publish.auto_fail`，`actor = "scheduler"`。审计写入失败不阻断整批任务。

### 9.2 媒体任务

`app/services/media.py::run_media_job` 在生成前读取租户：

- 租户缺失或 `status != active`：任务置为 `failed`，`error_code = "tenant_suspended"`，`error_message = "租户已停用，任务未执行"`，不调用 LLM 或媒体脚本。
- 该失败可通过现有重试接口重试，但租户未启用前仍会立即失败。

## 10. 前端

### 10.1 认证上下文

新增 `web/src/auth.tsx`：

- `AuthProvider` 使用 TanStack Query 获取 `/api/auth/me`，向子组件提供 `user`、`role`、`isPlatformAdmin`、`canWrite`（`editor` 及以上）、`isAdmin`（`admin` 及以上）、`isOwner`、`refresh`。
- `ProtectedLayout`：
  - 无 token → `/login`。
  - `must_change_password` 且当前路径不是 `/change-password` → 重定向到 `/change-password`。
  - 401 → `/login`；403 的 `tenant_suspended` / `account_disabled` → 展示专用提示并清除 token。

### 10.2 新增页面与导航

| 路径 | 页面 | 可见角色 |
|---|---|---|
| `/change-password` | 修改密码（含首次强制改密） | 已登录 |
| `/team` | 成员管理：列表、创建、改角色、启停、一次性临时密码展示与复制 | admin 读，owner 写 |
| `/audit` | 审计查询：动作/状态/操作人/时间过滤，分页加载 | admin |
| `/tenants` | 平台租户：列表、创建（含 owner 与临时密码）、停用/启用 | 平台管理员 |

导航按角色过滤：

| 导航项 | 可见角色 |
|---|---|
| 助手 | editor 及以上 |
| 账号画像 / 项目 / 日历 / 发布 / 数据 | viewer 及以上 |
| 发布账号 | editor 及以上 |
| 设置 / 团队 / 审计 | admin 及以上 |
| 租户 | 平台管理员 |

现有页面的主要写操作按角色隐藏或禁用：

- 项目：新建项目、创建内容/版本、上传、一稿多改、媒体生成与重试。
- 画像：新建/编辑/删除画像、增删记忆。
- 发布账号：新建/修改/停用（页面仅 editor 及以上可见，操作仅 admin）。
- 发布：viewer 只读任务列表；创建/预检/导出对 editor 开放；执行、排期、审核、驳回、取消、重试仅 admin。
- 数据：导入与复盘仅 editor 及以上。
- 设置：整页仅 admin 及以上。
- 助手：仅 editor 及以上可见。

后端始终是权限的唯一裁决方；前端隐藏只为减少误操作。

### 10.3 登录与错误提示

- 登录页展示 `account_disabled` / `tenant_suspended` 的具体文案。
- 任意请求收到 403 `forbidden` 时，页面展示"权限不足，请联系租户管理员"，不跳转登录页。

## 11. 错误码

| code | HTTP | 场景 |
|---|---|---|
| `unauthorized` | 401 | 缺少或非法凭证 |
| `token_revoked` | 401 | `token_version` 不匹配 |
| `invalid_credentials` | 401 | 邮箱或密码错误 |
| `forbidden` | 403 | 角色权限不足 |
| `account_disabled` | 403 | 用户被停用 |
| `tenant_suspended` | 403 | 租户被停用 |
| `platform_admin_required` | 403 | 非平台管理员访问租户管理 |
| `not_found` | 404 | 跨租户或不存在 |
| `invalid_password` | 400 | 当前密码错误 |
| `email_exists` | 409 | 邮箱已存在（任意租户） |
| `slug_exists` | 409 | 租户 slug 重复 |
| `last_owner` | 409 | 试图移除最后一个 owner |
| `cannot_modify_self` | 409 | 修改自己的角色或停用自己 |
| `cannot_suspend_self` | 409 | 平台管理员停用自己的租户 |
| `invalid_role` | 422 | 角色取值非法 |
| `validation_error` | 422 | 通用参数校验失败 |

## 12. 测试策略

### 12.1 后端

新增或修改：

- `tests/test_auth_saas.py`：登录成功/失败审计、停用用户、停用租户、改密、旧 token 失效、新 token 可用。
- `tests/test_permissions.py`：每个角色对代表性读写接口的允许/拒绝矩阵；平台接口对非平台管理员返回 403。
- `tests/test_members.py`：创建返回临时密码、首次登录强制改密、重复邮箱、非法角色、最后一个 owner 保护、自改保护、角色变更使旧 token 失效、跨租户成员 404。
- `tests/test_tenants.py`：平台创建租户 + owner、slug/邮箱冲突、列表成员数、停用使 token 失效、禁止停用自己、启用后可登录。
- `tests/test_audit.py`：事件写入、租户隔离、过滤器、分页、脱敏（密码/密钥字段被丢弃）、只读（无删除接口）。
- `tests/test_isolation.py`：租户 A/B 各建项目、内容、画像、记忆、发布账号、发布任务、指标、媒体任务，逐模块验证读取/修改/删除返回 404，列表不泄漏。
- `tests/test_worker.py`：停用租户的到期任务不被扫描；停用租户的媒体任务失败为 `tenant_suspended`。
- `tests/conftest.py`：`client` 假用户补 `role="owner"`、`token_version=0`；新增 `tenant_factory` 与"另一个租户"的认证夹具。

现有测试全部保持通过；受 `get_current_user` 新校验影响的测试需补 `is_active`、`token_version` 默认值。

### 12.2 前端

- `auth` 角色能力函数与导航过滤。
- `must_change_password` 强制跳转。
- 成员创建请求与一次性密码展示。
- 审计过滤器序列化与分页。
- 租户创建/停用状态切换。

### 12.3 验证命令

```bash
cd backend && uv run ruff check .
cd backend && uv run pytest -q
npm --prefix web run test -- --run
npm --prefix web run build
```

## 13. 验收标准

真实环境（本机 Docker + `make worker`）：

1. 平台管理员创建租户 B，返回 owner 临时密码；用该密码登录 B 的 owner，被强制改密。
2. 租户 A 与 B 分别创建项目、内容、画像、发布任务；A 的用户访问 B 的资源全部 404，列表互不出现。
3. 租户 A 的 owner 创建 editor 成员；editor 可创建内容与发布任务，访问发布审核/账号管理返回 403；admin 可审核但访问成员管理写入返回 403。
4. 停用 editor 后，其旧 token 立即失效；重新登录返回 403 `account_disabled`。
5. 平台管理员停用租户 B：B 的在线会话立即失效、登录返回 403 `tenant_suspended`、B 的定时发布不执行；重新启用后可登录。
6. 修改密码后旧 token 失效，新 token 正常使用。
7. 审计页面可查到登录、成员、发布、租户事件；任何审计 `detail` 中不出现密码、token 或平台凭据。
8. 迁移 `alembic upgrade head` 成功；默认租户与现有管理员角色正确回填。

## 14. 风险与后续

| 风险 | 影响 | 对策 |
|---|---|---|
| 一人一租户限制 | 自由职业者/代运营无法跨团队 | 文档明确；后续引入 `tenant_members` 时保持 API 兼容 |
| 所有旧 token 一次性失效 | 升级后需重新登录 | 上线说明注明 |
| 临时密码通过响应返回 | 仅一次，可能被截屏 | 强制首次改密；不写审计、不入日志 |
| 审计表持续增长 | 查询变慢、存储增加 | 本期不清理；后续增加按时间归档与保留策略 |
| 代理后 IP 不准确 | 审计溯源能力下降 | 当前只信任直连地址；部署接入可信代理后再评估 `X-Forwarded-For` |
| 前端隐藏不完整 | 用户可能看到无效按钮 | 后端强制鉴权；前端只做体验优化，不承担安全职责 |
| 平台管理员跨租户能力 | 误操作或权限过大 | 平台接口仅限租户元数据；业务数据接口仍按租户隔离；不提供删除租户 |
