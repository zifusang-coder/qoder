import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.billing.periods import BEIJING
from app.billing.statements import close_statement, reopen_statement
from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.billing import Plan
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


async def _assign_plan(db_session, tenant: Tenant, code: str) -> Plan:
    plan = (
        await db_session.execute(select(Plan).where(Plan.code == code))
    ).scalar_one()
    tenant.plan_id = plan.id
    await db_session.commit()
    return plan


async def _tiny_plan(db_session, tenant: Tenant) -> Plan:
    plan = Plan(
        code="tiny",
        name="微型套餐",
        description="测试超额计费",
        price_cents=0,
        currency="CNY",
        limits={"llm.prompt_tokens": 100},
        prices={"llm.prompt_tokens": 1},
        status="active",
    )
    db_session.add(plan)
    await db_session.flush()
    tenant.plan_id = plan.id
    await db_session.commit()
    return plan


async def test_plan_read_returns_tenant_plan(db_session, tenant_factory):
    tenant = await tenant_factory(name="套餐租户")
    await _assign_plan(db_session, tenant, "basic")

    async with _tenant_client(db_session, tenant, "admin") as (client, _):
        resp = await client.get("/api/billing/plan")
        assert resp.status_code == 200
        body = resp.json()
        assert body["code"] == "basic"
        assert body["name"] == "基础版"
        assert body["price_cents"] == 9_900
        assert body["currency"] == "CNY"
        assert body["limits"]["llm.prompt_tokens"] == 2_000_000
        assert body["prices"] == {
            "llm.prompt_tokens": 1,
            "llm.completion_tokens": 3,
        }
        assert body["plan_changed_at"] is not None


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
        assert body["plan"]["plan_changed_at"] is not None
        rows = {row["dimension"]: row for row in body["rows"]}
        assert len(rows) == 7
        assert rows["llm.prompt_tokens"]["used"] == 1200
        assert rows["llm.prompt_tokens"]["limit"] == 200_000
        assert rows["llm.prompt_tokens"]["remaining"] == 200_000 - 1200
        assert rows["llm.prompt_tokens"]["percent"] == 1
        assert rows["llm.prompt_tokens"]["over_limit"] is False
        assert rows["llm.prompt_tokens"]["amount_cents"] == 0
        assert rows["llm.completion_tokens"]["used"] == 800
        assert rows["run.skill"]["used"] == 1
        assert body["amount_cents"] == 0

    async with _tenant_client(db_session, tenant, "editor") as (client, _):
        assert (await client.get("/api/billing/usage")).status_code == 403


