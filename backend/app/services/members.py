from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.tenant import Tenant
from app.models.user import User


async def ensure_not_last_owner(
    session: AsyncSession, tenant_id: str, member: User
) -> None:
    if member.role != "owner" or not member.is_active:
        return
    # 同一租户的 owner 变更串行化；否则两个并发降级可能都读到 2 名 owner。
    # SQLite 会忽略 FOR UPDATE，但真实部署使用 PostgreSQL。
    await session.execute(
        select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()
    )
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
