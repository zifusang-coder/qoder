from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.user import User


async def ensure_not_last_owner(
    session: AsyncSession, tenant_id: str, member: User
) -> None:
    if member.role != "owner" or not member.is_active:
        return
    owners = (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(
                User.tenant_id == tenant_id,
                User.role == "owner",
                User.is_active.is_(True),
            )
        )
    ).scalar_one()
    if owners <= 1:
        raise AppError("last_owner", "租户必须保留至少一名所有者", 409)
