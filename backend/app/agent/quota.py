"""租户日 token 用量：API 创建工作流运行与每个 Agent 节点启动前共用。"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError
from app.models.agent import AgentRun


def day_start(now: datetime | None = None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


async def daily_token_usage(
    session: AsyncSession, tenant_id: str, *, now: datetime | None = None
) -> int:
    rows = (
        await session.execute(
            select(AgentRun.usage).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.created_at >= day_start(now),
            )
        )
    ).scalars().all()
    used = 0
    for usage in rows:
        if isinstance(usage, dict):
            used += int(usage.get("prompt_tokens") or 0)
            used += int(usage.get("completion_tokens") or 0)
    return used


async def ensure_daily_quota(
    session: AsyncSession,
    tenant_id: str,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> None:
    # 0 关闭日配额（与第 1 层口径一致）
    quota = int(settings.agent_daily_token_quota or 0)
    if quota <= 0:
        return
    if await daily_token_usage(session, tenant_id, now=now) >= quota:
        raise AppError("agent_daily_quota_exceeded", "今日 token 配额已用尽", 429)
