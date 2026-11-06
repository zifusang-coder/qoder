import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.base import DEFAULT_TENANT_ID, Base
from app.models.tenant import Tenant
from app.models.user import User
from app.seed import ensure_admin
from app.skills.registry import get_registry


@pytest.fixture(autouse=True)
def _disable_embeddings(monkeypatch):
    # 本地 .env 可能开启 EMBEDDING_MODEL；测试默认关闭，语义检索用例自行开启
    monkeypatch.setenv("EMBEDDING_MODEL", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_fks(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def auth_client(db_session):
    await ensure_admin(db_session)
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


@pytest_asyncio.fixture
async def auth_headers(auth_client):
    settings = get_settings()
    resp = await auth_client.post(
        "/api/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest_asyncio.fixture
async def client(db_session):
    fake = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="tester@local",
        password_hash="x",
        display_name="Tester",
        role="owner",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: fake
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_session, None)


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_registry.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_registry.cache_clear()


@pytest_asyncio.fixture
async def tenant_factory(db_session):
    async def create(*, name="测试租户", slug=None, status="active"):
        tenant = Tenant(
            id=str(uuid.uuid4()),
            name=name,
            slug=slug or f"t-{uuid.uuid4().hex[:8]}",
            status=status,
        )
        db_session.add(tenant)
        await db_session.commit()
        return tenant

    return create
