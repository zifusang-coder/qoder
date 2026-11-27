from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.billing.plans import DEFAULT_PLAN_ID, PLAN_SEEDS, ensure_plans
from app.models.billing import BillingStatement, Plan


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
    except IntegrityError:
        await db_session.rollback()
