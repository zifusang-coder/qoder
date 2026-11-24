import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.config import get_settings
from app.errors import AppError
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.security import hash_password
from app.seed import ensure_admin
from app.services.members import ensure_not_last_owner

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


async def _admin(session) -> User:
    return (
        await session.execute(
            select(User).where(User.email == get_settings().admin_email)
        )
    ).scalar_one()


async def _create_member(client, headers, email="member@local", role="editor"):
    return await client.post(
        "/api/members",
        headers=headers,
        json={"email": email, "display_name": "成员", "role": role},
    )


async def test_create_member_returns_one_time_password(auth_client, auth_headers):
    resp = await _create_member(auth_client, auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["role"] == "editor"
    assert body["is_active"] is True
    assert len(body["initial_password"]) >= 12
    assert set(body) == {
        "id",
        "email",
        "display_name",
        "role",
        "is_active",
        "created_at",
        "initial_password",
    }

    login = await auth_client.post(
        "/api/auth/login",
        json={"email": "member@local", "password": body["initial_password"]},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    me = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.json()["must_change_password"] is True


async def test_duplicate_email_and_invalid_role(auth_client, auth_headers):
    assert (await _create_member(auth_client, auth_headers)).status_code == 201
    dup = await _create_member(
        auth_client, auth_headers, email="member@local", role="viewer"
    )
    assert dup.status_code == 409
    assert dup.json()["code"] == "email_exists"
    bad = await _create_member(
        auth_client, auth_headers, email="other@local", role="superuser"
    )
    assert bad.status_code == 422


async def test_cannot_change_own_role_or_disable_self(
    auth_client, auth_headers, db_session
):
    admin = await _admin(db_session)
    role = await auth_client.patch(
        f"/api/members/{admin.id}", headers=auth_headers, json={"role": "admin"}
    )
    assert role.status_code == 409
    assert role.json()["code"] == "cannot_modify_self"
    disable = await auth_client.patch(
        f"/api/members/{admin.id}",
        headers=auth_headers,
        json={"is_active": False},
    )
    assert disable.status_code == 409
    assert disable.json()["code"] == "cannot_modify_self"


async def test_last_owner_guard(db_session):
    # db_session 本身不含种子数据（管理员由 auth_client 夹具写入），这里显式播种
    await ensure_admin(db_session)
    admin = await _admin(db_session)
    with pytest.raises(AppError) as exc:
        await ensure_not_last_owner(db_session, admin.tenant_id, admin)
    assert exc.value.code == "last_owner"


class _CountResult:
    def scalar_one(self) -> int:
        return 2


class _RecordingSession:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _CountResult()


async def test_last_owner_guard_locks_tenant_row():
    session = _RecordingSession()
    member = User(
        tenant_id=DEFAULT_TENANT_ID,
        email="owner@local",
        password_hash="x",
        display_name="Owner",
        role="owner",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )

    await ensure_not_last_owner(session, DEFAULT_TENANT_ID, member)

    lock_sql = str(session.statements[0].compile(dialect=postgresql.dialect()))
    assert "tenants" in lock_sql
    assert "FOR UPDATE" in lock_sql


async def test_role_change_revokes_existing_token(
    auth_client, auth_headers, db_session
):
    created = (await _create_member(auth_client, auth_headers)).json()
    login = await auth_client.post(
        "/api/auth/login",
        json={"email": "member@local", "password": created["initial_password"]},
    )
    token = login.json()["access_token"]
    changed = await auth_client.patch(
        f"/api/members/{created['id']}",
        headers=auth_headers,
        json={"role": "viewer"},
    )
    assert changed.status_code == 200
    old = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert old.status_code == 401
    assert old.json()["code"] == "token_revoked"


async def test_delete_member_disables_and_revokes(auth_client, auth_headers):
    created = (await _create_member(auth_client, auth_headers)).json()
    login = await auth_client.post(
        "/api/auth/login",
        json={"email": "member@local", "password": created["initial_password"]},
    )
    token = login.json()["access_token"]
    deleted = await auth_client.delete(
        f"/api/members/{created['id']}", headers=auth_headers
    )
    assert deleted.status_code == 204
    old = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert old.status_code == 403
    assert old.json()["code"] == "account_disabled"
    relogin = await auth_client.post(
        "/api/auth/login",
        json={"email": "member@local", "password": created["initial_password"]},
    )
    assert relogin.status_code == 403
    assert relogin.json()["code"] == "account_disabled"


async def test_cross_tenant_member_is_not_found(
    auth_client, auth_headers, db_session
):
    other = User(
        tenant_id=OTHER_TENANT,
        email="other-member@local",
        password_hash=hash_password("password123"),
        display_name="其他租户成员",
        role="editor",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    db_session.add(other)
    await db_session.commit()
    resp = await auth_client.patch(
        f"/api/members/{other.id}",
        headers=auth_headers,
        json={"display_name": "改名"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_list_members_returns_tenant_members_only(
    auth_client, auth_headers, db_session
):
    await _create_member(auth_client, auth_headers)
    other = User(
        tenant_id=OTHER_TENANT,
        email="other-member@local",
        password_hash="x",
        display_name="其他租户成员",
        role="editor",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    db_session.add(other)
    await db_session.commit()
    rows = (await auth_client.get("/api/members", headers=auth_headers)).json()
    emails = {row["email"] for row in rows}
    assert "member@local" in emails
    assert "other-member@local" not in emails


async def test_initial_passwords_are_random(auth_client, auth_headers):
    first = (
        await _create_member(auth_client, auth_headers, email="first@local")
    ).json()["initial_password"]
    second = (
        await _create_member(auth_client, auth_headers, email="second@local")
    ).json()["initial_password"]
    assert first != second
    assert len(first) >= 12


async def test_member_update_requires_a_field(auth_client, auth_headers):
    member = (await _create_member(auth_client, auth_headers)).json()
    resp = await auth_client.patch(
        f"/api/members/{member['id']}", headers=auth_headers, json={}
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [{"display_name": None}, {"role": None}, {"is_active": None}],
)
async def test_member_update_rejects_explicit_nulls(
    auth_client, auth_headers, payload
):
    member = (await _create_member(auth_client, auth_headers)).json()
    resp = await auth_client.patch(
        f"/api/members/{member['id']}", headers=auth_headers, json=payload
    )
    assert resp.status_code == 422


async def test_audit_failure_is_not_reported_as_email_exists(
    auth_client, auth_headers, monkeypatch
):
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.exc import IntegrityError

    from app.api import members as members_module
    from app.main import app

    async def _boom(*args, **kwargs):
        raise IntegrityError("INSERT audit_logs", {}, Exception("boom"))

    monkeypatch.setattr(members_module, "record", _boom)
    # ASGITransport 默认把未处理异常重新抛给调用方；关闭后由全局异常
    # 处理器返回 500 信封，断言才与真实客户端看到的响应一致。
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        resp = await client.post(
            "/api/members",
            headers=auth_headers,
            json={"email": "audit-boom@local", "display_name": "成员"},
        )
    assert resp.status_code == 500
    assert resp.json()["code"] == "internal_error"


async def test_enabling_member_revokes_old_token(auth_client, auth_headers):
    created = (await _create_member(auth_client, auth_headers)).json()
    login = await auth_client.post(
        "/api/auth/login",
        json={"email": "member@local", "password": created["initial_password"]},
    )
    token = login.json()["access_token"]
    assert (
        await auth_client.delete(
            f"/api/members/{created['id']}", headers=auth_headers
        )
    ).status_code == 204
    enabled = await auth_client.patch(
        f"/api/members/{created['id']}",
        headers=auth_headers,
        json={"is_active": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["is_active"] is True
    old = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert old.status_code == 401
    assert old.json()["code"] == "token_revoked"
