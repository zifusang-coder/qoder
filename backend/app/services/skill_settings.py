from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.skill import SkillSetting


async def enabled_map(session: AsyncSession, tenant_id: str) -> dict[str, bool]:
    rows = (
        await session.execute(
            select(SkillSetting).where(SkillSetting.tenant_id == tenant_id)
        )
    ).scalars().all()
    return {row.skill_id: row.enabled for row in rows}


async def require_enabled(
    session: AsyncSession, tenant_id: str, skill_id: str
) -> None:
    enabled = (await enabled_map(session, tenant_id)).get(skill_id, True)
    if not enabled:
        raise AppError("skill_disabled", "该技能已停用", 409)
