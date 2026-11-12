import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models.analytics import AnalyticsSnapshot
from app.models.base import DEFAULT_TENANT_ID
from app.models.media import MediaJob
from app.models.tenant import Tenant
from app.models.user import User
from app.security import create_token, hash_password
from app.seed import ensure_admin

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


@asynccontextmanager
async def _two_tenant_client(db_session):
    await ensure_admin(db_session)
    if await db_session.get(Tenant, OTHER_TENANT) is None:
        db_session.add(
            Tenant(
                id=OTHER_TENANT,
                name="租户 B",
                slug="tenant-b",
                status="active",
            )
        )
    other_owner = (
        await db_session.execute(
            select(User).where(User.email == "owner-b@local")
        )
    ).scalar_one_or_none()
    if other_owner is None:
        other_owner = User(
            tenant_id=OTHER_TENANT,
            email="owner-b@local",
            password_hash=hash_password("password123"),
            display_name="租户 B 所有者",
            role="owner",
            is_active=True,
            must_change_password=False,
            is_platform_admin=False,
            token_version=0,
        )
        db_session.add(other_owner)
    await db_session.commit()
    admin = (
        await db_session.execute(
            select(User).where(User.email == get_settings().admin_email)
        )
    ).scalar_one()
    headers_a = {
        "Authorization": "Bearer "
        + create_token(admin.email, admin.tenant_id, admin.token_version)
    }
    headers_b = {
        "Authorization": "Bearer "
        + create_token(
            other_owner.email, other_owner.tenant_id, other_owner.token_version
        )
    }
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, headers_a, headers_b
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_tenant_isolation_across_modules(db_session):
    async with _two_tenant_client(db_session) as (client, a, b):
        project = (
            await client.post(
                "/api/projects", headers=a, json={"title": "租户 A 项目"}
            )
        ).json()
        content = (
            await client.post(
                f"/api/projects/{project['id']}/contents",
                headers=a,
                json={
                    "platform": "wechat",
                    "title": "租户 A 内容",
                    "body": "租户 A 的正文内容。" * 20,
                },
            )
        ).json()
        await client.post(
            f"/api/contents/{content['id']}/versions",
            headers=a,
            json={"title": "租户 A 版本 2", "body": "版本 2 正文。" * 20},
        )
        profile = (
            await client.post(
                "/api/profiles", headers=a, json={"name": "租户 A 画像"}
            )
        ).json()
        memory = (
            await client.post(
                f"/api/profiles/{profile['id']}/memories",
                headers=a,
                json={"content": "租户 A 的记忆"},
            )
        ).json()
        account = (
            await client.post(
                "/api/accounts",
                headers=a,
                json={
                    "platform": "wechat",
                    "name": "租户 A 账号",
                    "credential": {"app_id": "a-id", "app_secret": "a-secret"},
                },
            )
        ).json()
        job = (
            await client.post(
                "/api/publish/jobs",
                headers=a,
                json={"content_id": content["id"], "platform": "wechat"},
            )
        ).json()
        media = MediaJob(
            tenant_id=DEFAULT_TENANT_ID,
            project_id=uuid.UUID(project["id"]),
            skill_id="short-video",
            status="queued",
            payload={"topic": "隔离测试"},
        )
        db_session.add(media)
        db_session.add(
            AnalyticsSnapshot(
                tenant_id=DEFAULT_TENANT_ID,
                content_id=uuid.UUID(content["id"]),
                platform="wechat",
                captured_at=date.today(),
                views=100,
            )
        )
        await db_session.commit()
        media_id = str(media.id)

        assert (
            await client.get(f"/api/projects/{project['id']}", headers=b)
        ).status_code == 404
        assert (
            await client.get(
                f"/api/projects/{project['id']}/contents", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/contents/{content['id']}/versions", headers=b
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/api/contents/{content['id']}/versions",
                headers=b,
                json={"title": "越权", "body": "越权"},
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/projects/{project['id']}/files", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/projects/{project['id']}/media", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/api/projects/{project['id']}/media/jobs/{media_id}",
                headers=b,
            )
        ).status_code == 404
        assert (
            await client.get(f"/api/profiles/{profile['id']}", headers=b)
        ).status_code == 404
        assert (
            await client.get(
                f"/api/profiles/{profile['id']}/memories", headers=b
            )
        ).status_code == 404
        assert (
            await client.delete(
                f"/api/profiles/{profile['id']}/memories/{memory['id']}",
                headers=b,
            )
        ).status_code == 404
        assert (
            await client.patch(
                f"/api/accounts/{account['id']}",
                headers=b,
                json={"name": "越权改名"},
            )
        ).status_code == 404
        assert (
            await client.delete(
                f"/api/accounts/{account['id']}", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(f"/api/publish/jobs/{job['id']}", headers=b)
        ).status_code == 404
        assert (
            await client.post(
                f"/api/publish/jobs/{job['id']}/precheck", headers=b
            )
        ).status_code == 404
        assert (
            await client.post(
                f"/api/publish/jobs/{job['id']}/execute", headers=b
            )
        ).status_code == 404
        assert (
            await client.get(
                "/api/analytics/overview",
                headers=b,
                params={"project_id": project["id"]},
            )
        ).status_code == 404

        projects_b = (await client.get("/api/projects", headers=b)).json()
        assert projects_b["items"] == []
        profiles_b = (await client.get("/api/profiles", headers=b)).json()
        assert profiles_b["items"] == []
        accounts_b = (await client.get("/api/accounts", headers=b)).json()
        assert accounts_b == []
        jobs_b = (await client.get("/api/publish/jobs", headers=b)).json()
        assert jobs_b["total"] == 0

        now = date.today()
        calendar = await client.get(
            "/api/calendar",
            headers=b,
            params={
                "start": f"{now.isoformat()}T00:00:00Z",
                "end": f"{(now + timedelta(days=7)).isoformat()}T00:00:00Z",
            },
        )
        assert calendar.status_code == 200
        assert calendar.json()["items"] == []

        own = await client.post(
            "/api/projects", headers=b, json={"title": "租户 B 自己的项目"}
        )
        assert own.status_code == 201
        assert own.json()["title"] == "租户 B 自己的项目"
