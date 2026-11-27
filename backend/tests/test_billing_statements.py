import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from app.billing.statements import (
    build_statement_data,
    close_statement,
    reopen_statement,
)
from app.errors import AppError
from app.models.audit import AuditLog
from app.models.billing import BillingStatement, Plan
from app.models.media import MediaJob
from app.models.project import Project
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
    tenant, _plan = await _prepare(db_session, tenant_factory)
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
    assert reopened.reopened_at is not None

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
    assert exc.value.status == 409


async def test_reopen_missing_statement_is_not_found(
    db_session, tenant_factory
):
    tenant, _ = await _prepare(db_session, tenant_factory)
    actor = _actor(tenant.id)
    with pytest.raises(AppError) as exc:
        await reopen_statement(
            db_session, tenant=tenant, period="2026-10", actor=actor
        )
    assert exc.value.code == "statement_not_found"
    assert exc.value.status == 404


async def test_statements_are_tenant_isolated(db_session, tenant_factory):
    tenant_a, _ = await _prepare(db_session, tenant_factory)
    tenant_b, _ = await _prepare(db_session, tenant_factory)
    await close_statement(
        db_session, tenant=tenant_a, period="2026-10", actor=_actor(tenant_a.id)
    )

    with pytest.raises(AppError) as exc:
        await reopen_statement(
            db_session,
            tenant=tenant_b,
            period="2026-10",
            actor=_actor(tenant_b.id),
        )
    assert exc.value.code == "statement_not_found"

    # 同一账期另一个租户可以独立关账，唯一约束按租户隔离
    other = await close_statement(
        db_session, tenant=tenant_b, period="2026-10", actor=_actor(tenant_b.id)
    )
    assert other.tenant_id == tenant_b.id
    rows = (
        await db_session.execute(
            select(BillingStatement).where(BillingStatement.period == "2026-10")
        )
    ).scalars().all()
    assert {row.tenant_id for row in rows} == {tenant_a.id, tenant_b.id}


async def test_close_and_reopen_write_audit(db_session, tenant_factory):
    tenant, _ = await _prepare(db_session, tenant_factory)
    actor = _actor(tenant.id)
    await close_statement(
        db_session, tenant=tenant, period="2026-10", actor=actor
    )
    await reopen_statement(
        db_session, tenant=tenant, period="2026-10", actor=actor
    )
    logs = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action.in_(["billing.close", "billing.reopen"]))
            .order_by(AuditLog.created_at, AuditLog.action)
        )
    ).scalars().all()
    by_action = {log.action: log for log in logs}
    assert set(by_action) == {"billing.close", "billing.reopen"}
    close_log = by_action["billing.close"]
    assert close_log.tenant_id == actor.tenant_id
    assert close_log.resource_type == "tenant"
    assert close_log.resource_id == tenant.id
    assert close_log.actor_email == "admin@local"
    assert close_log.detail["period"] == "2026-10"
    assert close_log.detail["amount_cents"] == 9_901
    assert set(close_log.detail) == {"period", "amount_cents"}
    assert by_action["billing.reopen"].tenant_id == actor.tenant_id
    assert by_action["billing.reopen"].detail == {"period": "2026-10"}


async def test_close_converts_unique_race_to_statement_exists(
    db_session, tenant_factory, monkeypatch
):
    tenant, _ = await _prepare(db_session, tenant_factory)
    tenant_id = tenant.id
    actor = _actor(tenant_id)
    await close_statement(
        db_session, tenant=tenant, period="2026-10", actor=actor
    )

    # 模拟并发：查询时看不到已存在行，插入时才撞上唯一约束
    monkeypatch.setattr(
        "app.billing.statements._existing",
        _return_none,
    )
    with pytest.raises(AppError) as exc:
        await close_statement(
            db_session, tenant=tenant, period="2026-10", actor=actor
        )
    assert exc.value.code == "statement_exists"
    assert exc.value.status == 409
    remaining = (
        await db_session.execute(
            select(BillingStatement).where(
                BillingStatement.tenant_id == tenant_id,
                BillingStatement.period == "2026-10",
            )
        )
    ).scalars().all()
    assert len(remaining) == 1


async def _return_none(*args, **kwargs):
    return None


async def test_closed_statement_survives_source_deletion(
    db_session, tenant_factory
):
    """关账快照固化：删除当期的 SkillRun/MediaJob 后账单数值与明细不变。"""
    tenant = await tenant_factory(name="快照租户")
    plan = (
        await db_session.execute(select(Plan).where(Plan.code == "free"))
    ).scalar_one()
    plan.price_cents = 9_900
    plan.limits = {"llm.prompt_tokens": 1_000}
    plan.prices = {"llm.prompt_tokens": 2}
    project = Project(tenant_id=tenant.id, title="一月项目")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        SkillRun(
            tenant_id=tenant.id,
            skill_id="title-hook",
            prompt_tokens=1_500,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
    )
    db_session.add(
        MediaJob(
            tenant_id=tenant.id,
            project_id=project.id,
            skill_id="short-video",
            status="succeeded",
            started_at=datetime(2026, 1, 10, 0, 0, tzinfo=UTC),
            finished_at=datetime(2026, 1, 10, 0, 5, tzinfo=UTC),
            created_at=datetime(2026, 1, 10, tzinfo=UTC),
        )
    )
    await db_session.commit()

    statement = await close_statement(
        db_session, tenant=tenant, period="2026-01", actor=_actor(tenant.id)
    )
    before = (
        statement.amount_cents,
        statement.plan_price_cents,
        dict(statement.usage),
        list(statement.charges),
    )
    assert statement.usage["llm.prompt_tokens"] == 1_500
    assert statement.usage["media.jobs"] == 1
    assert statement.usage["media.seconds"] == 300

    await db_session.execute(
        delete(SkillRun).where(SkillRun.tenant_id == tenant.id)
    )
    await db_session.execute(
        delete(MediaJob).where(MediaJob.tenant_id == tenant.id)
    )
    await db_session.commit()

    # expire_on_commit=False 会把实例留在 identity map 里，这里清空以强制真实回读
    db_session.expunge_all()

    stored = (
        await db_session.execute(
            select(BillingStatement).where(
                BillingStatement.tenant_id == tenant.id,
                BillingStatement.period == "2026-01",
            )
        )
    ).scalar_one()
    after = (
        stored.amount_cents,
        stored.plan_price_cents,
        dict(stored.usage),
        list(stored.charges),
    )
    assert after == before
    assert stored.usage["llm.prompt_tokens"] == 1_500
    assert stored.usage["media.jobs"] == 1
    assert stored.usage["media.seconds"] == 300
    assert stored.amount_cents == 9_901
