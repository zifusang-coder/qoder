from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.periods import period_bounds
from app.models.agent import AgentRun
from app.models.media import MediaJob
from app.models.skill import SkillRun
from app.models.workflow import WorkflowRun

# AgentRun.mode 里只有这些取值代表用户实际发起的一次运行；role 是团队运行的
# 子运行，只计 token，不重复计 run.agent。
_BILLABLE_AGENT_MODES = ("single", "node", "team")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def _skill_usage(
    session: AsyncSession, tenant_id: str, start: datetime, end: datetime
) -> tuple[int, int, int]:
    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(SkillRun.prompt_tokens), 0),
                func.coalesce(func.sum(SkillRun.completion_tokens), 0),
            )
            .select_from(SkillRun)
            .where(
                SkillRun.tenant_id == tenant_id,
                SkillRun.created_at >= start,
                SkillRun.created_at < end,
            )
        )
    ).one()
    return _as_int(row[0]), _as_int(row[1]), _as_int(row[2])


async def _agent_usage(
    session: AsyncSession, tenant_id: str, start: datetime, end: datetime
) -> tuple[int, int, int]:
    rows = (
        await session.execute(
            select(AgentRun.usage, AgentRun.mode).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.created_at >= start,
                AgentRun.created_at < end,
            )
        )
    ).all()
    prompt = completion = runs = 0
    for usage, mode in rows:
        payload = usage if isinstance(usage, dict) else {}
        prompt += _as_int(payload.get("prompt_tokens"))
        completion += _as_int(payload.get("completion_tokens"))
        if mode in _BILLABLE_AGENT_MODES:
            runs += 1
    return prompt, completion, runs


async def _count(
    session: AsyncSession,
    model,
    tenant_id: str,
    start: datetime,
    end: datetime,
) -> int:
    return _as_int(
        (
            await session.execute(
                select(func.count())
                .select_from(model)
                .where(
                    model.tenant_id == tenant_id,
                    model.created_at >= start,
                    model.created_at < end,
                )
            )
        ).scalar_one()
    )


async def _media_usage(
    session: AsyncSession, tenant_id: str, start: datetime, end: datetime
) -> tuple[int, int]:
    rows = (
        await session.execute(
            select(
                MediaJob.status,
                MediaJob.started_at,
                MediaJob.finished_at,
            ).where(
                MediaJob.tenant_id == tenant_id,
                MediaJob.created_at >= start,
                MediaJob.created_at < end,
            )
        )
    ).all()
    seconds = 0
    for status, started_at, finished_at in rows:
        if status != "succeeded" or started_at is None or finished_at is None:
            continue
        seconds += max(0, int((finished_at - started_at).total_seconds()))
    return len(rows), seconds


async def compute_usage(
    session: AsyncSession, tenant_id: str, period: str
) -> dict[str, int]:
    start, end = period_bounds(period)
    skill_runs, skill_prompt, skill_completion = await _skill_usage(
        session, tenant_id, start, end
    )
    agent_prompt, agent_completion, agent_runs = await _agent_usage(
        session, tenant_id, start, end
    )
    workflow_runs = await _count(session, WorkflowRun, tenant_id, start, end)
    media_jobs, media_seconds = await _media_usage(
        session, tenant_id, start, end
    )
    return {
        "llm.prompt_tokens": skill_prompt + agent_prompt,
        "llm.completion_tokens": skill_completion + agent_completion,
        "run.skill": skill_runs,
        "run.agent": agent_runs,
        "run.workflow": workflow_runs,
        "media.jobs": media_jobs,
        "media.seconds": media_seconds,
    }
