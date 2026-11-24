import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.base import DEFAULT_TENANT_ID
from app.models.publish import PlatformAccount, PublishJob
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


async def test_editor_cannot_execute_schedule_or_act_on_jobs(db_session):
    async with _role_client(db_session, "editor") as client:
        job_id = uuid.uuid4()
        execute = await client.post(f"/api/publish/jobs/{job_id}/execute")
        assert execute.status_code == 403
        assert execute.json()["code"] == "forbidden"
        schedule = await client.patch(
            f"/api/publish/jobs/{job_id}/schedule",
            json={"scheduled_at": "2026-11-09T00:00:00Z"},
        )
        assert schedule.status_code == 403
        assert schedule.json()["code"] == "forbidden"
        act = await client.post(
            f"/api/publish/jobs/{job_id}/actions", json={"action": "approve"}
        )
        assert act.status_code == 403
        assert act.json()["code"] == "forbidden"


async def test_viewer_cannot_list_accounts(db_session):
    async with _role_client(db_session, "viewer") as client:
        resp = await client.get("/api/accounts")
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"


async def test_editor_cannot_reschedule_approved_job_via_replay(db_session):
    from app.models.base import DEFAULT_TENANT_ID
    from app.services.publish import apply_action

    async with _role_client(db_session, "editor") as client:
        project = await client.post("/api/projects", json={"title": "重放提权测试"})
        assert project.status_code == 201
        content = await client.post(
            f"/api/projects/{project.json()['id']}/contents",
            json={"platform": "wechat", "title": "标题", "body": "正文"},
        )
        assert content.status_code == 201
        account = PlatformAccount(
            tenant_id=DEFAULT_TENANT_ID,
            platform="wechat",
            name="编辑测试账号",
            status="active",
        )
        db_session.add(account)
        await db_session.commit()
        await db_session.refresh(account)
        created = await client.post(
            "/api/publish/jobs",
            json={
                "content_id": content.json()["id"],
                "platform": "wechat",
                "account_id": str(account.id),
            },
        )
        assert created.status_code == 201
        job_id = uuid.UUID(created.json()["id"])
        for action in ("submit", "pass", "approve"):
            await apply_action(
                db_session,
                job_id,
                action,
                tenant_id=DEFAULT_TENANT_ID,
                actor="system",
            )
        replay = await client.post(
            "/api/publish/jobs",
            json={
                "content_id": content.json()["id"],
                "platform": "wechat",
                "account_id": str(account.id),
                "scheduled_at": "2026-11-09T00:00:00Z",
            },
        )
        assert replay.status_code == 403
        assert replay.json()["code"] == "forbidden"
        unchanged = (
            await db_session.execute(
                select(PublishJob).where(PublishJob.id == job_id)
            )
        ).scalar_one()
        assert unchanged.scheduled_at is None


async def test_member_management_permissions(db_session):
    async with _role_client(db_session, "admin") as client:
        assert (await client.get("/api/members")).status_code == 200
        created = await client.post(
            "/api/members",
            json={"email": "by-admin@local", "display_name": "管理员创建"},
        )
        assert created.status_code == 403
        assert created.json()["code"] == "forbidden"

    async with _role_client(db_session, "editor") as client:
        assert (await client.get("/api/members")).status_code == 403


async def test_platform_admin_dependency():
    from app.errors import AppError
    from app.permissions import require_platform_admin

    tenant_admin = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="admin-unit@local",
        password_hash="x",
        display_name="admin",
        role="admin",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    with pytest.raises(AppError) as exc:
        await require_platform_admin(tenant_admin)
    assert exc.value.code == "platform_admin_required"

    platform_admin = tenant_admin.__class__(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="root@local",
        password_hash="x",
        display_name="root",
        role="owner",
        is_active=True,
        must_change_password=False,
        is_platform_admin=True,
        token_version=0,
    )
    assert await require_platform_admin(platform_admin) is platform_admin
