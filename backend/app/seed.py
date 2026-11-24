import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DEFAULT_TENANT_ID, get_settings
from app.models.tenant import Tenant
from app.models.user import User
from app.security import hash_password

logger = logging.getLogger(__name__)


async def _seed(session: AsyncSession, *, email: str, password: str) -> None:
    """写入默认租户、管理员账号与平台管理员标记，并提交事务。"""
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
            password_hash=hash_password(password),
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

    await session.commit()


async def ensure_admin(session: AsyncSession) -> None:
    """幂等地保证默认管理员存在；并发启动时冲突则回滚后重试一次。"""
    settings = get_settings()
    email = settings.admin_email.strip().lower()
    password = settings.admin_password

    try:
        await _seed(session, email=email, password=password)
        return
    except IntegrityError:
        # 多进程同时启动时可能在此前的 flush 或 commit 上输掉写入竞争，
        # 回滚后重跑一次即可看到竞争胜出方已提交的行。
        await session.rollback()
        logger.warning("ensure_admin 写入冲突，重试一次")

    try:
        await _seed(session, email=email, password=password)
    except IntegrityError:
        await session.rollback()
        logger.exception("ensure_admin 重试后仍冲突")
        raise
