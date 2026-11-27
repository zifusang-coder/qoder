# 商业化计费 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付「可配置套餐 + 分项计量 + 月度账单」：平台管理员维护并指派套餐，租户管理员查看本期用量与预计费用，已结束月份由平台管理员关账生成不可变账单。

**Architecture:** 新增平台级 `plans` 表与 `tenants.plan_id`，账单快照写入 `billing_statements`。计量直接聚合既有 `skill_runs`、`agent_runs.usage`、`workflow_runs`、`media_jobs`，不新增记账表；计费维度、账期与金额计算集中在 `backend/app/billing/` 的纯函数中。租户 API 只读自己租户的数据，平台 API 负责套餐、指派与关账。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + Alembic + Pydantic v2；React + Vite + TanStack Query + Vitest。

**Spec:** [docs/superpowers/specs/2026-11-25-commercial-billing-design.md](../superpowers/specs/2026-11-25-commercial-billing-design.md)

## Global Constraints

- 账期按北京时间（UTC+8，无夏令时）自然月切分，`period` 形如 `2026-11`；只有已结束月份允许关账。
- LLM token（`llm.prompt_tokens` / `llm.completion_tokens`）是唯一计费项；运行次数与媒体指标只展示、默认不收费。
- token 只统计 `skill_runs` 与 `agent_runs.usage`；**不得**统计 `workflow_runs.usage`，避免重复计数。
- 所有金额为整数分（`*_cents`），向上取整；接口不返回浮点金额。
- 超额为软限额：不阻断功能，不新增站内信/邮件通知。
- 所有平台接口使用 `require_platform_admin`；租户接口使用 `admin_access` 并强制 `tenant_id == user.tenant_id`；跨租户一律 404。
- 审计复用 `app/services/audit.py` 的 `record(...)`，`detail` 不得包含凭据或密钥。
- 迁移必须可在真实 PostgreSQL 16 上 `upgrade head`，并提供可回退的 `downgrade`。
- 所有命令在 `backend/` 下用 `uv run pytest|ruff|alembic`，不得使用裸 `python`/`pytest`/`alembic`。
- 不修改 `.env` 中的既有凭据，不新增任何对外网络调用。
- 不修改 `82.157.177.14` 相关配置，不操作其他项目的容器与卷。
- 除非用户明确要求，执行过程中不创建 git commit；各任务末尾的提交步骤仅在用户授权后执行。

---

## File Structure

**后端新增**

- `backend/app/billing/__init__.py` — 空包。
- `backend/app/billing/dimensions.py` — 维度注册表与维度校验。
- `backend/app/billing/pricing.py` — 单维度费用、用量行、费用明细纯函数。
- `backend/app/billing/periods.py` — 账期解析、北京时间边界、是否已结束。
- `backend/app/billing/plans.py` — 内置套餐种子、默认套餐常量、`ensure_plans`。
- `backend/app/billing/usage.py` — 按租户与账期聚合用量。
- `backend/app/billing/statements.py` — 账单快照计算、关账、重开。
- `backend/app/models/billing.py` — `Plan` 与 `BillingStatement`。
- `backend/app/api/billing.py` — 租户与平台计费 API。
- `backend/migrations/versions/d1e2f3a4b5c6_add_billing.py` — 新迁移。
- `backend/tests/test_billing_dimensions.py`
- `backend/tests/test_billing_models.py`
- `backend/tests/test_billing_usage.py`
- `backend/tests/test_billing_statements.py`
- `backend/tests/test_billing_api.py`
- `backend/tests/test_billing_platform_api.py`

**后端修改**

- `backend/app/models/tenant.py` — 增加 `plan_id`、`plan_changed_at`。
- `backend/app/api/tenants.py` — `TenantOut` 增加套餐字段；创建租户绑定默认套餐。
- `backend/app/main.py` — 注册 billing 路由。
- `backend/tests/conftest.py` — 测试库建表后写入套餐种子。

**前端新增**

- `web/src/billing.ts` — 类型与格式化纯函数。
- `web/src/pages/Billing.tsx` — 账单页（租户视图 + 平台面板）。
- `web/src/__tests__/billing.test.ts`

**前端修改**

- `web/src/auth.ts` — `NavIconKey` 与 `NAV_ITEMS` 增加账单入口。
- `web/src/components/NavIcon.tsx` — 增加 `billing` 图标。
- `web/src/App.tsx` — 注册 `/billing` 路由（`minRole="admin"`）。
- `web/src/pages/Tenants.tsx` — 套餐列与指派下拉。
- `web/src/types.ts` — `Tenant` 增加 `plan_code`、`plan_name`。
- `web/src/__tests__/nav.test.ts` — 更新 manage 分组期望。

**文档**

- `README.md`、`docs/ops.md`、`docs/原生AI新媒体运营系统-开发文档.md`、设计规格的 Status。

---

### Task 1: 计费维度、金额与账期纯函数

**Files:**
- Create: `backend/app/billing/__init__.py`
- Create: `backend/app/billing/dimensions.py`
- Create: `backend/app/billing/pricing.py`
- Create: `backend/app/billing/periods.py`
- Test: `backend/tests/test_billing_dimensions.py`

**Interfaces:**
- Produces:
  - `Dimension(key, label, unit, unit_size, billable)`、`DIMENSIONS`、`DIMENSION_MAP`、`DIMENSION_KEYS`
  - `unknown_dimensions(mapping) -> list[str]`、`priced_non_billable(prices) -> list[str]`
  - `charge_cents(used, included, price_cents, unit_size) -> int`
  - `build_charges(usage, limits, prices) -> list[dict]`
  - `build_usage_rows(usage, limits, prices) -> list[dict]`
  - `normalize_period(period, now=None) -> str`、`period_bounds(period) -> tuple[datetime, datetime]`、`period_finished(period, now=None) -> bool`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_billing_dimensions.py
from datetime import UTC, datetime

import pytest

from app.billing.dimensions import (
    DIMENSION_KEYS,
    DIMENSION_MAP,
    priced_non_billable,
    unknown_dimensions,
)
from app.billing.periods import (
    normalize_period,
    period_bounds,
    period_finished,
)
from app.billing.pricing import build_charges, build_usage_rows, charge_cents


def test_registry_exposes_expected_dimensions():
    assert DIMENSION_KEYS == (
        "llm.prompt_tokens",
        "llm.completion_tokens",
        "run.skill",
        "run.agent",
        "run.workflow",
        "media.jobs",
        "media.seconds",
    )
    assert DIMENSION_MAP["llm.prompt_tokens"].billable is True
    assert DIMENSION_MAP["llm.prompt_tokens"].unit_size == 1000
    assert DIMENSION_MAP["media.seconds"].billable is False


def test_unknown_and_non_billable_keys_are_reported():
    assert unknown_dimensions({"llm.prompt_tokens": 1, "x.y": 2}) == ["x.y"]
    assert priced_non_billable({"llm.completion_tokens": 1}) == []
    assert priced_non_billable({"run.skill": 1, "media.jobs": 2}) == [
        "media.jobs",
        "run.skill",
    ]


def test_charge_rounds_up_to_full_cent():
    assert charge_cents(1000, 1000, 2, 1000) == 0
    assert charge_cents(1500, 1000, 2, 1000) == 1
    assert charge_cents(1999, 1000, 2, 1000) == 2
    assert charge_cents(2000, 1000, 2, 1000) == 2
    assert charge_cents(5000, 0, 0, 1000) == 0


def test_build_charges_keeps_dimension_order_and_skips_zero():
    usage = {"llm.completion_tokens": 1200, "llm.prompt_tokens": 1500}
    limits = {"llm.prompt_tokens": 1000, "llm.completion_tokens": 1200}
    prices = {"llm.prompt_tokens": 1, "llm.completion_tokens": 3}
    charges = build_charges(usage, limits, prices)
    assert charges == [
        {
            "dimension": "llm.prompt_tokens",
            "used": 1500,
            "included": 1000,
            "overage": 500,
            "unit_size": 1000,
            "price_cents": 1,
            "amount_cents": 1,
        }
    ]


def test_build_usage_rows_marks_over_limit_and_unlimited():
    rows = build_usage_rows(
        {"llm.prompt_tokens": 1500, "run.skill": 3},
        {"llm.prompt_tokens": 1000, "run.skill": 0},
        {"llm.prompt_tokens": 1},
    )
    by_key = {row["dimension"]: row for row in rows}
    prompt = by_key["llm.prompt_tokens"]
    assert prompt["percent"] == 150
    assert prompt["over_limit"] is True
    assert prompt["remaining"] == 0
    assert prompt["amount_cents"] == 1
    skills = by_key["run.skill"]
    assert skills["percent"] == 100
    assert skills["over_limit"] is True
    assert by_key["media.jobs"]["percent"] is None
    assert by_key["media.jobs"]["over_limit"] is False


def test_period_bounds_use_beijing_month():
    start, end = period_bounds("2026-11")
    assert start == datetime(2026, 10, 31, 16, tzinfo=UTC)
    assert end == datetime(2026, 11, 30, 16, tzinfo=UTC)
    dec_start, dec_end = period_bounds("2026-12")
    assert dec_start == datetime(2026, 11, 30, 16, tzinfo=UTC)
    assert dec_end == datetime(2026, 12, 31, 16, tzinfo=UTC)


def test_normalize_period_uses_beijing_time_and_rejects_bad_input():
    moment = datetime(2026, 11, 30, 17, tzinfo=UTC)
    assert normalize_period(None, now=moment) == "2026-12"
    assert normalize_period("2026-11") == "2026-11"
    with pytest.raises(ValueError):
        normalize_period("2026-13")
    with pytest.raises(ValueError):
        normalize_period("202611")


def test_period_finished_compares_against_beijing_end():
    assert period_finished("2026-10", now=datetime(2026, 11, 1, tzinfo=UTC))
    assert not period_finished("2026-11", now=datetime(2026, 11, 30, 15, 59, tzinfo=UTC))
    assert period_finished("2026-11", now=datetime(2026, 11, 30, 16, tzinfo=UTC))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_billing_dimensions.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.billing'`

- [ ] **Step 3: 实现维度注册表**

```python
# backend/app/billing/__init__.py
```

```python
# backend/app/billing/dimensions.py
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Dimension:
    key: str
    label: str
    unit: str
    unit_size: int = 1
    billable: bool = False


DIMENSIONS: tuple[Dimension, ...] = (
    Dimension("llm.prompt_tokens", "输入 token", "token", 1000, True),
    Dimension("llm.completion_tokens", "输出 token", "token", 1000, True),
    Dimension("run.skill", "技能运行", "次"),
    Dimension("run.agent", "Agent 运行", "次"),
    Dimension("run.workflow", "工作流运行", "次"),
    Dimension("media.jobs", "媒体任务", "次"),
    Dimension("media.seconds", "媒体时长", "秒"),
)

DIMENSION_MAP: dict[str, Dimension] = {item.key: item for item in DIMENSIONS}
DIMENSION_KEYS: tuple[str, ...] = tuple(item.key for item in DIMENSIONS)


def unknown_dimensions(mapping: Mapping[str, object]) -> list[str]:
    return sorted(key for key in mapping if key not in DIMENSION_MAP)


def priced_non_billable(prices: Mapping[str, object]) -> list[str]:
    return sorted(
        key
        for key in prices
        if key in DIMENSION_MAP and not DIMENSION_MAP[key].billable
    )
```

- [ ] **Step 4: 实现价格与用量行纯函数**

