import logging
import uuid
from datetime import UTC, datetime

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.policy import load_overrides, snapshot_policy
from app.agent.quota import ensure_daily_quota
from app.agent.registry import get_tool_registry
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.permissions import admin_access, editor_access, viewer_access
from app.queue import get_queue
from app.services.audit import record_best_effort
from app.workflow import approvals as workflow_approvals
from app.workflow import lifecycle
from app.workflow.validate import validate_inputs

logger = logging.getLogger(__name__)

router = APIRouter(tags=["workflows"])

QUEUE_UNAVAILABLE_MESSAGE = "队列暂不可用，请稍后重试"
LIMITED_STATUSES = ("queued", "running")


class StartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    note: str = Field(default="", max_length=2000)


class WorkflowRunOut(BaseModel):
    id: str
    workflow_id: str
    workflow_version_id: str
    version: int
    trigger: str
    triggered_by: str | None
    schedule_id: str | None
    scheduled_for: datetime | None
    status: str
    inputs: dict
    result: dict
    error_code: str
    error_message: str
    usage: dict
    attempts: int
    resume_count: int
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class NodeRunOut(BaseModel):
    node_id: str
    attempt: int
    kind: str
    status: str
    tool_step_index: int | None
    agent_run_id: str | None
    input: dict
    output: dict
    error: str
    duration_ms: int
    started_at: datetime | None
    finished_at: datetime | None


class ApprovalOut(BaseModel):
    id: str
    run_id: str
    workflow_id: str
    source: str
    kind: str
    node_id: str
    tool_name: str
    risk: str
    title: str
    payload: dict
    status: str
    note: str
    expires_at: datetime | None
    created_at: datetime | None


class WorkflowRunDetailOut(BaseModel):
    run: WorkflowRunOut
    node_runs: list[NodeRunOut]
    approvals: list[ApprovalOut]
    result: dict


def _now() -> datetime:
    return datetime.now(UTC)


