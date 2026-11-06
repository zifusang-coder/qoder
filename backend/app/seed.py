from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TENANT_ID, get_settings
from app.models.tenant import Tenant
from app.models.user import User
from app.security import hash_password


async def ensure_admin(session: AsyncSession) -> None:
    settings = get_settings()
    email = settings.admin_email.strip().lower()

    tenant = await session.get(Tenant, DEFAULT_TENANT_ID)
    if tenant is None:
        session.add(
            Tenant(
                id=DEFAULT_TENANT_ID,
                name="默认租户",
                slug="default",
                status="active",
            )
        )
        await session.flush()

    user = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            tenant_id=DEFAULT_TENANT_ID,
            email=email,
            password_hash=hash_password(settings.admin_password),
            display_name="管理员",
            role="owner",
            is_active=True,
            is_platform_admin=False,
            token_version=0,
        )
        session.add(user)
        await session.flush()

    has_platform_admin = (
        await session.execute(
            select(User.id).where(User.is_platform_admin.is_(True)).limit(1)
        )
    ).first()
    if has_platform_admin is None:
        user.is_platform_admin = True

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