```python
# backend/app/billing/pricing.py
from collections.abc import Mapping

from app.billing.dimensions import DIMENSIONS


def charge_cents(
    used: int, included: int, price_cents: int, unit_size: int
) -> int:
    overage = max(0, used - included)
    if overage == 0 or price_cents <= 0 or unit_size <= 0:
        return 0
    return (overage * price_cents + unit_size - 1) // unit_size


def build_charges(
    usage: Mapping[str, int],
    limits: Mapping[str, int],
    prices: Mapping[str, int],
) -> list[dict]:
    charges: list[dict] = []
    for dimension in DIMENSIONS:
        price = int(prices.get(dimension.key, 0) or 0)
        if not dimension.billable or price <= 0:
            continue
        used = int(usage.get(dimension.key, 0) or 0)
        included = int(limits.get(dimension.key, 0) or 0)
        amount = charge_cents(used, included, price, dimension.unit_size)
        if amount == 0:
            continue
        charges.append(
            {
                "dimension": dimension.key,
                "used": used,
                "included": included,
                "overage": max(0, used - included),
                "unit_size": dimension.unit_size,
                "price_cents": price,
                "amount_cents": amount,
            }
        )
    return charges


def _percent(used: int, limit: int) -> int:
    if limit <= 0:
        return 100 if used > 0 else 0
    return round(used * 100 / limit)


def build_usage_rows(
    usage: Mapping[str, int],
    limits: Mapping[str, int],
    prices: Mapping[str, int],
) -> list[dict]:
    rows: list[dict] = []
    for dimension in DIMENSIONS:
        used = int(usage.get(dimension.key, 0) or 0)
        limit = limits.get(dimension.key)
        included = int(limit or 0)
        price = int(prices.get(dimension.key, 0) or 0)
        rows.append(
            {
                "dimension": dimension.key,
                "label": dimension.label,
                "unit": dimension.unit,
                "unit_size": dimension.unit_size,
                "used": used,
                "limit": None if limit is None else included,
                "remaining": None if limit is None else max(0, included - used),
                "percent": None if limit is None else _percent(used, included),
                "over_limit": limit is not None and used > included,
                "price_cents": price,
                "amount_cents": charge_cents(
                    used, included, price, dimension.unit_size
                )
                if dimension.billable
                else 0,
            }
        )
    return rows
```

- [ ] **Step 5: 实现账期纯函数**

```python
# backend/app/billing/periods.py
import re
from datetime import UTC, datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
_PERIOD_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def normalize_period(period: str | None, *, now: datetime | None = None) -> str:
    if period is None:
        moment = (now or datetime.now(UTC)).astimezone(BEIJING)
        return f"{moment.year:04d}-{moment.month:02d}"
    if not _PERIOD_PATTERN.fullmatch(period):
        raise ValueError("period_invalid")
    return period


def period_bounds(period: str) -> tuple[datetime, datetime]:
    normalize_period(period)
    year = int(period[:4])
    month = int(period[5:])
    start = datetime(year, month, 1, tzinfo=BEIJING)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=BEIJING)
    else:
        end = datetime(year, month + 1, 1, tzinfo=BEIJING)
    return start.astimezone(UTC), end.astimezone(UTC)


def period_finished(period: str, *, now: datetime | None = None) -> bool:
    _, end = period_bounds(period)
    return end <= (now or datetime.now(UTC))
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_billing_dimensions.py -q`
Expected: PASS

- [ ] **Step 7: Lint 与（如已授权）提交**

Run: `cd backend && uv run ruff check app/billing tests/test_billing_dimensions.py`
Expected: 无输出

如用户已授权提交：

```bash
git add backend/app/billing backend/tests/test_billing_dimensions.py
git commit -m "feat(billing): add metering dimensions, pricing and period helpers"
```

---

### Task 2: 套餐/账单模型、种子与租户套餐字段

**Files:**
- Create: `backend/app/models/billing.py`
- Create: `backend/app/billing/plans.py`
- Modify: `backend/app/models/tenant.py`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_billing_models.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `Plan`（`code`、`name`、`description`、`price_cents`、`currency`、`limits`、`prices`、`status`）
  - `BillingStatement`（`tenant_id`、`period`、`status`、`plan_*`、`limits`、`prices`、`usage`、`charges`、`amount_cents`、`closed_by`、`closed_at`、`reopened_at`）
  - `PLAN_SEEDS`、`DEFAULT_PLAN_CODE`、`DEFAULT_PLAN_ID`、`ensure_plans(session)`
  - `Tenant.plan_id`、`Tenant.plan_changed_at`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_billing_models.py
import uuid

from sqlalchemy import select

from app.billing.plans import DEFAULT_PLAN_ID, PLAN_SEEDS, ensure_plans
from app.models.billing import BillingStatement, Plan
from app.models.tenant import Tenant


async def test_ensure_plans_is_idempotent(db_session):
    await ensure_plans(db_session)
    await ensure_plans(db_session)
    await db_session.commit()
    codes = (
        await db_session.execute(select(Plan.code).order_by(Plan.code))
    ).scalars().all()
    assert codes == sorted(seed["code"] for seed in PLAN_SEEDS)
    free = (
        await db_session.execute(select(Plan).where(Plan.code == "free"))
    ).scalar_one()
    assert str(free.id) == DEFAULT_PLAN_ID
    assert free.price_cents == 0


async def test_new_tenant_gets_default_plan(db_session, tenant_factory):
    tenant = await tenant_factory(name="套餐默认")
    assert str(tenant.plan_id) == DEFAULT_PLAN_ID
    assert tenant.plan_changed_at is not None


async def test_statement_unique_per_tenant_and_period(db_session, tenant_factory):
    tenant = await tenant_factory()
    plan = (
        await db_session.execute(select(Plan).where(Plan.code == "free"))
    ).scalar_one()
    first = BillingStatement(
        tenant_id=tenant.id,
        period="2026-10",
        plan_code=plan.code,
        plan_name=plan.name,
        plan_price_cents=plan.price_cents,
        limits={},
        prices={},
        usage={},
        charges=[],
        amount_cents=0,
    )
    db_session.add(first)
    await db_session.commit()
    db_session.add(
        BillingStatement(
            tenant_id=tenant.id,
            period="2026-10",
            plan_code=plan.code,
            plan_name=plan.name,
            plan_price_cents=0,
            limits={},
            prices={},
            usage={},
            charges=[],
            amount_cents=0,
        )
    )
    try:
        await db_session.commit()
        assert False, "重复账期应触发唯一约束"
    except Exception:
        await db_session.rollback()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_billing_models.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.models.billing'`

- [ ] **Step 3: 实现模型与种子**

```python
# backend/app/models/billing.py
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped, TimestampMixin, UUIDMixin, Base


class Plan(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    limits: Mapped[dict] = mapped_column(JSONType, default=dict)
    prices: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False
    )


class BillingStatement(TenantScoped):
    __tablename__ = "billing_statements"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "period", name="uq_billing_statements_tenant_period"
        ),
    )

    period: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="closed", nullable=False
    )
    plan_code: Mapped[str] = mapped_column(String(50), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(100), nullable=False)
    plan_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    limits: Mapped[dict] = mapped_column(JSONType, default=dict)
    prices: Mapped[dict] = mapped_column(JSONType, default=dict)
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    charges: Mapped[list] = mapped_column(JSONType, default=list)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    closed_by: Mapped[object | None] = mapped_column(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reopened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

上面的 `closed_by` 类型不够精确，改为与项目其它 UUID 列一致的写法：

```python
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
```

并在文件头使用：

```python
import uuid
from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
```

```python
# backend/app/billing/plans.py
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.billing import Plan

FREE_PLAN_ID = "1f0a0000-0000-4000-8000-000000000001"
BASIC_PLAN_ID = "1f0a0000-0000-4000-8000-000000000002"
PRO_PLAN_ID = "1f0a0000-0000-4000-8000-000000000003"
ENTERPRISE_PLAN_ID = "1f0a0000-0000-4000-8000-000000000004"

DEFAULT_PLAN_CODE = "free"
DEFAULT_PLAN_ID = FREE_PLAN_ID

PLAN_SEEDS: tuple[dict, ...] = (
    {
        "id": FREE_PLAN_ID,
        "code": "free",
        "name": "免费版",
        "description": "小团队试跑：基础额度，超出不计费。",
        "price_cents": 0,
        "limits": {
            "llm.prompt_tokens": 200_000,
            "llm.completion_tokens": 100_000,
            "run.skill": 500,
            "run.agent": 100,
            "run.workflow": 50,
            "media.jobs": 50,
            "media.seconds": 1_800,
        },
        "prices": {},
    },
    {
        "id": BASIC_PLAN_ID,
        "code": "basic",
        "name": "基础版",
        "description": "日常创作与发布，含超额按量计费。",
        "price_cents": 9_900,
        "limits": {
            "llm.prompt_tokens": 2_000_000,
            "llm.completion_tokens": 1_000_000,
            "run.skill": 5_000,
            "run.agent": 1_000,
            "run.workflow": 300,
            "media.jobs": 300,
            "media.seconds": 18_000,
        },
        "prices": {"llm.prompt_tokens": 1, "llm.completion_tokens": 3},
    },
    {
        "id": PRO_PLAN_ID,
        "code": "pro",
        "name": "专业版",
        "description": "多账号矩阵与更高 Agent/工作流额度。",
        "price_cents": 39_900,
        "limits": {
            "llm.prompt_tokens": 10_000_000,
            "llm.completion_tokens": 5_000_000,
            "run.skill": 20_000,
            "run.agent": 5_000,
            "run.workflow": 1_000,
            "media.jobs": 1_000,
            "media.seconds": 72_000,
        },
        "prices": {"llm.prompt_tokens": 1, "llm.completion_tokens": 3},
    },
    {
        "id": ENTERPRISE_PLAN_ID,
        "code": "enterprise",
        "name": "企业版",
        "description": "团队协作与大规模内容生产。",
        "price_cents": 199_900,
        "limits": {
            "llm.prompt_tokens": 50_000_000,
            "llm.completion_tokens": 25_000_000,
            "run.skill": 100_000,
            "run.agent": 20_000,
            "run.workflow": 5_000,
            "media.jobs": 5_000,
            "media.seconds": 360_000,
        },
        "prices": {"llm.prompt_tokens": 1, "llm.completion_tokens": 3},
    },
)


async def ensure_plans(session: AsyncSession) -> None:
    existing = set((await session.execute(select(Plan.code))).scalars().all())
    for seed in PLAN_SEEDS:
        if seed["code"] in existing:
            continue
        session.add(
            Plan(
                id=uuid.UUID(seed["id"]),
                code=seed["code"],
                name=seed["name"],
                description=seed["description"],
                price_cents=seed["price_cents"],
                currency="CNY",
                limits=dict(seed["limits"]),
                prices=dict(seed["prices"]),
                status="active",
            )
        )
```

- [ ] **Step 4: 扩展 Tenant 模型**

```python
# backend/app/models/tenant.py
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.billing.plans import DEFAULT_PLAN_ID
from app.models.base import Base, TimestampMixin


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plans.id", ondelete="RESTRICT"),
        default=lambda: uuid.UUID(DEFAULT_PLAN_ID),
        nullable=False,
    )
    plan_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 5: 测试库建表后写入套餐种子**

```python
# backend/tests/conftest.py
from app.billing.plans import ensure_plans  # 放到文件顶部导入区

    # db_session 内，create_all 之后：
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await ensure_plans(session)
        await session.commit()
        yield session
    await engine.dispose()
```

- [ ] **Step 6: 运行模型测试与既有租户测试**

Run: `cd backend && uv run pytest tests/test_billing_models.py tests/test_tenants.py -q`
Expected: PASS（既有租户用例不因 `plan_id` 非空而失败）

- [ ] **Step 7: Lint 与（如已授权）提交**

Run: `cd backend && uv run ruff check app/models app/billing tests/conftest.py tests/test_billing_models.py`

如已授权：

```bash
git add backend/app/models/billing.py backend/app/models/tenant.py \
  backend/app/billing/plans.py backend/tests/conftest.py \
  backend/tests/test_billing_models.py
git commit -m "feat(billing): add plan and statement models with seed plans"
```

---

### Task 3: Alembic 迁移

**Files:**
- Create: `backend/migrations/versions/d1e2f3a4b5c6_add_billing.py`
- Modify: `backend/tests/conftest.py`（仅 Task 2 已做，需确认）