def _uuid(raw: str, code: str, message: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        raise AppError(code, message, 404) from None


async def _owned_workflow(
    session: AsyncSession, raw: str, user: User
) -> Workflow:
    row = (
        await session.execute(
            select(Workflow).where(
                Workflow.id == _uuid(raw, "workflow_not_found", "工作流不存在"),
                Workflow.tenant_id == user.tenant_id,
                Workflow.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_not_found", "工作流不存在", 404)
    return row


async def _owned_run(session: AsyncSession, raw: str, user: User) -> WorkflowRun:
    row = (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.id
                == _uuid(raw, "workflow_run_not_found", "运行不存在"),
                WorkflowRun.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_run_not_found", "运行不存在", 404)
    return row


async def _owned_approval(
    session: AsyncSession, raw: str, user: User
) -> WorkflowApproval:
    row = (
        await session.execute(
            select(WorkflowApproval).where(
                WorkflowApproval.id
                == _uuid(raw, "workflow_not_found", "审批不存在"),
                WorkflowApproval.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_not_found", "审批不存在", 404)
    return row


async def _version_row(
    session: AsyncSession, workflow: Workflow, number: int | None
) -> WorkflowVersion:
    if number is None:
        row = (
            await session.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == workflow.id)
                .order_by(WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            raise AppError("workflow_not_published", "工作流尚未发布", 409)
        return row
    row = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.workflow_id == workflow.id,
                WorkflowVersion.version == number,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("workflow_not_found", "版本不存在", 404)
    return row


async def _version_number(session: AsyncSession, run: WorkflowRun) -> int:
    row = await session.get(WorkflowVersion, run.workflow_version_id)
    if row is None or row.tenant_id != run.tenant_id:
        raise AppError("workflow_run_not_found", "运行不存在", 404)
    return row.version


def _run_out(run: WorkflowRun, version: int) -> WorkflowRunOut:
    return WorkflowRunOut(
        id=str(run.id),
        workflow_id=str(run.workflow_id),
        workflow_version_id=str(run.workflow_version_id),
        version=version,
        trigger=run.trigger,
        triggered_by=str(run.triggered_by) if run.triggered_by else None,
        schedule_id=str(run.schedule_id) if run.schedule_id else None,
        scheduled_for=run.scheduled_for,
        status=run.status,
        inputs=run.inputs or {},
        result=run.result or {},
        error_code=run.error_code,
        error_message=run.error_message,
        usage=run.usage or {},
        attempts=run.attempts,
        resume_count=run.resume_count,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _node_out(row: WorkflowNodeRun) -> NodeRunOut:
    # raw_output 只用于变量解析，永远不进入响应。
    return NodeRunOut(
        node_id=row.node_id,
        attempt=row.attempt,
        kind=row.kind,
        status=row.status,
        tool_step_index=row.tool_step_index,
        agent_run_id=str(row.agent_run_id) if row.agent_run_id else None,
        input=row.input or {},
        output=row.output or {},
        error=row.error,
        duration_ms=row.duration_ms,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _approval_out(
    approval,
    run: WorkflowRun,
    *,
    source: str,
    node_id: str,
    risk: str,
) -> ApprovalOut:
    # agent_approvals 没有 title 列，工作流通道才有；节点审批只回工具名。
    title = getattr(approval, "title", "")
    return ApprovalOut(
        id=str(approval.id),
        run_id=str(run.id),
        workflow_id=str(run.workflow_id),
        source=source,
        kind=approval.kind if source == "workflow" else "tool",
        node_id=node_id,
        tool_name=approval.tool_name,
        risk=risk,
        title=title,
        payload=approval.payload or {},
        status=approval.status,
        note=approval.note,
        expires_at=approval.expires_at,
        created_at=approval.created_at,
    )


async def _enqueue(queue: ArqRedis | None, run_id: uuid.UUID) -> None:
    if queue is None:
        raise AppError("queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503)
    try:
        await queue.enqueue_job("run_workflow_job", str(run_id))
    except Exception:
        logger.exception("workflow enqueue failed run=%s", run_id)
        raise AppError(
            "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503
        ) from None


async def _find_by_key(
    session: AsyncSession, tenant_id: str, user_id: uuid.UUID, key: str
) -> WorkflowRun | None:
    return (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.triggered_by == user_id,
                WorkflowRun.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()


async def _key_owned_by_other(
    session: AsyncSession, tenant_id: str, key: str
) -> bool:
    return (
        await session.execute(
            select(WorkflowRun.id)
            .where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.idempotency_key == key,
            )
            .limit(1)
        )
    ).first() is not None


async def _ensure_active_limit(
    session: AsyncSession,
    tenant_id: str,
    user_id: uuid.UUID,
    settings: Settings,
) -> None:
    active = (
        await session.execute(
            select(func.count())
            .select_from(WorkflowRun)
            .where(
                WorkflowRun.tenant_id == tenant_id,
                WorkflowRun.triggered_by == user_id,
                WorkflowRun.status.in_(LIMITED_STATUSES),
            )
        )
    ).scalar_one()
    if active >= settings.workflow_max_active_runs_per_user:
        raise AppError(
            "workflow_run_limit",
            f"最多同时运行 {settings.workflow_max_active_runs_per_user} 个工作流",
            409,
        )


def _can_control(run: WorkflowRun, user: User) -> bool:
    if run.triggered_by == user.id:
        return True
    return run.trigger == "schedule" and user.role in ("owner", "admin")


async def _restore_pending(
    session: AsyncSession,
    approval: WorkflowApproval,
    run: WorkflowRun,
) -> None:
    reset_run = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status == "queued",
        )
        .values(
            status="waiting_approval",
            finished_at=None,
            error_code="",
            error_message="",
        )
        .execution_options(synchronize_session=False)
    )
    reset_approval = await session.execute(
        update(WorkflowApproval)
        .where(
            WorkflowApproval.id == approval.id,
            WorkflowApproval.tenant_id == approval.tenant_id,
            WorkflowApproval.status == "approved",
        )
        .values(status="pending", decided_by=None, decided_at=None, note="")
        .execution_options(synchronize_session=False)
    )
    if reset_run.rowcount and reset_approval.rowcount:
        await session.commit()
        return
    await session.rollback()
    logger.warning(
        "workflow approval compensation skipped run=%s approval=%s",
        run.id,
        approval.id,
    )


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=WorkflowRunOut,
    status_code=202,
)
async def start_run(
    workflow_id: str,
    payload: StartIn,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> WorkflowRunOut:
    key = idempotency_key.strip() if idempotency_key else None
    if idempotency_key is not None and (not key or len(key) > 64):
        raise AppError("workflow_invalid", "Idempotency-Key 不合法", 422)
    tenant_id = user.tenant_id
    user_id = user.id
    workflow = await _owned_workflow(session, workflow_id, user)
    version = await _version_row(session, workflow, payload.version)
    if key:
        existing = await _find_by_key(session, tenant_id, user_id, key)
        if existing is not None:
            return _run_out(existing, await _version_number(session, existing))

    issues = validate_inputs(version.graph, payload.inputs)
    if issues:
        raise AppError(
            "workflow_invalid",
            "运行入参不合法",
            422,
            detail={"issues": [item.as_dict() for item in issues]},
        )
    settings = get_settings()
    await _ensure_active_limit(session, tenant_id, user_id, settings)
    await ensure_daily_quota(session, tenant_id, settings)

    specs = {spec.name: spec for spec in get_tool_registry().all()}
    run = WorkflowRun(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger="manual",
        triggered_by=user_id,
        status="queued",
        inputs=payload.inputs,
        policy=snapshot_policy(
            await load_overrides(session, tenant_id), specs
        ),
        idempotency_key=key,
    )
    session.add(run)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = (
            await _find_by_key(session, tenant_id, user_id, key)
            if key
            else None
        )
        if existing is not None:
            return _run_out(existing, await _version_number(session, existing))
        if key and await _key_owned_by_other(session, tenant_id, key):
            raise AppError(
                "workflow_conflict", "幂等键已被占用", 409
            ) from None
        raise
    await session.refresh(run)

    try:
        await _enqueue(queue, run.id)
    except AppError:
        # 行保留为失败记录，但释放幂等键，重放会重新尝试而不是回放一个
        # 从未进队的运行。
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run.id, WorkflowRun.status == "queued")
            .values(
                status="failed",
                error_code="queue_unavailable",
                error_message=QUEUE_UNAVAILABLE_MESSAGE,
                idempotency_key=None,
                finished_at=_now(),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        raise

    await record_best_effort(
        session,
        tenant_id=tenant_id,
        action="workflow.run.create",
        actor=user,
        resource_type="workflow_run",
        resource_id=str(run.id),
        detail={"version": version.version},
    )
    return _run_out(run, version.version)


@router.get(
    "/workflows/{workflow_id}/runs",
    response_model=list[WorkflowRunOut],
)
async def list_runs(
    workflow_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[WorkflowRunOut]:
    workflow = await _owned_workflow(session, workflow_id, user)
    stmt = (
        select(WorkflowRun, WorkflowVersion.version)
        .join(
            WorkflowVersion,
            WorkflowVersion.id == WorkflowRun.workflow_version_id,
        )
        .where(
            WorkflowRun.workflow_id == workflow.id,
            WorkflowRun.tenant_id == user.tenant_id,
        )
    )
    if status:
        stmt = stmt.where(WorkflowRun.status == status)
    stmt = (
        stmt.order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).all()
    return [_run_out(run, version) for run, version in rows]


@router.get("/workflow-runs/{run_id}", response_model=WorkflowRunDetailOut)
async def get_run(
    run_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowRunDetailOut:
    run = await _owned_run(session, run_id, user)
    version = await _version_number(session, run)
    nodes = (
        (
            await session.execute(
                select(WorkflowNodeRun)
                .where(WorkflowNodeRun.run_id == run.id)
                .order_by(
                    WorkflowNodeRun.created_at,
                    WorkflowNodeRun.node_id,
                    WorkflowNodeRun.attempt,
                )
            )
        )
        .scalars()
        .all()
    )
    pending_workflow = (
        (
            await session.execute(
                select(WorkflowApproval)
                .where(
                    WorkflowApproval.run_id == run.id,
                    WorkflowApproval.status == "pending",
                )
                .order_by(WorkflowApproval.created_at, WorkflowApproval.id)
            )
        )
        .scalars()
        .all()
    )
    pending_agent = (
        await session.execute(
            select(AgentApproval, AgentRun.workflow_node_id)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .where(
                AgentRun.tenant_id == run.tenant_id,
                AgentRun.workflow_run_id == run.id,
                AgentApproval.status == "pending",
            )
            .order_by(AgentApproval.created_at, AgentApproval.id)
        )
    ).all()
    approvals = [
        _approval_out(
            row,
            run,
            source="workflow",
            node_id=row.node_id,
            risk=row.risk,
        )
        for row in pending_workflow
    ]
    approvals.extend(
        _approval_out(
            row,
            run,
            source="agent",
            node_id=node_id or "",
            risk=row.risk,
        )
        for row, node_id in pending_agent
    )
    return WorkflowRunDetailOut(
        run=_run_out(run, version),
        node_runs=[_node_out(row) for row in nodes],
        approvals=approvals,
        result=run.result or {},
    )


@router.post("/workflow-runs/{run_id}/cancel", response_model=WorkflowRunOut)
async def cancel_run(
    run_id: str,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> WorkflowRunOut:
    run = await _owned_run(session, run_id, user)
    if not _can_control(run, user):
        raise AppError("forbidden", "无权操作该运行", 403)
    if not await lifecycle.cancel_run(session, run, actor=str(user.id)):
        raise AppError(
            "workflow_run_not_cancellable", "运行已结束，无法取消", 409
        )
    await session.refresh(run)
    return _run_out(run, await _version_number(session, run))


@router.post("/workflow-runs/{run_id}/resume", response_model=WorkflowRunOut)
async def resume_run(
    run_id: str,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> WorkflowRunOut:
    run = await _owned_run(session, run_id, user)
    if not _can_control(run, user):
        raise AppError("forbidden", "无权操作该运行", 403)
    await lifecycle.resume_run(session, run, actor=str(user.id))
    try:
        await _enqueue(queue, run.id)
    except AppError:
        # 没进队就不算续跑成功：退回 failed，节点保持 interrupted，可再次续跑。
        # resume_count 也一并回退，否则每次队列故障都白耗一次续跑额度。
        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run.id, WorkflowRun.status == "queued")
            .values(
                status="failed",
                error_code="queue_unavailable",
                error_message=QUEUE_UNAVAILABLE_MESSAGE,
                finished_at=_now(),
                resume_count=WorkflowRun.resume_count - 1,
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        raise
    await session.refresh(run)
    return _run_out(run, await _version_number(session, run))


@router.get("/workflow-approvals", response_model=list[ApprovalOut])
async def list_approvals(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[ApprovalOut]:
    rows = (
        await session.execute(
            select(WorkflowApproval, WorkflowRun)
            .join(WorkflowRun, WorkflowRun.id == WorkflowApproval.run_id)
            .where(
                WorkflowApproval.tenant_id == user.tenant_id,
                WorkflowApproval.status == "pending",
                or_(
                    WorkflowRun.trigger != "manual",
                    WorkflowRun.triggered_by.is_(None),
                    WorkflowRun.triggered_by != user.id,
                ),
            )
            .order_by(WorkflowApproval.created_at, WorkflowApproval.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return [
        _approval_out(
            approval,
            run,
            source="workflow",
            node_id=approval.node_id,
            risk=approval.risk,
        )
        for approval, run in rows
    ]


@router.post(
    "/workflow-approvals/{approval_id}/decide",
    response_model=WorkflowRunOut,
)
async def decide(
    approval_id: str,
    payload: DecideIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> WorkflowRunOut:
    approval = await _owned_approval(session, approval_id, user)
    run = await session.get(WorkflowRun, approval.run_id)
    if run is None or run.tenant_id != user.tenant_id:
        raise AppError(
            "workflow_approval_not_pending", "审批与运行不匹配", 409
        )
    if run.trigger == "manual" and run.triggered_by == user.id:
        raise AppError("forbidden", "不能审批自己发起的运行", 403)

    state = await workflow_approvals.decide_approval(
        session,
        run=run,
        approval=approval,
        decision=payload.decision,
        actor_id=user.id,
        note=payload.note,
    )
    if state == "queued":
        try:
            await _enqueue(queue, run.id)
        except AppError:
            # 同意已落库但 worker 不可达：把审批和运行一起退回可再次决策的状态。
            await _restore_pending(session, approval, run)
            raise
    await record_best_effort(
        session,
        tenant_id=run.tenant_id,
        action=(
            "workflow.review.approve"
            if payload.decision == "approve"
            else "workflow.review.reject"
        ),
        actor=user,
        resource_type="workflow_run",
        resource_id=str(run.id),
        detail={"decision": payload.decision, "node": approval.node_id},
    )
    return _run_out(run, await _version_number(session, run))
