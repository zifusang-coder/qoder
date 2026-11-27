import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.billing.periods import normalize_period
from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.base import DEFAULT_TENANT_ID
from app.models.billing import BillingStatement, Plan
from app.models.skill import SkillRun
from app.models.tenant import Tenant
from app.models.user import User

# 固定使用已结束的账期，避免依赖真实当前月份
PAST_PERIOD = "2026-01"


@asynccontextmanager
async def _tenant_admin_client(db_session):
    """以非平台管理员的租户管理员身份发请求。"""
    fake = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="tenant-admin-billing@local",
        password_hash="x",
        display_name="tenant-admin",
        role="admin",
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
            yield client
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


async def _seed_skill_run(
    db_session, tenant: Tenant, prompt_tokens: int, completion_tokens: int
) -> None:
    db_session.add(
        SkillRun(
            tenant_id=tenant.id,
            skill_id="title-hook",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            created_at=datetime(2026, 1, 15, tzinfo=UTC),
        )
    )
    await db_session.commit()


def _statement_path(tenant_id, period: str, action: str) -> str:
    return f"/api/billing/platform/statements/{tenant_id}/{period}/{action}"


async def test_platform_overview_lists_tenants_with_usage(
    auth_client, auth_headers, db_session, tenant_factory
):
    tenant = await tenant_factory(name="总览租户")
    other = await tenant_factory(name="其它租户")
    await _assign_plan(db_session, other, "basic")
    await _seed_skill_run(db_session, tenant, 1200, 800)

    resp = await auth_client.get(
        "/api/billing/platform/usage",
        params={"period": PAST_PERIOD},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    rows = {row["tenant_id"]: row for row in resp.json()}

    mine = rows[tenant.id]
    assert mine["tenant_name"] == "总览租户"
    assert mine["tenant_slug"] == tenant.slug
    assert mine["plan_code"] == "free"
    assert mine["plan_name"] == "免费版"
    assert mine["prompt_tokens"] == 1200
    assert mine["completion_tokens"] == 800
    assert mine["amount_cents"] == 0
    assert mine["statement_status"] is None

    theirs = rows[other.id]
    assert theirs["plan_code"] == "basic"
    assert theirs["plan_name"] == "基础版"
    assert theirs["prompt_tokens"] == 0
    assert theirs["completion_tokens"] == 0
    # 基础版月费 9900 分，无用量时无额外计费
    assert theirs["amount_cents"] == 9_900
    assert theirs["statement_status"] is None


async def test_close_and_reopen_statement_through_platform_api(
    auth_client, auth_headers, db_session, tenant_factory
):
    tenant = await tenant_factory(name="关账租户")
    await _seed_skill_run(db_session, tenant, 1200, 800)

    closed = await auth_client.post(
        _statement_path(tenant.id, PAST_PERIOD, "close"), headers=auth_headers
    )
    assert closed.status_code == 200
    body = closed.json()
    assert body["period"] == PAST_PERIOD
    assert body["status"] == "closed"
    assert body["plan_code"] == "free"
    assert body["usage"]["llm.prompt_tokens"] == 1200
    assert body["amount_cents"] == 0
    assert body["closed_at"] is not None

    overview = await auth_client.get(
        "/api/billing/platform/usage",
        params={"period": PAST_PERIOD},
        headers=auth_headers,
    )
    assert overview.status_code == 200
    rows = {row["tenant_id"]: row for row in overview.json()}
    assert rows[tenant.id]["statement_status"] == "closed"

    again = await auth_client.post(
        _statement_path(tenant.id, PAST_PERIOD, "close"), headers=auth_headers
    )
    assert again.status_code == 409
    assert again.json()["code"] == "statement_exists"

    reopened = await auth_client.post(
        _statement_path(tenant.id, PAST_PERIOD, "reopen"), headers=auth_headers
    )
    assert reopened.status_code == 200
    opened = reopened.json()
    assert opened["status"] == "open"
    assert opened["closed_at"] is None
    assert opened["reopened_at"] is not None

    reclosing = await auth_client.post(
        _statement_path(tenant.id, PAST_PERIOD, "close"), headers=auth_headers
    )
    assert reclosing.status_code == 200
    assert reclosing.json()["status"] == "closed"


async def test_close_unfinished_period_is_rejected(
    auth_client, auth_headers, tenant_factory
):
    tenant = await tenant_factory()
    current = normalize_period(None)
    resp = await auth_client.post(
        _statement_path(tenant.id, current, "close"), headers=auth_headers
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "period_not_finished"


async def test_reopen_missing_statement_returns_404(
    auth_client, auth_headers, tenant_factory
):
    tenant = await tenant_factory()
    resp = await auth_client.post(
        _statement_path(tenant.id, "2026-02", "reopen"), headers=auth_headers
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "statement_not_found"


async def test_malformed_period_is_rejected(
    auth_client, auth_headers, tenant_factory
):
    tenant = await tenant_factory()

    for value in ("2026-13", "abc", "0000-01", "9999-12"):
        overview = await auth_client.get(
            "/api/billing/platform/usage",
            params={"period": value},
            headers=auth_headers,
        )
        assert overview.status_code == 422, value
        assert overview.json()["code"] == "period_invalid"

    for value in ("2026-13", "0000-01", "9999-12"):
        close = await auth_client.post(
            _statement_path(tenant.id, value, "close"), headers=auth_headers
        )
        assert close.status_code == 422, value
        assert close.json()["code"] == "period_invalid"

    for value in ("abc", "0000-01", "9999-12"):
        reopen = await auth_client.post(
            _statement_path(tenant.id, value, "reopen"), headers=auth_headers
        )
        assert reopen.status_code == 422, value
        assert reopen.json()["code"] == "period_invalid"


async def test_unknown_tenant_returns_404(auth_client, auth_headers):
    missing = uuid.uuid4()

    close = await auth_client.post(
        _statement_path(missing, PAST_PERIOD, "close"), headers=auth_headers
    )
    assert close.status_code == 404
    assert close.json()["code"] == "not_found"

    reopen = await auth_client.post(
        _statement_path(missing, PAST_PERIOD, "reopen"), headers=auth_headers
    )
    assert reopen.status_code == 404
    assert reopen.json()["code"] == "not_found"


async def test_non_platform_admin_is_rejected(db_session, tenant_factory):
    tenant = await tenant_factory()
    async with _tenant_admin_client(db_session) as client:
        overview = await client.get("/api/billing/platform/usage")
        assert overview.status_code == 403
        assert overview.json()["code"] == "platform_admin_required"

        close = await client.post(
            _statement_path(tenant.id, PAST_PERIOD, "close")
        )
        assert close.status_code == 403
        assert close.json()["code"] == "platform_admin_required"

        reopen = await client.post(
            _statement_path(tenant.id, PAST_PERIOD, "reopen")
        )
        assert reopen.status_code == 403
        assert reopen.json()["code"] == "platform_admin_required"


async def test_missing_token_is_rejected(auth_client):
    resp = await auth_client.get("/api/billing/platform/usage")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_delete_tenant_removes_billing_statements(
    auth_client, auth_headers, db_session, tenant_factory
):
    tenant = await tenant_factory(name="删除账单租户")
    await _seed_skill_run(db_session, tenant, 1200, 800)
    closed = await auth_client.post(
        _statement_path(tenant.id, PAST_PERIOD, "close"), headers=auth_headers
    )
    assert closed.status_code == 200
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(BillingStatement)
            .where(BillingStatement.tenant_id == tenant.id)
        )
    ).scalar_one() == 1

    tenant.status = "suspended"
    await db_session.commit()
    deleted = await auth_client.request(
        "DELETE",
        f"/api/tenants/{tenant.id}",
        headers=auth_headers,
        json={"confirm_slug": tenant.slug},
    )
    assert deleted.status_code == 204
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(BillingStatement)
            .where(BillingStatement.tenant_id == tenant.id)
        )
    ).scalar_one() == 0