**Interfaces:**
- Consumes: `PLAN_SEEDS`、`FREE_PLAN_ID`（Task 2）
- Produces: 数据库表 `plans`、`billing_statements`；`tenants.plan_id`、`tenants.plan_changed_at`

- [ ] **Step 1: 写迁移**

```python
# backend/migrations/versions/d1e2f3a4b5c6_add_billing.py
"""add billing plans and statements

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-11-25
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.billing.plans import FREE_PLAN_ID, PLAN_SEEDS

revision = "d1e2f3a4b5c6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("limits", JSON, nullable=False),
        sa.Column("prices", JSON, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_plans_code"),
    )
    op.create_index("ix_plans_code", "plans", ["code"])

    op.bulk_insert(
        sa.table(
            "plans",
            sa.column("id", sa.Uuid(as_uuid=True)),
            sa.column("code", sa.String()),
            sa.column("name", sa.String()),
            sa.column("description", sa.Text()),
            sa.column("price_cents", sa.Integer()),
            sa.column("currency", sa.String()),
            sa.column("limits", JSON),
            sa.column("prices", JSON),
            sa.column("status", sa.String()),
        ),
        [
            {
                "id": uuid.UUID(seed["id"]),
                "code": seed["code"],
                "name": seed["name"],
                "description": seed["description"],
                "price_cents": seed["price_cents"],
                "currency": "CNY",
                "limits": seed["limits"],
                "prices": seed["prices"],
                "status": "active",
            }
            for seed in PLAN_SEEDS
        ],
    )

    op.add_column("tenants", sa.Column("plan_id", sa.Uuid(as_uuid=True), nullable=True))
    op.add_column(
        "tenants",
        sa.Column(
            "plan_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
    )
    op.execute(
        sa.text("UPDATE tenants SET plan_id = :plan_id").bindparams(
            plan_id=uuid.UUID(FREE_PLAN_ID)
        )
    )
    op.alter_column("tenants", "plan_id", nullable=False)
    op.create_foreign_key(
        "fk_tenants_plan_id_plans",
        "tenants",
        "plans",
        ["plan_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "billing_statements",
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("plan_code", sa.String(length=50), nullable=False),
        sa.Column("plan_name", sa.String(length=100), nullable=False),
        sa.Column("plan_price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("limits", JSON, nullable=False),
        sa.Column("prices", JSON, nullable=False),
        sa.Column("usage", JSON, nullable=False),
        sa.Column("charges", JSON, nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("closed_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "period", name="uq_billing_statements_tenant_period"
        ),
    )
    op.create_index(
        "ix_billing_statements_tenant_id", "billing_statements", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_statements_tenant_id", table_name="billing_statements"
    )
    op.drop_table("billing_statements")
    op.drop_constraint("fk_tenants_plan_id_plans", "tenants", type_="foreignkey")
    op.drop_column("tenants", "plan_changed_at")
    op.drop_column("tenants", "plan_id")
    op.drop_index("ix_plans_code", table_name="plans")
    op.drop_table("plans")
```

- [ ] **Step 2: 在真实 PostgreSQL 上升级**

Run: `cd backend && uv run alembic upgrade head`
Expected: `Running upgrade c9d0e1f2a3b4 -> d1e2f3a4b5c6`

- [ ] **Step 3: 验证表与列**

Run: `cd backend && uv run alembic current`
Expected: `d1e2f3a4b5c6 (head)`

Run: `cd backend && uv run python -c "import asyncio"` 不允许；改用：

Run: `cd backend && uv run alembic check`
Expected: 无待生成迁移（或仅提示无需变更）

- [ ] **Step 4: 回退与再升级**

Run: `cd backend && uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: 两次成功，退出码 0

- [ ] **Step 5: 回归测试**

Run: `cd backend && uv run pytest tests/test_billing_models.py tests/test_tenants.py -q`
Expected: PASS

- [ ] **Step 6: （如已授权）提交**

```bash
git add backend/migrations/versions/d1e2f3a4b5c6_add_billing.py
git commit -m "feat(billing): migrate plans, tenant plan binding and statements"
```

---

### Task 4: 用量聚合服务

**Files:**
- Create: `backend/app/billing/usage.py`
- Test: `backend/tests/test_billing_usage.py`

**Interfaces:**
- Consumes: `period_bounds(period)`（Task 1）
- Produces: `async def compute_usage(session, tenant_id: str, period: str) -> dict[str, int]`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_billing_usage.py
import uuid
from datetime import UTC, datetime

from app.billing.usage import compute_usage
from app.models.agent import AgentRun
from app.models.media import MediaJob
from app.models.project import Project
from app.models.skill import SkillRun
from app.models.workflow import Workflow, WorkflowRun, WorkflowVersion


async def _skill_run(session, tenant_id, created_at, prompt=0, completion=0):
    session.add(
        SkillRun(
            tenant_id=tenant_id,
            skill_id="title-hook",
            prompt_tokens=prompt,
            completion_tokens=completion,
            created_at=created_at,
        )
    )


async def _agent_run(session, tenant_id, created_at, mode="single", usage=None):
    session.add(
        AgentRun(
            tenant_id=tenant_id,
            user_id=uuid.uuid4(),
            goal="g",
            mode=mode,
            usage=usage or {},
            created_at=created_at,
        )
    )


async def _workflow_run(session, tenant_id, created_at):
    workflow = Workflow(
        tenant_id=tenant_id, name="流", created_by=uuid.uuid4()
    )
    session.add(workflow)
    await session.flush()
    version = WorkflowVersion(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph={},
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=created_at,
    )
    session.add(version)
    await session.flush()
    session.add(
        WorkflowRun(
            tenant_id=tenant_id,
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            created_at=created_at,
        )
    )


async def _media_job(session, tenant_id, created_at, started, finished, status="succeeded"):
    project = Project(tenant_id=tenant_id, title="项目")
    session.add(project)
    await session.flush()
    session.add(
        MediaJob(
            tenant_id=tenant_id,
            project_id=project.id,
            skill_id="short-video",
            status=status,
            started_at=started,
            finished_at=finished,
            created_at=created_at,
        )
    )


async def test_usage_aggregates_period_and_tenant(db_session, tenant_factory):
    tenant = await tenant_factory()
    other = await tenant_factory()
    inside = datetime(2026, 10, 15, tzinfo=UTC)
    outside = datetime(2026, 9, 15, tzinfo=UTC)
    await _skill_run(db_session, tenant.id, inside, 1200, 800)
    await _skill_run(db_session, tenant.id, outside, 9000, 9000)
    await _skill_run(db_session, other.id, inside, 5000, 5000)
    await _agent_run(
        db_session,
        tenant.id,
        inside,
        usage={"prompt_tokens": 300, "completion_tokens": 200},
    )
    await _agent_run(
        db_session,
        tenant.id,
        inside,
        mode="role",
        usage={"prompt_tokens": 100, "completion_tokens": 50},
    )
    await _workflow_run(db_session, tenant.id, inside)
    await _media_job(
        db_session,
        tenant.id,
        inside,
        inside,
        inside.replace(minute=20),
    )
    await _media_job(
        db_session,
        tenant.id,
        inside,
        inside,
        inside.replace(minute=30),
        status="failed",
    )
    await db_session.commit()

    usage = await compute_usage(db_session, tenant.id, "2026-10")
    assert usage["llm.prompt_tokens"] == 1600
    assert usage["llm.completion_tokens"] == 1050
    assert usage["run.skill"] == 1
    assert usage["run.agent"] == 1
    assert usage["run.workflow"] == 1
    assert usage["media.jobs"] == 2
    assert usage["media.seconds"] == 1200


async def test_usage_respects_beijing_month_boundary(db_session, tenant_factory):
    tenant = await tenant_factory()
    before = datetime(2026, 10, 31, 15, 59, 59, tzinfo=UTC)
    at_start = datetime(2026, 10, 31, 16, 0, 0, tzinfo=UTC)
    at_end = datetime(2026, 11, 30, 16, 0, 0, tzinfo=UTC)
    await _skill_run(db_session, tenant.id, before, 100, 100)
    await _skill_run(db_session, tenant.id, at_start, 200, 200)
    await _skill_run(db_session, tenant.id, at_end, 400, 400)
    await db_session.commit()

    usage = await compute_usage(db_session, tenant.id, "2026-11")
    assert usage["llm.prompt_tokens"] == 200
    assert usage["run.skill"] == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_billing_usage.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.billing.usage'`

- [ ] **Step 3: 实现聚合**

```python
# backend/app/billing/usage.py
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.periods import period_bounds
from app.models.agent import AgentRun
from app.models.media import MediaJob
from app.models.skill import SkillRun
from app.models.workflow import WorkflowRun

_AGENT_RUN_MODES = ("single", "node", "team")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def _skill_tokens(
    session: AsyncSession, tenant_id: str, start, end
) -> tuple[int, int]:
    row = (
        await session.execute(
            select(
                func.coalesce(func.sum(SkillRun.prompt_tokens), 0),
                func.coalesce(func.sum(SkillRun.completion_tokens), 0),
            ).where(
                SkillRun.tenant_id == tenant_id,
                SkillRun.created_at >= start,
                SkillRun.created_at < end,
            )
        )
    ).one()
    return _as_int(row[0]), _as_int(row[1])


async def _agent_usage(
    session: AsyncSession, tenant_id: str, start, end
) -> tuple[int, int, int]:
    rows = (
        await session.execute(
            select(AgentRun.usage, AgentRun.mode).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.created_at >= start,
                AgentRun.created_at < end,
            )
        )
    ).all()
    prompt = completion = runs = 0
    for usage, mode in rows:
        payload = usage if isinstance(usage, dict) else {}
        prompt += _as_int(payload.get("prompt_tokens"))
        completion += _as_int(payload.get("completion_tokens"))
        if mode in _AGENT_RUN_MODES:
            runs += 1
    return prompt, completion, runs


async def _count(
    session: AsyncSession, model, tenant_id: str, start, end
) -> int:
    return _as_int(
        (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == tenant_id,
                    model.created_at >= start,
                    model.created_at < end,
                )
            )
        ).scalar_one()
    )


async def _media(
    session: AsyncSession, tenant_id: str, start, end
) -> tuple[int, int]:
    rows = (
        await session.execute(
            select(
                MediaJob.status,
                MediaJob.started_at,
                MediaJob.finished_at,
            ).where(
                MediaJob.tenant_id == tenant_id,
                MediaJob.created_at >= start,
                MediaJob.created_at < end,
            )
        )
    ).all()
    seconds = 0
    for status, started_at, finished_at in rows:
        if status != "succeeded" or started_at is None or finished_at is None:
            continue
        seconds += max(0, int((finished_at - started_at).total_seconds()))
    return len(rows), seconds


async def compute_usage(
    session: AsyncSession, tenant_id: str, period: str
) -> dict[str, int]:
    start, end = period_bounds(period)
    prompt, completion = await _skill_tokens(session, tenant_id, start, end)
    agent_prompt, agent_completion, agent_runs = await _agent_usage(
        session, tenant_id, start, end
    )
    media_jobs, media_seconds = await _media(session, tenant_id, start, end)
    return {
        "llm.prompt_tokens": prompt + agent_prompt,
        "llm.completion_tokens": completion + agent_completion,
        "run.skill": await _count(session, SkillRun, tenant_id, start, end),
        "run.agent": agent_runs,
        "run.workflow": await _count(
            session, WorkflowRun, tenant_id, start, end
        ),
        "media.jobs": media_jobs,
        "media.seconds": media_seconds,
    }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_billing_usage.py -q`
Expected: PASS

- [ ] **Step 5: Lint 与（如已授权）提交**

Run: `cd backend && uv run ruff check app/billing/usage.py tests/test_billing_usage.py`

如已授权：

```bash
git add backend/app/billing/usage.py backend/tests/test_billing_usage.py
git commit -m "feat(billing): aggregate tenant usage from existing run tables"
```

