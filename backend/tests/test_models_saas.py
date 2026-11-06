from sqlalchemy import select

from app.config import DEFAULT_TENANT_ID, get_settings
from app.models.audit import AuditLog
from app.models.tenant import Tenant
from app.models.user import User
from app.seed import ensure_admin


async def test_ensure_admin_creates_default_tenant_and_owner(db_session):
    await ensure_admin(db_session)

    tenant = await db_session.get(Tenant, DEFAULT_TENANT_ID)
    assert tenant is not None
    assert tenant.name == "默认租户"
    assert tenant.slug == "default"
    assert tenant.status == "active"

    admin = (
        await db_session.execute(
            select(User).where(User.email == get_settings().admin_email)
        )
    ).scalar_one()
    assert admin.role == "owner"
    assert admin.is_active is True
    assert admin.is_platform_admin is True
    assert admin.token_version == 0
    assert admin.must_change_password is False


async def test_ensure_admin_is_idempotent(db_session):
    await ensure_admin(db_session)
    await ensure_admin(db_session)

    tenants = (await db_session.execute(select(Tenant))).scalars().all()
    assert len(tenants) == 1

    platform_admins = (
        await db_session.execute(
            select(User).where(User.is_platform_admin.is_(True))
        )
    ).scalars().all()
    assert len(platform_admins) == 1


async def test_audit_log_persists_json_detail(db_session):
    db_session.add(
        AuditLog(
            tenant_id=DEFAULT_TENANT_ID,
            actor_email="scheduler",
            action="publish.auto_execute",
            detail={"ok": True},
        )
    )
    await db_session.commit()

    row = (await db_session.execute(select(AuditLog))).scalar_one()
    assert row.detail == {"ok": True}
    assert row.status == "success"
    assert row.actor_id is None
