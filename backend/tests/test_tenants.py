import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID


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
    auth_client, auth_headers
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


@pytest.mark.parametrize(
    "payload",
    [{}, {"name": None}, {"status": None}],
    ids=["empty", "null-name", "null-status"],
)
async def test_update_rejects_empty_and_explicit_nulls(
    auth_client, auth_headers, payload
):
    created = (await _create_tenant(auth_client, auth_headers)).json()
    resp = await auth_client.patch(
        f"/api/tenants/{created['id']}",
        headers=auth_headers,
        json=payload,
    )
    assert resp.status_code == 422


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


async def test_suspend_and_reenable_tenant(auth_client, auth_headers):
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


async def test_delete_tenant_requires_suspend_and_slug_confirmation(
    auth_client, auth_headers, db_session
):
    from sqlalchemy import func

    from app.models.project import Content, Project
    from app.models.publish import PlatformAccount
    from app.models.tenant import Tenant
    from app.models.user import User

    created = (await _create_tenant(auth_client, auth_headers)).json()
    tenant_id = created["id"]
    project = Project(tenant_id=tenant_id, title="客户项目")
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=tenant_id, project_id=project.id, platform="wechat", title="客户内容"
    )
    db_session.add(content)
    db_session.add(
        PlatformAccount(tenant_id=tenant_id, platform="wechat", name="客户账号")
    )
    await db_session.commit()

    early = await auth_client.request(
        "DELETE",
        f"/api/tenants/{tenant_id}",
        headers=auth_headers,
        json={"confirm_slug": "customer-a"},
    )
    assert early.status_code == 409
    assert early.json()["code"] == "tenant_not_suspended"

    suspended = await auth_client.patch(
        f"/api/tenants/{tenant_id}",
        headers=auth_headers,
        json={"status": "suspended"},
    )
    assert suspended.status_code == 200

    wrong = await auth_client.request(
        "DELETE",
        f"/api/tenants/{tenant_id}",
        headers=auth_headers,
        json={"confirm_slug": "wrong-slug"},
    )
    assert wrong.status_code == 409
    assert wrong.json()["code"] == "slug_mismatch"

    deleted = await auth_client.request(
        "DELETE",
        f"/api/tenants/{tenant_id}",
        headers=auth_headers,
        json={"confirm_slug": "customer-a"},
    )
    assert deleted.status_code == 204

    assert (await db_session.get(Tenant, tenant_id)) is None
    for model in (User, Project, Content, PlatformAccount):
        count = (
            await db_session.execute(
                select(func.count())
                .select_from(model)
                .where(model.tenant_id == tenant_id)
            )
        ).scalar_one()
        assert count == 0

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "tenant.delete")
        )
    ).scalars().all()
    assert [audit.resource_id for audit in audits] == [tenant_id]
    assert audits[0].tenant_id == DEFAULT_TENANT_ID
    assert audits[0].detail["slug"] == "customer-a"

    again = await auth_client.request(
        "DELETE",
        f"/api/tenants/{tenant_id}",
        headers=auth_headers,
        json={"confirm_slug": "customer-a"},
    )
    assert again.status_code == 404


async def test_cannot_delete_own_tenant(auth_client, auth_headers):
    denied = await auth_client.request(
        "DELETE",
        f"/api/tenants/{DEFAULT_TENANT_ID}",
        headers=auth_headers,
        json={"confirm_slug": "default"},
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "cannot_delete_self"


async def test_delete_tenant_requires_platform_admin(client):
    resp = await client.request(
        "DELETE",
        "/api/tenants/00000000-0000-0000-0000-000000000099",
        json={"confirm_slug": "x"},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "platform_admin_required"