---

### Task 5: 账单计算、关账与重开服务

**Files:**
- Create: `backend/app/billing/statements.py`
- Test: `backend/tests/test_billing_statements.py`

**Interfaces:**
- Consumes: `compute_usage`（Task 4）、`build_charges`（Task 1）、`BillingStatement`/`Plan`（Task 2）
- Produces:
  - `StatementData`（`plan_code`、`plan_name`、`plan_price_cents`、`currency`、`limits`、`prices`、`usage`、`charges`、`amount_cents`）
  - `async def build_statement_data(session, tenant, period) -> StatementData`
  - `async def close_statement(session, *, tenant, period, actor, request=None) -> BillingStatement`
  - `async def reopen_statement(session, *, tenant, period, actor, request=None) -> BillingStatement`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_billing_statements.py
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.billing.statements import (
    build_statement_data,
    close_statement,
    reopen_statement,
)
from app.errors import AppError
from app.models.billing import BillingStatement, Plan
from app.models.skill import SkillRun
from app.models.user import User


def _actor(tenant_id: str) -> User:
    return User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        email="admin@local",
        password_hash="x",
        display_name="管理员",
        role="owner",
        is_active=True,
        must_change_password=False,
        is_platform_admin=True,
        token_version=0,
    )


async def _prepare(db_session, tenant_factory):
    tenant = await tenant_factory()
    plan = (
        await db_session.execute(select(Plan).where(Plan.code == "free"))
    ).scalar_one()
    plan.price_cents = 9_900
    plan.limits = {"llm.prompt_tokens": 1_000}
    plan.prices = {"llm.prompt_tokens": 2}
    db_session.add(
        SkillRun(
            tenant_id=tenant.id,
            skill_id="title-hook",
            prompt_tokens=1_500,
            created_at=datetime(2026, 10, 15, tzinfo=UTC),
        )
    )
    await db_session.commit()
    return tenant, plan


async def test_statement_data_snapshots_plan_and_charges(
    db_session, tenant_factory
):
    tenant, plan = await _prepare(db_session, tenant_factory)
    data = await build_statement_data(db_session, tenant, "2026-10")
    assert data.plan_code == "free"
    assert data.plan_price_cents == 9_900
    assert data.usage["llm.prompt_tokens"] == 1_500
    assert data.charges == [
        {
            "dimension": "llm.prompt_tokens",
            "used": 1_500,
            "included": 1_000,
            "overage": 500,
            "unit_size": 1000,
            "price_cents": 2,
            "amount_cents": 1,
        }
    ]
    assert data.amount_cents == 9_901


async def test_close_is_idempotent_and_immutable(db_session, tenant_factory):
    tenant, plan = await _prepare(db_session, tenant_factory)
    actor = _actor(tenant.id)
    statement = await close_statement(
        db_session, tenant=tenant, period="2026-10", actor=actor
    )
    assert statement.status == "closed"
    assert statement.amount_cents == 9_901

    plan.price_cents = 1
    plan.prices = {}
    db_session.add(plan)
    await db_session.commit()
    stored = (
        await db_session.execute(
            select(BillingStatement).where(
                BillingStatement.tenant_id == tenant.id,
                BillingStatement.period == "2026-10",
            )
        )
    ).scalar_one()
    assert stored.amount_cents == 9_901
    assert stored.plan_price_cents == 9_900

    with pytest.raises(AppError) as exc:
        await close_statement(
            db_session, tenant=tenant, period="2026-10", actor=actor
        )
    assert exc.value.code == "statement_exists"


async def test_reopen_allows_recompute(db_session, tenant_factory):
    tenant, _ = await _prepare(db_session, tenant_factory)
    actor = _actor(tenant.id)
    await close_statement(
        db_session, tenant=tenant, period="2026-10", actor=actor
    )
    reopened = await reopen_statement(
        db_session, tenant=tenant, period="2026-10", actor=actor
    )
    assert reopened.status == "open"
    assert reopened.closed_at is None

    with pytest.raises(AppError) as exc:
        await reopen_statement(
            db_session, tenant=tenant, period="2026-10", actor=actor
        )
    assert exc.value.code == "statement_already_open"

    await db_session.execute(
        select(SkillRun).where(SkillRun.tenant_id == tenant.id)
    )
    again = await close_statement(
        db_session, tenant=tenant, period="2026-10", actor=actor
    )
    assert again.status == "closed"
    assert again.amount_cents == 9_901


async def test_close_rejects_unfinished_period(db_session, tenant_factory):
    tenant, _ = await _prepare(db_session, tenant_factory)
    actor = _actor(tenant.id)
    current = datetime.now(UTC)
    period = f"{current.year:04d}-{current.month:02d}"
    with pytest.raises(AppError) as exc:
        await close_statement(
            db_session, tenant=tenant, period=period, actor=actor
        )
    assert exc.value.code == "period_not_finished"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_billing_statements.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'app.billing.statements'`

- [ ] **Step 3: 实现账单服务**

```python
# backend/app/billing/statements.py
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.periods import period_finished
from app.billing.pricing import build_charges
from app.billing.usage import compute_usage
from app.errors import AppError
from app.models.billing import BillingStatement, Plan
from app.models.tenant import Tenant
from app.models.user import User
from app.services.audit import record


@dataclass(frozen=True)
class StatementData:
    plan_code: str
    plan_name: str
    plan_price_cents: int
    currency: str
    limits: dict
    prices: dict
    usage: dict
    charges: list
    amount_cents: int


async def build_statement_data(
    session: AsyncSession, tenant: Tenant, period: str
) -> StatementData:
    plan = await session.get(Plan, tenant.plan_id)
    if plan is None:
        raise AppError("plan_missing", "租户未绑定套餐", 500)
    limits = dict(plan.limits or {})
    prices = dict(plan.prices or {})
    usage = await compute_usage(session, tenant.id, period)
    charges = build_charges(usage, limits, prices)
    amount = plan.price_cents + sum(item["amount_cents"] for item in charges)
    return StatementData(
        plan_code=plan.code,
        plan_name=plan.name,
        plan_price_cents=plan.price_cents,
        currency=plan.currency,
        limits=limits,
        prices=prices,
        usage=usage,
        charges=charges,
        amount_cents=amount,
    )


async def _existing(
    session: AsyncSession, tenant_id: str, period: str
) -> BillingStatement | None:
    return (
        await session.execute(
            select(BillingStatement).where(
                BillingStatement.tenant_id == tenant_id,
                BillingStatement.period == period,
            )
        )
    ).scalar_one_or_none()


