"""团队黑板：只追加、服务端分配 seq、入库前脱敏与截断。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.sanitize import fit_bytes, scrub
from app.models.agent import AgentRun, AgentTeamEntry

MAX_ENTRY_BYTES = 4096


async def append_entry(
    session: AsyncSession,
    *,
    run: AgentRun,
    kind: str,
    role: str = "",
    content: dict,
) -> AgentTeamEntry:
    last = (
        await session.execute(
            select(func.max(AgentTeamEntry.seq)).where(
                AgentTeamEntry.run_id == run.id
            )
        )
    ).scalar()
    cleaned, _ = fit_bytes(scrub(content), MAX_ENTRY_BYTES)
    entry = AgentTeamEntry(
        tenant_id=run.tenant_id,
        run_id=run.id,
        seq=int(last or 0) + 1,
        kind=kind,
        role=role,
        content=cleaned if isinstance(cleaned, dict) else {"text": cleaned},
    )
    session.add(entry)
    await session.flush()
    return entry


async def recent_entries(
    session: AsyncSession, run_id, limit: int
) -> list[AgentTeamEntry]:
    rows = (
        await session.execute(
            select(AgentTeamEntry)
            .where(AgentTeamEntry.run_id == run_id)
            .order_by(AgentTeamEntry.seq.desc())
            .limit(max(1, limit))
        )
    ).scalars().all()
    return list(reversed(rows))


def render_entries(entries: list[AgentTeamEntry]) -> list[dict]:
    return [
        {"seq": e.seq, "kind": e.kind, "role": e.role, "content": e.content}
        for e in entries
    ]