async def test_usage_defaults_to_current_period_and_flags_over_limit(
    db_session, tenant_factory
):
    tenant = await tenant_factory(name="当期租户")
    await _tiny_plan(db_session, tenant)
    db_session.add(
        SkillRun(
            tenant_id=tenant.id,
            skill_id="title-hook",
            prompt_tokens=250,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.commit()
    expected_period = datetime.now(UTC).astimezone(BEIJING).strftime("%Y-%m")

    async with _tenant_client(db_session, tenant, "admin") as (client, _):
        resp = await client.get("/api/billing/usage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["period"] == expected_period
        assert body["plan"]["code"] == "tiny"
        rows = {row["dimension"]: row for row in body["rows"]}
        prompt = rows["llm.prompt_tokens"]
        assert prompt["used"] == 250
        assert prompt["limit"] == 100
        assert prompt["remaining"] == 0
        assert prompt["percent"] == 250
        assert prompt["over_limit"] is True
        # 150 超量按 1000 token 为 1 分向上取整
        assert prompt["price_cents"] == 1
        assert prompt["amount_cents"] == 1
        # 未配置额度的维度返回 null 百分比且不计超限
        skill = rows["run.skill"]
        assert skill["limit"] is None
        assert skill["remaining"] is None
        assert skill["percent"] is None
        assert skill["over_limit"] is False
        assert body["amount_cents"] == 1


async def test_bad_period_is_rejected(db_session, tenant_factory):
    tenant = await tenant_factory()
    async with _tenant_client(db_session, tenant) as (client, _):
        for value in ("2026-13", "abc", "0000-01", "0001-01", "9999-12"):
            resp = await client.get(
                "/api/billing/usage", params={"period": value}
            )
            assert resp.status_code == 422, value
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
        body = detail.json()
        assert body["status"] == "closed"
        assert body["plan_code"] == "free"
        assert body["usage"]["llm.prompt_tokens"] == 100
        assert body["amount_cents"] == 0
        assert body["closed_at"] is not None

        missing = await client.get("/api/billing/statements/2026-09")
        assert missing.status_code == 404
        assert missing.json()["code"] == "statement_not_found"

        bad = await client.get("/api/billing/statements/2026-13")
        assert bad.status_code == 422
        assert bad.json()["code"] == "period_invalid"

        for value in ("0000-01", "0001-01", "9999-12"):
            out_of_range = await client.get(f"/api/billing/statements/{value}")
            assert out_of_range.status_code == 422, value
            assert out_of_range.json()["code"] == "period_invalid"


async def test_statement_list_orders_desc_and_validates_limit(
    db_session, tenant_factory
):
    tenant = await tenant_factory(name="账单排序租户")
    async with _tenant_client(db_session, tenant) as (client, fake):
        for period in ("2026-08", "2026-10", "2026-09"):
            await close_statement(
                db_session, tenant=tenant, period=period, actor=fake
            )

        listed = await client.get("/api/billing/statements")
        assert listed.status_code == 200
        assert [item["period"] for item in listed.json()] == [
            "2026-10",
            "2026-09",
            "2026-08",
        ]

        two = await client.get("/api/billing/statements", params={"limit": 2})
        assert two.status_code == 200
        assert [item["period"] for item in two.json()] == ["2026-10", "2026-09"]

        default_limit = await client.get(
            "/api/billing/statements", params={"limit": 12}
        )
        assert default_limit.status_code == 200
        assert len(default_limit.json()) == 3

        zero = await client.get("/api/billing/statements", params={"limit": 0})
        assert zero.status_code == 422
        assert zero.json()["code"] == "limit_invalid"

        too_big = await client.get("/api/billing/statements", params={"limit": 37})
        assert too_big.status_code == 422
        assert too_big.json()["code"] == "limit_invalid"


async def test_statement_detail_reports_reopened_status(
    db_session, tenant_factory
):
    tenant = await tenant_factory(name="重开租户")
    async with _tenant_client(db_session, tenant) as (client, fake):
        await close_statement(
            db_session, tenant=tenant, period="2026-10", actor=fake
        )
        await reopen_statement(
            db_session, tenant=tenant, period="2026-10", actor=fake
        )
        detail = await client.get("/api/billing/statements/2026-10")
        assert detail.status_code == 200
        body = detail.json()
        # 状态以 status 列为准，而不是 reopened_at 是否为空
        assert body["status"] == "open"
        assert body["closed_at"] is None
        assert body["reopened_at"] is not None


async def test_statements_are_tenant_isolated(db_session, tenant_factory):
    tenant_a = await tenant_factory(name="账单租户 A")
    tenant_b = await tenant_factory(name="账单租户 B")
    async with _tenant_client(db_session, tenant_a) as (client_a, fake_a):
        await close_statement(
            db_session, tenant=tenant_a, period="2026-10", actor=fake_a
        )
        own = await client_a.get("/api/billing/statements/2026-10")
        assert own.status_code == 200

    async with _tenant_client(db_session, tenant_b) as (client_b, _):
        listed = await client_b.get("/api/billing/statements")
        assert listed.status_code == 200
        assert listed.json() == []

        other = await client_b.get("/api/billing/statements/2026-10")
        assert other.status_code == 404
        assert other.json()["code"] == "statement_not_found"


async def test_viewer_role_is_forbidden(db_session, tenant_factory):
    tenant = await tenant_factory(name="只读租户")
    async with _tenant_client(db_session, tenant, "viewer") as (client, _):
        for path in (
            "/api/billing/plan",
            "/api/billing/usage",
            "/api/billing/statements",
            "/api/billing/statements/2026-10",
        ):
            resp = await client.get(path)
            assert resp.status_code == 403, path
            assert resp.json()["code"] == "forbidden"