async def close_statement(
    session: AsyncSession,
    *,
    tenant: Tenant,
    period: str,
    actor: User,
    request: Request | None = None,
) -> BillingStatement:
    if not period_finished(period):
        raise AppError("period_not_finished", "账期尚未结束", 409)
    existing = await _existing(session, tenant.id, period)
    if existing is not None and existing.status == "closed":
        raise AppError("statement_exists", "该账期已关账", 409)
    data = await build_statement_data(session, tenant, period)
    if existing is None:
        existing = BillingStatement(tenant_id=tenant.id, period=period)
        session.add(existing)
    existing.status = "closed"
    existing.plan_code = data.plan_code
    existing.plan_name = data.plan_name
    existing.plan_price_cents = data.plan_price_cents
    existing.currency = data.currency
    existing.limits = data.limits
    existing.prices = data.prices
    existing.usage = data.usage
    existing.charges = data.charges
    existing.amount_cents = data.amount_cents
    existing.closed_by = actor.id
    existing.closed_at = datetime.now(UTC)
    await record(
        session,
        tenant_id=actor.tenant_id,
        action="billing.close",
        actor=actor,
        resource_type="tenant",
        resource_id=tenant.id,
        detail={"period": period, "amount_cents": data.amount_cents},
        request=request,
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("statement_exists", "该账期已关账", 409) from None
    await session.refresh(existing)
    return existing


async def reopen_statement(
    session: AsyncSession,
    *,
    tenant: Tenant,
    period: str,
    actor: User,
    request: Request | None = None,
) -> BillingStatement:
    statement = await _existing(session, tenant.id, period)
    if statement is None:
        raise AppError("statement_not_found", "账单不存在", 404)
    if statement.status != "closed":
        raise AppError("statement_already_open", "账单已处于重开状态", 409)
    statement.status = "open"
    statement.closed_by = None
    statement.closed_at = None
    statement.reopened_at = datetime.now(UTC)
    await record(
        session,
        tenant_id=actor.tenant_id,
        action="billing.reopen",
        actor=actor,
        resource_type="tenant",
        resource_id=tenant.id,
        detail={"period": period},
        request=request,
    )
    await session.commit()
    await session.refresh(statement)
    return statement
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_billing_statements.py -q`
Expected: PASS

- [ ] **Step 5: Lint 与（如已授权）提交**

Run: `cd backend && uv run ruff check app/billing/statements.py tests/test_billing_statements.py`

如已授权：

```bash
git add backend/app/billing/statements.py backend/tests/test_billing_statements.py
git commit -m "feat(billing): compute statements and support close/reopen"
```

---

### Task 6: 套餐与指派 API、租户套餐字段

**Files:**
- Create: `backend/app/api/billing.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/tenants.py`
- Test: `backend/tests/test_billing_api.py`

**Interfaces:**
- Consumes: `Plan`、`PLAN_SEEDS`、`DEFAULT_PLAN_CODE`（Task 2）
- Produces:
  - `GET/POST /api/billing/plans`、`PATCH /api/billing/plans/{code}`
  - `PATCH /api/billing/tenants/{tenant_id}/plan`
  - `TenantOut(plan_code, plan_name)`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_billing_api.py
from contextlib import asynccontextmanager
import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.billing import Plan
from app.models.user import User
from app.models.base import DEFAULT_TENANT_ID


@asynccontextmanager
async def _role_client(db_session, role: str, platform_admin: bool = False):
    fake = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email=f"{role}-billing@local",
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


async def test_platform_admin_manages_plans(auth_client, auth_headers):
    created = await auth_client.post(
        "/api/billing/plans",
        headers=auth_headers,
        json={
            "code": "starter",
            "name": "入门版",
            "description": "测试套餐",
            "price_cents": 1000,
            "limits": {"llm.prompt_tokens": 1000},
            "prices": {"llm.prompt_tokens": 1},
        },
    )
    assert created.status_code == 201
    assert created.json()["code"] == "starter"

    dup = await auth_client.post(
        "/api/billing/plans",
        headers=auth_headers,
        json={"code": "starter", "name": "重复"},
    )
    assert dup.status_code == 409
    assert dup.json()["code"] == "plan_exists"

    patched = await auth_client.patch(
        "/api/billing/plans/starter",
        headers=auth_headers,
        json={"price_cents": 2000, "status": "archived"},
    )
    assert patched.status_code == 200
    assert patched.json()["price_cents"] == 2000
    assert patched.json()["status"] == "archived"


async def test_invalid_dimensions_are_rejected(auth_client, auth_headers):
    resp = await auth_client.post(
        "/api/billing/plans",
        headers=auth_headers,
        json={"code": "bad", "name": "坏套餐", "limits": {"x.y": 1}},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "plan_dimension_invalid"

    priced = await auth_client.post(
        "/api/billing/plans",
        headers=auth_headers,
        json={"code": "bad2", "name": "坏套餐", "prices": {"run.skill": 1}},
    )
    assert priced.status_code == 422
    assert priced.json()["code"] == "plan_dimension_invalid"


async def test_only_platform_admin_manages_plans(db_session):
    async with _role_client(db_session, "admin") as client:
        assert (await client.get("/api/billing/plans")).status_code == 403


async def test_assign_plan_to_tenant_and_audit(
    auth_client, auth_headers, db_session
):
    created = await auth_client.post(
        "/api/tenants",
        headers=auth_headers,
        json={
            "name": "客户 B",
            "slug": "customer-b-plan",
            "owner_email": "owner-b-plan@local",
            "owner_display_name": "客户 B 管理员",
        },
    )
    assert created.status_code == 201
    tenant_id = created.json()["id"]
    assert created.json()["plan_code"] == "free"

    changed = await auth_client.patch(
        f"/api/billing/tenants/{tenant_id}/plan",
        headers=auth_headers,
        json={"plan_code": "basic"},
    )
    assert changed.status_code == 200
    assert changed.json()["plan_code"] == "basic"
    assert changed.json()["plan_name"] == "基础版"

    archived = await auth_client.patch(
        "/api/billing/plans/basic",
        headers=auth_headers,
        json={"status": "archived"},
    )
    assert archived.status_code == 200
    rejected = await auth_client.patch(
        f"/api/billing/tenants/{tenant_id}/plan",
        headers=auth_headers,
        json={"plan_code": "basic"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "plan_archived"

    listed = await auth_client.get("/api/tenants", headers=auth_headers)
    row = next(item for item in listed.json() if item["id"] == tenant_id)
    assert row["plan_code"] == "basic"
    assert row["plan_name"] == "基础版"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_billing_api.py -q`
Expected: FAIL，`404`（路由不存在）

- [ ] **Step 3: 实现套餐与指派 API**

```python
# backend/app/api/billing.py
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.dimensions import unknown_dimensions
from app.billing.dimensions import priced_non_billable
from app.billing.plans import DEFAULT_PLAN_CODE
from app.db import get_session
from app.errors import AppError
from app.models.billing import Plan
from app.models.tenant import Tenant
from app.models.user import User
from app.permissions import admin_access, require_platform_admin
from app.services.audit import record

router = APIRouter(prefix="/billing", tags=["billing"])

PlanCode = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,49}$")
]
PlanName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class PlanOut(BaseModel):
    code: str
    name: str
    description: str
    price_cents: int
    currency: str
    limits: dict[str, int]
    prices: dict[str, int]
    status: str
    updated_at: datetime


class PlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: PlanCode
    name: PlanName
    description: Annotated[str, StringConstraints(max_length=500)] = ""
    price_cents: Annotated[int, Field(ge=0)] = 0
    limits: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)
    prices: dict[str, Annotated[int, Field(ge=0)]] = Field(default_factory=dict)


class PlanPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: PlanName | None = None
    description: Annotated[str, StringConstraints(max_length=500)] | None = None
    price_cents: Annotated[int, Field(ge=0)] | None = None
    limits: dict[str, Annotated[int, Field(ge=0)]] | None = None
    prices: dict[str, Annotated[int, Field(ge=0)]] | None = None
    status: str | None = None


class AssignPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_code: PlanCode


def _plan_out(plan: Plan) -> PlanOut:
    return PlanOut(
        code=plan.code,
        name=plan.name,
        description=plan.description,
        price_cents=plan.price_cents,
        currency=plan.currency,
        limits=plan.limits or {},
        prices=plan.prices or {},
        status=plan.status,
        updated_at=plan.updated_at,
    )


def _validate_maps(limits: dict | None, prices: dict | None) -> None:
    limits = limits or {}
    prices = prices or {}
    keys = set(limits) | set(prices)
    unknown = unknown_dimensions({key: 0 for key in keys})
    if unknown:
        raise AppError(
            "plan_dimension_invalid",
            f"未知计费维度：{', '.join(unknown)}",
            422,
        )
    bad = priced_non_billable(prices)
    if bad:
        raise AppError(
            "plan_dimension_invalid",
            f"该维度不可计价：{', '.join(bad)}",
            422,
        )


async def _get_plan(session: AsyncSession, code: str) -> Plan:
    plan = (
        await session.execute(select(Plan).where(Plan.code == code))
    ).scalar_one_or_none()
    if plan is None:
        raise AppError("plan_not_found", "套餐不存在", 404)
    return plan


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    _: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[PlanOut]:
    rows = (
        await session.execute(select(Plan).order_by(Plan.price_cents, Plan.code))
    ).scalars().all()
    return [_plan_out(plan) for plan in rows]


@router.post("/plans", response_model=PlanOut, status_code=201)
async def create_plan(
    payload: PlanIn,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PlanOut:
    _validate_maps(payload.limits, payload.prices)
    session.add(
        Plan(
            code=payload.code,
            name=payload.name,
            description=payload.description,
            price_cents=payload.price_cents,
            currency="CNY",
            limits=payload.limits,
            prices=payload.prices,
            status="active",
        )
    )
    try:
        await record(
            session,
            tenant_id=user.tenant_id,
            action="plan.create",
            actor=user,
            resource_type="plan",
            resource_id=payload.code,
            detail={"price_cents": payload.price_cents},
            request=request,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("plan_exists", "套餐标识已存在", 409) from None
    return _plan_out(await _get_plan(session, payload.code))


@router.patch("/plans/{code}", response_model=PlanOut)
async def update_plan(
    code: str,
    payload: PlanPatch,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PlanOut:
    if not payload.model_fields_set:
        raise AppError("empty_update", "至少需要提供一个要更新的字段", 422)
    plan = await _get_plan(session, code)
    fields = payload.model_dump(exclude_unset=True)
    if "status" in fields and fields["status"] not in ("active", "archived"):
        raise AppError("plan_status_invalid", "套餐状态无效", 422)
    _validate_maps(fields.get("limits"), fields.get("prices"))
    for key, value in fields.items():
        setattr(plan, key, value)
    await record(
        session,
        tenant_id=user.tenant_id,
        action="plan.update",
        actor=user,
        resource_type="plan",
        resource_id=plan.code,
        detail={"fields": sorted(fields)},
        request=request,
    )
    await session.commit()
    await session.refresh(plan)
    return _plan_out(plan)


@router.patch("/tenants/{tenant_id}/plan", response_model=PlanOut)
async def assign_tenant_plan(
    tenant_id: str,
    payload: AssignPlanIn,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PlanOut:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    plan = await _get_plan(session, payload.plan_code)
    if plan.status != "active":
        raise AppError("plan_archived", "该套餐已归档，不能指派", 409)
    tenant.plan_id = plan.id
    tenant.plan_changed_at = datetime.now(UTC)
    await record(
        session,
        tenant_id=user.tenant_id,
        action="tenant.plan_change",
        actor=user,
        resource_type="tenant",
        resource_id=tenant.id,
        detail={"plan_code": plan.code},
        request=request,
    )
    await session.commit()
    await session.refresh(plan)
    return _plan_out(plan)
```

- [ ] **Step 4: 注册路由并扩展租户输出**

`backend/app/main.py` 导入区加入 `from app.api import billing`，并在 `tenants` 路由之后注册：

```python
app.include_router(billing.router, prefix="/api")
```

`backend/app/api/tenants.py`：

```python
from app.billing.plans import DEFAULT_PLAN_CODE
from app.models.billing import Plan


class TenantOut(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    plan_code: str
    plan_name: str
    members_count: int
    created_at: datetime


def _out(
    tenant: Tenant, members_count: int, plan_code: str, plan_name: str
) -> TenantOut:
    return TenantOut(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        plan_code=plan_code,
        plan_name=plan_name,
        members_count=members_count,
        created_at=tenant.created_at,
    )
```

`list_tenants` 查询改为联表：

```python
    rows = (
        await session.execute(
            select(
                Tenant,
                func.coalesce(counts.c.cnt, 0),
                Plan.code,
                Plan.name,
            )
            .join(Plan, Plan.id == Tenant.plan_id)
            .outerjoin(counts, counts.c.tenant_id == Tenant.id)
            .order_by(Tenant.created_at.desc(), Tenant.id.desc())
        )
    ).all()
    return [
        _out(tenant, count, plan_code, plan_name)
        for tenant, count, plan_code, plan_name in rows
    ]
```

`create_tenant` 在创建租户前取默认套餐，并写入 `plan_id`：

```python
    default_plan = (
        await session.execute(
            select(Plan).where(Plan.code == DEFAULT_PLAN_CODE)
        )
    ).scalar_one_or_none()
    if default_plan is None:
        raise AppError("default_plan_missing", "默认套餐未配置", 500)
```

```python
    tenant = Tenant(
        id=str(uuid.uuid4()),
        name=payload.name,
        slug=payload.slug,
        status="active",
        plan_id=default_plan.id,
    )
```

`create_tenant` 的返回改为：

```python
    return TenantCreatedOut(
        **_out(
            tenant,
            1,
            default_plan.code,
            default_plan.name,
        ).model_dump(),
        owner=owner_out,
    )
```

`update_tenant` 返回改为：

```python
    plan = await session.get(Plan, tenant.plan_id)
    return _out(tenant, await _member_count(session, tenant.id), plan.code, plan.name)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_billing_api.py tests/test_tenants.py -q`
Expected: PASS

- [ ] **Step 6: Lint 与（如已授权）提交**

Run: `cd backend && uv run ruff check app/api/billing.py app/api/tenants.py app/main.py tests/test_billing_api.py`

如已授权：

```bash
git add backend/app/api/billing.py backend/app/api/tenants.py \
  backend/app/main.py backend/tests/test_billing_api.py
git commit -m "feat(billing): add plan management and tenant plan assignment"
```

---

### Task 7: 租户账单读取 API

**Files:**
- Modify: `backend/app/api/billing.py`
- Test: `backend/tests/test_billing_usage_api.py`

**Interfaces:**
- Consumes: `compute_usage`、`build_usage_rows`、`build_statement_data`、`BillingStatement`
- Produces:
  - `GET /api/billing/plan`
  - `GET /api/billing/usage?period=`
  - `GET /api/billing/statements?limit=12`
  - `GET /api/billing/statements/{period}`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_billing_usage_api.py
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.billing.statements import close_statement
from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.skill import SkillRun
from app.models.tenant import Tenant
from app.models.user import User


@asynccontextmanager
async def _tenant_client(db_session, tenant: Tenant, role: str = "admin"):
    fake = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email=f"{role}-{tenant.slug}@local",
        password_hash="x",
        display_name=role,
        role=role,
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    app.dependency_overrides[get_current_user] = lambda: fake
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, fake
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_session, None)


