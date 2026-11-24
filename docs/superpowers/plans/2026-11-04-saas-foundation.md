# Phase 3 SaaS 基础实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有 `tenant_id` 隔离之上补齐租户管理、四级角色权限、成员管理与安全审计，使系统具备可运营、可验证的多租户基础。

**Architecture:** 新增 `tenants` 与 `audit_logs` 表，扩展 `users` 的角色与会话字段；JWT 绑定 `tenant_id` 与 `token_version`，每个请求实时校验用户、租户与会话；所有业务接口按最小角色声明依赖；关键写操作与审计同事务落库；平台管理员通过独立权限位管理租户生命周期。

**Tech Stack:** Python 3.11、uv、FastAPI、Pydantic v2、SQLAlchemy 2.0 async、Alembic、PostgreSQL 16、arq、React 19、TanStack Query 5、react-router-dom 7、Tailwind v4、vitest 4。

**设计依据:** [docs/superpowers/specs/2026-11-04-saas-foundation-design.md](../specs/2026-11-04-saas-foundation-design.md)

---

## 0. 执行约定

- 仓库根目录：`/Users/tongxi/Desktop/新媒体运营系统`，当前分支 `phase-3-saas-foundation`。
- 后端命令一律通过 `uv`：`cd backend && uv run pytest ...`、`uv run ruff check .`、`uv run alembic ...`。禁止裸 `python` / `pytest` / `alembic`。
- 前端命令通过 `npm --prefix web ...`，或先 `cd web`。
- 每个 Task 按 TDD 执行：先写失败测试，运行确认失败原因正确，再写最小实现，运行通过，最后提交。
- 每个 Task 结束提交一次；提交信息使用 `feat:` / `test:` / `refactor:` / `docs:` 前缀。**只有用户批准执行本计划后才开始提交。**
- 不记录任何密码、token、API Key、平台凭据到审计或日志。测试断言中只使用假值。
- 现有 552 个后端测试必须保持通过；`test_worker.py` 的租户种子在 Task 10 统一补齐。
- 前端 vitest 使用 `environment: "node"`，没有 jsdom/testing-library。前端只测试纯逻辑函数；页面交互在 Task 19 的真实浏览器验收中验证。
- 不修改全局 git 配置，不使用 `--no-verify`，不 force-push，不 amend 已发布提交。

## 1. 文件结构

### 后端新增

| 文件 | 职责 |
|---|---|
| `backend/app/models/tenant.py` | `Tenant` ORM 模型 |
| `backend/app/models/audit.py` | `AuditLog` ORM 模型与索引 |
| `backend/app/services/audit.py` | 审计写入、脱敏、best-effort 写入 |
| `backend/app/permissions.py` | 角色层级、`require_role`、`require_platform_admin`、预置依赖 |
| `backend/app/api/members.py` | 租户内成员 CRUD、最后 owner 保护、临时密码 |
| `backend/app/api/tenants.py` | 平台租户创建/列表/改名/停用/启用 |
| `backend/app/api/audit.py` | 审计查询 |
| `backend/migrations/versions/c3d4e5f6a7b8_add_saas_tenancy_roles_audit.py` | Phase 3 数据库迁移 |
| `backend/tests/test_models_saas.py` | 模型与种子测试 |
| `backend/tests/test_audit_service.py` | 审计服务与脱敏测试 |
| `backend/tests/test_auth_saas.py` | 会话、登录、改密测试 |
| `backend/tests/test_permissions.py` | 角色矩阵测试 |
| `backend/tests/test_members.py` | 成员管理测试 |
| `backend/tests/test_tenants.py` | 租户管理测试 |
| `backend/tests/test_audit.py` | 审计查询与脱敏测试 |
| `backend/tests/test_audit_events.py` | 业务事件审计测试 |
| `backend/tests/test_isolation.py` | 双租户隔离测试 |

### 后端修改

- `backend/app/models/user.py`：新增 `role`、`is_active`、`must_change_password`、`is_platform_admin`、`token_version`，删除 `is_admin`。
- `backend/app/seed.py`：确保默认租户、环境管理员为 owner、首个平台管理员。
- `backend/app/security.py`：JWT 增加必需 `ver`。
- `backend/app/deps.py`：用户 + 租户联合查询与会话校验。
- `backend/app/api/auth.py`：登录校验/审计、改密、`/me` 扩展。
- `backend/app/main.py`：注册 `members`、`tenants`、`audit` 路由。
- `backend/app/worker.py`：到期任务按活跃租户过滤、调度审计。
- `backend/app/services/media.py`：停用租户的媒体任务失败。
- `backend/app/api/{accounts,settings,skills,chat,calendar,analytics,profiles,projects,publish}.py`：角色依赖与审计钩子。
- `backend/tests/conftest.py`：假用户字段、租户工厂。
- `backend/tests/test_worker.py`：租户种子、停用租户与媒体任务测试。

### 前端新增

| 文件 | 职责 |
|---|---|
| `web/src/auth.ts` | 角色层级、能力判断、导航过滤、强制改密判断（纯逻辑） |
| `web/src/auth-context.tsx` | `AuthProvider` / `useAuth` / `ProtectedLayout` |
| `web/src/team.ts` | 成员创建请求体与角色文案（纯逻辑） |
| `web/src/audit.ts` | 审计过滤参数序列化（纯逻辑） |
| `web/src/tenants.ts` | 租户创建请求体与状态切换（纯逻辑） |
| `web/src/pages/ChangePassword.tsx` | 改密页 |
| `web/src/pages/Team.tsx` | 团队成员页 |
| `web/src/pages/Audit.tsx` | 审计页 |
| `web/src/pages/Tenants.tsx` | 平台租户页 |
| `web/src/__tests__/auth.test.ts` | 角色与导航测试 |
| `web/src/__tests__/team.test.ts` | 成员请求体测试 |
| `web/src/__tests__/audit.test.ts` | 审计查询序列化测试 |
| `web/src/__tests__/tenants.test.ts` | 租户状态切换测试 |

### 前端修改

- `web/src/types.ts`：新增 `Role`、`Me`、`Member`、`Tenant`、`AuditLog` 等类型。
- `web/src/App.tsx`：`AuthProvider`、强制改密、角色路由守卫、新页面路由。
- `web/src/components/Layout.tsx`：角色化导航。
- `web/src/pages/{Projects,ProjectDetail,Profiles,Accounts,Publish,Analytics}.tsx`：写操作按角色隐藏。
- `CHANGELOG.md`、`README.md`、`docs/phase-3-checklist.md`：文档与验收清单。

---

## Task 1: 数据模型、种子与测试夹具

**Files:**
- Create: `backend/app/models/tenant.py`
- Create: `backend/app/models/audit.py`
- Create: `backend/tests/test_models_saas.py`
- Modify: `backend/app/models/user.py:1-13`
- Modify: `backend/app/seed.py:1-29`
- Modify: `backend/tests/conftest.py:1-99`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_models_saas.py`：

```python
from sqlalchemy import select

from app.config import DEFAULT_TENANT_ID, get_settings
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.user import User
from app.seed import ensure_admin


async def test_ensure_admin_creates_default_tenant_and_owner(db_session):
    await ensure_admin(db_session)

    tenant = await db_session.get(Tenant, DEFAULT_TENANT_ID)
    assert tenant is not None
    assert tenant.name == "默认租户"
    assert tenant.slug == "default"
    assert tenant.status == "active"

    admin = (
        await db_session.execute(
            select(User).where(User.email == get_settings().admin_email)
        )
    ).scalar_one()
    assert admin.role == "owner"
    assert admin.is_active is True
    assert admin.is_platform_admin is True
    assert admin.token_version == 0
    assert admin.must_change_password is False


async def test_ensure_admin_is_idempotent(db_session):
    await ensure_admin(db_session)
    await ensure_admin(db_session)

    tenants = (await db_session.execute(select(Tenant))).scalars().all()
    assert len(tenants) == 1

    platform_admins = (
        await db_session.execute(
            select(User).where(User.is_platform_admin.is_(True))
        )
    ).scalars().all()
    assert len(platform_admins) == 1


async def test_audit_log_persists_json_detail(db_session):
    db_session.add(
        AuditLog(
            tenant_id=DEFAULT_TENANT_ID,
            actor_email="scheduler",
            action="publish.auto_execute",
            detail={"ok": True},
        )
    )
    await db_session.commit()

    row = (await db_session.execute(select(AuditLog))).scalar_one()
    assert row.detail == {"ok": True}
    assert row.status == "success"
    assert row.actor_id is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_models_saas.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.models.tenant'`。

- [ ] **Step 3: 新增 Tenant 模型**

创建 `backend/app/models/tenant.py`：

```python
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
```

- [ ] **Step 4: 新增 AuditLog 模型**

创建 `backend/app/models/audit.py`：

```python
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_logs_tenant_action", "tenant_id", "action"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_email: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(
        String(32), default="", nullable=False
    )
    resource_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="success", nullable=False
    )
    ip: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    user_agent: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 5: 扩展 User 模型**

把 `backend/app/models/user.py` 整体替换为：

```python
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantScoped


class User(TenantScoped):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(100), default="运营同学")
    role: Mapped[str] = mapped_column(String(16), default="editor", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
```

- [ ] **Step 6: 更新种子逻辑**

把 `backend/app/seed.py` 整体替换为：

```python
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TENANT_ID, get_settings
from app.models.tenant import Tenant
from app.models.user import User
from app.security import hash_password


async def ensure_admin(session: AsyncSession) -> None:
    settings = get_settings()
    email = settings.admin_email.strip().lower()

    tenant = await session.get(Tenant, DEFAULT_TENANT_ID)
    if tenant is None:
        session.add(
            Tenant(
                id=DEFAULT_TENANT_ID,
                name="默认租户",
                slug="default",
                status="active",
            )
        )
        await session.flush()

    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            tenant_id=DEFAULT_TENANT_ID,
            email=email,
            password_hash=hash_password(settings.admin_password),
            display_name="管理员",
            role="owner",
            is_active=True,
            is_platform_admin=False,
            token_version=0,
        )
        session.add(user)
        await session.flush()

    has_platform_admin = (
        await session.execute(
            select(User.id).where(User.is_platform_admin.is_(True)).limit(1)
        )
    ).first()
    if has_platform_admin is None:
        user.is_platform_admin = True

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
```

- [ ] **Step 7: 更新测试夹具**

在 `backend/tests/conftest.py` 中：

1. 增加导入：

```python
from app.models.tenant import Tenant
```

2. 把 `client` fixture 的假用户改为：

```python
    fake = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="tester@local",
        password_hash="x",
        display_name="Tester",
        role="owner",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
```

3. 在文件末尾增加租户工厂：

```python
@pytest_asyncio.fixture
async def tenant_factory(db_session):
    async def create(*, name="测试租户", slug=None, status="active"):
        tenant = Tenant(
            id=str(uuid.uuid4()),
            name=name,
            slug=slug or f"t-{uuid.uuid4().hex[:8]}",
            status=status,
        )
        db_session.add(tenant)
        await db_session.commit()
        return tenant

    return create
```

- [ ] **Step 8: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_models_saas.py tests/test_auth.py -q`
Expected: PASS（`test_auth.py` 仍应通过；此时 JWT 尚未改）。

- [ ] **Step 9: 运行全量后端测试，记录受影响范围**

Run: `cd backend && uv run pytest -q`
Expected: 可能仅 `test_worker.py` 因 `Tenant` 模型导入无关而通过；若出现与 `is_admin` 相关的失败，在本 Task 内修正引用（仅 `seed.py` 与模型应使用 `role`）。

- [ ] **Step 10: 提交**

```bash
git add backend/app/models/tenant.py backend/app/models/audit.py backend/app/models/user.py backend/app/seed.py backend/tests/test_models_saas.py backend/tests/conftest.py
git commit -m "feat: add tenants, audit log and user role models"
```

---

## Task 2: Alembic 迁移

**Files:**
- Create: `backend/migrations/versions/c3d4e5f6a7b8_add_saas_tenancy_roles_audit.py`

迁移属于配置生成代码，不在 pytest 内伪造数据库；本 Task 用真实 PostgreSQL 验证 upgrade → downgrade → upgrade。

- [ ] **Step 1: 确认当前 head**

Run: `cd backend && uv run alembic heads`
Expected: `9a2f5c7d1b44 (head)`

- [ ] **Step 2: 写迁移文件**

创建 `backend/migrations/versions/c3d4e5f6a7b8_add_saas_tenancy_roles_audit.py`：

```python
"""add tenants, roles and audit logs

Revision ID: c3d4e5f6a7b8
Revises: 9a2f5c7d1b44
Create Date: 2026-11-04 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "9a2f5c7d1b44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=50), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenants")),
    )
    op.create_index(op.f("ix_tenants_slug"), "tenants", ["slug"], unique=True)
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, name, slug, status) "
            "VALUES (:id, :name, :slug, 'active')"
        ).bindparams(
            id=DEFAULT_TENANT_ID,
            name="默认租户",
            slug="default",
        )
    )

    op.add_column(
        "users",
        sa.Column(
            "role", sa.String(length=16), server_default="editor", nullable=False
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "is_platform_admin",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "token_version",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.execute("UPDATE users SET role = 'owner' WHERE is_admin = true")
    op.drop_column("users", "is_admin")

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("actor_id", sa.String(length=36), nullable=True),
        sa.Column("actor_email", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "resource_type", sa.String(length=32), server_default="", nullable=False
        ),
        sa.Column(
            "resource_id", sa.String(length=64), server_default="", nullable=False
        ),
        sa.Column(
            "status", sa.String(length=16), server_default="success", nullable=False
        ),
        sa.Column("ip", sa.String(length=64), server_default="", nullable=False),
        sa.Column(
            "user_agent", sa.String(length=300), server_default="", nullable=False
        ),
        sa.Column(
            "detail",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_tenant_created",
        "audit_logs",
        ["tenant_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_tenant_action",
        "audit_logs",
        ["tenant_id", "action"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_audit_logs_tenant_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_created", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.add_column(
        "users",
        sa.Column(
            "is_admin", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.execute(
        "UPDATE users SET is_admin = true WHERE role IN ('owner', 'admin')"
    )
    op.drop_column("users", "token_version")
    op.drop_column("users", "is_platform_admin")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "is_active")
    op.drop_column("users", "role")

    op.drop_index(op.f("ix_tenants_slug"), table_name="tenants")
    op.drop_table("tenants")
```

- [ ] **Step 3: 校验迁移链**

Run: `cd backend && uv run alembic heads`
Expected: `c3d4e5f6a7b8 (head)`

Run: `cd backend && uv run alembic history | head -5`
Expected: 最上方为 `c3d4e5f6a7b8 -> 9a2f5c7d1b44`。

- [ ] **Step 4: 真实 PostgreSQL 验证（需要 Docker）**

Run:
```bash
make up
make migrate
cd backend && uv run alembic downgrade -1 && uv run alembic upgrade head
```
Expected: 三次命令均成功；`alembic current` 输出 `c3d4e5f6a7b8 (head)`。

验证默认租户与管理员回填：
```bash
docker compose exec -T db psql -U app -d newmedia -c "select id,name,slug,status from tenants;"
docker compose exec -T db psql -U app -d newmedia -c "select email,role,is_active,is_platform_admin,token_version from users;"
```
Expected: 存在 `default` 租户；原管理员 `role = owner`。

Run: `make down`
Expected: 容器停止，`pgdata` 卷保留。

- [ ] **Step 5: 提交**

```bash
git add backend/migrations/versions/c3d4e5f6a7b8_add_saas_tenancy_roles_audit.py
git commit -m "feat: migrate tenants, roles and audit logs"
```

---

## Task 3: 审计服务与脱敏

**Files:**
- Create: `backend/app/services/audit.py`
- Test: `backend/tests/test_audit_service.py`

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_audit_service.py`：

```python
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import DEFAULT_TENANT_ID
from app.models.audit import AuditLog
from app.models.user import User
from app.services.audit import record, record_best_effort, sanitize_detail


def test_sanitize_drops_sensitive_keys_and_truncates():
    detail = {
        "password": "p",
        "new_password": "np",
        "current_password": "cp",
        "access_token": "t",
        "api_key": "k",
        "secret": "s",
        "credential": {"app_secret": "x"},
        "authorization": "Bearer x",
        "cookie": "sid",
        "safe": "ok",
        "long": "x" * 300,
        "nested": {"role": "editor", "password": "p", "objects": object()},
        "items": [1, True, None, "abc", object()],
        "too_deep": {"a": {"b": 1}},
    }

    cleaned = sanitize_detail(detail)

    assert "password" not in cleaned
    assert "new_password" not in cleaned
    assert "current_password" not in cleaned
    assert "access_token" not in cleaned
    assert "api_key" not in cleaned
    assert "secret" not in cleaned
    assert "credential" not in cleaned
    assert "authorization" not in cleaned
    assert "cookie" not in cleaned
    assert cleaned["safe"] == "ok"
    assert cleaned["long"] == "x" * 200
    assert cleaned["nested"] == {"role": "editor"}
    assert cleaned["items"] == [1, True, None, "abc"]
    assert cleaned["too_deep"] == {}


def test_sanitize_non_dict_returns_empty():
    assert sanitize_detail(None) == {}
    assert sanitize_detail(["x"]) == {}


async def test_record_writes_actor_and_request_metadata(db_session):
    user = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="actor@local",
        password_hash="x",
        role="owner",
    )
    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.1"),
        headers={"user-agent": "UA" * 200},
    )

    await record(
        db_session,
        tenant_id=DEFAULT_TENANT_ID,
        action="member.create",
        actor=user,
        resource_type="user",
        resource_id=str(user.id),
        detail={"role": "editor"},
        request=request,
    )
    await db_session.commit()

    row = (await db_session.execute(select(AuditLog))).scalar_one()
    assert row.actor_id == str(user.id)
    assert row.actor_email == "actor@local"
    assert row.action == "member.create"
    assert row.resource_type == "user"
    assert row.resource_id == str(user.id)
    assert row.ip == "10.0.0.1"
    assert len(row.user_agent) == 300
    assert row.detail == {"role": "editor"}


async def test_record_string_actor_has_no_actor_id(db_session):
    await record(
        db_session,
        tenant_id=DEFAULT_TENANT_ID,
        action="publish.auto_execute",
        actor="scheduler",
    )
    await db_session.commit()

    row = (await db_session.execute(select(AuditLog))).scalar_one()
    assert row.actor_id is None
    assert row.actor_email == "scheduler"


async def test_record_best_effort_swallows_commit_errors(db_session, monkeypatch):
    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db_session, "commit", boom)

    await record_best_effort(
        db_session,
        tenant_id=DEFAULT_TENANT_ID,
        action="auth.login",
        actor="tester@local",
    )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_audit_service.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.services.audit'`。

- [ ] **Step 3: 实现审计服务**

创建 `backend/app/services/audit.py`：

