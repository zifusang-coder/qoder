from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.security import hash_password


async def _temp_user(db_session) -> User:
    user = User(
        tenant_id=DEFAULT_TENANT_ID,
        email="temp-editor@local",
        password_hash=hash_password("temp-pass-123"),
        display_name="临时编辑",
        role="editor",
        is_active=True,
        must_change_password=True,
        is_platform_admin=False,
        token_version=0,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _login(auth_client) -> str:
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": "temp-editor@local", "password": "temp-pass-123"},
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_business_endpoints_blocked_until_password_changed(
    auth_client, db_session
):
    await _temp_user(db_session)
    token = await _login(auth_client)
    headers = {"Authorization": f"Bearer {token}"}

    me = await auth_client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True

    blocked = await auth_client.get("/api/projects", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "password_change_required"

    changed = await auth_client.post(
        "/api/auth/change-password",
        headers=headers,
        json={
            "current_password": "temp-pass-123",
            "new_password": "changed-pass-123",
        },
    )
    assert changed.status_code == 200
    fresh = {"Authorization": f"Bearer {changed.json()['access_token']}"}

    allowed = await auth_client.get("/api/projects", headers=fresh)
    assert allowed.status_code == 200


async def test_password_gate_applies_to_platform_admin(auth_client, db_session):
    user = await _temp_user(db_session)
    user.email = "temp-platform@local"
    user.is_platform_admin = True
    await db_session.commit()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": "temp-platform@local", "password": "temp-pass-123"},
    )
    token = resp.json()["access_token"]
    tenants = await auth_client.get(
        "/api/tenants", headers={"Authorization": f"Bearer {token}"}
    )
    assert tenants.status_code == 403
    assert tenants.json()["code"] == "password_change_required"