async def test_usage_preview_and_admin_only(db_session, tenant_factory):
    tenant = await tenant_factory(name="用量租户")
    db_session.add(
        SkillRun(
            tenant_id=tenant.id,
            skill_id="title-hook",
            prompt_tokens=1200,
            completion_tokens=800,
            created_at=datetime(2026, 10, 15, tzinfo=UTC),
        )
    )
    await db_session.commit()

    async with _tenant_client(db_session, tenant, "admin") as (client, _):
        resp = await client.get("/api/billing/usage", params={"period": "2026-10"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["period"] == "2026-10"
        assert body["plan"]["code"] == "free"
        rows = {row["dimension"]: row for row in body["rows"]}
        assert rows["llm.prompt_tokens"]["used"] == 1200
        assert rows["llm.prompt_tokens"]["limit"] == 200_000
        assert body["amount_cents"] == 0

    async with _tenant_client(db_session, tenant, "editor") as (client, _):
        assert (await client.get("/api/billing/usage")).status_code == 403


async def test_bad_period_is_rejected(db_session, tenant_factory):
    tenant = await tenant_factory()
    async with _tenant_client(db_session, tenant) as (client, _):
        resp = await client.get("/api/billing/usage", params={"period": "2026-13"})
        assert resp.status_code == 422
        assert resp.json()["code"] == "period_invalid"


async def test_statement_list_and_detail(db_session, tenant_factory):
    tenant = await tenant_factory()
    db_session.add(
        SkillRun(
            tenant_id=tenant.id,
            skill_id="title-hook",
            prompt_tokens=100,
            created_at=datetime(2026, 10, 15, tzinfo=UTC),
        )
    )
    await db_session.commit()

    async with _tenant_client(db_session, tenant) as (client, fake):
        await close_statement(
            db_session, tenant=tenant, period="2026-10", actor=fake
        )
        listed = await client.get("/api/billing/statements")
        assert listed.status_code == 200
        assert [item["period"] for item in listed.json()] == ["2026-10"]

        detail = await client.get("/api/billing/statements/2026-10")
        assert detail.status_code == 200
        assert detail.json()["status"] == "closed"

        missing = await client.get("/api/billing/statements/2026-09")
        assert missing.status_code == 404
        assert missing.json()["code"] == "statement_not_found"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_billing_usage_api.py -q`
Expected: FAIL，`404`（`/api/billing/usage` 不存在）

- [ ] **Step 3: 实现读取 API**

在 `backend/app/api/billing.py` 增加导入：

```python
from app.billing.periods import normalize_period, period_bounds
from app.billing.pricing import build_usage_rows
from app.models.billing import BillingStatement
```

在文件末尾追加：

```python
class UsageRowOut(BaseModel):
    dimension: str
    label: str
    unit: str
    unit_size: int
    used: int
    limit: int | None
    remaining: int | None
    percent: int | None
    over_limit: bool
    price_cents: int
    amount_cents: int


class UsageOut(BaseModel):
    period: str
    period_start: datetime
    period_end: datetime
    plan: PlanOut
    rows: list[UsageRowOut]
    amount_cents: int


class StatementOut(BaseModel):
    period: str
    status: str
    plan_code: str
    plan_name: str
    plan_price_cents: int
    currency: str
    limits: dict[str, int]
    prices: dict[str, int]
    usage: dict[str, int]
    charges: list[dict]
    amount_cents: int
    closed_at: datetime | None
    reopened_at: datetime | None


def _statement_out(row: BillingStatement) -> StatementOut:
    return StatementOut(
        period=row.period,
        status=row.status,
        plan_code=row.plan_code,
        plan_name=row.plan_name,
        plan_price_cents=row.plan_price_cents,
        currency=row.currency,
        limits=row.limits or {},
        prices=row.prices or {},
        usage=row.usage or {},
        charges=row.charges or [],
        amount_cents=row.amount_cents,
        closed_at=row.closed_at,
        reopened_at=row.reopened_at,
    )


async def _tenant_plan(
    session: AsyncSession, tenant: Tenant
) -> Plan:
    plan = await session.get(Plan, tenant.plan_id)
    if plan is None:
        raise AppError("plan_missing", "租户未绑定套餐", 500)
    return plan


@router.get("/plan", response_model=PlanOut)
async def current_plan(
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PlanOut:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    return _plan_out(await _tenant_plan(session, tenant))


@router.get("/usage", response_model=UsageOut)
async def current_usage(
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    period: str | None = None,
) -> UsageOut:
    try:
        normalized = normalize_period(period)
    except ValueError:
        raise AppError("period_invalid", "账期格式应为 YYYY-MM", 422) from None
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    plan = await _tenant_plan(session, tenant)
    usage = await compute_usage(session, tenant.id, normalized)
    limits = plan.limits or {}
    prices = plan.prices or {}
    rows = build_usage_rows(usage, limits, prices)
    start, end = period_bounds(normalized)
    return UsageOut(
        period=normalized,
        period_start=start,
        period_end=end,
        plan=_plan_out(plan),
        rows=rows,
        amount_cents=plan.price_cents
        + sum(row["amount_cents"] for row in rows),
    )


@router.get("/statements", response_model=list[StatementOut])
async def list_statements(
    limit: int = 12,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[StatementOut]:
    if limit < 1 or limit > 36:
        raise AppError("limit_invalid", "limit 需在 1-36 之间", 422)
    rows = (
        await session.execute(
            select(BillingStatement)
            .where(BillingStatement.tenant_id == user.tenant_id)
            .order_by(BillingStatement.period.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [_statement_out(row) for row in rows]


@router.get("/statements/{period}", response_model=StatementOut)
async def get_statement(
    period: str,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StatementOut:
    row = (
        await session.execute(
            select(BillingStatement).where(
                BillingStatement.tenant_id == user.tenant_id,
                BillingStatement.period == period,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("statement_not_found", "账单不存在", 404)
    return _statement_out(row)
```

同时在导入区加入：

```python
from app.billing.usage import compute_usage
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_billing_usage_api.py tests/test_billing_api.py -q`
Expected: PASS

- [ ] **Step 5: Lint 与（如已授权）提交**

Run: `cd backend && uv run ruff check app/api/billing.py tests/test_billing_usage_api.py`

如已授权：

```bash
git add backend/app/api/billing.py backend/tests/test_billing_usage_api.py
git commit -m "feat(billing): expose tenant usage and statement read APIs"
```

---

### Task 8: 平台总览与关账 API

**Files:**
- Modify: `backend/app/api/billing.py`
- Test: `backend/tests/test_billing_platform_api.py`

**Interfaces:**
- Consumes: `StatementData`、`close_statement`、`reopen_statement`、`compute_usage`
- Produces:
  - `GET /api/billing/platform/usage?period=`
  - `POST /api/billing/platform/statements/{tenant_id}/{period}/close`
  - `POST /api/billing/platform/statements/{tenant_id}/{period}/reopen`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_billing_platform_api.py
from datetime import UTC, datetime

from app.models.skill import SkillRun


async def test_platform_overview_lists_tenants(
    auth_client, auth_headers, tenant_factory
):
    tenant = await tenant_factory(name="总览租户")
    other = await tenant_factory(name="其它租户")
    from sqlalchemy import select
    from app.models.tenant import Tenant as TenantModel

    # tenant_factory 只提交租户；这里直接查询确认两个租户都出现在总览
    resp = await auth_client.get(
        "/api/billing/platform/usage", headers=auth_headers
    )
    assert resp.status_code == 200
    rows = {row["tenant_id"]: row for row in resp.json()}
    assert rows[tenant.id]["plan_code"] == "free"
    assert rows[other.id]["plan_code"] == "free"
    assert rows[tenant.id]["amount_cents"] == 0


async def test_close_and_reopen_statement(auth_client, auth_headers, tenant_factory):
    tenant = await tenant_factory()
    from sqlalchemy import select
    from app.models.tenant import Tenant as TenantModel

    closed = await auth_client.post(
        f"/api/billing/platform/statements/{tenant.id}/2026-10/close",
        headers=auth_headers,
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"

    again = await auth_client.post(
        f"/api/billing/platform/statements/{tenant.id}/2026-10/close",
        headers=auth_headers,
    )
    assert again.status_code == 409
    assert again.json()["code"] == "statement_exists"

    reopened = await auth_client.post(
        f"/api/billing/platform/statements/{tenant.id}/2026-10/reopen",
        headers=auth_headers,
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"

    reclosing = await auth_client.post(
        f"/api/billing/platform/statements/{tenant.id}/2026-10/close",
        headers=auth_headers,
    )
    assert reclosing.status_code == 200


async def test_close_rejects_unfinished_and_unknown_tenant(
    auth_client, auth_headers, tenant_factory
):
    tenant = await tenant_factory()
    current = datetime.now(UTC)
    period = f"{current.year:04d}-{current.month:02d}"
    resp = await auth_client.post(
        f"/api/billing/platform/statements/{tenant.id}/{period}/close",
        headers=auth_headers,
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "period_not_finished"

    missing = await auth_client.post(
        "/api/billing/platform/statements/00000000-0000-0000-0000-0000000000ff/2026-10/close",
        headers=auth_headers,
    )
    assert missing.status_code == 404


async def test_non_platform_admin_is_rejected(auth_client):
    # 未带 token 时由鉴权层拒绝
    resp = await auth_client.get("/api/billing/platform/usage")
    assert resp.status_code == 401
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && uv run pytest tests/test_billing_platform_api.py -q`
Expected: FAIL，`404`（平台路由不存在）

- [ ] **Step 3: 实现平台 API**

在 `backend/app/api/billing.py` 导入区加入：

```python
from app.billing.statements import close_statement, reopen_statement
```

在文件末尾追加：

```python
class PlatformUsageRow(BaseModel):
    tenant_id: str
    tenant_name: str
    tenant_slug: str
    plan_code: str
    plan_name: str
    prompt_tokens: int
    completion_tokens: int
    amount_cents: int
    statement_status: str | None


@router.get("/platform/usage", response_model=list[PlatformUsageRow])
async def platform_usage(
    period: str | None = None,
    _: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[PlatformUsageRow]:
    try:
        normalized = normalize_period(period)
    except ValueError:
        raise AppError("period_invalid", "账期格式应为 YYYY-MM", 422) from None
    tenants = (
        await session.execute(
            select(Tenant).order_by(Tenant.created_at.desc(), Tenant.id.desc())
        )
    ).scalars().all()
    statements = {
        row.tenant_id: row.status
        for row in (
            await session.execute(
                select(BillingStatement).where(
                    BillingStatement.period == normalized
                )
            )
        ).scalars().all()
    }
    rows: list[PlatformUsageRow] = []
    for tenant in tenants:
        plan = await _tenant_plan(session, tenant)
        usage = await compute_usage(session, tenant.id, normalized)
        rows.append(
            PlatformUsageRow(
                tenant_id=tenant.id,
                tenant_name=tenant.name,
                tenant_slug=tenant.slug,
                plan_code=plan.code,
                plan_name=plan.name,
                prompt_tokens=usage["llm.prompt_tokens"],
                completion_tokens=usage["llm.completion_tokens"],
                amount_cents=plan.price_cents
                + sum(
                    row["amount_cents"]
                    for row in build_usage_rows(
                        usage, plan.limits or {}, plan.prices or {}
                    )
                ),
                statement_status=statements.get(tenant.id),
            )
        )
    return rows


@router.post(
    "/platform/statements/{tenant_id}/{period}/close",
    response_model=StatementOut,
)
async def close_platform_statement(
    tenant_id: str,
    period: str,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StatementOut:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    try:
        normalize_period(period)
    except ValueError:
        raise AppError("period_invalid", "账期格式应为 YYYY-MM", 422) from None
    row = await close_statement(
        session, tenant=tenant, period=period, actor=user, request=request
    )
    return _statement_out(row)


@router.post(
    "/platform/statements/{tenant_id}/{period}/reopen",
    response_model=StatementOut,
)
async def reopen_platform_statement(
    tenant_id: str,
    period: str,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> StatementOut:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    row = await reopen_statement(
        session, tenant=tenant, period=period, actor=user, request=request
    )
    return _statement_out(row)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && uv run pytest tests/test_billing_platform_api.py tests/test_billing_usage_api.py -q`
Expected: PASS

- [ ] **Step 5: Lint 与（如已授权）提交**

Run: `cd backend && uv run ruff check app/api/billing.py tests/test_billing_platform_api.py`

如已授权：

```bash
git add backend/app/api/billing.py backend/tests/test_billing_platform_api.py
git commit -m "feat(billing): add platform overview and statement close/reopen APIs"
```

---

### Task 9: 前端计费纯函数与类型

**Files:**
- Create: `web/src/billing.ts`
- Test: `web/src/__tests__/billing.test.ts`

**Interfaces:**
- Produces:
  - 类型 `PlanOut`、`UsageRowOut`、`UsageOut`、`StatementOut`、`PlatformUsageRow`
  - `formatCents`、`formatTokens`、`formatSeconds`、`rowPercent`、`isOverLimit`、`statementStatusLabel`、`periodLabel`、`yuanToCents`、`centsToYuan`

- [ ] **Step 1: 写失败测试**

```ts
// web/src/__tests__/billing.test.ts
import { describe, expect, it } from "vitest";
import {
  centsToYuan,
  formatCents,
  formatSeconds,
  formatTokens,
  isOverLimit,
  periodLabel,
  rowPercent,
  statementStatusLabel,
  yuanToCents,
} from "../billing";

describe("billing formatting", () => {
  it("formats cents as yuan", () => {
    expect(formatCents(0)).toBe("¥0.00");
    expect(formatCents(9901)).toBe("¥99.01");
  });

  it("formats tokens with 万 above ten thousand", () => {
    expect(formatTokens(9999)).toBe("9,999");
    expect(formatTokens(12_000)).toBe("1.2 万");
  });

  it("formats media seconds as minutes", () => {
    expect(formatSeconds(0)).toBe("0.0 分钟");
    expect(formatSeconds(750)).toBe("12.5 分钟");
  });

  it("computes percent and over-limit safely", () => {
    expect(rowPercent(1500, 1000)).toBe(150);
    expect(rowPercent(3, 0)).toBe(100);
    expect(rowPercent(0, 0)).toBe(0);
    expect(rowPercent(3, null)).toBeNull();
    expect(isOverLimit(1500, 1000)).toBe(true);
    expect(isOverLimit(3, null)).toBe(false);
  });

  it("labels statement status and period", () => {
    expect(statementStatusLabel("closed")).toBe("已关账");
    expect(statementStatusLabel("open")).toBe("重开中");
    expect(statementStatusLabel("other")).toBe("other");
    expect(periodLabel("2026-11")).toBe("2026 年 11 月");
  });

  it("converts yuan input to integer cents", () => {
    expect(yuanToCents("99")).toBe(9900);
    expect(yuanToCents("0.20")).toBe(20);
    expect(yuanToCents("abc")).toBe(0);
    expect(centsToYuan(9900)).toBe("99");
    expect(centsToYuan(20)).toBe("0.2");
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd web && npx vitest run src/__tests__/billing.test.ts`
Expected: FAIL，无法解析 `../billing`

- [ ] **Step 3: 实现类型与纯函数**

```ts
// web/src/billing.ts
export interface PlanOut {
  code: string;
  name: string;
  description: string;
  price_cents: number;
  currency: string;
  limits: Record<string, number>;
  prices: Record<string, number>;
  status: string;
  updated_at: string;
}

export interface UsageRowOut {
  dimension: string;
  label: string;
  unit: string;
  unit_size: number;
  used: number;
  limit: number | null;
  remaining: number | null;
  percent: number | null;
  over_limit: boolean;
  price_cents: number;
  amount_cents: number;
}

export interface UsageOut {
  period: string;
  period_start: string;
  period_end: string;
  plan: PlanOut;
  rows: UsageRowOut[];
  amount_cents: number;
}

export interface StatementOut {
  period: string;
  status: string;
  plan_code: string;
  plan_name: string;
  plan_price_cents: number;
  currency: string;
  limits: Record<string, number>;
  prices: Record<string, number>;
  usage: Record<string, number>;
  charges: Array<{
    dimension: string;
    used: number;
    included: number;
    overage: number;
    unit_size: number;
    price_cents: number;
    amount_cents: number;
  }>;
  amount_cents: number;
  closed_at: string | null;
  reopened_at: string | null;
}

export interface PlatformUsageRow {
  tenant_id: string;
  tenant_name: string;
  tenant_slug: string;
  plan_code: string;
  plan_name: string;
  prompt_tokens: number;
  completion_tokens: number;
  amount_cents: number;
  statement_status: string | null;
}

export function formatCents(cents: number): string {
  return `¥${(cents / 100).toFixed(2)}`;
}

export function formatTokens(value: number): string {
  if (value >= 10000) return `${(value / 10000).toFixed(1)} 万`;
  return value.toLocaleString("zh-CN");
}

export function formatSeconds(value: number): string {
  return `${(value / 60).toFixed(1)} 分钟`;
}

export function rowPercent(
  used: number,
  limit: number | null,
): number | null {
  if (limit === null) return null;
  if (limit <= 0) return used > 0 ? 100 : 0;
  return Math.round((used * 100) / limit);
}

export function isOverLimit(used: number, limit: number | null): boolean {
  return limit !== null && used > limit;
}

const STATUS_LABELS: Record<string, string> = {
  closed: "已关账",
  open: "重开中",
};

export function statementStatusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function periodLabel(period: string): string {
  const [year, month] = period.split("-");
  return `${year} 年 ${Number(month)} 月`;
}

export function yuanToCents(input: string): number {
  const value = Number(input);
  if (!Number.isFinite(value) || value < 0) return 0;
  return Math.round(value * 100);
}

export function centsToYuan(cents: number): string {
  const yuan = cents / 100;
  return Number.isInteger(yuan) ? String(yuan) : String(Number(yuan.toFixed(2)));
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd web && npx vitest run src/__tests__/billing.test.ts`
Expected: PASS

- [ ] **Step 5: 类型检查**

Run: `cd web && npx tsc -b --noEmit`
Expected: 通过

- [ ] **Step 6: （如已授权）提交**

```bash
git add web/src/billing.ts web/src/__tests__/billing.test.ts
git commit -m "feat(web): add billing types and formatting helpers"
```

---

### Task 10: 账单页面与导航

**Files:**
- Create: `web/src/pages/Billing.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/auth.ts`
- Modify: `web/src/components/NavIcon.tsx`
- Modify: `web/src/__tests__/nav.test.ts`

**Interfaces:**
- Consumes: `web/src/billing.ts` 类型与格式化函数
- Produces: `/billing` 页面，平台管理员可见套餐管理与跨租户关账面板

- [ ] **Step 1: 更新导航测试（先失败）**

`web/src/__tests__/nav.test.ts` 中 manage 分组期望改为：

```ts
    const manage = groups.find((group) => group.key === "manage");
    expect(manage?.items.map((item) => item.to)).toEqual([
      "/settings",
      "/team",
      "/audit",
      "/billing",
    ]);
```

Run: `cd web && npx vitest run src/__tests__/nav.test.ts`
Expected: FAIL（`/billing` 尚不存在）

- [ ] **Step 2: 增加导航与图标**

`web/src/auth.ts` 的 `NavIconKey` 增加 `| "billing"`，`NAV_ITEMS` 在 `/audit` 之后加入：

```ts
  { to: "/billing", label: "账单", minRole: "admin", group: "manage", icon: "billing" },
```

`web/src/components/NavIcon.tsx` 的 `ICONS` 增加：

```tsx
  billing: (
    <>
      <path d="M5 4h14v16l-7-3-7 3V4z" />
      <path d="M9 8h6M9 12h6" />
    </>
  ),
```

- [ ] **Step 3: 注册路由**

`web/src/App.tsx` 导入 `Billing`，在 `/audit` 路由之后加入：

```tsx
          <Route
            path="/billing"
            element={
              <RequireRole minRole="admin">
                <Billing />
              </RequireRole>
            }
          />
```

- [ ] **Step 4: 实现账单页**

```tsx
// web/src/pages/Billing.tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useAuth } from "../auth-context";
import {
  centsToYuan,
  formatCents,
  formatSeconds,
  formatTokens,
  periodLabel,
  rowPercent,
  statementStatusLabel,
  type PlatformUsageRow,
  type PlanOut,
  type StatementOut,
  type UsageOut,
} from "../billing";
import { api } from "../client";
import EmptyState, { InlineError } from "../components/EmptyState";
import { errorText } from "../types";

function recentPeriods(): string[] {
  const now = new Date();
  const items: string[] = [];
  for (let offset = 0; offset < 12; offset += 1) {
    const date = new Date(now.getFullYear(), now.getMonth() - offset, 1);
    const month = String(date.getMonth() + 1).padStart(2, "0");
    items.push(`${date.getFullYear()}-${month}`);
  }
  return items;
}

export default function Billing() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const [period, setPeriod] = useState(recentPeriods()[0]);
  const [message, setMessage] = useState<string | null>(null);

  const usage = useQuery({
    queryKey: ["billing-usage", period],
    queryFn: () => api<UsageOut>(`/billing/usage?period=${period}`),
  });
  const statements = useQuery({
    queryKey: ["billing-statements"],
    queryFn: () => api<StatementOut[]>("/billing/statements"),
  });
  const plans = useQuery({
    queryKey: ["billing-plans"],
    queryFn: () => api<PlanOut[]>("/billing/plans"),
    enabled: Boolean(user?.is_platform_admin),
  });
  const overview = useQuery({
    queryKey: ["billing-overview", period],
    queryFn: () =>
      api<PlatformUsageRow[]>(`/billing/platform/usage?period=${period}`),
    enabled: Boolean(user?.is_platform_admin),
  });

  const close = useMutation({
    mutationFn: ({ tenantId, target }: { tenantId: string; target: string }) =>
      api<StatementOut>(
        `/billing/platform/statements/${tenantId}/${target}/close`,
        { method: "POST" },
      ),
    onSuccess: () => {
      setMessage("账单已关账");
      qc.invalidateQueries({ queryKey: ["billing-overview"] });
      qc.invalidateQueries({ queryKey: ["billing-statements"] });
    },
    onError: (err) => setMessage(errorText(err)),
  });
  const reopen = useMutation({
    mutationFn: ({ tenantId, target }: { tenantId: string; target: string }) =>
      api<StatementOut>(
        `/billing/platform/statements/${tenantId}/${target}/reopen`,
        { method: "POST" },
      ),
    onSuccess: () => {
      setMessage("账单已重开");
      qc.invalidateQueries({ queryKey: ["billing-overview"] });
      qc.invalidateQueries({ queryKey: ["billing-statements"] });
    },
    onError: (err) => setMessage(errorText(err)),
  });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">账单</h1>
        <p className="mt-1 text-sm text-ink-500">
          按北京时间自然月计量；输入/输出 token 计费，运行次数与媒体用量仅作指标。
        </p>
      </div>

      <label className="flex items-center gap-3 text-sm text-ink-500">
        <span>账期</span>
        <select
          className="input w-40 text-ink-900"
          value={period}
          onChange={(event) => setPeriod(event.target.value)}
        >
          {recentPeriods().map((item) => (
            <option key={item} value={item}>
              {periodLabel(item)}
            </option>
          ))}
        </select>
      </label>

      {message && <p className="text-sm text-brand-600">{message}</p>}
      {usage.isError && <InlineError>{errorText(usage.error)}</InlineError>}
      {usage.isLoading && <p className="text-sm text-ink-500">加载中…</p>}

      {usage.data && (
        <>
          <section className="card p-5">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <div>
                <h2 className="font-medium">{usage.data.plan.name}</h2>
                <p className="mt-1 text-xs text-ink-500">
                  {usage.data.plan.description}
                </p>
              </div>
              <p className="text-sm text-ink-700">
                月费 {formatCents(usage.data.plan.price_cents)}
              </p>
            </div>
          </section>

          <section className="card p-5">
            <h2 className="font-medium">本期用量</h2>
            <div className="mt-3 space-y-3">
              {usage.data.rows.map((row) => {
                const percent = rowPercent(row.used, row.limit);
                const usedText =
                  row.dimension === "media.seconds"
                    ? formatSeconds(row.used)
                    : formatTokens(row.used);
                const limitText =
                  row.limit === null
                    ? "不限"
                    : row.dimension === "media.seconds"
                      ? formatSeconds(row.limit)
                      : formatTokens(row.limit);
                return (
                  <div key={row.dimension}>
                    <div className="flex items-center justify-between text-sm">
                      <span className={row.over_limit ? "text-coral-600" : ""}>
                        {row.label}
                      </span>
                      <span className="text-xs text-ink-500">
                        {usedText} / {limitText}
                        {row.over_limit ? " · 已超额" : ""}
                      </span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-paper">
                      <div
                        className={
                          row.over_limit ? "h-full bg-coral-500" : "h-full bg-brand-500"
                        }
                        style={{ width: `${Math.min(percent ?? 0, 100)}%` }}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
            <p className="mt-4 text-sm text-ink-700">
              按当前用量预计：{formatCents(usage.data.amount_cents)}
            </p>
          </section>

          <section className="space-y-2">
            <h2 className="font-medium">账单</h2>
            {statements.isError && (
              <InlineError>{errorText(statements.error)}</InlineError>
            )}
            {statements.data?.length === 0 && (
              <EmptyState title="还没有账单" hint="账期结束后由平台管理员关账生成。" />
            )}
            {statements.data && statements.data.length > 0 && (
              <table className="table1">
                <thead>
                  <tr>
                    <th>账期</th>
                    <th>金额</th>
                    <th>状态</th>
                    <th>关账时间</th>
                  </tr>
                </thead>
                <tbody>
                  {statements.data.map((item) => (
                    <tr key={item.period}>
                      <td>{periodLabel(item.period)}</td>
                      <td>{formatCents(item.amount_cents)}</td>
                      <td>{statementStatusLabel(item.status)}</td>
                      <td>{item.closed_at?.slice(0, 10) ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}

      {user?.is_platform_admin && (
        <>
          <section className="space-y-2">
            <h2 className="font-medium">套餐管理</h2>
            {plans.isError && <InlineError>{errorText(plans.error)}</InlineError>}
            {plans.data && (
              <table className="table1">
                <thead>
                  <tr>
                    <th>标识</th>
                    <th>名称</th>
                    <th>月费</th>
                    <th>输入 token 单价</th>
                    <th>输出 token 单价</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {plans.data.map((plan) => (
                    <tr key={plan.code}>
                      <td>{plan.code}</td>
                      <td>{plan.name}</td>
                      <td>{formatCents(plan.price_cents)}</td>
                      <td>
                        {formatCents(plan.prices["llm.prompt_tokens"] ?? 0)} / 千 token
                      </td>
                      <td>
                        {formatCents(plan.prices["llm.completion_tokens"] ?? 0)} / 千 token
                      </td>
                      <td>{plan.status === "active" ? "启用" : "已归档"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="text-xs text-ink-500">
              价格与额度调整入口在租户页的套餐指派与后续版本；当前页面只读展示。
            </p>
          </section>

          <section className="space-y-2">
            <h2 className="font-medium">跨租户用量（{periodLabel(period)}）</h2>
            {overview.isError && (
              <InlineError>{errorText(overview.error)}</InlineError>
            )}
            {overview.data && overview.data.length > 0 && (
              <table className="table1">
                <thead>
                  <tr>
                    <th>租户</th>
                    <th>套餐</th>
                    <th>输入 token</th>
                    <th>输出 token</th>
                    <th>预计金额</th>
                    <th>账单</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {overview.data.map((row) => (
                    <tr key={row.tenant_id}>
                      <td>{row.tenant_name}</td>
                      <td>{row.plan_name}</td>
                      <td>{formatTokens(row.prompt_tokens)}</td>
                      <td>{formatTokens(row.completion_tokens)}</td>
                      <td>{formatCents(row.amount_cents)}</td>
                      <td>
                        {row.statement_status
                          ? statementStatusLabel(row.statement_status)
                          : "未关账"}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="rounded border border-line px-2 py-1 text-xs text-ink-700"
                          disabled={close.isPending || reopen.isPending}
                          onClick={() => {
                            const action =
                              row.statement_status === "closed" ? reopen : close;
                            action.mutate({ tenantId: row.tenant_id, target: period });
                          }}
                        >
                          {row.statement_status === "closed" ? "重开" : "关账"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <p className="text-xs text-ink-500">
              输入 token 按千 token 计价；金额为「月费 + 超额费用」的整数分换算结果。
            </p>
          </section>
        </>
      )}
    </div>
  );
}
```

- [ ] **Step 5: 运行导航测试与构建**

Run: `cd web && npx vitest run src/__tests__/nav.test.ts`
Expected: PASS

Run: `cd web && npm run build`
Expected: 构建成功

- [ ] **Step 6: 浏览器验收**

启动后端与前端后，用平台管理员登录访问 `/billing`：

- 默认显示当前账期的套餐、用量与账单区；
- 平台管理员额外看到套餐管理与跨租户用量；
- 切换账期后用量与跨租户表同步刷新；
- 关账后跨租户行状态变为「已关账」，再次点击变为「重开」。

若执行环境无法启动浏览器，明确说明「UI 未在浏览器中验证」，不要声称已验收。

- [ ] **Step 7: （如已授权）提交**

```bash
git add web/src/pages/Billing.tsx web/src/App.tsx web/src/auth.ts \
  web/src/components/NavIcon.tsx web/src/__tests__/nav.test.ts
git commit -m "feat(web): add billing page, navigation and platform panels"
```

---

### Task 11: 租户列表套餐列与指派

**Files:**
- Modify: `web/src/types.ts`
- Modify: `web/src/pages/Tenants.tsx`

**Interfaces:**
- Consumes: `PlanOut`（Task 9）、`PATCH /api/billing/tenants/{id}/plan`（Task 6）
- Produces: 租户列表展示套餐并可切换

- [ ] **Step 1: 扩展 Tenant 类型**

`web/src/types.ts`：

```ts
export interface Tenant {
  id: string;
  name: string;
  slug: string;
  status: string;
  plan_code: string;
  plan_name: string;
  members_count: number;
  created_at: string;
}
```

- [ ] **Step 2: 租户页加载套餐并展示**

`web/src/pages/Tenants.tsx` 增加导入：

```tsx
import type { PlanOut } from "../billing";
```

在 `tenants` 查询之后增加：

```tsx
  const plans = useQuery({
    queryKey: ["billing-plans"],
    queryFn: () => api<PlanOut[]>("/billing/plans"),
  });

  const assignPlan = useMutation({
    mutationFn: ({ id, planCode }: { id: string; planCode: string }) =>
      api<PlanOut>(`/billing/tenants/${id}/plan`, {
        method: "PATCH",
        body: JSON.stringify({ plan_code: planCode }),
      }),
    onSuccess: () => {
      setMessage({ kind: "ok", text: "套餐已更新" });
      qc.invalidateQueries({ queryKey: ["tenants"] });
    },
    onError: (err) => setMessage({ kind: "error", text: errorText(err) }),
  });
```

租户卡片信息行加入套餐展示：

```tsx
                <p className="mt-1 text-xs text-ink-500">
                  {tenant.slug} · {tenant.plan_name} · {tenant.members_count} 名成员 · 创建于{" "}
                  {tenant.created_at.slice(0, 10)}
                </p>
```

操作区加入套餐下拉：

```tsx
                <select
                  className="rounded border border-line px-2 py-1 text-xs text-ink-700"
                  aria-label={`调整 ${tenant.name} 的套餐`}
                  value={tenant.plan_code}
                  disabled={assignPlan.isPending}
                  onChange={(event) => {
                    if (event.target.value === tenant.plan_code) return;
                    assignPlan.mutate({
                      id: tenant.id,
                      planCode: event.target.value,
                    });
                  }}
                >
                  {(plans.data ?? []).map((plan) => (
                    <option key={plan.code} value={plan.code}>
                      {plan.name}
                    </option>
                  ))}
                </select>
```

- [ ] **Step 3: 运行前端测试与构建**

Run: `cd web && npm run test`
Expected: 全部通过

Run: `cd web && npm run build`
Expected: 构建成功

- [ ] **Step 4: 浏览器验收**

以平台管理员登录 `/tenants`：

- 每个租户显示当前套餐名称；
- 切换下拉后提示「套餐已更新」，列表套餐名称变化；
- 当前租户也可切换（不受停用/删除限制影响）。

- [ ] **Step 5: （如已授权）提交**

```bash
git add web/src/types.ts web/src/pages/Tenants.tsx
git commit -m "feat(web): show tenant plan and allow reassignment"
```

---

### Task 12: 文档与全量验证

**Files:**
- Modify: `README.md`
- Modify: `docs/ops.md`
- Modify: `docs/原生AI新媒体运营系统-开发文档.md`
- Modify: `docs/superpowers/specs/2026-11-25-commercial-billing-design.md`

**Interfaces:**
- Consumes: 全部前置任务
- Produces: 文档与最终验证记录

- [ ] **Step 1: 更新 README**

在「核心能力」的「多租户 SaaS」条目之后加入：

```markdown
- **套餐与账单**：平台管理员维护可配置套餐并指派给租户；按北京时间自然月分项计量输入/输出 token（运行次数与媒体用量为指标），关账生成不可变账单，支持重开重算；不含在线支付。
```

- [ ] **Step 2: 更新运维文档**

`docs/ops.md` 第 12 节 API 清单中，在租户接口之后加入：

```
GET/POST/PATCH   /api/billing/plans                # 套餐目录与维护（平台管理员）
PATCH            /api/billing/tenants/{id}/plan    # 指派套餐（平台管理员）
GET              /api/billing/plan|usage|statements # 租户套餐、用量与账单
GET              /api/billing/platform/usage       # 跨租户用量（平台管理员）
POST             /api/billing/platform/statements/{tenant_id}/{period}/close|reopen
```

将「Phase 3 已知限制」一行替换为：

```
Phase 3/4 已知限制：一人一租户；无公开注册与在线支付（套餐、计量与账单已交付，关账由平台管理员手工触发）；审计 IP 只取直连地址（不信任 `X-Forwarded-For`）；登录限流依赖 Redis，单机部署下 Redis 故障期间限流暂时失效。
```

- [ ] **Step 3: 更新开发文档路线图**

`docs/原生AI新媒体运营系统-开发文档.md` 第 24 行改为：

```
- **架构预留 SaaS 演进**:所有业务表带 `tenant_id`,认证层预留 RBAC,存储用对象存储抽象而非本地路径耦合。Phase 3 开多租户与权限，Phase 4 交付套餐、计量与账单。
```

在 Phase 3 章节之后新增：

```markdown
### Phase 4 — 商业化(第 13-16 周)

- 可配置套餐（平台级 `plans`）、租户套餐绑定、分项计量（输入/输出 token 计费，运行次数与媒体用量为指标）。
- 北京时间自然月账期；平台管理员关账生成不可变账单，支持重开重算；租户账单页与跨租户用量总览。
- **验收**:两个租户用量与账单互不可见；关账金额等于月费加分项向上取整费用；改价与删数据不影响已关账账单。
```

- [ ] **Step 4: 更新规格状态**

`docs/superpowers/specs/2026-11-25-commercial-billing-design.md`：

```markdown
**Status:** 已实现
```

- [ ] **Step 5: 后端全量验证**

Run: `cd backend && uv run pytest -q`
Expected: 全部通过

Run: `cd backend && uv run ruff check .`
Expected: 无输出

Run: `cd backend && uv run alembic current`
Expected: `d1e2f3a4b5c6 (head)`

- [ ] **Step 6: 前端全量验证**

Run: `cd web && npm run test`
Expected: 全部通过

Run: `cd web && npm run build`
Expected: 构建成功

- [ ] **Step 7: 端到端账单核对**

在真实 PostgreSQL 上：

1. 用平台管理员创建一个测试租户并切到 `basic`；
2. 为该租户插入一条当期已结束月份（如 `2026-10`）的 `skill_runs` 记录，`prompt_tokens` 超过 basic 的 200 万额度；
3. 调用关账接口，核对账单金额 = 9900 分 + 向上取整的超额 token 费用；
4. 修改 basic 套餐价格后再次查询账单，金额不变；
5. 重开后重新关账，金额按新价格与当前数据重算。

将结果写入验收报告或提交说明；若某一步无法执行，明确说明原因。

- [ ] **Step 8: （如已授权）提交**

```bash
git add README.md docs/ops.md docs/原生AI新媒体运营系统-开发文档.md \
  docs/superpowers/specs/2026-11-25-commercial-billing-design.md
git commit -m "docs: document billing plans, metering and statements"
```

---

## 自检清单（执行前）

- 规格覆盖：维度注册、账期、金额取整、模型、迁移、聚合去重、关账/重开、租户与平台 API、前端页面与导航、审计、测试与文档均有对应任务。
- 类型一致：`DIMENSIONS` key 在模型、聚合、API、前端中保持完全一致；`StatementData` 字段与 `BillingStatement`/`StatementOut` 对齐。
- 无占位符：每个任务包含实际代码与验证命令；文档任务给出最终文本。
- 约束：不新增网络调用，不修改 `.env`，不自动提交 git。
