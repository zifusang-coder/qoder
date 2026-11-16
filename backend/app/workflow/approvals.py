"""工作流级审批：创建、查询、决策与过期。

Agent 子运行内部的审批仍在 ``agent_approvals``；本模块在判断「还有没有
待处理审批」时把两条通道合并，保证并行分支不会提前唤醒运行。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.sanitize import fit_bytes, scrub
from app.config import get_settings
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.workflow import WorkflowApproval, WorkflowNodeRun, WorkflowRun

APPROVAL_KINDS = frozenset({"tool_confirm", "review"})
DECISIONS = frozenset({"approve", "reject"})


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _presentable_payload(payload: dict) -> dict:
    """审批 payload 只用于展示：脱敏、限长，原文永不落库。"""
    cleaned, truncated = fit_bytes(scrub(payload if isinstance(payload, dict) else {}))
    if not isinstance(cleaned, dict):
        cleaned = {}
    if truncated:
        cleaned = {**cleaned, "truncated": True}
    return cleaned


async def create_approval(
    session: AsyncSession,
    *,
    run: WorkflowRun,
    node_id: str,
    kind: str,
    title: str,
    payload: dict,
    tool_name: str = "",
    risk: str = "",
    ttl_hours: int | None = None,
    now: datetime | None = None,
) -> WorkflowApproval:
    """建一条 pending 审批；只 ``add`` 不 ``commit``，与运行/节点状态同批提交。"""
    if kind not in APPROVAL_KINDS:
        raise ValueError(f"未知审批类型: {kind}")
    moment = now or _now()
    hours = ttl_hours
    if hours is None:
        hours = get_settings().workflow_approval_ttl_hours
    approval = WorkflowApproval(
        tenant_id=run.tenant_id,
        run_id=run.id,
        node_id=node_id,
        kind=kind,
        tool_name=tool_name[:100],
        risk=risk[:16],
        payload=_presentable_payload(payload),
        title=title[:200],
        status="pending",
        expires_at=moment + timedelta(hours=max(1, hours)),
    )
    session.add(approval)
    return approval


async def node_approval(
    session: AsyncSession,
    *,
    tenant_id: str,
    run_id: uuid.UUID,
    node_id: str,
) -> WorkflowApproval | None:
    """该节点最近一次需要展示的审批，优先 pending，否则取最近决定。

    SQLite 的 ``created_at`` 只有秒级精度，所以排序以 ``decided_at`` 为主，
    依次退化到 ``expires_at``、``created_at``，最后用 id 兜底。
    """
    rows = (
        (
            await session.execute(
                select(WorkflowApproval).where(
                    WorkflowApproval.tenant_id == tenant_id,
                    WorkflowApproval.run_id == run_id,
                    WorkflowApproval.node_id == node_id,
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    pending = [row for row in rows if row.status == "pending"]
    pool = pending or rows
    floor = datetime.min.replace(tzinfo=UTC)

    def order(row: WorkflowApproval) -> tuple[datetime, datetime, str]:
        moment = (
            _aware(row.decided_at) or _aware(row.expires_at) or _aware(row.created_at)
        )
        created = _aware(row.created_at)
        return (moment or floor, created or floor, str(row.id))

    return max(pool, key=order)


async def has_pending(
    session: AsyncSession,
    *,
    tenant_id: str,
    run_id: uuid.UUID,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    """两条通道里还有没有待处理审批（可选排除刚决策的那条）。"""
    workflow_stmt = select(WorkflowApproval.id).where(
        WorkflowApproval.tenant_id == tenant_id,
        WorkflowApproval.run_id == run_id,
        WorkflowApproval.status == "pending",
    )
    if exclude_id is not None:
        workflow_stmt = workflow_stmt.where(WorkflowApproval.id != exclude_id)
    if (await session.execute(workflow_stmt.limit(1))).first() is not None:
        return True
    agent_stmt = (
        select(AgentApproval.id)
        .join(AgentRun, AgentRun.id == AgentApproval.run_id)
        .where(
            AgentRun.tenant_id == tenant_id,
            AgentRun.workflow_run_id == run_id,
            AgentApproval.tenant_id == tenant_id,
            AgentApproval.status == "pending",
        )
        .limit(1)
    )
    return (await session.execute(agent_stmt)).first() is not None


async def _mark_decision(
    session: AsyncSession,
    approval: WorkflowApproval,
    *,
    status: str,
    actor_id: uuid.UUID,
    now: datetime,
    note: str,
) -> int:
    """``pending → approved/rejected`` 的 CAS，返回受影响行数。"""
    marked = await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.id == approval.id,
            WorkflowApproval.tenant_id == approval.tenant_id,
            WorkflowApproval.status == "pending",
        )
        .values(
            status=status,
            decided_by=actor_id,
            decided_at=now,
            note=note,
        )
        .execution_options(synchronize_session=False)
    )
    return marked.rowcount


async def _cancel_children(
    session: AsyncSession, run: WorkflowRun, moment: datetime
) -> None:
    """驳回即终结：子运行、它们的待处理审批与在途步骤一并关闭。

    否则驳回后仍能审批一个永远不会继续的分支。
    """
    child_ids = select(AgentRun.id).where(
        AgentRun.tenant_id == run.tenant_id,
        AgentRun.workflow_run_id == run.id,
    )
    await session.execute(
        update(AgentApproval)
        .where(
            AgentApproval.run_id.in_(child_ids),
            AgentApproval.status == "pending",
        )
        .values(status="expired", decided_at=moment, note="工作流运行已驳回")
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentStep)
        .where(
            AgentStep.run_id.in_(child_ids),
            AgentStep.status == "running",
        )
        .values(
            status="interrupted",
            error="工作流运行已驳回",
            finished_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentRun)
        .where(
            AgentRun.tenant_id == run.tenant_id,
            AgentRun.workflow_run_id == run.id,
            AgentRun.status.in_(("queued", "running", "waiting_approval")),
        )
        .values(status="cancelled", finished_at=moment)
        .execution_options(synchronize_session=False)
    )


async def decide_approval(
    session: AsyncSession,
    *,
    run: WorkflowRun,
    approval: WorkflowApproval,
    decision: str,
    actor_id: uuid.UUID,
    note: str = "",
    now: datetime | None = None,
) -> str:
    """做出一次决策，返回运行的新状态（queued/waiting_approval/failed）。

    角色与「不能审批自己发起」的校验在 API 层；这里只保证状态机与并发。
    审批与运行状态的 CAS 在同一事务内：任一失败整体回滚，不会留下
    「审批已通过但运行已取消」的脏数据。
    """
    if decision not in DECISIONS:
        raise AppError("workflow_invalid", "审批决定不合法", 422)
    if approval.run_id != run.id or approval.tenant_id != run.tenant_id:
        raise AppError("workflow_approval_not_pending", "审批与运行不匹配", 409)
    if approval.status != "pending":
        raise AppError("workflow_approval_not_pending", "该审批已处理或已过期", 409)
    moment = now or _now()
    expires = _aware(approval.expires_at)
    if expires is not None and expires <= moment:
        await session.execute(
            update(WorkflowApproval)
            .where(
                WorkflowApproval.id == approval.id,
                WorkflowApproval.tenant_id == approval.tenant_id,
                WorkflowApproval.status == "pending",
            )
            .values(status="expired", decided_at=moment)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        raise AppError("workflow_approval_not_pending", "该审批已过期", 409)

    target = "approved" if decision == "approve" else "rejected"
    marked = await _mark_decision(
        session,
        approval,
        status=target,
        actor_id=actor_id,
        now=moment,
        note=note[:2000],
    )
    if marked == 0:
        await session.rollback()
        raise AppError("workflow_approval_not_pending", "该审批已处理或已过期", 409)

    if decision == "approve":
        others = await has_pending(
            session,
            tenant_id=run.tenant_id,
            run_id=run.id,
            exclude_id=approval.id,
        )
        if others:
            # 并行分支还有别的待处理审批：保持等待，等待时长留给最后一条结算。
            await session.commit()
            await session.refresh(approval)
            return "waiting_approval"
        usage = dict(run.usage or {})
        paused_at = _aware(run.heartbeat_at) or moment
        waited_ms = int(
            max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000
        )
        usage["paused_ms"] = int(usage.get("paused_ms") or 0) + waited_ms
        queued = await session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run.id,
                WorkflowRun.tenant_id == run.tenant_id,
                WorkflowRun.status == "waiting_approval",
            )
            .values(
                status="queued",
                usage=usage,
                heartbeat_at=moment,
                error_code="",
                error_message="",
                finished_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if queued.rowcount == 0:
            # 取消或过期先赢了：回滚审批决定，运行保持原终态。
            await session.rollback()
            raise AppError("workflow_approval_not_pending", "运行状态已变化，无法继续", 409)
        await session.commit()
        await session.refresh(run)
        await session.refresh(approval)
        return "queued"

    error_code = "review_rejected" if approval.kind == "review" else "approval_rejected"
    message = "审核未通过" if approval.kind == "review" else "审批被驳回"
    await session.execute(
        update(WorkflowNodeRun)
        .where(
            WorkflowNodeRun.run_id == run.id,
            WorkflowNodeRun.node_id == approval.node_id,
            WorkflowNodeRun.status == "waiting",
        )
        .values(status="denied", error=message, finished_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.tenant_id == run.tenant_id,
            WorkflowApproval.run_id == run.id,
            WorkflowApproval.status == "pending",
            WorkflowApproval.id != approval.id,
        )
        .values(status="expired", decided_at=moment)
        .execution_options(synchronize_session=False)
    )
    await _cancel_children(session, run, moment)
    failed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(
            status="failed",
            error_code=error_code,
            error_message=message,
            finished_at=moment,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )
    if failed.rowcount == 0:
        await session.rollback()
        raise AppError("workflow_approval_not_pending", "运行状态已变化，无法继续", 409)
    await session.commit()
    await session.refresh(run)
    await session.refresh(approval)
    return "failed"
