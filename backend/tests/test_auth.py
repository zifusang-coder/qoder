from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import jwt
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.seed import ensure_admin


@asynccontextmanager
async def auth_client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    await ensure_admin(session)
    app.dependency_overrides[get_session] = lambda: session
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def test_login_rejects_wrong_password(db_session):
    async with auth_client(db_session) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"email": "admin@local", "password": "wrong-password"},
        )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


async def test_login_and_me_roundtrip(db_session):
    settings = get_settings()
    async with auth_client(db_session) as client:
        login_resp = await client.post(
            "/api/auth/login",
            json={
                "email": settings.admin_email,
                "password": settings.admin_password,
            },
        )
        assert login_resp.status_code == 200
        body = login_resp.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"]

        me_resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == settings.admin_email


async def test_me_requires_token(db_session):
    async with auth_client(db_session) as client:
        resp = await client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_login_unknown_email_returns_401(db_session):
    async with auth_client(db_session) as client:
        resp = await client.post(
            "/api/auth/login",
            json={"email": "nobody@local", "password": "any-password"},
        )
    assert resp.status_code == 401
    assert resp.json()["code"] == "invalid_credentials"


async def test_expired_token_rejected(db_session):
    settings = get_settings()
    now = datetime.now(UTC)
    expired = jwt.encode(
        {
            "sub": settings.admin_email,
            "tid": "default",
            "iat": now - timedelta(minutes=10),
            "exp": now - timedelta(minutes=5),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )
    async with auth_client(db_session) as client:
        resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {expired}"},
        )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


async def test_tampered_token_rejected(db_session):
    settings = get_settings()
    async with auth_client(db_session) as client:
        login_resp = await client.post(
            "/api/auth/login",
            json={
                "email": settings.admin_email,
                "password": settings.admin_password,
            },
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["access_token"]
        resp = await client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}x"},
        )
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"
