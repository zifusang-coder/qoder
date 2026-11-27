import uuid
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID
from app.models.billing import Plan
from app.models.user import User


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


async def _create_tenant(client, headers, slug="customer-a-plan", email="owner-a-plan@local"):
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


async def test_platform_admin_manages_plans(auth_client, auth_headers, db_session):
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
    body = created.json()
    assert body["code"] == "starter"
    assert body["price_cents"] == 1000
    assert body["status"] == "active"
    assert body["limits"] == {"llm.prompt_tokens": 1000}
    assert body["plan_changed_at"] is None

    listed = await auth_client.get("/api/billing/plans", headers=auth_headers)
    assert listed.status_code == 200
    codes = [item["code"] for item in listed.json()]
    assert "starter" in codes
    assert "free" in codes
    assert all(item["plan_changed_at"] is None for item in listed.json())

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

    empty = await auth_client.patch(
        "/api/billing/plans/starter", headers=auth_headers, json={}
    )
    assert empty.status_code == 422
    assert empty.json()["code"] == "empty_update"

    bad_status = await auth_client.patch(
        "/api/billing/plans/starter",
        headers=auth_headers,
        json={"status": "deleted"},
    )
    assert bad_status.status_code == 422
    assert bad_status.json()["code"] == "plan_status_invalid"

    null_name = await auth_client.patch(
        "/api/billing/plans/starter",
        headers=auth_headers,
        json={"name": None},
    )
    assert null_name.status_code == 422
    assert null_name.json()["code"] == "validation_error"

    actions = (
        await db_session.execute(
            select(AuditLog.action).where(
                AuditLog.action.in_(["plan.create", "plan.update"])
            )
        )
    ).scalars().all()
    assert actions.count("plan.create") == 1
    assert actions.count("plan.update") == 1


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


async def test_update_unknown_plan_returns_404(auth_client, auth_headers):
    resp = await auth_client.patch(
        "/api/billing/plans/missing",
        headers=auth_headers,
        json={"price_cents": 1},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "plan_not_found"


async def test_only_platform_admin_manages_plans(db_session):
    async with _role_client(db_session, "admin") as client:
        listed = await client.get("/api/billing/plans")
        assert listed.status_code == 403
        assert listed.json()["code"] == "platform_admin_required"
        created = await client.post(
            "/api/billing/plans",
            json={"code": "sneaky", "name": "越权套餐"},
        )
        assert created.status_code == 403
        assert created.json()["code"] == "platform_admin_required"
        patched = await client.patch(
            "/api/billing/plans/free", json={"price_cents": 1}
        )
        assert patched.status_code == 403
        assert patched.json()["code"] == "platform_admin_required"


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

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "tenant.plan_change")
        )
    ).scalars().all()
    assert len(audits) == 1
    assert audits[0].resource_id == tenant_id
    assert audits[0].tenant_id == DEFAULT_TENANT_ID
    assert audits[0].detail["plan_code"] == "basic"
    assert audits[0].detail["tenant_id"] == tenant_id


async def test_assign_plan_to_unknown_tenant_returns_404(
    auth_client, auth_headers
):
    resp = await auth_client.patch(
        f"/api/billing/tenants/{uuid.uuid4()}/plan",
        headers=auth_headers,
        json={"plan_code": "free"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_assign_unknown_plan_returns_404(auth_client, auth_headers):
    created = await _create_tenant(auth_client, auth_headers)
    tenant_id = created.json()["id"]
    resp = await auth_client.patch(
        f"/api/billing/tenants/{tenant_id}/plan",
        headers=auth_headers,
        json={"plan_code": "missing-plan"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "plan_not_found"


async def test_tenant_admin_cannot_assign_plan(db_session):
    async with _role_client(db_session, "admin") as client:
        own = await client.patch(
            f"/api/billing/tenants/{DEFAULT_TENANT_ID}/plan",
            json={"plan_code": "basic"},
        )
        assert own.status_code == 403
        assert own.json()["code"] == "platform_admin_required"

        other = await client.patch(
            f"/api/billing/tenants/{uuid.uuid4()}/plan",
            json={"plan_code": "basic"},
        )
        assert other.status_code == 403
        assert other.json()["code"] == "platform_admin_required"

    async with _role_client(db_session, "owner") as client:
        owner = await client.patch(
            f"/api/billing/tenants/{DEFAULT_TENANT_ID}/plan",
            json={"plan_code": "basic"},
        )
        assert owner.status_code == 403
        assert owner.json()["code"] == "platform_admin_required"


async def test_new_tenant_gets_default_free_plan(
    auth_client, auth_headers
):
    created = await _create_tenant(
        auth_client,
        auth_headers,
        slug="customer-default-plan",
        email="owner-default-plan@local",
    )
    assert created.status_code == 201
    assert created.json()["plan_code"] == "free"
    assert created.json()["plan_name"] == "免费版"

    listed = await auth_client.get("/api/tenants", headers=auth_headers)
    row = next(
        item for item in listed.json() if item["id"] == created.json()["id"]
    )
    assert row["plan_code"] == "free"
    assert row["plan_name"] == "免费版"

    updated = await auth_client.patch(
        f"/api/tenants/{created.json()['id']}",
        headers=auth_headers,
        json={"name": "客户 A 改名"},
    )
    assert updated.status_code == 200
    assert updated.json()["plan_code"] == "free"
    assert updated.json()["plan_name"] == "免费版"


async def test_create_tenant_binds_default_plan_by_code(
    auth_client, auth_headers, db_session
):
    plan = (
        await db_session.execute(select(Plan).where(Plan.code == "free"))
    ).scalar_one()
    plan.code = "free-legacy"
    await db_session.commit()

    resp = await _create_tenant(
        auth_client,
        auth_headers,
        slug="customer-no-default",
        email="owner-no-default@local",
    )
    assert resp.status_code == 500
    assert resp.json()["code"] == "default_plan_missing"


async def test_create_tenant_requires_active_default_plan(
    auth_client, auth_headers, db_session
):
    plan = (
        await db_session.execute(select(Plan).where(Plan.code == "free"))
    ).scalar_one()
    plan.status = "archived"
    await db_session.commit()

    resp = await _create_tenant(
        auth_client,
        auth_headers,
        slug="customer-archived-default",
        email="owner-archived-default@local",
    )
    assert resp.status_code == 500
    assert resp.json()["code"] == "default_plan_missing"