```python
import logging
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User

logger = logging.getLogger(__name__)

BLOCKED_KEY_PARTS = {
    "password",
    "token",
    "secret",
    "api_key",
    "credential",
    "authorization",
    "cookie",
}
_UNSUPPORTED = object()


def _is_blocked_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in BLOCKED_KEY_PARTS)


def _clean_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:200]
    return _UNSUPPORTED


def sanitize_detail(detail: dict | None) -> dict:
    if not isinstance(detail, dict):
        return {}

    cleaned: dict[str, Any] = {}
    for key, value in detail.items():
        if not isinstance(key, str) or _is_blocked_key(key):
            continue
        if isinstance(value, dict):
            nested: dict[str, Any] = {}
            for nested_key, nested_value in value.items():
                if not isinstance(nested_key, str) or _is_blocked_key(nested_key):
                    continue
                scalar = _clean_scalar(nested_value)
                if scalar is not _UNSUPPORTED:
                    nested[nested_key] = scalar
            cleaned[key] = nested
        elif isinstance(value, list):
            items = []
            for item in value[:50]:
                scalar = _clean_scalar(item)
                if scalar is not _UNSUPPORTED:
                    items.append(scalar)
            cleaned[key] = items
        else:
            scalar = _clean_scalar(value)
            if scalar is not _UNSUPPORTED:
                cleaned[key] = scalar
    return cleaned


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
    if isinstance(actor, str):
        actor_id = None
        actor_email = actor
    else:
        actor_id = str(actor.id)
        actor_email = actor.email

    ip = ""
    user_agent = ""
    if request is not None:
        ip = request.client.host if request.client is not None else ""
        user_agent = (request.headers.get("user-agent") or "")[:300]

    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_email=actor_email,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            ip=ip,
            user_agent=user_agent,
            detail=sanitize_detail(detail),
        )
    )
    await session.flush()


async def record_best_effort(
    session: AsyncSession,
    **kwargs: Any,
) -> None:
    """用于已经提交或独立事件的审计；失败只记日志，不影响主流程。"""
    try:
        await record(session, **kwargs)
        await session.commit()
    except Exception:  # noqa: BLE001 - 审计失败不能改变业务响应
        logger.exception("审计写入失败 action=%s", kwargs.get("action"))
        await session.rollback()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_audit_service.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/audit.py backend/tests/test_audit_service.py
git commit -m "feat: add sanitized audit logging service"
```

### Task 4: JWT 会话版本与请求鉴权

**Files:**
- Modify: `backend/app/security.py:26-44`
- Modify: `backend/app/deps.py`
- Modify: `backend/app/api/auth.py:28-59`
- Test: `backend/tests/test_auth_saas.py`

- [ ] **Step 1: 编写失败测试**

创建 `backend/tests/test_auth_saas.py`：

```python
from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select

from app.config import get_settings
from app.models.base import DEFAULT_TENANT_ID
from app.models.tenant import Tenant
from app.models.user import User

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


async def _admin(session) -> User:
    return (
        await session.execute(
            select(User).where(User.email == get_settings().admin_email)
        )
    ).scalar_one()


async def test_me_returns_role_tenant_and_flags(auth_client, auth_headers):
    resp = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == get_settings().admin_email
    assert body["role"] == "owner"
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    assert body["tenant_name"] == "默认租户"
    assert body["is_platform_admin"] is True
    assert body["must_change_password"] is False


async def test_token_without_version_is_rejected(auth_client, db_session):
    admin = await _admin(db_session)
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": admin.email,
            "tid": admin.tenant_id,
            "iat": now,
            "exp": now + timedelta(minutes=30),
        },
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    resp = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_token_version_mismatch_is_revoked(
    auth_client, auth_headers, db_session
):
    admin = await _admin(db_session)
    admin.token_version += 1
    await db_session.commit()
    resp = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == "token_revoked"


async def test_disabled_user_is_forbidden(auth_client, auth_headers, db_session):
    admin = await _admin(db_session)
    admin.is_active = False
    await db_session.commit()
    resp = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "account_disabled"


async def test_suspended_tenant_is_forbidden(
    auth_client, auth_headers, db_session
):
    tenant = await db_session.get(Tenant, DEFAULT_TENANT_ID)
    tenant.status = "suspended"
    await db_session.commit()
    resp = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "tenant_suspended"


async def test_missing_tenant_is_forbidden(auth_client, db_session):
    from app.security import create_token, hash_password

    user = User(
        tenant_id=OTHER_TENANT,
        email="orphan@local",
        password_hash=hash_password("password123"),
        display_name="Orphan",
        role="owner",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    db_session.add(user)
    await db_session.commit()
    token = create_token(user.email, user.tenant_id, user.token_version)
    resp = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "tenant_suspended"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_auth_saas.py -q`
Expected: FAIL，`/auth/me` 不含 `role` 字段，且 `create_token` 不接受版本参数。

- [ ] **Step 3: 修改 token 生成与解析**

`backend/app/security.py` 中替换 `create_token` 与 `decode_token`：

```python
def create_token(subject: str, tenant_id: str, token_version: int) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "tid": tenant_id,
        "ver": token_version,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(
        token,
        get_settings().jwt_secret,
        algorithms=["HS256"],
        options={"require": ["sub", "tid", "ver", "iat", "exp"]},
    )
```

- [ ] **Step 4: 重写 `get_current_user`**

`backend/app/deps.py` 全量替换为：

```python
from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models.tenant import Tenant
from app.models.user import User
from app.security import decode_token


async def get_current_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError("unauthorized", "缺少登录凭证", 401)
    try:
        payload = decode_token(authorization.removeprefix("Bearer ").strip())
    except Exception as exc:  # token 过期、签名错误统一按未授权处理
        raise AppError("unauthorized", "登录已失效，请重新登录", 401) from exc
    subject = payload.get("sub")
    tenant_id = payload.get("tid")
    if not subject or not tenant_id:
        raise AppError("unauthorized", "登录已失效，请重新登录", 401)
    row = (
        await session.execute(
            select(User, Tenant)
            .outerjoin(Tenant, Tenant.id == User.tenant_id)
            .where(User.email == subject, User.tenant_id == tenant_id)
        )
    ).first()
    if row is None:
        raise AppError("unauthorized", "用户不存在", 401)
    user, tenant = row
    if not user.is_active:
        raise AppError("account_disabled", "账号已停用，请联系租户管理员", 403)
    if tenant is None or tenant.status != "active":
        raise AppError("tenant_suspended", "租户已停用，请联系平台管理员", 403)
    if int(payload["ver"]) != user.token_version:
        raise AppError("token_revoked", "登录状态已失效，请重新登录", 401)
    return user
```

- [ ] **Step 5: 扩展 `/auth/me` 并补 login 的版本参数**

`backend/app/api/auth.py` 中 `MeOut` 替换为：

```python
class MeOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    tenant_id: str
    tenant_name: str
    is_platform_admin: bool
    must_change_password: bool
```

`login` 的返回改为：

```python
    return TokenOut(
        access_token=create_token(
            user.email, user.tenant_id, user.token_version
        ),
        display_name=user.display_name,
    )
```

`me` 替换为：

```python
@router.get("/me", response_model=MeOut)
async def me(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MeOut:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or tenant.status != "active":
        raise AppError("tenant_suspended", "租户已停用，请联系平台管理员", 403)
    return MeOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tenant_id=user.tenant_id,
        tenant_name=tenant.name,
        is_platform_admin=user.is_platform_admin,
        must_change_password=user.must_change_password,
    )
```

补充 import：

```python
from app.models.tenant import Tenant
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_auth_saas.py tests/test_auth.py -q`
Expected: PASS。`test_auth.py` 中过期 token 用例仍返回 401。

- [ ] **Step 7: 提交**

```bash
git add backend/app/security.py backend/app/deps.py backend/app/api/auth.py backend/tests/test_auth_saas.py
git commit -m "feat: bind jwt sessions to token version and tenant status"
```

### Task 5: 登录校验、审计与修改密码

**Files:**
- Modify: `backend/app/api/auth.py`
- Test: `backend/tests/test_auth_saas.py`（追加）

- [ ] **Step 1: 追加失败测试**

在 `backend/tests/test_auth_saas.py` 末尾追加：

```python
async def _audit_rows(session, action):
    from app.models.audit import AuditLog

    return (
        await session.execute(
            select(AuditLog).where(AuditLog.action == action)
        )
    ).scalars().all()


async def test_login_writes_success_audit(auth_client, db_session):
    settings = get_settings()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert resp.status_code == 200
    rows = await _audit_rows(db_session, "auth.login")
    assert len(rows) == 1
    assert rows[0].tenant_id == DEFAULT_TENANT_ID
    assert rows[0].actor_email == settings.admin_email


async def test_bad_password_writes_failure_audit(auth_client, db_session):
    settings = get_settings()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": settings.admin_email, "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"
    rows = await _audit_rows(db_session, "auth.login_failed")
    assert len(rows) == 1
    assert rows[0].status == "failure"
    assert rows[0].detail == {"reason": "bad_password"}


async def test_disabled_user_cannot_login(auth_client, db_session):
    settings = get_settings()
    admin = await _admin(db_session)
    admin.is_active = False
    await db_session.commit()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "account_disabled"


async def test_suspended_tenant_cannot_login(auth_client, db_session):
    settings = get_settings()
    tenant = await db_session.get(Tenant, DEFAULT_TENANT_ID)
    tenant.status = "suspended"
    await db_session.commit()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "tenant_suspended"


async def test_change_password_rotates_token(auth_client, auth_headers, db_session):
    resp = await auth_client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": get_settings().admin_password,
            "new_password": "new-password-123",
        },
    )
    assert resp.status_code == 200
    new_token = resp.json()["access_token"]
    old = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert old.status_code == 401
    assert old.json()["code"] == "token_revoked"
    fresh = await auth_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert fresh.status_code == 200
    assert fresh.json()["must_change_password"] is False
    rows = await _audit_rows(db_session, "auth.change_password")
    assert len(rows) == 1


async def test_change_password_rejects_wrong_current(auth_client, auth_headers):
    resp = await auth_client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={"current_password": "wrong", "new_password": "new-password-123"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_password"


async def test_change_password_rejects_short_and_same(auth_client, auth_headers):
    short = await auth_client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": get_settings().admin_password,
            "new_password": "short",
        },
    )
    assert short.status_code == 422
    same = await auth_client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": get_settings().admin_password,
            "new_password": get_settings().admin_password,
        },
    )
    assert same.status_code == 422
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_auth_saas.py -q`
Expected: FAIL，登录不写审计且 `/auth/change-password` 返回 404。

- [ ] **Step 3: 实现登录校验与审计**

`backend/app/api/auth.py` 补充 import：

```python
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import (
    BaseModel,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.tenant import Tenant
from app.services.audit import record, record_best_effort
```

`login` 替换为：

```python
@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginIn,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TokenOut:
    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user is None:
        verify_password(payload.password, _DUMMY_HASH)
        raise AppError("invalid_credentials", "邮箱或密码错误", 401)
    if not verify_password(payload.password, user.password_hash):
        await record_best_effort(
            session,
            tenant_id=user.tenant_id,
            action="auth.login_failed",
            actor=user,
            status="failure",
            detail={"reason": "bad_password"},
            request=request,
        )
        raise AppError("invalid_credentials", "邮箱或密码错误", 401)
    if not user.is_active:
        raise AppError("account_disabled", "账号已停用，请联系租户管理员", 403)
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or tenant.status != "active":
        raise AppError("tenant_suspended", "租户已停用，请联系平台管理员", 403)
    token = create_token(user.email, user.tenant_id, user.token_version)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="auth.login",
        actor=user,
        request=request,
    )
    return TokenOut(access_token=token, display_name=user.display_name)
```

- [ ] **Step 4: 实现修改密码**

在 `MeOut` 前新增请求模型：

```python
class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: Annotated[
        str, StringConstraints(min_length=8, max_length=128)
    ]

    @model_validator(mode="after")
    def _different(self) -> "ChangePasswordIn":
        if self.new_password == self.current_password:
            raise ValueError("新密码不能与当前密码相同")
        return self
```

在 `me` 前新增路由：

```python
@router.post("/change-password", response_model=TokenOut)
async def change_password(
    payload: ChangePasswordIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TokenOut:
    if not verify_password(payload.current_password, user.password_hash):
        raise AppError("invalid_password", "当前密码不正确", 400)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    user.token_version += 1
    await record(
        session,
        tenant_id=user.tenant_id,
        action="auth.change_password",
        actor=user,
        resource_type="user",
        resource_id=str(user.id),
        request=request,
    )
    await session.commit()
    token = create_token(user.email, user.tenant_id, user.token_version)
    return TokenOut(access_token=token, display_name=user.display_name)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_auth_saas.py tests/test_auth.py tests/test_audit_service.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/auth.py backend/tests/test_auth_saas.py
git commit -m "feat: audit login events and rotate sessions on password change"
```

### Task 6: 角色依赖与全接口鉴权

**Files:**
- Create: `backend/app/permissions.py`
- Modify: `backend/app/api/accounts.py`
- Modify: `backend/app/api/analytics.py`
- Modify: `backend/app/api/calendar.py`
- Modify: `backend/app/api/chat.py`
- Modify: `backend/app/api/profiles.py`
- Modify: `backend/app/api/projects.py`
- Modify: `backend/app/api/publish.py`
- Modify: `backend/app/api/settings.py`
- Modify: `backend/app/api/skills.py`
- Test: `backend/tests/test_permissions.py`

- [ ] **Step 1: 编写失败测试**

创建 `backend/tests/test_permissions.py`：

```python
import uuid
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User


@asynccontextmanager
async def _role_client(db_session, role: str, platform_admin: bool = False):
    fake = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email=f"{role}@local",
        password_hash="x",
        display_name=role,
        role=role,
        is_active=True,
        must_change_password=False,
        is_platform_admin=platform_admin,
        token_version=0,
    )
    app.dependency_overrides[get_current_user] = lambda: fake
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_session, None)


async def test_viewer_can_read_but_not_write(db_session):
    async with _role_client(db_session, "viewer") as client:
        assert (await client.get("/api/projects")).status_code == 200
        assert (await client.get("/api/skills")).status_code == 200
        resp = await client.post("/api/projects", json={"title": "只读测试"})
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"


async def test_editor_can_create_but_not_manage_admin_resources(db_session):
    async with _role_client(db_session, "editor") as client:
        created = await client.post("/api/projects", json={"title": "编辑测试"})
        assert created.status_code == 201
        assert (await client.get("/api/settings/providers")).status_code == 403
        toggle = await client.patch(
            "/api/skills/article-outline/toggle", json={"enabled": False}
        )
        assert toggle.status_code == 403
        account = await client.post(
            "/api/accounts",
            json={
                "platform": "wechat",
                "name": "编辑不可建账号",
                "credential": {"app_id": "id", "app_secret": "secret"},
            },
        )
        assert account.status_code == 403


async def test_editor_can_read_accounts_for_publish_selection(db_session):
    async with _role_client(db_session, "editor") as client:
        assert (await client.get("/api/accounts")).status_code == 200


async def test_admin_can_manage_settings_and_skills(db_session):
    async with _role_client(db_session, "admin") as client:
        assert (await client.get("/api/settings/providers")).status_code == 200
        toggle = await client.patch(
            "/api/skills/article-outline/toggle", json={"enabled": False}
        )
        assert toggle.status_code == 200


async def test_viewer_cannot_use_chat(db_session):
    async with _role_client(db_session, "viewer") as client:
        resp = await client.post("/api/chat", json={"message": "你好"})
        assert resp.status_code == 403
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_permissions.py -q`
Expected: FAIL，viewer/editor 仍可执行写操作。

- [ ] **Step 3: 新增权限模块**

创建 `backend/app/permissions.py`：

```python
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import Depends

from app.deps import get_current_user
from app.errors import AppError
from app.models.user import User

Role = Literal["viewer", "editor", "admin", "owner"]

ROLE_RANK: dict[str, int] = {
    "viewer": 0,
    "editor": 1,
    "admin": 2,
    "owner": 3,
}

UserDep = Annotated[User, Depends(get_current_user)]


def require_role(min_role: Role) -> Callable[..., Awaitable[User]]:
    async def dependency(user: UserDep) -> User:
        if user.role not in ROLE_RANK or ROLE_RANK[user.role] < ROLE_RANK[min_role]:
            raise AppError("forbidden", "权限不足", 403)
        return user

    return dependency


async def require_platform_admin(user: UserDep) -> User:
    if not user.is_platform_admin:
        raise AppError("platform_admin_required", "需要平台管理员权限", 403)
    return user


viewer_access = require_role("viewer")
editor_access = require_role("editor")
admin_access = require_role("admin")
owner_access = require_role("owner")
```

- [ ] **Step 4: 按矩阵替换路由依赖**

每个文件把 `from app.deps import get_current_user` 改为 `from app.permissions import admin_access, editor_access, viewer_access`（按文件实际使用裁剪），并把对应函数的 `Depends(get_current_user)` 替换为下表依赖：

| 文件 | 使用 `viewer_access` 的函数 | 使用 `editor_access` 的函数 | 使用 `admin_access` 的函数 |
|---|---|---|---|
| `accounts.py` | — | `list_accounts` | `create_account`、`update_account`、`revoke_account` |
| `analytics.py` | `get_overview` | `import_analytics`、`postmortem` | — |
| `calendar.py` | `get_calendar` | — | — |
| `chat.py` | — | `chat` | — |
| `profiles.py` | `list_profiles`、`get_profile`、`list_memories` | `create_profile`、`update_profile`、`delete_profile`、`create_memory`、`delete_memory` | — |
| `projects.py` | `list_projects`、`get_project`、`list_contents`、`list_versions`、`list_project_files`、`download_project_file`、`preview_project_file`、`list_project_media`、`get_project_media_job` | `create_project`、`create_content`、`create_version`、`upload_project_file`、`create_project_media`、`retry_project_media_job`、`repurpose` | — |
| `publish.py` | `list_jobs`、`get_job` | `create_job`、`precheck_job`、`export_publish` | `execute_publish`、`schedule_job`、`act_on_job` |
| `settings.py` | — | — | `list_providers`、`create_provider`、`delete_provider` |
| `skills.py` | `list_skills`、`get_skill` | `run_skill`、`stream_skill` | `toggle_skill` |

替换后每个函数的签名形如：

```python
async def list_projects(
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProjectList:
```

- [ ] **Step 5: 运行权限测试与全量后端测试**

Run: `cd backend && uv run pytest tests/test_permissions.py -q`
Expected: PASS。

Run: `cd backend && uv run pytest -q`
Expected: 原有测试全部通过；若有测试直接调用写接口，需把 `client` 假用户角色保持 `owner`（Task 1 已设置）。

- [ ] **Step 6: 提交**

```bash
git add backend/app/permissions.py backend/app/api/accounts.py backend/app/api/analytics.py backend/app/api/calendar.py backend/app/api/chat.py backend/app/api/profiles.py backend/app/api/projects.py backend/app/api/publish.py backend/app/api/settings.py backend/app/api/skills.py backend/tests/test_permissions.py
git commit -m "feat: enforce role hierarchy across api routes"
```

### Task 7: 租户成员管理

**Files:**
- Create: `backend/app/api/members.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_members.py`

- [ ] **Step 1: 编写失败测试**

创建 `backend/tests/test_members.py`：

