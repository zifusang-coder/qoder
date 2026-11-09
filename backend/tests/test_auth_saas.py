from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select

from app.config import get_settings
from app.models.base import DEFAULT_TENANT_ID
from app.models.tenant import Tenant
from app.models.user import User

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


async def _admin(session) -> User:
    return (
        await session.execute(
            select(User).where(User.email == get_settings().admin_email)
        )
    ).scalar_one()


async def test_me_returns_role_tenant_and_flags(auth_client, auth_headers):
    resp = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == get_settings().admin_email
    assert body["role"] == "owner"
    assert body["tenant_id"] == DEFAULT_TENANT_ID
    assert body["tenant_name"] == "默认租户"
    assert body["is_platform_admin"] is True
    assert body["must_change_password"] is False


async def test_token_without_version_is_rejected(auth_client, db_session):
    admin = await _admin(db_session)
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": admin.email,
            "tid": admin.tenant_id,
            "iat": now,
            "exp": now + timedelta(minutes=30),
        },
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    resp = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_token_version_mismatch_is_revoked(
    auth_client, auth_headers, db_session
):
    admin = await _admin(db_session)
    admin.token_version += 1
    await db_session.commit()
    resp = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 401
    assert resp.json()["code"] == "token_revoked"


async def test_token_with_non_integer_version_is_revoked(auth_client, db_session):
    admin = await _admin(db_session)
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": admin.email,
            "tid": admin.tenant_id,
            "ver": "abc",
            "iat": now,
            "exp": now + timedelta(minutes=30),
        },
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    resp = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "token_revoked"


async def test_disabled_user_is_forbidden(auth_client, auth_headers, db_session):
    admin = await _admin(db_session)
    admin.is_active = False
    await db_session.commit()
    resp = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "account_disabled"


async def test_suspended_tenant_is_forbidden(
    auth_client, auth_headers, db_session
):
    tenant = await db_session.get(Tenant, DEFAULT_TENANT_ID)
    tenant.status = "suspended"
    await db_session.commit()
    resp = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert resp.status_code == 403
    assert resp.json()["code"] == "tenant_suspended"


async def test_missing_tenant_is_forbidden(auth_client, db_session):
    from app.security import create_token, hash_password

    user = User(
        tenant_id=OTHER_TENANT,
        email="orphan@local",
        password_hash=hash_password("password123"),
        display_name="Orphan",
        role="owner",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    db_session.add(user)
    await db_session.commit()
    token = create_token(user.email, user.tenant_id, user.token_version)
    resp = await auth_client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "tenant_suspended"


async def _audit_rows(session, action):
    from app.models.audit import AuditLog

    return (
        await session.execute(
            select(AuditLog).where(AuditLog.action == action)
        )
    ).scalars().all()


async def test_login_writes_success_audit(auth_client, db_session):
    settings = get_settings()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert resp.status_code == 200
    rows = await _audit_rows(db_session, "auth.login")
    assert len(rows) == 1
    assert rows[0].tenant_id == DEFAULT_TENANT_ID
    assert rows[0].actor_email == settings.admin_email


async def test_bad_password_writes_failure_audit(auth_client, db_session):
    settings = get_settings()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": settings.admin_email, "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"
    rows = await _audit_rows(db_session, "auth.login_failed")
    assert len(rows) == 1
    assert rows[0].status == "failure"
    assert rows[0].detail == {"reason": "bad_password"}


async def test_disabled_user_cannot_login(auth_client, db_session):
    settings = get_settings()
    admin = await _admin(db_session)
    admin.is_active = False
    await db_session.commit()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "account_disabled"


async def test_suspended_tenant_cannot_login(auth_client, db_session):
    settings = get_settings()
    tenant = await db_session.get(Tenant, DEFAULT_TENANT_ID)
    tenant.status = "suspended"
    await db_session.commit()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "tenant_suspended"


async def test_change_password_rotates_token(auth_client, auth_headers, db_session):
    # 先置为待改密码，证明改密确实清除了该标记（token_version 不变，登录令牌仍有效）
    admin = await _admin(db_session)
    admin.must_change_password = True
    await db_session.commit()
    resp = await auth_client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": get_settings().admin_password,
            "new_password": "new-password-123",
        },
    )
    assert resp.status_code == 200
    new_token = resp.json()["access_token"]
    old = await auth_client.get("/api/auth/me", headers=auth_headers)
    assert old.status_code == 401
    assert old.json()["code"] == "token_revoked"
    fresh = await auth_client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert fresh.status_code == 200
    assert fresh.json()["must_change_password"] is False
    rows = await _audit_rows(db_session, "auth.change_password")
    assert len(rows) == 1


async def test_change_password_rejects_wrong_current(auth_client, auth_headers):
    resp = await auth_client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={"current_password": "wrong", "new_password": "new-password-123"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_password"


async def test_change_password_rejects_oversized_current(auth_client, auth_headers):
    resp = await auth_client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={"current_password": "x" * 200, "new_password": "new-password-123"},
    )
    assert resp.status_code == 422


async def test_unknown_email_login_logs_without_audit(auth_client, db_session, caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="app.api.auth"):
        resp = await auth_client.post(
            "/api/auth/login",
            json={"email": "ghost@local", "password": "whatever-123"},
        )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"
    rows = await _audit_rows(db_session, "auth.login_failed")
    assert rows == []
    assert any("ghost@local" in record.message for record in caplog.records)
    assert not any("whatever-123" in record.message for record in caplog.records)


async def test_change_password_rejects_short_and_same(auth_client, auth_headers):
    short = await auth_client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": get_settings().admin_password,
            "new_password": "short",
        },
    )
    assert short.status_code == 422
    same = await auth_client.post(
        "/api/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": get_settings().admin_password,
            "new_password": get_settings().admin_password,
        },
    )
    assert same.status_code == 422
