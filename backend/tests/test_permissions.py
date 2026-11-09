import uuid
from contextlib import asynccontextmanager

from httpx import ASGITransport, AsyncClient

from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.base import DEFAULT_TENANT_ID
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