```python
import secrets
import uuid

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.errors import AppError
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.security import create_token, hash_password
from app.services.members import ensure_not_last_owner

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


async def _admin(session) -> User:
    return (
        await session.execute(
            select(User).where(User.email == get_settings().admin_email)
        )
    ).scalar_one()


async def _create_member(client, headers, email="member@local", role="editor"):
    return await client.post(
        "/api/members",
        headers=headers,
        json={"email": email, "display_name": "成员", "role": role},
    )


async def test_create_member_returns_one_time_password(auth_client, auth_headers):
    resp = await _create_member(auth_client, auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "editor"
    assert body["is_active"] is True
    assert len(body["initial_password"]) >= 12
    assert "password" not in {key for key in body if key != "initial_password"}

    login = await auth_client.post(
        "/api/auth/login",
        json={"email": "member@local", "password": body["initial_password"]},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    me = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.json()["must_change_password"] is True


async def test_duplicate_email_and_invalid_role(auth_client, auth_headers):
    assert (await _create_member(auth_client, auth_headers)).status_code == 201
    dup = await _create_member(
        auth_client, auth_headers, email="member@local", role="viewer"
    )
    assert dup.status_code == 409
    assert dup.json()["code"] == "email_exists"
    bad = await _create_member(
        auth_client, auth_headers, email="other@local", role="superuser"
    )
    assert bad.status_code == 422


async def test_cannot_change_own_role_or_disable_self(
    auth_client, auth_headers, db_session
):
    admin = await _admin(db_session)
    role = await auth_client.patch(
        f"/api/members/{admin.id}", headers=auth_headers, json={"role": "admin"}
    )
    assert role.status_code == 409
    assert role.json()["code"] == "cannot_modify_self"
    disable = await auth_client.patch(
        f"/api/members/{admin.id}",
        headers=auth_headers,
        json={"is_active": False},
    )
    assert disable.status_code == 409
    assert disable.json()["code"] == "cannot_modify_self"


async def test_last_owner_guard(db_session):
    admin = await _admin(db_session)
    with pytest.raises(AppError) as exc:
        await ensure_not_last_owner(db_session, admin.tenant_id, admin)
    assert exc.value.code == "last_owner"


async def test_role_change_revokes_existing_token(
    auth_client, auth_headers, db_session
):
    created = (await _create_member(auth_client, auth_headers)).json()
    login = await auth_client.post(
        "/api/auth/login",
        json={"email": "member@local", "password": created["initial_password"]},
    )
    token = login.json()["access_token"]
    changed = await auth_client.patch(
        f"/api/members/{created['id']}",
        headers=auth_headers,
        json={"role": "viewer"},
    )
    assert changed.status_code == 200
    old = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert old.status_code == 401
    assert old.json()["code"] == "token_revoked"


async def test_delete_member_disables_and_revokes(auth_client, auth_headers):
    created = (await _create_member(auth_client, auth_headers)).json()
    login = await auth_client.post(
        "/api/auth/login",
        json={"email": "member@local", "password": created["initial_password"]},
    )
    token = login.json()["access_token"]
    deleted = await auth_client.delete(
        f"/api/members/{created['id']}", headers=auth_headers
    )
    assert deleted.status_code == 204
    old = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert old.status_code == 401
    relogin = await auth_client.post(
        "/api/auth/login",
        json={"email": "member@local", "password": created["initial_password"]},
    )
    assert relogin.status_code == 403
    assert relogin.json()["code"] == "account_disabled"


async def test_cross_tenant_member_is_not_found(
    auth_client, auth_headers, db_session
):
    other = User(
        tenant_id=OTHER_TENANT,
        email="other-member@local",
        password_hash=hash_password("password123"),
        display_name="其他租户成员",
        role="editor",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    db_session.add(other)
    await db_session.commit()
    resp = await auth_client.patch(
        f"/api/members/{other.id}",
        headers=auth_headers,
        json={"display_name": "改名"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_list_members_returns_tenant_members_only(
    auth_client, auth_headers, db_session
):
    await _create_member(auth_client, auth_headers)
    other = User(
        tenant_id=OTHER_TENANT,
        email="other-member@local",
        password_hash="x",
        display_name="其他租户成员",
        role="editor",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    db_session.add(other)
    await db_session.commit()
    rows = (await auth_client.get("/api/members", headers=auth_headers)).json()
    emails = {row["email"] for row in rows}
    assert "member@local" in emails
    assert "other-member@local" not in emails


def test_password_generator_is_random():
    first = secrets.token_urlsafe(12)
    second = secrets.token_urlsafe(12)
    assert first != second
    assert len(first) >= 12
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_members.py -q`
Expected: FAIL，`/api/members` 返回 404。

- [ ] **Step 3: 实现成员服务辅助**

创建 `backend/app/services/members.py`：

```python
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.user import User


async def ensure_not_last_owner(
    session: AsyncSession, tenant_id: str, member: User
) -> None:
    if member.role != "owner" or not member.is_active:
        return
    owners = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.tenant_id == tenant_id,
                User.role == "owner",
                User.is_active.is_(True),
            )
        )
    ).scalar_one()
    if owners <= 1:
        raise AppError("last_owner", "租户必须保留至少一名所有者", 409)
```

- [ ] **Step 4: 实现成员 API**

创建 `backend/app/api/members.py`：

```python
import secrets
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import (
    BaseModel,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models.user import User
from app.permissions import Role, admin_access, owner_access
from app.security import hash_password
from app.services.audit import record
from app.services.members import ensure_not_last_owner

router = APIRouter(prefix="/members", tags=["members"])

Email = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=255)
]
DisplayName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class MemberOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime


class MemberCreatedOut(MemberOut):
    initial_password: str


class MemberCreateIn(BaseModel):
    email: Email
    display_name: DisplayName
    role: Role = "editor"

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.lower()


class MemberUpdateIn(BaseModel):
    display_name: DisplayName | None = None
    role: Role | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "MemberUpdateIn":
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个要更新的字段")
        return self


def _out(member: User) -> MemberOut:
    return MemberOut(
        id=str(member.id),
        email=member.email,
        display_name=member.display_name,
        role=member.role,
        is_active=member.is_active,
        created_at=member.created_at,
    )


async def _owned_member(
    session: AsyncSession, member_id: str, actor: User
) -> User:
    try:
        parsed = uuid.UUID(member_id)
    except (ValueError, TypeError):
        raise AppError("not_found", "成员不存在", 404) from None
    member = (
        await session.execute(
            select(User).where(
                User.id == parsed,
                User.tenant_id == actor.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise AppError("not_found", "成员不存在", 404)
    return member


@router.get("", response_model=list[MemberOut])
async def list_members(
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[MemberOut]:
    rows = (
        await session.execute(
            select(User)
            .where(User.tenant_id == user.tenant_id)
            .order_by(User.created_at, User.id)
        )
    ).scalars().all()
    return [_out(member) for member in rows]


@router.post("", response_model=MemberCreatedOut, status_code=201)
async def create_member(
    payload: MemberCreateIn,
    request: Request,
    user: User = Depends(owner_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MemberCreatedOut:
    existing = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError("email_exists", "该邮箱已被使用", 409)
    initial_password = secrets.token_urlsafe(12)
    member = User(
        tenant_id=user.tenant_id,
        email=payload.email,
        password_hash=hash_password(initial_password),
        display_name=payload.display_name,
        role=payload.role,
        is_active=True,
        must_change_password=True,
        is_platform_admin=False,
        token_version=0,
    )
    session.add(member)
    try:
        await session.flush()
        await record(
            session,
            tenant_id=user.tenant_id,
            action="member.create",
            actor=user,
            resource_type="user",
            resource_id=str(member.id),
            detail={"role": member.role},
            request=request,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("email_exists", "该邮箱已被使用", 409) from None
    await session.refresh(member)
    return MemberCreatedOut(
        **_out(member).model_dump(), initial_password=initial_password
    )


@router.patch("/{member_id}", response_model=MemberOut)
async def update_member(
    member_id: str,
    payload: MemberUpdateIn,
    request: Request,
    user: User = Depends(owner_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MemberOut:
    member = await _owned_member(session, member_id, user)
    fields = payload.model_dump(exclude_unset=True)
    if "role" in fields and member.id == user.id:
        raise AppError("cannot_modify_self", "不能修改自己的角色", 409)
    if fields.get("is_active") is False and member.id == user.id:
        raise AppError("cannot_modify_self", "不能停用自己", 409)

    role_changed = "role" in fields and fields["role"] != member.role
    active_changed = (
        "is_active" in fields and fields["is_active"] != member.is_active
    )
    if role_changed or (active_changed and member.is_active):
        await ensure_not_last_owner(session, user.tenant_id, member)

    for key, value in fields.items():
        setattr(member, key, value)
    if role_changed or active_changed:
        member.token_version += 1
    await record(
        session,
        tenant_id=user.tenant_id,
        action="member.update",
        actor=user,
        resource_type="user",
        resource_id=str(member.id),
        detail={"role": member.role, "is_active": member.is_active},
        request=request,
    )
    await session.commit()
    await session.refresh(member)
    return _out(member)


@router.delete("/{member_id}", status_code=204)
async def disable_member(
    member_id: str,
    request: Request,
    user: User = Depends(owner_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    member = await _owned_member(session, member_id, user)
    if member.id == user.id:
        raise AppError("cannot_modify_self", "不能停用自己", 409)
    await ensure_not_last_owner(session, user.tenant_id, member)
    if member.is_active:
        member.is_active = False
        member.token_version += 1
    await record(
        session,
        tenant_id=user.tenant_id,
        action="member.disable",
        actor=user,
        resource_type="user",
        resource_id=str(member.id),
        detail={"is_active": False},
        request=request,
    )
    await session.commit()
    return Response(status_code=204)
```

- [ ] **Step 5: 注册路由**

`backend/app/main.py` 的 API import 加入 `members`，并在 settings 路由前注册：

```python
from app.api import (
    accounts,
    analytics,
    auth,
    calendar,
    chat,
    members,
    profiles,
    projects,
    publish,
    skills,
)
```

```python
app.include_router(members.router, prefix="/api")
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_members.py -q`
Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add backend/app/api/members.py backend/app/services/members.py backend/app/main.py backend/tests/test_members.py
git commit -m "feat: add tenant member management with session invalidation"
```

### Task 8: 平台租户管理

**Files:**
- Create: `backend/app/api/tenants.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_tenants.py`

- [ ] **Step 1: 编写失败测试**

创建 `backend/tests/test_tenants.py`：

```python
from sqlalchemy import select

from app.config import get_settings
from app.models.base import DEFAULT_TENANT_ID
from app.models.tenant import Tenant
from app.models.user import User


async def _create_tenant(client, headers, slug="customer-a", email="owner-a@local"):
    return await client.post(
        "/api/tenants",
        headers=headers,
        json={
            "name": "客户 A",
            "slug": slug,
            "owner_email": email,
            "owner_display_name": "客户 A 管理员",
        },
    )


async def test_platform_admin_creates_tenant_with_owner(
    auth_client, auth_headers, db_session
):
    resp = await _create_tenant(auth_client, auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["slug"] == "customer-a"
    assert body["status"] == "active"
    assert body["members_count"] == 1
    assert body["owner"]["role"] == "owner"
    assert body["owner"]["email"] == "owner-a@local"
    password = body["owner"]["initial_password"]

    login = await auth_client.post(
        "/api/auth/login",
        json={"email": "owner-a@local", "password": password},
    )
    assert login.status_code == 200
    me = await auth_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert me.json()["tenant_name"] == "客户 A"
    assert me.json()["must_change_password"] is True


async def test_duplicate_slug_and_email_conflict(auth_client, auth_headers):
    assert (await _create_tenant(auth_client, auth_headers)).status_code == 201
    dup_slug = await _create_tenant(
        auth_client, auth_headers, slug="customer-a", email="another@local"
    )
    assert dup_slug.status_code == 409
    assert dup_slug.json()["code"] == "slug_exists"
    dup_email = await _create_tenant(
        auth_client, auth_headers, slug="customer-b", email="owner-a@local"
    )
    assert dup_email.status_code == 409
    assert dup_email.json()["code"] == "email_exists"


async def test_invalid_slug_and_status_are_rejected(auth_client, auth_headers):
    bad_slug = await _create_tenant(auth_client, auth_headers, slug="A")
    assert bad_slug.status_code == 422
    created = (await _create_tenant(auth_client, auth_headers)).json()
    bad_status = await auth_client.patch(
        f"/api/tenants/{created['id']}",
        headers=auth_headers,
        json={"status": "deleted"},
    )
    assert bad_status.status_code == 422


async def test_list_tenants_includes_members_count(auth_client, auth_headers):
    created = (await _create_tenant(auth_client, auth_headers)).json()
    resp = await auth_client.get("/api/tenants", headers=auth_headers)
    assert resp.status_code == 200
    rows = {row["id"]: row for row in resp.json()}
    assert rows[created["id"]]["members_count"] == 1
    assert rows[DEFAULT_TENANT_ID]["members_count"] == 1


async def test_cannot_suspend_own_tenant(auth_client, auth_headers):
    resp = await auth_client.patch(
        f"/api/tenants/{DEFAULT_TENANT_ID}",
        headers=auth_headers,
        json={"status": "suspended"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "cannot_suspend_self"


async def test_suspend_and_reenable_tenant(auth_client, auth_headers, db_session):
    created = (await _create_tenant(auth_client, auth_headers)).json()
    password = created["owner"]["initial_password"]
    login = await auth_client.post(
        "/api/auth/login",
        json={"email": "owner-a@local", "password": password},
    )
    owner_headers = {
        "Authorization": f"Bearer {login.json()['access_token']}"
    }

    suspended = await auth_client.patch(
        f"/api/tenants/{created['id']}",
        headers=auth_headers,
        json={"status": "suspended"},
    )
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"

    online = await auth_client.get("/api/auth/me", headers=owner_headers)
    assert online.status_code == 403
    assert online.json()["code"] == "tenant_suspended"

    blocked = await auth_client.post(
        "/api/auth/login",
        json={"email": "owner-a@local", "password": password},
    )
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "tenant_suspended"

    enabled = await auth_client.patch(
        f"/api/tenants/{created['id']}",
        headers=auth_headers,
        json={"status": "active"},
    )
    assert enabled.status_code == 200
    again = await auth_client.post(
        "/api/auth/login",
        json={"email": "owner-a@local", "password": password},
    )
    assert again.status_code == 200


async def test_non_platform_admin_is_forbidden(client):
    resp = await client.get("/api/tenants")
    assert resp.status_code == 403
    assert resp.json()["code"] == "platform_admin_required"


async def test_tenant_operations_are_audited(
    auth_client, auth_headers, db_session
):
    from app.models.audit import AuditLog

    created = (await _create_tenant(auth_client, auth_headers)).json()
    await auth_client.patch(
        f"/api/tenants/{created['id']}",
        headers=auth_headers,
        json={"name": "客户 A 改名"},
    )
    rows = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "tenant.create")
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].resource_id == created["id"]
    assert rows[0].detail["slug"] == "customer-a"
    updates = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "tenant.update")
        )
    ).scalars().all()
    assert len(updates) == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_tenants.py -q`
Expected: FAIL，`/api/tenants` 返回 404。

- [ ] **Step 3: 实现租户 API**

创建 `backend/app/api/tenants.py`：

```python
import secrets
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, StringConstraints, field_validator, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.members import MemberCreatedOut
from app.db import get_session
from app.errors import AppError
from app.models.tenant import Tenant
from app.models.user import User
from app.permissions import require_platform_admin
from app.security import hash_password
from app.services.audit import record

router = APIRouter(prefix="/tenants", tags=["tenants"])

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=2,
        max_length=50,
        pattern=r"^[a-z0-9][a-z0-9-]{1,49}$",
    ),
]
Email = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=255)
]


class TenantOut(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    members_count: int
    created_at: datetime


class TenantCreatedOut(TenantOut):
    owner: MemberCreatedOut


class TenantCreateIn(BaseModel):
    name: Name
    slug: Slug
    owner_email: Email
    owner_display_name: Name

    @field_validator("owner_email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.lower()


class TenantUpdateIn(BaseModel):
    name: Name | None = None
    status: Literal["active", "suspended"] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "TenantUpdateIn":
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个要更新的字段")
        return self


def _out(tenant: Tenant, members_count: int) -> TenantOut:
    return TenantOut(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        members_count=members_count,
        created_at=tenant.created_at,
    )


async def _member_count(session: AsyncSession, tenant_id: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant_id)
        )
    ).scalar_one()


@router.get("", response_model=list[TenantOut])
async def list_tenants(
    _: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[TenantOut]:
    counts = (
        select(User.tenant_id, func.count().label("cnt"))
        .group_by(User.tenant_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Tenant, func.coalesce(counts.c.cnt, 0))
            .outerjoin(counts, counts.c.tenant_id == Tenant.id)
            .order_by(Tenant.created_at.desc(), Tenant.id.desc())
        )
    ).all()
    return [_out(tenant, count) for tenant, count in rows]


