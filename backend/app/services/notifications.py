"""汇总需要人工关注的事项，供通知铃铛与 SSE 流共用。

平台错误文本可能回显凭据，通知只保留内容标题与平台信息，
详细原因仍留在各自页面的受控展示里。
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentApproval, AgentRun
from app.models.media import MediaJob
from app.models.project import Content
from app.models.publish import PublishJob
from app.models.workflow import WorkflowApproval, WorkflowRun

FAILURE_WINDOW = timedelta(days=7)
FAILED_PUBLISH_STATUSES = ("precheck_failed", "export_failed", "publish_failed")
ITEM_LIMIT = 30
DETAIL_CHARS = 80

KINDS = ("approval", "review", "failure")


def _excerpt(text: str, limit: int = DETAIL_CHARS) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned[:limit]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _item(
    *,
    kind: str,
    source: str,
    severity: str,
    title: str,
    detail: str,
    href: str,
    ref: Any,
    at: datetime | None,
) -> dict[str, Any]:
    return {
        "id": f"{kind}:{ref}",
        "kind": kind,
        "source": source,
        "severity": severity,
        "title": title,
        "detail": detail,
        "href": href,
        "created_at": _iso(at),
    }


async def _agent_approval_items(session: AsyncSession, tenant_id: str) -> list[dict]:
    rows = (
        await session.execute(
            select(AgentApproval, AgentRun)
            .join(
                AgentRun,
                (AgentRun.id == AgentApproval.run_id)
                & (AgentRun.tenant_id == AgentApproval.tenant_id),
            )
            .where(
                AgentApproval.tenant_id == tenant_id,
                AgentApproval.status == "pending",
            )
            .order_by(AgentApproval.created_at.desc())
            .limit(ITEM_LIMIT)
        )
    ).all()
    items: list[dict] = []
    seen: set = set()
    for approval, run in rows:
        if run.id in seen:
            continue
        seen.add(run.id)
        team = run.mode == "team"
        items.append(
            _item(
                kind="approval",
                source="assistant",
                severity="warn",
                title="团队运行等待审批" if team else "AI 助理等待审批",
                detail=f"{approval.tool_name} · {_excerpt(run.goal)}",
                href=f"/assistant/teams/{run.id}" if team else "/assistant",
                ref=run.id,
                at=approval.created_at,
            )
        )
    return items


async def _workflow_approval_items(
    session: AsyncSession, tenant_id: str
) -> list[dict]:
    rows = (
        await session.execute(
            select(WorkflowApproval, WorkflowRun)
            .join(
                WorkflowRun,
                (WorkflowRun.id == WorkflowApproval.run_id)
                & (WorkflowRun.tenant_id == WorkflowApproval.tenant_id),
            )
            .where(
                WorkflowApproval.tenant_id == tenant_id,
                WorkflowApproval.status == "pending",
            )
            .order_by(WorkflowApproval.created_at.desc())
            .limit(ITEM_LIMIT)
        )
    ).all()
    items: list[dict] = []
    seen: set = set()
    for approval, run in rows:
        if run.id in seen:
            continue
        seen.add(run.id)
        label = approval.title or approval.node_id
        if approval.tool_name:
            label = f"{label} · {approval.tool_name}"
        items.append(
            _item(
                kind="approval",
                source="workflow",
                severity="warn",
                title="工作流等待审批",
                detail=label,
                href=f"/workflows/{run.workflow_id}/runs/{run.id}",
                ref=run.id,
                at=approval.created_at,
            )
        )
    return items


async def _review_items(session: AsyncSession, tenant_id: str) -> list[dict]:
    rows = (
        await session.execute(
            select(PublishJob, Content)
            .join(
                Content,
                (Content.id == PublishJob.content_id)
                & (Content.tenant_id == PublishJob.tenant_id),
            )
            .where(
                PublishJob.tenant_id == tenant_id,
                PublishJob.status == "awaiting_review",
            )
            .order_by(PublishJob.updated_at.desc())
            .limit(ITEM_LIMIT)
        )
    ).all()
    return [
        _item(
            kind="review",
            source="publish",
            severity="warn",
            title="发布任务等待审核",
            detail=f"{_excerpt(content.title) or '未命名内容'} · {job.platform}",
            href="/publish",
            ref=job.id,
            at=job.updated_at,
        )
        for job, content in rows
    ]


async def _failure_items(session: AsyncSession, tenant_id: str) -> list[dict]:
    cutoff = datetime.now(UTC) - FAILURE_WINDOW
    items: list[dict] = []

    publish_rows = (
        await session.execute(
            select(PublishJob, Content)
            .join(
                Content,
                (Content.id == PublishJob.content_id)
                & (Content.tenant_id == PublishJob.tenant_id),
            )
            .where(
                PublishJob.tenant_id == tenant_id,
                PublishJob.status.in_(FAILED_PUBLISH_STATUSES),
                PublishJob.updated_at >= cutoff,
            )
            .order_by(PublishJob.updated_at.desc())
            .limit(ITEM_LIMIT)
        )
    ).all()
    items.extend(
        _item(
            kind="failure",
            source="publish",
            severity="error",
            title="发布任务失败",
            detail=f"{_excerpt(content.title) or '未命名内容'} · {job.platform}",
            href="/publish",
            ref=job.id,
            at=job.updated_at,
        )
        for job, content in publish_rows
    )

    agent_rows = (
        await session.execute(
            select(AgentRun)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.status == "failed",
                func.coalesce(AgentRun.finished_at, AgentRun.updated_at) >= cutoff,
            )
            .order_by(func.coalesce(AgentRun.finished_at, AgentRun.updated_at).desc())
            .limit(ITEM_LIMIT)
        )
    ).scalars().all()
    items.extend(
        _item(
            kind="failure",
            source="assistant",
            severity="error",
            title="AI 助理运行失败",
            detail=_excerpt(run.goal),
            href="/assistant",
            ref=run.id,
            at=run.finished_at or run.updated_at,
        )
        for run in agent_rows
    )

    workflow_rows = (
        await session.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.status == "failed",
                func.coalesce(WorkflowRun.finished_at, WorkflowRun.updated_at)
                >= cutoff,
            )
            .order_by(
                func.coalesce(WorkflowRun.finished_at, WorkflowRun.updated_at).desc()
            )
            .limit(ITEM_LIMIT)
        )
    ).scalars().all()
    items.extend(
        _item(
            kind="failure",
            source="workflow",
            severity="error",
            title="工作流运行失败",
            detail=f"运行 {str(run.id)[:8]}",
            href=f"/workflows/{run.workflow_id}/runs/{run.id}",
            ref=run.id,
            at=run.finished_at or run.updated_at,
        )
        for run in workflow_rows
    )

    media_rows = (
        await session.execute(
            select(MediaJob)
            .where(
                MediaJob.tenant_id == tenant_id,
                MediaJob.status == "failed",
                or_(
                    MediaJob.finished_at >= cutoff,
                    (MediaJob.finished_at.is_(None))
                    & (MediaJob.updated_at >= cutoff),
                ),
            )
            .order_by(func.coalesce(MediaJob.finished_at, MediaJob.updated_at).desc())
            .limit(ITEM_LIMIT)
        )
    ).scalars().all()
    items.extend(
        _item(
            kind="failure",
            source="media",
            severity="error",
            title="媒体任务失败",
            detail=f"{job.skill_id}",
            href=f"/projects/{job.project_id}",
            ref=job.id,
            at=job.finished_at or job.updated_at,
        )
        for job in media_rows
    )
    return items


def _sort_key(item: dict[str, Any]) -> str:
    return item["created_at"] or ""


async def collect_notifications(
    session: AsyncSession, tenant_id: str
) -> dict[str, Any]:
    items: list[dict] = []
    items.extend(await _agent_approval_items(session, tenant_id))
    items.extend(await _workflow_approval_items(session, tenant_id))
    items.extend(await _review_items(session, tenant_id))
    items.extend(await _failure_items(session, tenant_id))
    items.sort(key=_sort_key, reverse=True)
    items = items[:ITEM_LIMIT]
    counts = {kind: 0 for kind in KINDS}
    for item in items:
        counts[item["kind"]] += 1
    return {"items": items, "counts": counts}
