from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.user import User
from app.security import hash_password


async def ensure_admin(session: AsyncSession) -> None:
    settings = get_settings()
    existing = (
        await session.execute(select(User).where(User.email == settings.admin_email))
    ).scalar_one_or_none()
    if existing:
        return
    session.add(
        User(
            email=settings.admin_email,
            password_hash=hash_password(settings.admin_password),
            display_name="管理员",
        )
    )
    await session.commit()