@router.post("", response_model=TenantCreatedOut, status_code=201)
async def create_tenant(
    payload: TenantCreateIn,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TenantCreatedOut:
    slug_taken = (
        await session.execute(
            select(Tenant).where(Tenant.slug == payload.slug)
        )
    ).scalar_one_or_none()
    if slug_taken is not None:
        raise AppError("slug_exists", "租户标识已存在", 409)
    email_taken = (
        await session.execute(
            select(User).where(User.email == payload.owner_email)
        )
    ).scalar_one_or_none()
    if email_taken is not None:
        raise AppError("email_exists", "该邮箱已被使用", 409)

    initial_password = secrets.token_urlsafe(12)
    tenant = Tenant(
        id=str(uuid.uuid4()),
        name=payload.name,
        slug=payload.slug,
        status="active",
    )
    owner = User(
        tenant_id=tenant.id,
        email=payload.owner_email,
        password_hash=hash_password(initial_password),
        display_name=payload.owner_display_name,
        role="owner",
        is_active=True,
        must_change_password=True,
        is_platform_admin=False,
        token_version=0,
    )
    session.add(tenant)
    session.add(owner)
    await session.flush()
    await record(
        session,
        tenant_id=user.tenant_id,
        action="tenant.create",
        actor=user,
        resource_type="tenant",
        resource_id=tenant.id,
        detail={
            "slug": tenant.slug,
            "owner_email": owner.email,
        },
        request=request,
    )
    await session.commit()
    await session.refresh(tenant)
    await session.refresh(owner)
    owner_out = MemberCreatedOut(
        id=str(owner.id),
        email=owner.email,
        display_name=owner.display_name,
        role=owner.role,
        is_active=owner.is_active,
        created_at=owner.created_at,
        initial_password=initial_password,
    )
    return TenantCreatedOut(
        **_out(tenant, 1).model_dump(), owner=owner_out
    )


@router.patch("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: str,
    payload: TenantUpdateIn,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TenantOut:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("status") == "suspended" and tenant.id == user.tenant_id:
        raise AppError("cannot_suspend_self", "不能停用自己的租户", 409)

    turning_off = (
        fields.get("status") == "suspended" and tenant.status != "suspended"
    )
    for key, value in fields.items():
        setattr(tenant, key, value)
    if turning_off:
        await session.execute(
            update(User)
            .where(User.tenant_id == tenant.id)
            .values(token_version=User.token_version + 1)
        )
    await record(
        session,
        tenant_id=user.tenant_id,
        action="tenant.update",
        actor=user,
        resource_type="tenant",
        resource_id=tenant.id,
        detail={"status": tenant.status},
        request=request,
    )
    await session.commit()
    await session.refresh(tenant)
    return _out(tenant, await _member_count(session, tenant.id))
```

- [ ] **Step 4: 注册路由**

`backend/app/main.py` 的 import 加入 `tenants`，并注册：

```python
app.include_router(tenants.router, prefix="/api")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_tenants.py tests/test_members.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/tenants.py backend/app/main.py backend/tests/test_tenants.py
git commit -m "feat: add platform tenant lifecycle management"
```

### Task 9: 审计查询 API

**Files:**
- Create: `backend/app/api/audit.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_audit.py`

- [ ] **Step 1: 编写失败测试**

创建 `backend/tests/test_audit.py`：

```python
from datetime import UTC, datetime

from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID
from app.services.audit import record

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


async def _seed(session):
    session.add_all(
        [
            AuditLog(
                tenant_id=DEFAULT_TENANT_ID,
                actor_email="owner@local",
                action="member.create",
                status="success",
                detail={"role": "editor"},
                created_at=datetime(2026, 11, 1, 10, tzinfo=UTC),
            ),
            AuditLog(
                tenant_id=DEFAULT_TENANT_ID,
                actor_email="owner@local",
                action="auth.login_failed",
                status="failure",
                detail={"reason": "bad_password"},
                created_at=datetime(2026, 11, 2, 10, tzinfo=UTC),
            ),
            AuditLog(
                tenant_id=OTHER_TENANT,
                actor_email="other@local",
                action="member.create",
                status="success",
                detail={"role": "viewer"},
                created_at=datetime(2026, 11, 2, 11, tzinfo=UTC),
            ),
        ]
    )
    await session.commit()


async def test_audit_is_tenant_scoped_and_sorted(client, db_session):
    await _seed(db_session)
    resp = await client.get("/api/audit-logs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert [item["action"] for item in body["items"]] == [
        "auth.login_failed",
        "member.create",
    ]
    assert all(item["actor_email"] != "other@local" for item in body["items"])


async def test_audit_filters(client, db_session):
    await _seed(db_session)
    by_action = await client.get(
        "/api/audit-logs", params={"action": "member.create"}
    )
    assert by_action.json()["total"] == 1
    by_status = await client.get(
        "/api/audit-logs", params={"status": "failure"}
    )
    assert by_status.json()["total"] == 1
    assert by_status.json()["items"][0]["detail"] == {"reason": "bad_password"}
    by_actor = await client.get(
        "/api/audit-logs", params={"actor_email": "other@local"}
    )
    assert by_actor.json()["total"] == 0


async def test_audit_time_range_and_pagination(client, db_session):
    await _seed(db_session)
    ranged = await client.get(
        "/api/audit-logs",
        params={
            "from": "2026-11-01T00:00:00Z",
            "to": "2026-11-02T00:00:00Z",
        },
    )
    assert ranged.json()["total"] == 1
    assert ranged.json()["items"][0]["action"] == "member.create"

    page = await client.get(
        "/api/audit-logs", params={"limit": 1, "offset": 0}
    )
    assert page.json()["total"] == 2
    assert len(page.json()["items"]) == 1
    second = await client.get(
        "/api/audit-logs", params={"limit": 1, "offset": 1}
    )
    assert second.json()["items"][0]["id"] != page.json()["items"][0]["id"]


async def test_audit_detail_is_sanitized(client, db_session):
    await record(
        db_session,
        tenant_id=DEFAULT_TENANT_ID,
        action="auth.change_password",
        actor="scheduler",
        detail={
            "password": "raw-password",
            "new_password": "raw-new",
            "access_token": "raw-token",
            "role": "editor",
        },
    )
    await db_session.commit()
    resp = await client.get(
        "/api/audit-logs", params={"action": "auth.change_password"}
    )
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["detail"] == {"role": "editor"}
    assert item["actor_email"] == "scheduler"


async def test_audit_has_no_delete_route(client):
    resp = await client.delete("/api/audit-logs")
    assert resp.status_code == 405


async def test_viewer_cannot_read_audit(client, db_session):
    from tests.test_permissions import _role_client

    async with _role_client(db_session, "viewer") as viewer:
        resp = await viewer.get("/api/audit-logs")
        assert resp.status_code == 403
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_audit.py -q`
Expected: FAIL，`/api/audit-logs` 返回 404。

- [ ] **Step 3: 实现审计 API**

创建 `backend/app/api/audit.py`：

```python
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.audit import AuditLog
from app.models.user import User
from app.permissions import admin_access

router = APIRouter(prefix="/audit-logs", tags=["audit"])


class AuditLogOut(BaseModel):
    id: str
    actor_email: str
    action: str
    resource_type: str
    resource_id: str
    status: str
    ip: str
    user_agent: str
    detail: dict
    created_at: datetime


class AuditLogList(BaseModel):
    items: list[AuditLogOut]
    total: int


def _out(row: AuditLog) -> AuditLogOut:
    return AuditLogOut(
        id=str(row.id),
        actor_email=row.actor_email,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        status=row.status,
        ip=row.ip,
        user_agent=row.user_agent,
        detail=row.detail or {},
        created_at=row.created_at,
    )


@router.get("", response_model=AuditLogList)
async def list_audit_logs(
    action: str | None = None,
    status: Literal["success", "failure"] | None = None,
    actor_email: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AuditLogList:
    conditions = [AuditLog.tenant_id == user.tenant_id]
    if action is not None:
        conditions.append(AuditLog.action == action)
    if status is not None:
        conditions.append(AuditLog.status == status)
    if actor_email is not None:
        conditions.append(AuditLog.actor_email == actor_email)
    if from_ is not None:
        conditions.append(AuditLog.created_at >= from_)
    if to is not None:
        conditions.append(AuditLog.created_at <= to)

    total = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(AuditLog)
            .where(*conditions)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return AuditLogList(items=[_out(row) for row in rows], total=total)
```

- [ ] **Step 4: 注册路由**

`backend/app/main.py` 的 import 加入 `audit`，并注册：

```python
app.include_router(audit.router, prefix="/api")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_audit.py tests/test_audit_service.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/audit.py backend/app/main.py backend/tests/test_audit.py
git commit -m "feat: add tenant-scoped audit log query api"
```

### Task 10: 后台任务感知租户状态

**Files:**
- Modify: `backend/app/worker.py`
- Modify: `backend/app/services/media.py:312-331`
- Test: `backend/tests/test_worker.py`

- [ ] **Step 1: 编写失败测试**

在 `backend/tests/test_worker.py` 顶部补充 import：

```python
from app.models.audit import AuditLog
from app.models.media import MediaJob
from app.models.tenant import Tenant
from app.services.media import run_media_job
from app.skills.registry import get_registry
from app.storage import get_storage
```

把 `worker_factory` fixture 改为在建表后写入两个租户：

```python
@pytest_asyncio.fixture
async def worker_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Tenant(
                    id=DEFAULT_TENANT_ID,
                    name="默认租户",
                    slug="default",
                    status="active",
                ),
                Tenant(
                    id=OTHER_TENANT,
                    name="其他租户",
                    slug="other",
                    status="active",
                ),
            ]
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()
```

在 `test_publish_due_jobs_executes_once` 的 `assert state["events"] == ...` 之后追加：

```python
    async with worker_factory() as session:
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "publish.auto_execute"
                )
            )
        ).scalars().all()
    assert len(audits) == 1
    assert audits[0].actor_email == "scheduler"
    assert audits[0].tenant_id == DEFAULT_TENANT_ID
    assert audits[0].resource_id == str(job_id)
```

在文件末尾追加：

```python
async def test_list_due_jobs_skips_suspended_tenant(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session)
        session.add(
            _job(
                content,
                key="suspended",
                status="approved",
                scheduled_at=now - timedelta(minutes=1),
            )
        )
        tenant = await session.get(Tenant, DEFAULT_TENANT_ID)
        tenant.status = "suspended"
        await session.commit()

        due = await list_due_jobs(session, now)
    assert due == []


async def test_media_job_fails_when_tenant_suspended(worker_factory):
    async with worker_factory() as session:
        project = Project(tenant_id=DEFAULT_TENANT_ID, title="媒体项目")
        session.add(project)
        await session.flush()
        job = MediaJob(
            tenant_id=DEFAULT_TENANT_ID,
            project_id=project.id,
            skill_id="short-video",
            status="queued",
            payload={"topic": "测试"},
        )
        session.add(job)
        await session.commit()

        tenant = await session.get(Tenant, DEFAULT_TENANT_ID)
        tenant.status = "suspended"
        await session.commit()

        result = await run_media_job(
            session=session,
            job_id=str(job.id),
            registry=get_registry(),
            storage=get_storage(),
        )

    assert result is not None
    assert result.status == "failed"
    assert result.error_code == "tenant_suspended"
    assert result.error_message == "租户已停用，任务未执行"
    assert result.attempts == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_worker.py -q`
Expected: FAIL，停用租户的到期任务仍被扫描，媒体任务仍进入生成流程。

- [ ] **Step 3: 修改定时发布扫描与审计**

`backend/app/worker.py` 补充 import：

```python
from app.models.tenant import Tenant
from app.services.audit import record_best_effort
```

`list_due_jobs` 的查询加入租户 join 与状态条件：

```python
    rows = (
        await session.execute(
            select(PublishJob)
            .join(Tenant, Tenant.id == PublishJob.tenant_id)
            .where(
                Tenant.status == "active",
                PublishJob.status == "approved",
                PublishJob.platform == "wechat",
                PublishJob.scheduled_at.is_not(None),
                PublishJob.scheduled_at <= now,
            )
            .order_by(PublishJob.scheduled_at, PublishJob.created_at)
            .limit(limit)
        )
    ).scalars().all()
```

`reconcile_stale_publishing` 中 `reconciled += 1` 之后追加：

```python
            await record_best_effort(
                session,
                tenant_id=tenant_id,
                action="publish.auto_fail",
                actor="scheduler",
                resource_type="publish_job",
                resource_id=str(job_id),
                detail={"reason": "stale_publish"},
            )
```

`_fail_blocked_job` 中成功写入状态后、`logger.warning(...)` 前追加：

```python
    await record_best_effort(
        session,
        tenant_id=tenant_id,
        action="publish.auto_fail",
        actor="scheduler",
        resource_type="publish_job",
        resource_id=str(job_id),
        detail={"reason": message[:200]},
    )
```

`publish_due_jobs` 中 `executed += 1` 之后追加：

```python
                await record_best_effort(
                    session,
                    tenant_id=tenant_id,
                    action="publish.auto_execute",
                    actor="scheduler",
                    resource_type="publish_job",
                    resource_id=str(job_id),
                )
```

- [ ] **Step 4: 媒体任务执行前校验租户**

`backend/app/services/media.py` 补充 import：

```python
from app.models.tenant import Tenant
```

`run_media_job` 中 `if job.status == "succeeded": return job` 之后插入：

```python
    tenant = await session.get(Tenant, job.tenant_id)
    if tenant is None or tenant.status != "active":
        job.status = "failed"
        job.error_code = "tenant_suspended"
        job.error_message = "租户已停用，任务未执行"
        job.finished_at = datetime.now(UTC)
        await session.commit()
        return job
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_worker.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/worker.py backend/app/services/media.py backend/tests/test_worker.py
git commit -m "feat: skip suspended tenants in background jobs"
```

### Task 11: 业务接口审计埋点

**Files:**
- Modify: `backend/app/api/accounts.py`
- Modify: `backend/app/api/settings.py`
- Modify: `backend/app/api/skills.py`
- Modify: `backend/app/api/publish.py`
- Modify: `backend/app/api/projects.py`
- Test: `backend/tests/test_audit_events.py`

- [ ] **Step 1: 编写失败测试**

创建 `backend/tests/test_audit_events.py`：

```python
import json

from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID
from app.models.project import Content, ContentVersion, Project


async def _audits(session, action):
    return (
        await session.execute(
            select(AuditLog).where(AuditLog.action == action)
        )
    ).scalars().all()


async def test_account_create_and_revoke_are_audited(client, db_session):
    created = await client.post(
        "/api/accounts",
        json={
            "platform": "wechat",
            "name": "审计账号",
            "credential": {"app_id": "app-id", "app_secret": "raw-secret"},
        },
    )
    assert created.status_code == 201
    account_id = created.json()["id"]

    rows = await _audits(db_session, "account.create")
    assert len(rows) == 1
    assert rows[0].detail == {"platform": "wechat"}
    assert "raw-secret" not in json.dumps(rows[0].detail, ensure_ascii=False)

    revoked = await client.delete(f"/api/accounts/{account_id}")
    assert revoked.status_code == 204
    rows = await _audits(db_session, "account.revoke")
    assert len(rows) == 1
    assert rows[0].resource_id == account_id


async def test_provider_create_and_delete_are_audited(client, db_session):
    created = await client.post(
        "/api/settings/providers",
        json={
            "name": "审计提供商",
            "kind": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-audit-key-123456",
        },
    )
    assert created.status_code == 201, created.text
    provider_id = created.json()["id"]

    rows = await _audits(db_session, "provider.create")
    assert len(rows) == 1
    assert rows[0].resource_id == provider_id
    assert rows[0].detail == {"name": "审计提供商"}
    assert "sk-audit" not in json.dumps(rows[0].detail, ensure_ascii=False)

    deleted = await client.delete(f"/api/settings/providers/{provider_id}")
    assert deleted.status_code == 204
    rows = await _audits(db_session, "provider.delete")
    assert len(rows) == 1
    assert rows[0].resource_id == provider_id


async def test_skill_toggle_is_audited(client, db_session):
    resp = await client.patch(
        "/api/skills/article-outline/toggle", json={"enabled": False}
    )
    assert resp.status_code == 200
    rows = await _audits(db_session, "skill.toggle")
    assert len(rows) == 1
    assert rows[0].detail == {"enabled": False}


async def test_publish_create_is_audited(client, db_session):
    project = Project(tenant_id=DEFAULT_TENANT_ID, title="审计项目")
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=DEFAULT_TENANT_ID,
        project_id=project.id,
        platform="wechat",
        title="审计内容",
        current_version=1,
    )
    db_session.add(content)
    await db_session.flush()
    db_session.add(
        ContentVersion(
            tenant_id=DEFAULT_TENANT_ID,
            content_id=content.id,
            version=1,
            title="审计内容",
            body="正文内容足够长，用于通过发布任务创建。" * 10,
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/publish/jobs",
        json={"content_id": str(content.id), "platform": "wechat"},
    )
    assert resp.status_code == 201
    rows = await _audits(db_session, "publish.create")
    assert len(rows) == 1
    assert rows[0].detail == {"platform": "wechat"}


async def test_media_create_is_audited(client, db_session, tmp_path, monkeypatch):
    from tests.test_media_api import _setup, _teardown

    _setup(tmp_path, monkeypatch)
    try:
        project = (
            await client.post("/api/projects", json={"title": "媒体审计"})
        ).json()
        resp = await client.post(
            f"/api/projects/{project['id']}/media/short-video",
            json={"topic": "AI 周报", "duration_seconds": 8},
        )
        assert resp.status_code == 202, resp.text
        rows = await _audits(db_session, "media.create")
        assert len(rows) == 1
        assert rows[0].resource_id == resp.json()["id"]
        assert rows[0].detail == {"skill_id": "short-video"}
    finally:
        _teardown(monkeypatch)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_audit_events.py -q`
Expected: FAIL，以上动作均未写审计。

- [ ] **Step 3: 账号接口埋点**

`backend/app/api/accounts.py` 补充 import：

```python
from fastapi import APIRouter, Depends, Request, Response

from app.services.audit import record
```

`create_account` 增加 `request: Request` 参数；在 `session.add(account)` 之后先 `await session.flush()` 拿到 id，再插入审计、最后 `await session.commit()`：

```python
    session.add(account)
    await session.flush()
    await record(
        session,
        tenant_id=user.tenant_id,
        action="account.create",
        actor=user,
        resource_type="account",
        resource_id=str(account.id),
        detail={"platform": account.platform},
        request=request,
    )
```

`update_account` 增加 `request: Request`，在 `await session.commit()` 之前插入：

```python
    await record(
        session,
        tenant_id=user.tenant_id,
        action="account.update",
        actor=user,
        resource_type="account",
        resource_id=str(account.id),
        request=request,
    )
```

`revoke_account` 增加 `request: Request`，在 `await session.commit()` 之前插入：

```python
        await record(
            session,
            tenant_id=user.tenant_id,
            action="account.revoke",
            actor=user,
            resource_type="account",
            resource_id=str(account.id),
            request=request,
        )
```

- [ ] **Step 4: 提供商接口埋点**

`backend/app/api/settings.py` 补充 import：

```python
from fastapi import APIRouter, Depends, Request, Response

from app.services.audit import record
```

`create_provider` 增加 `request: Request`，在 `session.add(provider)` 之后先 `await session.flush()`，再插入审计、最后 `await session.commit()`：

```python
    session.add(provider)
    await session.flush()
    await record(
        session,
        tenant_id=user.tenant_id,
        action="provider.create",
        actor=user,
        resource_type="provider",
        resource_id=str(provider.id),
        detail={"name": provider.name},
        request=request,
    )
```

`delete_provider` 增加 `request: Request`，在 `await session.delete(provider)` 之前插入：

```python
    await record(
        session,
        tenant_id=user.tenant_id,
        action="provider.delete",
        actor=user,
        resource_type="provider",
        resource_id=str(provider.id),
        request=request,
    )
```

- [ ] **Step 5: 技能开关埋点**

`backend/app/api/skills.py` 补充 import：

```python
from fastapi import APIRouter, Depends, Request

from app.services.audit import record
```

`toggle_skill` 增加 `request: Request` 参数。无论是首次写入还是已有 setting 的分支，都在每次 `await session.commit()` 之前插入同一段审计：

```python
    await record(
        session,
        tenant_id=user.tenant_id,
        action="skill.toggle",
        actor=user,
        resource_type="skill",
        resource_id=skill_id,
        detail={"enabled": payload.enabled},
        request=request,
    )
```

即：`try` 内的第一次 `await session.commit()` 前插入一次；`except IntegrityError` 分支在第二次 `await session.commit()` 前再插入一次。

- [ ] **Step 6: 发布接口埋点**

`backend/app/api/publish.py` 补充 import：

```python
from fastapi import APIRouter, Depends, Query, Request, Response

from app.services.audit import record, record_best_effort
```

`create_job` 增加 `request: Request`，在 `await session.commit()` 之前插入：

```python
        await record(
            session,
            tenant_id=user.tenant_id,
            action="publish.create",
            actor=user,
            resource_type="publish_job",
            resource_id=str(job.id),
            detail={"platform": job.platform},
            request=request,
        )
```

`execute_publish` 增加 `request: Request`，在 `return _out(job)` 之前插入：

```python
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="publish.execute",
        actor=user,
        resource_type="publish_job",
        resource_id=str(job.id),
        request=request,
    )
```

`export_publish` 增加 `request: Request`，在 `return _out(job)` 前插入：

```python
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="publish.export",
        actor=user,
        resource_type="publish_job",
        resource_id=str(job.id),
        detail={"platform": job.platform},
        request=request,
    )
```

`schedule_job` 增加 `request: Request`，在 `return _out(job)` 前插入：

```python
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="publish.schedule",
        actor=user,
        resource_type="publish_job",
        resource_id=str(job.id),
        detail={
            "scheduled_at": (
                payload.scheduled_at.isoformat()
                if payload.scheduled_at is not None
                else ""
            )
        },
        request=request,
    )
```

`act_on_job` 增加 `request: Request`，在 `return _out(job)` 前插入：

```python
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="publish.action",
        actor=user,
        resource_type="publish_job",
        resource_id=str(job.id),
        detail={"action": payload.action},
        request=request,
    )
```

- [ ] **Step 7: 媒体任务埋点**

`backend/app/api/projects.py` 补充 import：

```python
from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile

from app.services.audit import record_best_effort
```

`create_project_media` 增加 `request: Request` 参数，在 `await _enqueue_media_job(queue, session, job)` 之后插入：

```python
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="media.create",
        actor=user,
        resource_type="media_job",
        resource_id=str(job.id),
        detail={"skill_id": skill_id},
        request=request,
    )
```

`retry_project_media_job` 增加 `request: Request` 参数，在 `await _enqueue_media_job(queue, session, job)` 之后插入：

```python
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="media.retry",
        actor=user,
        resource_type="media_job",
        resource_id=str(job.id),
        request=request,
    )
```

- [ ] **Step 8: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_audit_events.py tests/test_audit.py -q`
Expected: PASS。

- [ ] **Step 9: 提交**

```bash
git add backend/app/api/accounts.py backend/app/api/settings.py backend/app/api/skills.py backend/app/api/publish.py backend/app/api/projects.py backend/tests/test_audit_events.py
git commit -m "feat: record audit events for tenant business actions"
```

### Task 12: 双租户隔离自动化测试

**Files:**
- Create: `backend/tests/test_isolation.py`

