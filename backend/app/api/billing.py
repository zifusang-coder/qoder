from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.dimensions import priced_non_billable, unknown_dimensions
from app.billing.periods import normalize_period, period_bounds
from app.billing.pricing import build_usage_rows
from app.billing.statements import close_statement, reopen_statement
from app.billing.usage import compute_usage
from app.db import get_session
from app.errors import AppError
from app.models.billing import BillingStatement, Plan
from app.models.tenant import Tenant
from app.models.user import User
from app.permissions import admin_access, require_platform_admin
from app.services.audit import record

router = APIRouter(prefix="/billing", tags=["billing"])

PlanCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,49}$")]
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
    # 目录接口不携带绑定时间；租户套餐接口填充 tenant.plan_changed_at
    plan_changed_at: datetime | None = None


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

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> "PlanPatch":
        # 字段非空约束交给校验层，避免显式 null 落到数据库触发 500
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} 不能为空")
        return self


class AssignPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_code: PlanCode


class TenantPlanOut(BaseModel):
    """指派结果：以 plan_code/plan_name 命名，便于与租户列表字段对齐。"""

    plan_code: str
    plan_name: str
    price_cents: int
    currency: str
    limits: dict[str, int]
    prices: dict[str, int]
    status: str
    updated_at: datetime


def _plan_out(
    plan: Plan, plan_changed_at: datetime | None = None
) -> PlanOut:
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
        plan_changed_at=plan_changed_at,
    )


def _tenant_plan_out(plan: Plan) -> TenantPlanOut:
    return TenantPlanOut(
        plan_code=plan.code,
        plan_name=plan.name,
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
    plan = Plan(
        code=payload.code,
        name=payload.name,
        description=payload.description,
        price_cents=payload.price_cents,
        currency="CNY",
        limits=payload.limits,
        prices=payload.prices,
        status="active",
    )
    session.add(plan)
    # 仅把套餐写入纳入唯一约束判定，审计写入失败不应被误报为 plan_exists
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("plan_exists", "套餐标识已存在", 409) from None
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
    await session.refresh(plan)
    return _plan_out(plan)


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


@router.patch("/tenants/{tenant_id}/plan", response_model=TenantPlanOut)
async def assign_tenant_plan(
    tenant_id: str,
    payload: AssignPlanIn,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TenantPlanOut:
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
        detail={"plan_code": plan.code, "tenant_id": tenant.id},
        request=request,
    )
    await session.commit()
    await session.refresh(plan)
    return _tenant_plan_out(plan)


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


async def _tenant_plan(session: AsyncSession, tenant: Tenant) -> Plan:
    plan = await session.get(Plan, tenant.plan_id)
    if plan is None:
        raise AppError("plan_missing", "租户未绑定套餐", 500)
    return plan


async def _current_tenant(session: AsyncSession, user: User) -> Tenant:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    return tenant


@router.get("/plan", response_model=PlanOut)
async def current_plan(
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PlanOut:
    tenant = await _current_tenant(session, user)
    return _plan_out(
        await _tenant_plan(session, tenant), tenant.plan_changed_at
    )


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
    tenant = await _current_tenant(session, user)
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
        plan=_plan_out(plan, tenant.plan_changed_at),
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
    try:
        normalized = normalize_period(period)
    except ValueError:
        raise AppError("period_invalid", "账期格式应为 YYYY-MM", 422) from None
    row = (
        await session.execute(
            select(BillingStatement).where(
                BillingStatement.tenant_id == user.tenant_id,
                BillingStatement.period == normalized,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("statement_not_found", "账单不存在", 404)
    return _statement_out(row)


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


def _platform_period(period: str | None) -> str:
    try:
        return normalize_period(period)
    except ValueError:
        raise AppError("period_invalid", "账期格式应为 YYYY-MM", 422) from None


@router.get("/platform/usage", response_model=list[PlatformUsageRow])
async def platform_usage(
    period: str | None = None,
    _: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[PlatformUsageRow]:
    normalized = _platform_period(period)
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
    normalized = _platform_period(period)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    row = await close_statement(
        session, tenant=tenant, period=normalized, actor=user, request=request
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
    normalized = _platform_period(period)
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    row = await reopen_statement(
        session, tenant=tenant, period=normalized, actor=user, request=request
    )
    return _statement_out(row)
