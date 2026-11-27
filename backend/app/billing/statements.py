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

# 同一租户同一账期只能有一条账单（models.billing 的命名约定）。
_STATEMENT_CONFLICT = "uq_billing_statements_tenant_period"


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
    """按当前套餐与用量算出账单快照，不落库。"""
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
    session: AsyncSession,
    tenant_id: str,
    period: str,
    *,
    for_update: bool = False,
) -> BillingStatement | None:
    query = select(BillingStatement).where(
        BillingStatement.tenant_id == tenant_id,
        BillingStatement.period == period,
    )
    if for_update:
        # 关账/重开是读改写序列；锁住已有行，避免并发重复审计或状态交错。
        # SQLite 方言会忽略 FOR UPDATE，PostgreSQL 下行锁生效。
        query = query.with_for_update()
    return (await session.execute(query)).scalar_one_or_none()


def _is_statement_conflict(error: IntegrityError) -> bool:
    """只把 (tenant_id, period) 唯一冲突翻译成业务错误，其余照常抛出。"""
    text = str(error.orig or error).lower()
    if _STATEMENT_CONFLICT in text:
        return True
    return "billing_statements" in text and "period" in text


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
    existing = await _existing(session, tenant.id, period, for_update=True)
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
    try:
        # record() 内部会 flush，并发下的唯一冲突可能在这里就抛出
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
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _is_statement_conflict(exc):
            raise AppError("statement_exists", "该账期已关账", 409) from None
        raise
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
    statement = await _existing(session, tenant.id, period, for_update=True)
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