- [ ] **Step 1: 写隔离测试**

创建 `backend/tests/test_isolation.py`：

```python
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models.analytics import AnalyticsSnapshot
from app.models.base import DEFAULT_TENANT_ID
from app.models.media import MediaJob
from app.models.tenant import Tenant
from app.models.user import User
from app.security import create_token, hash_password
from app.seed import ensure_admin

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


@asynccontextmanager
async def _two_tenant_client(db_session):
    await ensure_admin(db_session)
    if await db_session.get(Tenant, OTHER_TENANT) is None:
        db_session.add(
            Tenant(
                id=OTHER_TENANT,
                name="租户 B",
                slug="tenant-b",
                status="active",
            )
        )
    other_owner = (
        await db_session.execute(
            select(User).where(User.email == "owner-b@local")
        )
    ).scalar_one_or_none()
    if other_owner is None:
        other_owner = User(
            tenant_id=OTHER_TENANT,
            email="owner-b@local",
            password_hash=hash_password("password123"),
            display_name="租户 B 所有者",
            role="owner",
            is_active=True,
            must_change_password=False,
            is_platform_admin=False,
            token_version=0,
        )
        db_session.add(other_owner)
    await db_session.commit()
    admin = (
        await db_session.execute(
            select(User).where(User.email == get_settings().admin_email)
        )
    ).scalar_one()
    headers_a = {
        "Authorization": "Bearer "
        + create_token(admin.email, admin.tenant_id, admin.token_version)
    }
    headers_b = {
        "Authorization": "Bearer "
        + create_token(
            other_owner.email, other_owner.tenant_id, other_owner.token_version
        )
    }
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, headers_a, headers_b
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_tenant_isolation_across_modules(db_session):
    async with _two_tenant_client(db_session) as (client, a, b):
        project = (
            await client.post(
                "/api/projects", headers=a, json={"title": "租户 A 项目"}
            )
        ).json()
        content = (
            await client.post(
                f"/api/projects/{project['id']}/contents",
                headers=a,
                json={
                    "platform": "wechat",
                    "title": "租户 A 内容",
                    "body": "租户 A 的正文内容。" * 20,
                },
            )
        ).json()
        await client.post(
            f"/api/contents/{content['id']}/versions",
            headers=a,
            json={"title": "租户 A 版本 2", "body": "版本 2 正文。" * 20},
        )
        profile = (
            await client.post(
                "/api/profiles", headers=a, json={"name": "租户 A 画像"}
            )
        ).json()
        memory = (
            await client.post(
                f"/api/profiles/{profile['id']}/memories",
                headers=a,
                json={"content": "租户 A 的记忆"},
            )
        ).json()
        account = (
            await client.post(
                "/api/accounts",
                headers=a,
                json={
                    "platform": "wechat",
                    "name": "租户 A 账号",
                    "credential": {"app_id": "a-id", "app_secret": "a-secret"},
                },
            )
        ).json()
        job = (
            await client.post(
                "/api/publish/jobs",
                headers=a,
                json={"content_id": content["id"], "platform": "wechat"},
            )
        ).json()
        media = MediaJob(
            tenant_id=DEFAULT_TENANT_ID,
            project_id=uuid.UUID(project["id"]),
            skill_id="short-video",
            status="queued",
            payload={"topic": "隔离测试"},
        )
        db_session.add(media)
        db_session.add(
            AnalyticsSnapshot(
                tenant_id=DEFAULT_TENANT_ID,
                content_id=uuid.UUID(content["id"]),
                platform="wechat",
                captured_at=date.today(),
                views=100,
            )
        )
        await db_session.commit()
        media_id = str(media.id)

        assert (
            await client.get(f"/api/projects/{project['id']}", headers=b)
        ).status_code == 404
        assert (
            await client.get(
                f"/api/projects/{project['id']}/contents", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/contents/{content['id']}/versions", headers=b
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/api/contents/{content['id']}/versions",
                headers=b,
                json={"title": "越权", "body": "越权"},
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/projects/{project['id']}/files", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/projects/{project['id']}/media", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/projects/{project['id']}/media/jobs/{media_id}",
                headers=b,
            )
        ).status_code == 404
        assert (
            await client.get(f"/api/profiles/{profile['id']}", headers=b)
        ).status_code == 404
        assert (
            await client.get(
                f"/api/profiles/{profile['id']}/memories", headers=b
            )
        ).status_code == 404
        assert (
            await client.delete(
                f"/api/profiles/{profile['id']}/memories/{memory['id']}",
                headers=b,
            )
        ).status_code == 404
        assert (
            await client.patch(
                f"/api/accounts/{account['id']}",
                headers=b,
                json={"name": "越权改名"},
            )
        ).status_code == 404
        assert (
            await client.delete(
                f"/api/accounts/{account['id']}", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(f"/api/publish/jobs/{job['id']}", headers=b)
        ).status_code == 404
        assert (
            await client.post(
                f"/api/publish/jobs/{job['id']}/precheck", headers=b
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/api/publish/jobs/{job['id']}/execute", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(
                "/api/analytics/overview",
                headers=b,
                params={"project_id": project["id"]},
            )
        ).status_code == 404

        projects_b = (await client.get("/api/projects", headers=b)).json()
        assert projects_b["items"] == []
        profiles_b = (await client.get("/api/profiles", headers=b)).json()
        assert profiles_b["items"] == []
        accounts_b = (await client.get("/api/accounts", headers=b)).json()
        assert accounts_b == []
        jobs_b = (await client.get("/api/publish/jobs", headers=b)).json()
        assert jobs_b["total"] == 0

        now = date.today()
        calendar = await client.get(
            "/api/calendar",
            headers=b,
            params={
                "start": f"{now.isoformat()}T00:00:00Z",
                "end": f"{(now + timedelta(days=7)).isoformat()}T00:00:00Z",
            },
        )
        assert calendar.status_code == 200
        assert calendar.json()["items"] == []

        own = await client.post(
            "/api/projects", headers=b, json={"title": "租户 B 自己的项目"}
        )
        assert own.status_code == 201
        assert own.json()["title"] == "租户 B 自己的项目"
```

- [ ] **Step 2: 运行测试**

Run: `cd backend && uv run pytest tests/test_isolation.py -q`
Expected: PASS。若某个模块读取未返回 404，说明该查询漏了 `tenant_id` 条件，先修业务代码再重跑。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_isolation.py
git commit -m "test: cover cross-tenant isolation across business modules"
```

### Task 13: 前端认证上下文与角色导航

**Files:**
- Create: `web/src/auth.ts`
- Create: `web/src/auth-context.tsx`
- Create: `web/src/__tests__/auth.test.ts`
- Modify: `web/src/types.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/components/Layout.tsx`
- Modify: `web/src/main.tsx`
- Modify: `web/src/pages/Login.tsx`

- [ ] **Step 1: 写纯逻辑测试**

创建 `web/src/__tests__/auth.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import {
  atLeast,
  canWrite,
  defaultPath,
  isAdmin,
  isOwner,
  mustChangePath,
  visibleNavItems,
} from "../auth";

describe("role capabilities", () => {
  it("ranks the four fixed roles", () => {
    expect(atLeast("owner", "admin")).toBe(true);
    expect(atLeast("admin", "admin")).toBe(true);
    expect(atLeast("editor", "admin")).toBe(false);
    expect(atLeast("viewer", "editor")).toBe(false);
  });

  it("derives write and admin capabilities", () => {
    expect(canWrite("viewer")).toBe(false);
    expect(canWrite("editor")).toBe(true);
    expect(isAdmin("editor")).toBe(false);
    expect(isAdmin("admin")).toBe(true);
    expect(isOwner("admin")).toBe(false);
    expect(isOwner("owner")).toBe(true);
  });
});

describe("navigation filtering", () => {
  it("hides chat and admin tools from viewers", () => {
    const items = visibleNavItems({ role: "viewer", is_platform_admin: false });
    expect(items.map((item) => item.to)).toEqual([
      "/profiles",
      "/projects",
      "/calendar",
      "/publish",
      "/analytics",
    ]);
  });

  it("shows chat and accounts to editors but not admin tools", () => {
    const items = visibleNavItems({ role: "editor", is_platform_admin: false });
    expect(items.map((item) => item.to)).toContain("/chat");
    expect(items.map((item) => item.to)).toContain("/accounts");
    expect(items.map((item) => item.to)).not.toContain("/settings");
    expect(items.map((item) => item.to)).not.toContain("/team");
  });

  it("shows admin tools to admins and owner-only tools to owners", () => {
    const admin = visibleNavItems({ role: "admin", is_platform_admin: false });
    expect(admin.map((item) => item.to)).toContain("/audit");
    expect(admin.map((item) => item.to)).not.toContain("/tenants");
    const owner = visibleNavItems({ role: "owner", is_platform_admin: false });
    expect(owner.map((item) => item.to)).toContain("/team");
    expect(owner.map((item) => item.to)).not.toContain("/tenants");
  });

  it("shows tenants only to platform admins", () => {
    const platform = visibleNavItems({ role: "owner", is_platform_admin: true });
    expect(platform.map((item) => item.to)).toContain("/tenants");
    const viewerPlatform = visibleNavItems({
      role: "viewer",
      is_platform_admin: true,
    });
    expect(viewerPlatform.map((item) => item.to)).toContain("/tenants");
  });
});

describe("post-login routing", () => {
  it("forces first-login password change", () => {
    expect(
      mustChangePath({ must_change_password: true }, "/projects"),
    ).toBe("/change-password");
    expect(
      mustChangePath({ must_change_password: true }, "/change-password"),
    ).toBeNull();
    expect(
      mustChangePath({ must_change_password: false }, "/projects"),
    ).toBeNull();
  });

  it("sends viewers to projects and writers to chat", () => {
    expect(defaultPath({ role: "viewer" })).toBe("/projects");
    expect(defaultPath({ role: "editor" })).toBe("/chat");
    expect(defaultPath({ role: "owner" })).toBe("/chat");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm --prefix web run test -- --run src/__tests__/auth.test.ts`
Expected: FAIL，模块 `../auth` 不存在。

- [ ] **Step 3: 实现纯逻辑模块**

创建 `web/src/auth.ts`：

```ts
export type Role = "viewer" | "editor" | "admin" | "owner";

export interface Me {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  tenant_id: string;
  tenant_name: string;
  is_platform_admin: boolean;
  must_change_password: boolean;
}

export const ROLE_RANK: Record<Role, number> = {
  viewer: 0,
  editor: 1,
  admin: 2,
  owner: 3,
};

export function atLeast(role: Role, minRole: Role): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[minRole];
}

export function canWrite(role: Role): boolean {
  return atLeast(role, "editor");
}

export function isAdmin(role: Role): boolean {
  return atLeast(role, "admin");
}

export function isOwner(role: Role): boolean {
  return role === "owner";
}

export interface NavItem {
  to: string;
  label: string;
  minRole: Role;
  platformOnly?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/chat", label: "助手", minRole: "editor" },
  { to: "/profiles", label: "账号画像", minRole: "viewer" },
  { to: "/projects", label: "项目", minRole: "viewer" },
  { to: "/accounts", label: "发布账号", minRole: "editor" },
  { to: "/calendar", label: "日历", minRole: "viewer" },
  { to: "/publish", label: "发布", minRole: "viewer" },
  { to: "/analytics", label: "数据", minRole: "viewer" },
  { to: "/settings", label: "设置", minRole: "admin" },
  { to: "/team", label: "团队", minRole: "admin" },
  { to: "/audit", label: "审计", minRole: "admin" },
  { to: "/tenants", label: "租户", minRole: "viewer", platformOnly: true },
];

export function visibleNavItems(
  me: Pick<Me, "role" | "is_platform_admin">,
): NavItem[] {
  return NAV_ITEMS.filter((item) =>
    item.platformOnly
      ? me.is_platform_admin
      : atLeast(me.role, item.minRole),
  );
}

export function mustChangePath(
  me: Pick<Me, "must_change_password">,
  pathname: string,
): string | null {
  if (me.must_change_password && pathname !== "/change-password") {
    return "/change-password";
  }
  return null;
}

export function defaultPath(me: Pick<Me, "role">): string {
  return canWrite(me.role) ? "/chat" : "/projects";
}
```

- [ ] **Step 4: 增加 Me 类型**

`web/src/types.ts` 顶部追加：

```ts
export type { Me } from "./auth";
```

- [ ] **Step 5: 实现 AuthProvider**

创建 `web/src/auth-context.tsx`（不要命名为 `auth.tsx`，否则和 `auth.ts` 在 `./auth` 解析时冲突）：

```tsx
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useContext,
  type ReactNode,
} from "react";
import { Navigate, useLocation } from "react-router-dom";
import { ApiError, api, getToken } from "./client";
import {
  atLeast,
  canWrite,
  isAdmin,
  isOwner,
  type Me,
  type Role,
} from "./auth";

interface AuthState {
  user: Me | null;
  role: Role | null;
  loading: boolean;
  error: unknown;
  canWrite: boolean;
  isAdmin: boolean;
  isOwner: boolean;
  isPlatformAdmin: boolean;
  refresh: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const token = getToken();
  const query = useQuery({
    queryKey: ["auth", "me"],
    queryFn: () => api<Me>("/auth/me"),
    enabled: Boolean(token),
    retry: false,
  });
  const user = query.data ?? null;
  return (
    <AuthContext.Provider
      value={{
        user,
        role: user?.role ?? null,
        loading: Boolean(token) && query.isLoading,
        error: query.error,
        canWrite: user ? canWrite(user.role) : false,
        isAdmin: user ? isAdmin(user.role) : false,
        isOwner: user ? isOwner(user.role) : false,
        isPlatformAdmin: Boolean(user?.is_platform_admin),
        refresh: () => {
          void queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
        },
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth 必须在 AuthProvider 内使用");
  }
  return value;
}

export function RequireRole({
  minRole,
  platformAdmin = false,
  children,
}: {
  minRole?: Role;
  platformAdmin?: boolean;
  children: ReactNode;
}) {
  const { role, isPlatformAdmin } = useAuth();
  const roleOk =
    minRole === undefined || (role !== null && atLeast(role, minRole));
  if (!roleOk || (platformAdmin && !isPlatformAdmin)) {
    return <p className="text-sm text-red-400">权限不足，请联系租户管理员。</p>;
  }
  return <>{children}</>;
}

export function ProtectedLayout() {
  const location = useLocation();
  const { user, loading, error } = useAuth();
  const token = getToken();

  if (!token) return <Navigate to="/login" replace />;
  if (loading) {
    return <p className="p-8 text-sm text-slate-400">正在校验登录状态…</p>;
  }
  if (
    error instanceof ApiError &&
    (error.code === "tenant_suspended" || error.code === "account_disabled")
  ) {
    return (
      <main className="mx-auto mt-24 max-w-md rounded-xl border border-red-900 bg-slate-900 p-8">
        <h1 className="text-lg font-semibold text-red-400">无法继续访问</h1>
        <p className="mt-2 text-sm text-slate-300">{error.message}</p>
        <button
          type="button"
          className="mt-4 rounded bg-indigo-600 px-4 py-2 text-sm"
          onClick={() => {
            localStorage.removeItem("nm_token");
            window.location.assign("/login");
          }}
        >
          重新登录
        </button>
      </main>
    );
  }
  if (error instanceof ApiError && error.status === 401) {
    return <Navigate to="/login" replace />;
  }
  if (user) {
    const forced = user.must_change_password && location.pathname !== "/change-password";
    if (forced) return <Navigate to="/change-password" replace />;
  }
  return <Outlet />;
}
```

在文件顶部补充 `Outlet` import：

```tsx
import { Navigate, Outlet, useLocation } from "react-router-dom";
```

`localStorage.removeItem("nm_token")` 是为了同时清理客户端 token；也可改用 `setToken(null)`，实现时统一用：

```tsx
import { ApiError, api, getToken, setToken } from "./client";
```

按钮内改为：

```tsx
            setToken(null);
            location.assign("/login");
```

- [ ] **Step 6: 接入 App 与 main**

`web/src/main.tsx` 全量替换为（用 `AuthProvider` 包裹 `App`）：

```tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./auth-context";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
```

`web/src/App.tsx` 全量替换为：

```tsx
import { Navigate, Route, Routes } from "react-router-dom";
import { ProtectedLayout, RequireRole, useAuth } from "./auth-context";
import { defaultPath } from "./auth";
import { getToken } from "./client";
import Layout from "./components/Layout";
import Accounts from "./pages/Accounts";
import Analytics from "./pages/Analytics";
import Calendar from "./pages/Calendar";
import Chat from "./pages/Chat";
import Login from "./pages/Login";
import Profiles from "./pages/Profiles";
import ProjectDetail from "./pages/ProjectDetail";
import Projects from "./pages/Projects";
import Publish from "./pages/Publish";
import Settings from "./pages/Settings";

function HomeRedirect() {
  const { user } = useAuth();
  if (!user) return <Navigate to="/login" replace />;
  return <Navigate to={defaultPath(user)} replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<ProtectedLayout />}>
        <Route element={<Layout />}>
          <Route index element={<HomeRedirect />} />
          <Route
            path="/chat"
            element={
              <RequireRole minRole="editor">
                <Chat />
              </RequireRole>
            }
          />
          <Route path="/profiles" element={<Profiles />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/:id" element={<ProjectDetail />} />
          <Route path="/accounts" element={<Accounts />} />
          <Route path="/calendar" element={<Calendar />} />
          <Route path="/publish" element={<Publish />} />
          <Route path="/analytics" element={<Analytics />} />
          <Route
            path="/settings"
            element={
              <RequireRole minRole="admin">
                <Settings />
              </RequireRole>
            }
          />
        </Route>
      </Route>
      <Route path="*" element={<Navigate to={getToken() ? "/" : "/login"} replace />} />
    </Routes>
  );
}
```

- [ ] **Step 7: 导航与登录跳转改用角色**

`web/src/components/Layout.tsx` 替换 `NAV_ITEMS` 常量与渲染来源：

```tsx
import { visibleNavItems } from "../auth";
import { useAuth } from "../auth-context";
```

删除文件内的 `NAV_ITEMS` 定义，组件内增加：

```tsx
  const { user } = useAuth();
  const navItems = user
    ? visibleNavItems(user)
    : [];
```

`NAV_ITEMS` 无需再导入；渲染改为 `{navItems.map((item) => ...)}`。品牌区域可追加租户名：

```tsx
          <span className="text-xs text-slate-500">
            {user?.tenant_name}
          </span>
```

`web/src/pages/Login.tsx` 的登录成功跳转改为：

```tsx
      navigate("/");
```

- [ ] **Step 8: 运行前端测试与构建**

Run: `npm --prefix web run test -- --run src/__tests__/auth.test.ts`
Expected: PASS。

Run: `npm --prefix web run build`
Expected: 构建成功。

- [ ] **Step 9: 提交**

```bash
git add web/src/auth.ts web/src/auth-context.tsx web/src/__tests__/auth.test.ts web/src/types.ts web/src/App.tsx web/src/components/Layout.tsx web/src/main.tsx web/src/pages/Login.tsx
git commit -m "feat: add role-aware frontend auth context"
```

### Task 14: 修改密码页面

**Files:**
- Create: `web/src/changePassword.ts`
- Create: `web/src/__tests__/change-password.test.ts`
- Create: `web/src/pages/ChangePassword.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/components/Layout.tsx`

- [ ] **Step 1: 写纯逻辑测试**

创建 `web/src/__tests__/change-password.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import { validatePasswordChange } from "../changePassword";

describe("validatePasswordChange", () => {
  it("requires all fields", () => {
    expect(validatePasswordChange("", "new-password", "new-password")).toBe(
      "请输入当前密码",
    );
    expect(validatePasswordChange("old-password", "", "")).toBe(
      "新密码至少 8 位",
    );
    expect(validatePasswordChange("old-password", "new-password", "")).toBe(
      "两次输入的新密码不一致",
    );
  });

  it("enforces length and rejects reuse", () => {
    expect(validatePasswordChange("old-password", "short", "short")).toBe(
      "新密码至少 8 位",
    );
    expect(
      validatePasswordChange("old-password", "x".repeat(129), "x".repeat(129)),
    ).toBe("新密码不能超过 128 位");
    expect(
      validatePasswordChange("same-password", "same-password", "same-password"),
    ).toBe("新密码不能与当前密码相同");
  });

  it("accepts a valid change", () => {
    expect(
      validatePasswordChange("old-password", "new-password", "new-password"),
    ).toBeNull();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm --prefix web run test -- --run src/__tests__/change-password.test.ts`
Expected: FAIL，模块 `../changePassword` 不存在。

- [ ] **Step 3: 实现校验函数**

创建 `web/src/changePassword.ts`：

```ts
export function validatePasswordChange(
  current: string,
  next: string,
  confirm: string,
): string | null {
  if (!current) return "请输入当前密码";
  if (next.length < 8) return "新密码至少 8 位";
  if (next.length > 128) return "新密码不能超过 128 位";
  if (next === current) return "新密码不能与当前密码相同";
  if (next !== confirm) return "两次输入的新密码不一致";
  return null;
}
```

- [ ] **Step 4: 实现页面**

创建 `web/src/pages/ChangePassword.tsx`：

```tsx
import { useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../auth-context";
import { validatePasswordChange } from "../changePassword";
import { api, setToken } from "../client";
import { errorText } from "../types";

export default function ChangePassword() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const invalid = validatePasswordChange(current, next, confirm);
    if (invalid) {
      setError(invalid);
      return;
    }
    setLoading(true);
    setError("");
    try {
      const data = await api<{ access_token: string }>(
        "/auth/change-password",
        {
          method: "POST",
          body: JSON.stringify({
            current_password: current,
            new_password: next,
          }),
        },
      );
      setToken(data.access_token);
      // 新 token 的 ver 已递增；刷新 me 后再跳转，避免被强制改密重定向回来。
      await queryClient.invalidateQueries({ queryKey: ["auth", "me"] });
      navigate("/", { replace: true });
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }

  function logout() {
    queryClient.clear();
    setToken(null);
    navigate("/login", { replace: true });
  }

  return (
    <main className="mx-auto mt-24 w-full max-w-md rounded-xl border border-slate-800 bg-slate-900 p-8">
      <h1 className="text-xl font-semibold">修改密码</h1>
      <p className="mt-1 text-sm text-slate-400">
        {user?.must_change_password
          ? "首次登录或密码被重置后，需要先设置新密码。"
          : "修改后其他设备上的登录状态会立即失效。"}
      </p>
      <form className="mt-6 space-y-3" onSubmit={submit}>
        <input
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
          aria-label="当前密码"
          required
          type="password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          placeholder="当前密码"
        />
        <input
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
          aria-label="新密码"
          required
          minLength={8}
          maxLength={128}
          type="password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          placeholder="新密码（8-128 位）"
        />
        <input
          className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2"
          aria-label="确认新密码"
          required
          minLength={8}
          maxLength={128}
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          placeholder="再次输入新密码"
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button
          className="w-full rounded bg-indigo-600 py-2 font-medium disabled:opacity-50"
          disabled={loading}
        >
          {loading ? "保存中…" : "保存新密码"}
        </button>
      </form>
      <button
        type="button"
        className="mt-4 text-sm text-slate-400 hover:text-slate-200"
        onClick={logout}
      >
        退出登录
      </button>
    </main>
  );
}
```

- [ ] **Step 5: 注册路由与入口**

`web/src/App.tsx` 顶部新增 import：

```tsx
import ChangePassword from "./pages/ChangePassword";
```

在 `<Route element={<ProtectedLayout />}>` 内、`<Route element={<Layout />}>` 之前新增（改密页不显示主导航）：

```tsx
        <Route path="/change-password" element={<ChangePassword />} />
```

`web/src/components/Layout.tsx` 的 react-router-dom import 增加 `Link`：

```tsx
import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
```

在「退出登录」按钮之前新增入口：

```tsx
          <Link
            className="text-sm text-slate-400 hover:text-slate-200"
            to="/change-password"
          >
            修改密码
          </Link>
```

- [ ] **Step 6: 运行测试与构建**

Run: `npm --prefix web run test -- --run src/__tests__/change-password.test.ts`
Expected: PASS。

Run: `npm --prefix web run build`
Expected: 构建成功。

- [ ] **Step 7: 提交**

```bash
git add web/src/changePassword.ts web/src/__tests__/change-password.test.ts web/src/pages/ChangePassword.tsx web/src/App.tsx web/src/components/Layout.tsx
git commit -m "feat: add forced password change page"
```

### Task 15: 团队成员管理页面

**Files:**
- Create: `web/src/team.ts`
- Create: `web/src/__tests__/team.test.ts`
- Create: `web/src/pages/Team.tsx`
- Modify: `web/src/types.ts`
- Modify: `web/src/App.tsx`

- [ ] **Step 1: 写纯逻辑测试**

创建 `web/src/__tests__/team.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import {
  canEditMember,
  canManageMembers,
  memberStatusLabel,
  roleLabel,
} from "../team";

describe("team helpers", () => {
  it("labels the four roles with a fallback", () => {
    expect(roleLabel("viewer")).toBe("只读");
    expect(roleLabel("editor")).toBe("编辑");
    expect(roleLabel("admin")).toBe("管理员");
    expect(roleLabel("owner")).toBe("所有者");
    expect(roleLabel("unknown")).toBe("unknown");
  });

  it("allows only owners to manage members and never themselves", () => {
    expect(canManageMembers("admin")).toBe(false);
    expect(canManageMembers("owner")).toBe(true);
    expect(
      canEditMember({ id: "u1", role: "owner" }, { id: "u1" }),
    ).toBe(false);
    expect(
      canEditMember({ id: "u1", role: "owner" }, { id: "u2" }),
    ).toBe(true);
    expect(
      canEditMember({ id: "u1", role: "admin" }, { id: "u2" }),
    ).toBe(false);
  });

  it("labels active state", () => {
    expect(memberStatusLabel(true)).toBe("启用");
    expect(memberStatusLabel(false)).toBe("已停用");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm --prefix web run test -- --run src/__tests__/team.test.ts`
Expected: FAIL，模块 `../team` 不存在。

- [ ] **Step 3: 实现纯逻辑模块**

创建 `web/src/team.ts`：

```ts
import type { Role } from "./auth";

export const ROLE_LABELS: Record<Role, string> = {
  viewer: "只读",
  editor: "编辑",
  admin: "管理员",
  owner: "所有者",
};

export function roleLabel(role: string): string {
  return ROLE_LABELS[role as Role] ?? role;
}

export function canManageMembers(role: Role): boolean {
  return role === "owner";
}

export function canEditMember(
  me: { id: string; role: Role },
  member: { id: string },
): boolean {
  return canManageMembers(me.role) && me.id !== member.id;
}

export function memberStatusLabel(isActive: boolean): string {
  return isActive ? "启用" : "已停用";
}
```

- [ ] **Step 4: 增加成员类型**

`web/src/types.ts` 在 `ChatOut` 之前追加：

```ts
export interface Member {
  id: string;
  email: string;
  display_name: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

export interface MemberCreated extends Member {
  initial_password: string;
}
```

- [ ] **Step 5: 实现团队页面**

创建 `web/src/pages/Team.tsx`：

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import type { Role } from "../auth";
import { useAuth } from "../auth-context";
import { api } from "../client";
import {
  canEditMember,
  canManageMembers,
  memberStatusLabel,
  roleLabel,
} from "../team";
import { errorText, type Member, type MemberCreated } from "../types";

const ROLE_VALUES: Role[] = ["viewer", "editor", "admin", "owner"];

export default function Team() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [form, setForm] = useState({
    email: "",
    display_name: "",
    role: "editor" as Role,
  });
  const [created, setCreated] = useState<MemberCreated | null>(null);
  const [message, setMessage] = useState<{
    kind: "ok" | "error";
    text: string;
  } | null>(null);

  const members = useQuery({
    queryKey: ["members"],
    queryFn: () => api<Member[]>("/members"),
  });

  const create = useMutation({
    mutationFn: (payload: {
      email: string;
      display_name: string;
      role: Role;
    }) =>
      api<MemberCreated>("/members", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: (member) => {
      setCreated(member);
      setForm({ email: "", display_name: "", role: "editor" });
      setMessage({ kind: "ok", text: `已创建成员 ${member.email}` });
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: () => setCreated(null),
  });

  const update = useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: string;
      payload: { role?: Role; is_active?: boolean };
    }) =>
      api<Member>(`/members/${id}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      }),
    onSuccess: () => {
      setMessage({ kind: "ok", text: "成员已更新" });
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (err) =>
      setMessage({ kind: "error", text: errorText(err) }),
  });

  const disable = useMutation({
    mutationFn: (id: string) =>
      api<void>(`/members/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      setMessage({ kind: "ok", text: "成员已停用" });
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (err) =>
      setMessage({ kind: "error", text: errorText(err) }),
  });

  const canManage = user ? canManageMembers(user.role) : false;

  function submit(event: FormEvent) {
    event.preventDefault();
    const email = form.email.trim().toLowerCase();
    const displayName = form.display_name.trim();
    if (!email || !displayName) {
      setMessage({ kind: "error", text: "请填写邮箱与显示名" });
      return;
    }
    setCreated(null);
    setMessage(null);
    create.mutate({ email, display_name: displayName, role: form.role });
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">团队</h1>
        <p className="mt-1 text-sm text-slate-400">
          管理当前租户的成员与角色。管理员可查看，所有者可创建与调整。
        </p>
      </div>

      {!canManage && (
        <p className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
          只有租户所有者可以创建成员或调整角色。
        </p>
      )}

      {canManage && (
        <form
          className="grid gap-3 rounded-xl border border-slate-800 bg-slate-900 p-5 md:grid-cols-[1fr_1fr_140px_auto]"
          onSubmit={submit}
        >
          <label className="space-y-1 text-sm text-slate-400">
            <span>邮箱 *</span>
            <input
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              maxLength={255}
              required
              type="email"
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
              placeholder="member@example.com"
            />
          </label>
          <label className="space-y-1 text-sm text-slate-400">
            <span>显示名 *</span>
            <input
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              maxLength={100}
              required
              value={form.display_name}
              onChange={(e) =>
                setForm({ ...form, display_name: e.target.value })
              }
              placeholder="例如：运营小林"
            />
          </label>
          <label className="space-y-1 text-sm text-slate-400">
            <span>角色</span>
            <select
              className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
              value={form.role}
              onChange={(e) =>
                setForm({ ...form, role: e.target.value as Role })
              }
            >
              {ROLE_VALUES.map((role) => (
                <option key={role} value={role}>
                  {roleLabel(role)}
                </option>
              ))}
            </select>
          </label>
          <div className="flex items-end">
            <button
              className="w-full rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50 md:w-auto"
              disabled={create.isPending}
            >
              {create.isPending ? "创建中…" : "创建成员"}
            </button>
          </div>
        </form>
      )}

      {created && (
        <div className="rounded-xl border border-emerald-900 bg-slate-900 p-4 text-sm">
          <p className="text-emerald-400">
            已创建 {created.email}，请立即把一次性临时密码交给成员：
          </p>
          <code className="mt-2 block break-all rounded bg-slate-950 px-3 py-2 font-mono text-slate-100">
            {created.initial_password}
          </code>
          <p className="mt-2 text-xs text-slate-500">
            临时密码只显示一次；成员首次登录后必须修改密码。
          </p>
        </div>
      )}

      {message && (
        <p
          className={
            message.kind === "error"
              ? "text-sm text-red-400"
              : "text-sm text-emerald-400"
          }
        >
          {message.text}
        </p>
      )}
      {create.isError && (
        <p className="text-sm text-red-400">{errorText(create.error)}</p>
      )}

      <section className="space-y-2">
        <h2 className="font-medium">成员列表</h2>
        {members.isLoading && (
          <p className="text-sm text-slate-400">加载中…</p>
        )}
        {members.isError && (
          <p className="text-sm text-red-400">{errorText(members.error)}</p>
        )}
        {members.data?.length === 0 && (
          <p className="text-sm text-slate-400">还没有其他成员。</p>
        )}
        {members.data?.map((member) => {
          const isSelf = member.id === user?.id;
          const editable = user ? canEditMember(user, member) : false;
          const pending =
            (update.isPending && update.variables?.id === member.id) ||
            (disable.isPending && disable.variables === member.id);
          return (
            <article
              key={member.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900 p-4"
            >
              <div>
                <h3 className="font-medium">
                  {member.display_name}
                  {isSelf && (
                    <span className="ml-2 text-xs text-slate-500">（我）</span>
                  )}
                </h3>
                <p className="mt-1 text-xs text-slate-500">
                  {member.email} · 加入于 {member.created_at.slice(0, 10)}
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-sm">
                <span
                  className={
                    member.is_active
                      ? "text-xs text-emerald-400"
                      : "text-xs text-slate-500"
                  }
                >
                  {memberStatusLabel(member.is_active)}
                </span>
                <select
                  aria-label={`角色 ${member.email}`}
                  className="rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-slate-100 disabled:opacity-50"
                  value={member.role}
                  disabled={!editable || pending}
                  onChange={(e) =>
                    update.mutate({
                      id: member.id,
                      payload: { role: e.target.value as Role },
                    })
                  }
                >
                  {ROLE_VALUES.map((role) => (
                    <option key={role} value={role}>
                      {roleLabel(role)}
                    </option>
                  ))}
                </select>
                {member.is_active ? (
                  <button
                    type="button"
                    className="rounded border border-red-900 px-3 py-1.5 text-red-400 hover:border-red-800 disabled:opacity-50"
                    disabled={!editable || pending}
                    onClick={() => disable.mutate(member.id)}
                  >
                    停用
                  </button>
                ) : (
                  <button
                    type="button"
                    className="rounded border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-600 disabled:opacity-50"
                    disabled={!editable || pending}
                    onClick={() =>
                      update.mutate({
                        id: member.id,
                        payload: { is_active: true },
                      })
                    }
                  >
                    启用
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </section>
    </div>
  );
}
```

- [ ] **Step 6: 注册路由**

`web/src/App.tsx` 顶部新增：

```tsx
import Team from "./pages/Team";
```

在 `/settings` 路由之后追加：

```tsx
          <Route
            path="/team"
            element={
              <RequireRole minRole="admin">
                <Team />
              </RequireRole>
            }
          />
```

- [ ] **Step 7: 运行测试与构建**

Run: `npm --prefix web run test -- --run src/__tests__/team.test.ts`
Expected: PASS。

Run: `npm --prefix web run build`
Expected: 构建成功。

- [ ] **Step 8: 提交**

```bash
git add web/src/team.ts web/src/__tests__/team.test.ts web/src/pages/Team.tsx web/src/types.ts web/src/App.tsx
git commit -m "feat: add tenant member management page"
```

### Task 16: 审计查询页面

**Files:**
- Create: `web/src/audit.ts`
- Create: `web/src/__tests__/audit.test.ts`
- Create: `web/src/pages/Audit.tsx`
- Modify: `web/src/types.ts`
- Modify: `web/src/App.tsx`

- [ ] **Step 1: 写纯逻辑测试**

创建 `web/src/__tests__/audit.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import {
  auditActionLabel,
  auditStatusLabel,
  buildAuditQuery,
} from "../audit";

describe("buildAuditQuery", () => {
  it("omits empty filters and keeps pagination", () => {
    expect(
      buildAuditQuery(
        { action: "auth.login", status: "", actor_email: "  ", from: "", to: "" },
        20,
        40,
      ),
    ).toBe("action=auth.login&limit=20&offset=40");
  });

  it("sends the time range as UTC ISO strings", () => {
    const query = buildAuditQuery({
      action: "",
      status: "failure",
      actor_email: "a@local",
      from: "2026-11-01T08:00",
      to: "2026-11-02T08:00",
    });
    const params = new URLSearchParams(query);
    expect(params.get("status")).toBe("failure");
    expect(params.get("actor_email")).toBe("a@local");
    expect(params.get("from")).toBe(
      new Date("2026-11-01T08:00").toISOString(),
    );
    expect(params.get("to")).toBe(
      new Date("2026-11-02T08:00").toISOString(),
    );
    expect(params.get("limit")).toBe("50");
    expect(params.get("offset")).toBe("0");
  });
});

describe("audit labels", () => {
  it("labels known actions and falls back to the raw code", () => {
    expect(auditActionLabel("auth.login")).toBe("登录成功");
    expect(auditActionLabel("publish.auto_execute")).toBe("定时自动发布");
    expect(auditActionLabel("custom.action")).toBe("custom.action");
  });

  it("labels status", () => {
    expect(auditStatusLabel("success")).toBe("成功");
    expect(auditStatusLabel("failure")).toBe("失败");
    expect(auditStatusLabel("other")).toBe("other");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm --prefix web run test -- --run src/__tests__/audit.test.ts`
Expected: FAIL，模块 `../audit` 不存在。

- [ ] **Step 3: 实现纯逻辑模块**

创建 `web/src/audit.ts`：

```ts
export interface AuditFilters {
  action: string;
  status: string;
  actor_email: string;
  from: string;
  to: string;
}

export function buildAuditQuery(
  filters: AuditFilters,
  limit = 50,
  offset = 0,
): string {
  const params = new URLSearchParams();
  const action = filters.action.trim();
  const status = filters.status.trim();
  const actorEmail = filters.actor_email.trim();
  if (action) params.set("action", action);
  if (status) params.set("status", status);
  if (actorEmail) params.set("actor_email", actorEmail);
  if (filters.from) params.set("from", new Date(filters.from).toISOString());
  if (filters.to) params.set("to", new Date(filters.to).toISOString());
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  return params.toString();
}

export const AUDIT_ACTION_LABELS: Record<string, string> = {
  "auth.login": "登录成功",
  "auth.login_failed": "登录失败",
  "auth.change_password": "修改密码",
  "member.create": "创建成员",
  "member.update": "更新成员",
  "member.disable": "停用成员",
  "tenant.create": "创建租户",
  "tenant.update": "更新租户",
  "account.create": "创建发布账号",
  "account.update": "更新发布账号",
  "account.revoke": "停用发布账号",
  "provider.create": "创建模型提供商",
  "provider.delete": "删除模型提供商",
  "skill.toggle": "启停技能",
  "publish.create": "创建发布任务",
  "publish.execute": "执行发布",
  "publish.export": "导出素材包",
  "publish.schedule": "调整定时",
  "publish.action": "发布任务操作",
  "publish.auto_execute": "定时自动发布",
  "publish.auto_fail": "定时发布失败",
  "media.create": "创建媒体任务",
  "media.retry": "重试媒体任务",
};

export function auditActionLabel(action: string): string {
  return AUDIT_ACTION_LABELS[action] ?? action;
}

export const AUDIT_ACTION_OPTIONS = Object.keys(AUDIT_ACTION_LABELS);

export const AUDIT_STATUS_LABELS: Record<string, string> = {
  success: "成功",
  failure: "失败",
};

export function auditStatusLabel(status: string): string {
  return AUDIT_STATUS_LABELS[status] ?? status;
}
```

- [ ] **Step 4: 增加审计类型**

`web/src/types.ts` 在 `MediaJob` 之后追加：

```ts
export interface AuditLog {
  id: string;
  actor_email: string;
  action: string;
  resource_type: string;
  resource_id: string;
  status: string;
  ip: string;
  user_agent: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface AuditLogList {
  items: AuditLog[];
  total: number;
}
```

- [ ] **Step 5: 实现审计页面**

创建 `web/src/pages/Audit.tsx`：

```tsx
import { useQuery } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import {
  AUDIT_ACTION_OPTIONS,
  auditActionLabel,
  auditStatusLabel,
  buildAuditQuery,
  type AuditFilters,
} from "../audit";
import { api } from "../client";
import { errorText, type AuditLogList } from "../types";

const PAGE_SIZE = 50;

const EMPTY_FILTERS: AuditFilters = {
  action: "",
  status: "",
  actor_email: "",
  from: "",
  to: "",
};

export default function Audit() {
  const [filters, setFilters] = useState<AuditFilters>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<AuditFilters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);

  const logs = useQuery({
    queryKey: ["audit", applied, offset],
    queryFn: () =>
      api<AuditLogList>(
        `/audit-logs?${buildAuditQuery(applied, PAGE_SIZE, offset)}`,
      ),
  });

  function apply(event: FormEvent) {
    event.preventDefault();
    setApplied(filters);
    setOffset(0);
  }

  function reset() {
    setFilters(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
    setOffset(0);
  }

  const total = logs.data?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">审计</h1>
        <p className="mt-1 text-sm text-slate-400">
          查看当前租户的安全与业务关键事件。审计只追加，不提供修改或删除。
        </p>
      </div>

      <form
        className="grid gap-3 rounded-xl border border-slate-800 bg-slate-900 p-5 md:grid-cols-3"
        onSubmit={apply}
      >
        <label className="space-y-1 text-sm text-slate-400">
          <span>动作</span>
          <select
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            value={filters.action}
            onChange={(e) => setFilters({ ...filters, action: e.target.value })}
          >
            <option value="">全部</option>
            {AUDIT_ACTION_OPTIONS.map((action) => (
              <option key={action} value={action}>
                {auditActionLabel(action)}
              </option>
            ))}
          </select>
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>状态</span>
          <select
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            value={filters.status}
            onChange={(e) => setFilters({ ...filters, status: e.target.value })}
          >
            <option value="">全部</option>
            <option value="success">成功</option>
            <option value="failure">失败</option>
          </select>
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>操作人邮箱</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            value={filters.actor_email}
            onChange={(e) =>
              setFilters({ ...filters, actor_email: e.target.value })
            }
            placeholder="精确匹配"
          />
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>开始时间</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            type="datetime-local"
            value={filters.from}
            onChange={(e) => setFilters({ ...filters, from: e.target.value })}
          />
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>结束时间</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            type="datetime-local"
            value={filters.to}
            onChange={(e) => setFilters({ ...filters, to: e.target.value })}
          />
        </label>
        <div className="flex items-end gap-2">
          <button className="rounded bg-indigo-600 px-4 py-2 font-medium">
            查询
          </button>
          <button
            type="button"
            className="rounded border border-slate-700 px-4 py-2 text-slate-300"
            onClick={reset}
          >
            重置
          </button>
        </div>
      </form>

      {logs.isLoading && <p className="text-sm text-slate-400">加载中…</p>}
      {logs.isError && (
        <p className="text-sm text-red-400">{errorText(logs.error)}</p>
      )}
      {logs.isSuccess && logs.data.items.length === 0 && (
        <p className="text-sm text-slate-400">没有匹配的审计记录。</p>
      )}

      {logs.data && logs.data.items.length > 0 && (
        <section className="overflow-x-auto rounded-xl border border-slate-800">
          <table className="min-w-full text-left text-sm">
            <thead className="bg-slate-900 text-xs text-slate-500">
              <tr>
                <th className="px-4 py-3">时间</th>
                <th className="px-4 py-3">操作人</th>
                <th className="px-4 py-3">动作</th>
                <th className="px-4 py-3">状态</th>
                <th className="px-4 py-3">资源</th>
                <th className="px-4 py-3">详情</th>
              </tr>
            </thead>
            <tbody className="bg-slate-950">
              {logs.data.items.map((row) => (
                <tr key={row.id} className="border-t border-slate-800">
                  <td className="whitespace-nowrap px-4 py-3 text-slate-400">
                    {new Date(row.created_at).toLocaleString()}
                  </td>
                  <td className="px-4 py-3 text-slate-300">
                    {row.actor_email}
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-slate-200">
                      {auditActionLabel(row.action)}
                    </span>
                    <span className="mt-1 block font-mono text-xs text-slate-500">
                      {row.action}
                    </span>
                  </td>
                  <td
                    className={`px-4 py-3 ${
                      row.status === "success"
                        ? "text-emerald-400"
                        : "text-red-400"
                    }`}
                  >
                    {auditStatusLabel(row.status)}
                  </td>
                  <td className="px-4 py-3 text-slate-400">
                    {row.resource_type}
                    <span className="mt-1 block font-mono text-xs text-slate-500">
                      {row.resource_id || "—"}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {Object.keys(row.detail).length === 0 ? (
                      <span className="text-slate-500">—</span>
                    ) : (
                      <details>
                        <summary className="cursor-pointer text-indigo-400">
                          查看
                        </summary>
                        <pre className="mt-2 max-w-md overflow-x-auto rounded bg-slate-900 p-2 text-xs text-slate-300">
                          {JSON.stringify(row.detail, null, 2)}
                        </pre>
                      </details>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {logs.data && (
        <div className="flex items-center gap-3 text-sm text-slate-400">
          <button
            type="button"
            className="rounded border border-slate-700 px-3 py-1.5 disabled:opacity-50"
            disabled={!hasPrev}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            上一页
          </button>
          <span>
            共 {total} 条，当前 {total === 0 ? 0 : offset + 1}–
            {Math.min(offset + PAGE_SIZE, total)}
          </span>
          <button
            type="button"
            className="rounded border border-slate-700 px-3 py-1.5 disabled:opacity-50"
            disabled={!hasNext}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            下一页
          </button>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 6: 注册路由**

`web/src/App.tsx` 顶部新增：

```tsx
import Audit from "./pages/Audit";
```

在 `/team` 路由之后追加：

```tsx
          <Route
            path="/audit"
            element={
              <RequireRole minRole="admin">
                <Audit />
              </RequireRole>
            }
          />
```

- [ ] **Step 7: 运行测试与构建**

Run: `npm --prefix web run test -- --run src/__tests__/audit.test.ts`
Expected: PASS。

Run: `npm --prefix web run build`
Expected: 构建成功。

- [ ] **Step 8: 提交**

```bash
git add web/src/audit.ts web/src/__tests__/audit.test.ts web/src/pages/Audit.tsx web/src/types.ts web/src/App.tsx
git commit -m "feat: add tenant audit log page"
```

### Task 17: 平台租户管理页面

**Files:**
- Create: `web/src/tenants.ts`
- Create: `web/src/__tests__/tenants.test.ts`
- Create: `web/src/pages/Tenants.tsx`
- Modify: `web/src/types.ts`
- Modify: `web/src/App.tsx`

- [ ] **Step 1: 写纯逻辑测试**

创建 `web/src/__tests__/tenants.test.ts`：

```ts
import { describe, expect, it } from "vitest";
import {
  canEnableTenant,
  canSuspendTenant,
  tenantStatusLabel,
  validateTenantForm,
} from "../tenants";

const VALID_FORM = {
  name: "客户 A",
  slug: "customer-a",
  owner_email: "owner@example.com",
  owner_display_name: "客户管理员",
};

describe("validateTenantForm", () => {
  it("accepts a valid form", () => {
    expect(validateTenantForm(VALID_FORM)).toBeNull();
  });

  it("requires a name and display name", () => {
    expect(validateTenantForm({ ...VALID_FORM, name: "  " })).toBe(
      "请填写租户名称",
    );
    expect(
      validateTenantForm({ ...VALID_FORM, owner_display_name: " " }),
    ).toBe("请填写所有者显示名");
  });

  it("validates slug format", () => {
    expect(validateTenantForm({ ...VALID_FORM, slug: "A" })).toBe(
      "租户标识需为 2-50 位小写字母、数字或连字符",
    );
    expect(validateTenantForm({ ...VALID_FORM, slug: "customer_a" })).toBe(
      "租户标识需为 2-50 位小写字母、数字或连字符",
    );
  });

  it("validates the owner email", () => {
    expect(validateTenantForm({ ...VALID_FORM, owner_email: "owner" })).toBe(
      "请填写有效的所有者邮箱",
    );
    expect(
      validateTenantForm({ ...VALID_FORM, owner_email: "owner@example.com" }),
    ).toBeNull();
  });
});

describe("tenant status actions", () => {
  it("allows suspending only other active tenants", () => {
    expect(canSuspendTenant({ id: "t2", status: "active" }, "t1")).toBe(true);
    expect(canSuspendTenant({ id: "t1", status: "active" }, "t1")).toBe(false);
    expect(canSuspendTenant({ id: "t2", status: "suspended" }, "t1")).toBe(
      false,
    );
  });

  it("allows enabling only suspended tenants", () => {
    expect(canEnableTenant({ status: "suspended" })).toBe(true);
    expect(canEnableTenant({ status: "active" })).toBe(false);
  });

  it("labels tenant status", () => {
    expect(tenantStatusLabel("active")).toBe("启用");
    expect(tenantStatusLabel("suspended")).toBe("已停用");
    expect(tenantStatusLabel("other")).toBe("other");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `npm --prefix web run test -- --run src/__tests__/tenants.test.ts`
Expected: FAIL，模块 `../tenants` 不存在。

- [ ] **Step 3: 实现纯逻辑模块**

创建 `web/src/tenants.ts`：

```ts
export const TENANT_STATUS_LABELS: Record<string, string> = {
  active: "启用",
  suspended: "已停用",
};

export function tenantStatusLabel(status: string): string {
  return TENANT_STATUS_LABELS[status] ?? status;
}

export interface TenantForm {
  name: string;
  slug: string;
  owner_email: string;
  owner_display_name: string;
}

const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{1,49}$/;
const EMAIL_PATTERN = /^[^@\s]+@[^@\s]+$/;

export function validateTenantForm(form: TenantForm): string | null {
  if (!form.name.trim()) return "请填写租户名称";
  if (!SLUG_PATTERN.test(form.slug.trim())) {
    return "租户标识需为 2-50 位小写字母、数字或连字符";
  }
  if (!EMAIL_PATTERN.test(form.owner_email.trim())) {
    return "请填写有效的所有者邮箱";
  }
  if (!form.owner_display_name.trim()) return "请填写所有者显示名";
  return null;
}

export function canSuspendTenant(
  tenant: { id: string; status: string },
  currentTenantId: string,
): boolean {
  return tenant.status === "active" && tenant.id !== currentTenantId;
}

export function canEnableTenant(tenant: { status: string }): boolean {
  return tenant.status === "suspended";
}
```

- [ ] **Step 4: 增加租户类型**

`web/src/types.ts` 在 `MemberCreated` 之后追加：

```ts
export interface Tenant {
  id: string;
  name: string;
  slug: string;
  status: string;
  members_count: number;
  created_at: string;
}

export interface TenantCreated extends Tenant {
  owner: MemberCreated;
}
```

- [ ] **Step 5: 实现租户页面**

创建 `web/src/pages/Tenants.tsx`：

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { useAuth } from "../auth-context";
import { api } from "../client";
import {
  canEnableTenant,
  canSuspendTenant,
  tenantStatusLabel,
  validateTenantForm,
} from "../tenants";
import {
  errorText,
  type Tenant,
  type TenantCreated,
} from "../types";

const EMPTY_FORM = {
  name: "",
  slug: "",
  owner_email: "",
  owner_display_name: "",
};

export default function Tenants() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [form, setForm] = useState(EMPTY_FORM);
  const [created, setCreated] = useState<TenantCreated | null>(null);
  const [message, setMessage] = useState<{
    kind: "ok" | "error";
    text: string;
  } | null>(null);

  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api<Tenant[]>("/tenants"),
  });

  const create = useMutation({
    mutationFn: (payload: typeof EMPTY_FORM) =>
      api<TenantCreated>("/tenants", {
        method: "POST",
        body: JSON.stringify(payload),
      }),
    onSuccess: (tenant) => {
      setCreated(tenant);
      setForm(EMPTY_FORM);
      setMessage({ kind: "ok", text: `已创建租户 ${tenant.name}` });
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
    onError: () => setCreated(null),
  });

  const update = useMutation({
    mutationFn: ({ id, status }: { id: string; status: string }) =>
      api<Tenant>(`/tenants/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      }),
    onSuccess: () => {
      setMessage({ kind: "ok", text: "租户状态已更新" });
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
    onError: (err) =>
      setMessage({ kind: "error", text: errorText(err) }),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const payload = {
      name: form.name.trim(),
      slug: form.slug.trim(),
      owner_email: form.owner_email.trim().toLowerCase(),
      owner_display_name: form.owner_display_name.trim(),
    };
    const invalid = validateTenantForm(payload);
    if (invalid) {
      setMessage({ kind: "error", text: invalid });
      return;
    }
    setCreated(null);
    setMessage(null);
    create.mutate(payload);
  }

  function copyPassword() {
    if (!created) return;
    void navigator.clipboard.writeText(created.owner.initial_password).then(
      () => setMessage({ kind: "ok", text: "已复制临时密码" }),
      () => setMessage({ kind: "error", text: "复制失败，请手动选择" }),
    );
  }

  const currentTenantId = user?.tenant_id ?? "";

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">租户</h1>
        <p className="mt-1 text-sm text-slate-400">
          平台级租户管理。创建租户会同时开通所有者账号，并生成一次性临时密码。
        </p>
      </div>

      <form
        className="grid gap-3 rounded-xl border border-slate-800 bg-slate-900 p-5 md:grid-cols-2 lg:grid-cols-4"
        onSubmit={submit}
      >
        <label className="space-y-1 text-sm text-slate-400">
          <span>租户名称 *</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            maxLength={100}
            required
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="例如：客户 A"
          />
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>租户标识 *</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            minLength={2}
            maxLength={50}
            pattern="[a-z0-9][a-z0-9-]{1,49}"
            required
            value={form.slug}
            onChange={(e) => setForm({ ...form, slug: e.target.value })}
            placeholder="customer-a"
          />
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>所有者邮箱 *</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            maxLength={255}
            required
            type="email"
            value={form.owner_email}
            onChange={(e) =>
              setForm({ ...form, owner_email: e.target.value })
            }
            placeholder="owner@example.com"
          />
        </label>
        <label className="space-y-1 text-sm text-slate-400">
          <span>所有者显示名 *</span>
          <input
            className="w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-100"
            maxLength={100}
            required
            value={form.owner_display_name}
            onChange={(e) =>
              setForm({ ...form, owner_display_name: e.target.value })
            }
            placeholder="例如：客户管理员"
          />
        </label>
        <div className="md:col-span-2 lg:col-span-4">
          <button
            className="rounded bg-indigo-600 px-4 py-2 font-medium disabled:opacity-50"
            disabled={create.isPending}
          >
            {create.isPending ? "创建中…" : "创建租户"}
          </button>
        </div>
      </form>

      {message && (
        <p
          className={
            message.kind === "error"
              ? "text-sm text-red-400"
              : "text-sm text-emerald-400"
          }
        >
          {message.text}
        </p>
      )}
      {create.isError && (
        <p className="text-sm text-red-400">{errorText(create.error)}</p>
      )}

      {created && (
        <div className="rounded-xl border border-emerald-900 bg-slate-900 p-4 text-sm">
          <p className="text-emerald-400">
            已创建租户「{created.name}」，所有者 {created.owner.email}
          </p>
          <code className="mt-2 block break-all rounded bg-slate-950 px-3 py-2 font-mono text-slate-100">
            {created.owner.initial_password}
          </code>
          <div className="mt-2 flex items-center gap-3">
            <button
              type="button"
              className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300"
              onClick={copyPassword}
            >
              复制临时密码
            </button>
            <span className="text-xs text-slate-500">
              只显示一次；所有者首次登录后必须修改密码。
            </span>
          </div>
        </div>
      )}

      <section className="space-y-2">
        <h2 className="font-medium">全部租户</h2>
        {tenants.isLoading && (
          <p className="text-sm text-slate-400">加载中…</p>
        )}
        {tenants.isError && (
          <p className="text-sm text-red-400">{errorText(tenants.error)}</p>
        )}
        {tenants.data?.length === 0 && (
          <p className="text-sm text-slate-400">还没有租户。</p>
        )}
        {tenants.data?.map((tenant) => {
          const pending =
            update.isPending && update.variables?.id === tenant.id;
          return (
            <article
              key={tenant.id}
              className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900 p-4"
            >
              <div>
                <h3 className="font-medium">
                  {tenant.name}
                  {tenant.id === currentTenantId && (
                    <span className="ml-2 text-xs text-slate-500">（当前）</span>
                  )}
                </h3>
                <p className="mt-1 text-xs text-slate-500">
                  {tenant.slug} · {tenant.members_count} 名成员 · 创建于{" "}
                  {tenant.created_at.slice(0, 10)}
                </p>
              </div>
              <div className="flex items-center gap-2 text-sm">
                <span
                  className={
                    tenant.status === "active"
                      ? "text-xs text-emerald-400"
                      : "text-xs text-slate-500"
                  }
                >
                  {tenantStatusLabel(tenant.status)}
                </span>
                {canSuspendTenant(tenant, currentTenantId) && (
                  <button
                    type="button"
                    className="rounded border border-red-900 px-3 py-1.5 text-red-400 hover:border-red-800 disabled:opacity-50"
                    disabled={pending}
                    onClick={() =>
                      update.mutate({ id: tenant.id, status: "suspended" })
                    }
                  >
                    停用
                  </button>
                )}
                {canEnableTenant(tenant) && (
                  <button
                    type="button"
                    className="rounded border border-slate-700 px-3 py-1.5 text-slate-300 hover:border-slate-600 disabled:opacity-50"
                    disabled={pending}
                    onClick={() =>
                      update.mutate({ id: tenant.id, status: "active" })
                    }
                  >
                    启用
                  </button>
                )}
              </div>
            </article>
          );
        })}
      </section>
    </div>
  );
}
```

- [ ] **Step 6: 注册路由**

`web/src/App.tsx` 顶部新增：

```tsx
import Tenants from "./pages/Tenants";
```

在 `/audit` 路由之后追加（平台管理员与业务角色解耦，不设 `minRole`）：

```tsx
          <Route
            path="/tenants"
            element={
              <RequireRole platformAdmin>
                <Tenants />
              </RequireRole>
            }
          />
```

- [ ] **Step 7: 运行测试与构建**

Run: `npm --prefix web run test -- --run src/__tests__/tenants.test.ts`
Expected: PASS。

Run: `npm --prefix web run build`
Expected: 构建成功。

- [ ] **Step 8: 提交**

```bash
git add web/src/tenants.ts web/src/__tests__/tenants.test.ts web/src/pages/Tenants.tsx web/src/types.ts web/src/App.tsx
git commit -m "feat: add platform tenant management page"
```

### Task 18: 只读角色的前端写入收敛

本任务只做条件渲染，复用 Task 13 已测试的 `canWrite`；后端权限仍由 Task 6 的依赖兜底。改动完成后用构建与 Task 19 的浏览器验收确认。

**Files:**
- Modify: `web/src/pages/Projects.tsx`
- Modify: `web/src/pages/ProjectDetail.tsx`
- Modify: `web/src/pages/Profiles.tsx`
- Modify: `web/src/pages/Accounts.tsx`
- Modify: `web/src/pages/Publish.tsx`
- Modify: `web/src/pages/Analytics.tsx`

- [ ] **Step 1: 项目列表隐藏创建表单**

`web/src/pages/Projects.tsx` 顶部新增：

```tsx
import { useAuth } from "../auth-context";
```

组件内 `const qc = useQueryClient();` 之后新增：

```tsx
  const { canWrite } = useAuth();
```

在 `<form` 前插入 `{canWrite && (`，在对应 `</form>` 之后插入 `)}`，让创建项目表单仅对编辑及以上角色渲染。

- [ ] **Step 2: 项目详情隐藏写入入口**

`web/src/pages/ProjectDetail.tsx` 顶部新增：

```tsx
import { useAuth } from "../auth-context";
```

组件内 `const qc = useQueryClient();` 之后新增：

```tsx
  const { canWrite } = useAuth();
```

对以下三个表单分别执行「在 `<form` 前插入 `{canWrite && (`，在对应 `</form>` 后插入 `)}`」：

1. 标题为「新建内容」的表单（`onSubmit={submitContent}`）。
2. 标题为「一稿多改」的表单（`onSubmit={submitRepurpose}`）。
3. 标题为「AI 媒体生成」的表单（`onSubmit={submitMedia}`）。

媒体失败重试按钮的 `disabled` 改为：

```tsx
                      disabled={!canWrite || retryMedia.isPending}
```

「创建发布任务」按钮的 `disabled` 与 `title` 改为：

```tsx
                      disabled={
                        !canWrite ||
                        createJob.isPending ||
                        content.current_version < 1 ||
                        accountBlocked
                      }
                      title={
                        !canWrite
                          ? "只读成员不能创建发布任务"
                          : content.current_version < 1
                            ? "内容还没有版本，无法发布"
                            : accountBlocked
                              ? accounts.isSuccess
                                ? "请先授权公众号账号"
                                : "正在加载发布账号"
                              : undefined
                      }
```

- [ ] **Step 3: 画像页隐藏创建与记忆写入**

`web/src/pages/Profiles.tsx` 顶部新增：

```tsx
import { useAuth } from "../auth-context";
```

组件内 `const qc = useQueryClient();` 之后新增：

```tsx
  const { canWrite } = useAuth();
```

对以下两个表单分别执行「在 `<form` 前插入 `{canWrite && (`，在对应 `</form>` 后插入 `)}`」：

1. 标题为「新建画像」的表单（`onSubmit={submitProfile}`）。
2. 标题为「画像记忆」的表单（`onSubmit={submitMemory}`）。

- [ ] **Step 4: 发布账号页隐藏全部写入**

`web/src/pages/Accounts.tsx` 顶部新增：

```tsx
import { useAuth } from "../auth-context";
```

组件内 `const qc = useQueryClient();` 之后新增（账号列表对 editor 开放，写操作仅 admin）：

```tsx
  const { isAdmin } = useAuth();
```

「授权公众号」表单执行「在 `<form` 前插入 `{isAdmin && (`，在对应 `</form>` 后插入 `)}`」。

账号卡片的操作区条件 `{active && (` 改为：

```tsx
                {active && isAdmin && (
```

- [ ] **Step 5: 发布页隐藏任务操作**

`web/src/pages/Publish.tsx` 顶部新增：

```tsx
import { useAuth } from "../auth-context";
```

组件内找到 `const qc = useQueryClient();`，之后新增：

```tsx
  const { canWrite, isAdmin } = useAuth();
```

在 `jobs.data?.items.map((job) => {` 内、`const action = actionFor(job);` 之后新增：

```tsx
          const actionNeedsAdmin =
            action?.action === "approve" ||
            action?.action === "execute" ||
            action?.action === "retry" ||
            action?.action === "retry_export";
          const canAct =
            action !== null && (actionNeedsAdmin ? isAdmin : canWrite);
```

替换以下条件（创建任务在项目详情页，本页只有审核与执行类操作）：

```tsx
                  {canReject(job) && isAdmin && (
```

```tsx
                  {canCancel(job) &&
                    isAdmin &&
                    (cancelingId === job.id ? (
```

```tsx
                  {canReschedule(job) && isAdmin && (
```

```tsx
                  {canAct && action ? (
```

同时把该三元表达式的兜底分支改为只在没有可用动作时提示，避免只读成员看到“暂不支持”误导文案：

```tsx
                  ) : job.status === "approved" && action === null ? (
```

```tsx
              {canReject(job) && isAdmin && (
```

```tsx
              {scheduleOpen === job.id && isAdmin && (
```

顶部「管理发布账号」链接改为仅写角色可见：

```tsx
          {canWrite && (
            <Link className="text-indigo-400" to="/accounts">
              管理发布账号
            </Link>
          )}
```

- [ ] **Step 6: 数据页隐藏导入与复盘**

`web/src/pages/Analytics.tsx` 顶部新增：

```tsx
import { useAuth } from "../auth-context";
```

组件内 `const qc = useQueryClient();` 之后新增：

```tsx
  const { canWrite } = useAuth();
```

CSV 文件输入的 `disabled` 属性改为：

```tsx
              disabled={!canWrite}
```

「导入」按钮的 `disabled` 改为：

```tsx
              disabled={!canWrite || !file || importCsv.isPending}
```

「复盘」按钮的 `disabled` 与 `title` 改为：

```tsx
                          disabled={!canWrite || !item.has_data || postmortem.isPending}
                          title={
                            !canWrite
                              ? "只读成员不能生成复盘"
                              : item.has_data
                                ? undefined
                                : "请先导入该内容的发布数据"
                          }
```

- [ ] **Step 7: 运行前端全量测试与构建**

Run: `npm --prefix web run test`
Expected: 全部 PASS。

Run: `npm --prefix web run build`
Expected: 构建成功。

- [ ] **Step 8: 提交**

```bash
git add web/src/pages/Projects.tsx web/src/pages/ProjectDetail.tsx web/src/pages/Profiles.tsx web/src/pages/Accounts.tsx web/src/pages/Publish.tsx web/src/pages/Analytics.tsx
git commit -m "feat: hide write controls from viewer role"
```

### Task 19: 文档、全量验证与真实环境验收

**Files:**
- Create: `docs/phase-3-checklist.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: 创建 Phase 3 验收清单**

创建 `docs/phase-3-checklist.md`：

```md
# Phase 3 验收清单

验收日期：执行后填写。环境：macOS 本地；自动化测试使用内存 SQLite；真实环境使用 Docker Compose 中的 PostgreSQL 16 + Redis 7、uvicorn、vite、arq worker，以及浏览器手工验收。

## 功能

- [ ] 默认租户与平台管理员：迁移后存在 `tenants` 默认行、原管理员 `role=owner`；首次启动后 `ensure_admin` 授予首位平台管理员，`is_platform_admin=true`、`token_version=0`（验证：`tests/test_models_saas.py`、迁移后 SQL 查询 + 启动后 SQL 查询）
- [ ] 登录 token 含 `tid`/`ver`；`/auth/me` 返回角色、租户、平台权限与强制改密标记（验证：`tests/test_auth_saas.py`）
- [ ] 成员创建返回一次性临时密码，首次登录强制改密（验证：`tests/test_members.py`）
- [ ] 角色矩阵：viewer 只读、editor 可写、admin 管理账号与发布审核、owner 管理成员（验证：`tests/test_permissions.py`）
- [ ] 最后一名 owner 不可降级/停用；不能修改自己的角色或停用自己（验证：`tests/test_members.py`）
- [ ] 平台管理员创建租户并同时创建 owner；slug/邮箱冲突返回 409（验证：`tests/test_tenants.py`）
- [ ] 停用租户使该租户所有在线 token 失效，重新启用后可登录（验证：`tests/test_tenants.py`）
- [ ] 停用租户的定时发布不被扫描；媒体任务立即失败为 `tenant_suspended`（验证：`tests/test_worker.py`）
- [ ] 双租户隔离：跨租户读取/修改/删除返回 404，列表不泄漏（验证：`tests/test_isolation.py`）
- [ ] 审计覆盖登录、改密、成员、租户、账号、设置、技能、发布、媒体事件；支持过滤与分页（验证：`tests/test_audit.py`、`tests/test_audit_events.py`）
- [ ] 审计详情脱敏：password/token/secret/api_key/credential/authorization/cookie 字段被丢弃（验证：`tests/test_audit_service.py`、`tests/test_audit.py`）
- [ ] 前端角色化导航与页面：团队（admin 读/owner 写）、审计（admin）、租户（平台管理员）、改密（强制跳转）（验证：`web/src/__tests__/*.test.ts` 与浏览器验收）
- [ ] viewer 在业务页面看不到写入按钮（验证：浏览器验收）

## 安全与合规

- [ ] 新增写接口使用角色依赖，未授权返回 403 `forbidden`（验证：`tests/test_permissions.py`）
- [ ] 跨租户操作统一 404，不暴露资源存在性（验证：`tests/test_members.py`、`tests/test_isolation.py`）
- [ ] `token_version` 在改密、改角色、停用用户、停用租户时递增，旧 token 返回 401 `token_revoked`（验证：`tests/test_auth_saas.py`、`tests/test_members.py`）
- [ ] 审计只追加：无 PATCH/DELETE 接口；审计写入失败不阻断认证响应（验证：`tests/test_audit.py`、`tests/test_audit_service.py`）
- [ ] 临时密码只返回一次，不写审计、不入日志（验证：代码审查 `app/api/members.py`、`app/api/tenants.py` 与 `tests/test_members.py`）
- [ ] 平台接口只管理租户元数据，不读取其他租户业务数据（验证：`tests/test_tenants.py`、`tests/test_permissions.py`）

## 工程

- [ ] `alembic upgrade head` 成功，当前 head `c3d4e5f6a7b8`（真实 PostgreSQL）
- [ ] `cd backend && uv run ruff check .` 通过
- [ ] `cd backend && uv run pytest -q` 全部通过
- [ ] `npm --prefix web run test` 全部通过
- [ ] `npm --prefix web run build` 通过
- [ ] 验收结束后 `make down`，8000/5173/15432/16379 均无残留监听

## 真实环境验收（对应设计第 13 节）

1. [ ] 平台管理员创建租户 B，返回 owner 临时密码；用该密码登录 B 的 owner，被强制改密。
2. [ ] 租户 A 与 B 分别创建项目、内容、画像、发布任务；A 的用户访问 B 的资源全部 404，列表互不出现。
3. [ ] 租户 A 的 owner 创建 editor 成员；editor 可创建内容与发布任务，访问发布审核/账号管理写入返回 403；admin 可审核但访问成员管理写入返回 403。
4. [ ] 停用 editor 后，其旧 token 立即失效；重新登录返回 403 `account_disabled`。
5. [ ] 平台管理员停用租户 B：B 的在线会话立即失效、登录返回 403 `tenant_suspended`、B 的定时发布不执行；重新启用后可登录。
6. [ ] 修改密码后旧 token 失效，新 token 正常使用。
7. [ ] 审计页面可查到登录、成员、发布、租户事件；任何审计 `detail` 中不出现密码、token 或平台凭据。
8. [ ] 迁移成功；默认租户与现有管理员角色正确回填。

## 本次结果（执行后填写）

- 后端测试：
- 前端测试：
- 前端构建：
- 迁移：
- 真实环境验收：
```

- [ ] **Step 2: 更新 README**

把首段：

```md
原生 AI 新媒体运营系统（Phase 0/1 MVP，已开始 Phase 2 工程基础）。设计见 [设计文档](docs/原生AI新媒体运营系统-开发文档.md)。
```

替换为：

```md
原生 AI 新媒体运营系统（Phase 0–3：创作发布闭环 + 多租户 SaaS 基础）。设计见 [设计文档](docs/原生AI新媒体运营系统-开发文档.md)。
```

在 `## Phase 1 已知限制` 之前插入：

```md
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

```

- [ ] **Step 3: 更新 CHANGELOG**

在 `## [Unreleased]` 的 `### 新增` 之后插入：

```md
- **多租户 SaaS 基础**：新增 `tenants` 表与平台管理员权限位，支持租户创建、改名、停用/启用；创建租户时同步开通 owner 并返回一次性临时密码。
- **四级角色与鉴权**：新增 `viewer/editor/admin/owner` 角色层级与全接口依赖；JWT 增加 `ver`，改密、改角色、停用用户或租户时递增 `token_version` 使旧 token 立即失效。
- **成员管理**：新增成员创建、列表、改角色/显示名、停用/启用接口；临时密码仅返回一次，首次登录强制改密；保护最后一名 owner 与自我操作。
- **安全审计**：新增 `audit_logs` 表与 `/api/audit-logs` 查询接口，覆盖认证、成员、租户、账号、设置、技能、发布与媒体关键事件；审计只追加，敏感字段写库前脱敏。
- **前端 SaaS 页面**：新增改密、团队、审计、租户页面与角色化导航；只读角色隐藏写入控件。
```

- [ ] **Step 4: 运行自动化验证**

逐条执行并记录实际结果：

```bash
cd backend && uv run ruff check .
cd backend && uv run pytest -q
npm --prefix web run test
npm --prefix web run build
```

Expected: ruff 无错误；pytest 全部通过；vitest 全部通过；`tsc -b && vite build` 成功。

- [ ] **Step 5: 真实 PostgreSQL 迁移与回填检查**

```bash
make up
make migrate
cd backend && uv run alembic current
docker compose exec db psql -U app -d newmedia -c "select id, name, slug, status from tenants order by created_at;"
docker compose exec db psql -U app -d newmedia -c "select email, role, is_platform_admin, token_version, must_change_password from users order by created_at;"
docker compose exec db psql -U app -d newmedia -c "\d+ audit_logs"
```

Expected: `alembic current` 输出 `c3d4e5f6a7b8 (head)`；`tenants` 至少包含默认租户；默认管理员 `role=owner`、`is_platform_admin=t`；`audit_logs` 表及索引存在。

- [ ] **Step 6: 真实环境验收步骤 1–8**

启动后端、前端与 worker（三个终端）：

```bash
make dev
npm --prefix web run dev
make worker
```

按设计第 13 节逐项执行：

1. 用平台管理员登录 `/tenants`，创建租户 B（slug 填 `customer-b`，owner 邮箱填 `owner-b@local`），记下一次性临时密码；退出后用该邮箱与临时密码登录，确认被重定向到 `/change-password`，改密后进入业务页。
2. 在默认租户 A 创建项目、内容、画像与发布任务；登录 B 的 owner，确认项目/画像/发布列表为空；在地址栏直接访问 A 的项目 id（`/projects/<A 项目 id>`），确认页面报错且不展示数据。
3. 在 A 的 `/team` 由 owner 创建 editor 与 admin 成员；用 editor 登录（首次改密后）确认可创建项目/内容与发布任务，但 `/accounts` 无授权表单、发布页无审核/执行/排期按钮；用 admin 登录确认可审核与执行，但 `/team` 无创建表单。
4. 回到 A 的 owner，在 `/team` 停用 editor；在 editor 的浏览器刷新任意页面，确认被退回登录页；重新登录确认提示「账号已停用，请联系租户管理员」。
5. 验证停用租户的定时任务不被执行。在 B 仍启用时，用 B 的 owner token 通过 API 创建一条 2 分钟后到期的无账号公众号任务（前端会阻止无账号创建，这里只为验收 worker 的租户过滤）：

```bash
B_TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"owner-b@local","password":"<B 改密后的密码>"}' | jq -r .access_token)

PROJECT_ID=$(curl -s -X POST http://localhost:8000/api/projects \
  -H "Authorization: Bearer $B_TOKEN" -H 'Content-Type: application/json' \
  -d '{"title":"停用验收","topic":"tenant suspension","profile_id":null}' | jq -r .id)

CONTENT_ID=$(curl -s -X POST http://localhost:8000/api/projects/$PROJECT_ID/contents \
  -H "Authorization: Bearer $B_TOKEN" -H 'Content-Type: application/json' \
  -d '{"platform":"wechat","kind":"article","title":"停用验收","body":"这是一段用于停用验收的正文。"}' | jq -r .id)

SCHEDULED_AT=$(date -u -v+2M +"%Y-%m-%dT%H:%M:%SZ")
JOB_ID=$(curl -s -X POST http://localhost:8000/api/publish/jobs \
  -H "Authorization: Bearer $B_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"content_id\":\"$CONTENT_ID\",\"platform\":\"wechat\",\"scheduled_at\":\"$SCHEDULED_AT\"}" | jq -r .id)

curl -s -X POST http://localhost:8000/api/publish/jobs/$JOB_ID/precheck \
  -H "Authorization: Bearer $B_TOKEN"
curl -s -X POST http://localhost:8000/api/publish/jobs/$JOB_ID/actions \
  -H "Authorization: Bearer $B_TOKEN" -H 'Content-Type: application/json' \
  -d '{"action":"approve"}'
```

随后用平台管理员在 `/tenants` 停用 B：B 的在线页面刷新后显示租户停用提示，重新登录返回 403。保持 worker 运行，等待到期后执行：

```bash
B_TENANT_ID=$(docker compose exec -T db psql -U app -d newmedia -tA -c "select id from tenants where slug = 'customer-b';")
docker compose exec db psql -U app -d newmedia -c "select id, tenant_id, status, scheduled_at from publish_jobs where tenant_id = '$B_TENANT_ID' order by created_at desc limit 3;"
docker compose exec db psql -U app -d newmedia -c "select count(*) from audit_logs where action = 'publish.auto_execute' and tenant_id = '$B_TENANT_ID';"
```

Expected: 任务仍为 `approved`（未被扫描执行），`publish.auto_execute` 计数为 0。随后在 `/tenants` 启用 B，确认可重新登录。

6. 在任意账号点击顶部「修改密码」，提交后确认旧标签页的下一次请求被退出登录，使用新密码或返回的新 token 可继续访问。
7. 用 A 的 admin 打开 `/audit`，分别按动作（登录、成员、发布、租户）、状态、操作人与时间过滤；展开详情，确认 `detail` 中不出现密码、token、AppSecret 或 `access_token`。
8. 执行步骤 5 的 SQL 查询，确认默认租户存在、默认管理员为 owner 且是平台管理员。

- [ ] **Step 7: 填写验收结果并补充 CHANGELOG**

把 Step 4–6 的实际输出填入 `docs/phase-3-checklist.md` 的「本次结果」，并勾选已通过项。

在 `CHANGELOG.md` 的 `### 验证` 下追加（数量替换为本次实际结果）：

```md
- 后端：`uv run pytest -q` 全部通过，`uv run ruff check .` 通过。
- 前端：`npm test` 全部通过，`npm run build` 通过。
- 真实 PostgreSQL：迁移到 `c3d4e5f6a7b8`，默认租户与管理员回填正确。
- 真实环境验收：租户创建与强制改密、双租户隔离、角色矩阵、停用会话失效、租户停用、审计脱敏全部通过。
```

在 `### 已知限制` 下追加：

```md
- 一人一租户，跨租户成员与租户切换未实现。
- 无公开注册、租户删除与订阅计费。
- 审计日志不自动归档；部署在代理后时审计 IP 仅记录直连地址。
```

- [ ] **Step 8: 清理进程与容器**

停止 `make dev`、`npm --prefix web run dev`、`make worker`（Ctrl-C），然后：

```bash
make down
docker compose ps
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
lsof -nP -iTCP:15432 -sTCP:LISTEN
lsof -nP -iTCP:16379 -sTCP:LISTEN
```

Expected: `docker compose ps` 无运行中容器；四条 `lsof` 均无输出。`pgdata` 卷保留，不执行 `down -v`。

- [ ] **Step 9: 提交**

```bash
git add docs/phase-3-checklist.md README.md CHANGELOG.md
git commit -m "docs: record phase 3 saas acceptance"
```

