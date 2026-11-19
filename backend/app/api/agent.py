import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.policy import (
    clamped_overrides,
    default_action,
    load_overrides,
    save_overrides,
    snapshot_policy,
    validate_overrides,
)
from app.agent.registry import get_tool_registry
from app.agent.runtime import (
    APPROVAL_REJECTED,
    decide_approval,
    fail_run,
)
from app.agent.team import fail_team_run
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.profile import Profile
from app.models.project import Project
from app.models.user import User
from app.models.workflow import WorkflowRun
from app.permissions import admin_access, editor_access, viewer_access
from app.queue import get_queue
from app.services.audit import record_best_effort
from app.workflow import approvals as workflow_approvals
from app.workflow import lifecycle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

ACTIVE_STATUSES = ("queued", "running", "waiting_approval")
# Per-user create cap: a run parked on a human does not consume worker capacity,
# so only queued/running count.
LIMITED_STATUSES = ("queued", "running")
RETRYABLE_STATUSES = ("failed", "cancelled")
ACTIVE_RUNS_PER_USER = 2
BUDGET_KEYS = ("max_steps", "max_tokens", "max_tool_calls", "max_seconds")
QUEUE_UNAVAILABLE_MESSAGE = "队列不可用，请稍后重试"
IDEMPOTENCY_CONFLICT_MESSAGE = "该幂等键不可用，请更换后重试"


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, Any] = {}


class PolicyOut(BaseModel):
    overrides: dict[str, str]
    defaults: dict[str, str]


class ToolOut(BaseModel):
    name: str
    description: str
    parameters: dict
    risk: str


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    profile_id: str | None = None
    project_id: str | None = None
    # Kept loose on purpose: every invalid dimension (unknown key, wrong type,
    # non-positive value) must surface as ``agent_budget_invalid`` from
    # ``_clamp_budget`` rather than as a Pydantic ``validation_error``.
    budget: dict[str, Any] = Field(default_factory=dict)


class StepOut(BaseModel):
    step_index: int
    kind: str
    tool_name: str
    status: str
    input: dict
    output: dict
    error: str
    duration_ms: int
    started_at: datetime | None
    finished_at: datetime | None


class ApprovalOut(BaseModel):
    id: str
    run_id: str
    step_id: str
    tool_name: str
    risk: str
    payload: dict
    status: str
    note: str
    expires_at: datetime | None
    decided_at: datetime | None


class RunOut(BaseModel):
    id: str
    user_id: str
    goal: str
    mode: str
    status: str
    plan: dict
    result_text: str
    result: dict
    error_code: str
    error_message: str
    budget: dict
    usage: dict
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    pending_approval: ApprovalOut | None = None


class RunDetailOut(RunOut):
    steps: list[StepOut]


class DecideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str = Field(default="", max_length=2000)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _approval_out(approval: AgentApproval) -> ApprovalOut:
    return ApprovalOut(
        id=str(approval.id),
        run_id=str(approval.run_id),
        step_id=str(approval.step_id),
        tool_name=approval.tool_name,
        risk=approval.risk,
        payload=approval.payload or {},
        status=approval.status,
        note=approval.note,
        expires_at=_utc(approval.expires_at),
        decided_at=_utc(approval.decided_at),
    )


def _step_out(step: AgentStep) -> StepOut:
    return StepOut(
        step_index=step.step_index,
        kind=step.kind,
        tool_name=step.tool_name,
        status=step.status,
        input=step.input or {},
        output=step.output or {},
        error=step.error,
        duration_ms=step.duration_ms,
        started_at=_utc(step.started_at),
        finished_at=_utc(step.finished_at),
    )


def _run_out(run: AgentRun, approval: AgentApproval | None = None) -> RunOut:
    return RunOut(
        id=str(run.id),
        user_id=str(run.user_id),
        goal=run.goal,
        mode=run.mode,
        status=run.status,
        plan=run.plan or {},
        result_text=run.result_text,
        result=run.result or {},
        error_code=run.error_code,
        error_message=run.error_message,
        budget=run.budget or {},
        usage=run.usage or {},
        created_at=_utc(run.created_at),
        started_at=_utc(run.started_at),
        finished_at=_utc(run.finished_at),
        pending_approval=(
            _approval_out(approval) if approval is not None else None
        ),
    )


def _ceilings(settings: Settings) -> dict[str, int]:
    """Upper bound per budget dimension, in ``BUDGET_KEYS`` order.

    ``max_steps`` is the one ceiling an operator can move: it bounds a single
    run's tool loop and comes from ``AGENT_MAX_STEPS_CEILING``. The other three
    are plan constants, so no environment change can widen them.
    """
    return {
        "max_steps": settings.agent_max_steps_ceiling,
        "max_tokens": 200_000,
        "max_tool_calls": 40,
        "max_seconds": 900,
    }


def _budget_defaults(settings: Settings) -> dict[str, int]:
    """Defaults for omitted budget dimensions, straight from configuration.

    The runtime reads the same settings when a run's budget is missing a key,
    so a run created here always carries the identical, complete budget. A
    configured default above its plan ceiling is clamped: the server never
    hands out a budget larger than the plan allows.
    """
    configured = {
        "max_steps": settings.agent_max_steps_default,
        "max_tokens": settings.agent_token_budget_default,
        "max_tool_calls": settings.agent_max_tool_calls,
        "max_seconds": settings.agent_max_seconds,
    }
    ceilings = _ceilings(settings)
    return {key: min(configured[key], ceilings[key]) for key in BUDGET_KEYS}


def _clamp_budget(
    raw: dict[str, Any] | None, settings: Settings
) -> dict[str, int]:
    """Validate explicit values, clamp them to ceilings and fill omissions.

    Unknown dimensions are rejected (a typo must not silently run with the
    defaults), explicit non-positive or non-integer values are rejected rather
    than silently replaced, and omitted dimensions use the operator's settings
    defaults. The result always lists all four keys in a stable order.
    """
    cleaned: dict[str, int] = {}
    ceilings = _ceilings(settings)
    for key, value in (raw or {}).items():
        ceiling = ceilings.get(key)
        if ceiling is None:
            raise AppError(
                "agent_budget_invalid", f"未知预算维度：{key}", 422
            )
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AppError(
                "agent_budget_invalid", f"预算 {key} 必须是正整数", 422
            )
        cleaned[key] = min(value, ceiling)
    defaults = _budget_defaults(settings)
    for key in BUDGET_KEYS:
        cleaned.setdefault(key, defaults[key])
    return {key: cleaned[key] for key in BUDGET_KEYS}


def _stored_budget(raw: dict | None) -> dict[str, int]:
    """Re-read a persisted budget leniently before clamping it again.

    Retrying an old run must not fail on a row written before a dimension
    changed: unknown keys and unusable values are dropped, and the missing
    dimensions fall back to the settings defaults like any other omission.
    """
    values = raw if isinstance(raw, dict) else {}
    return {
        key: value
        for key, value in values.items()
        if key in BUDGET_KEYS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    }


async def _ensure_limits(
    session: AsyncSession, user: User, settings: Settings
) -> None:
    active = (
        await session.execute(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.tenant_id == user.tenant_id,
                AgentRun.user_id == user.id,
                AgentRun.status.in_(LIMITED_STATUSES),
            )
        )
    ).scalar_one()
    if active >= ACTIVE_RUNS_PER_USER:
        raise AppError(
            "agent_run_limit",
            f"最多同时运行 {ACTIVE_RUNS_PER_USER} 个任务",
            409,
        )

    start = datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    rows = (
        await session.execute(
            select(AgentRun.usage).where(
                AgentRun.tenant_id == user.tenant_id,
                AgentRun.created_at >= start,
            )
        )
    ).scalars().all()
    used = 0
    for usage in rows:
        if isinstance(usage, dict):
            used += int(usage.get("prompt_tokens") or 0)
            used += int(usage.get("completion_tokens") or 0)
    # spec §15: a zero quota disables the daily token limit.
    quota = settings.agent_daily_token_quota
    if quota > 0 and used >= quota:
        raise AppError(
            "agent_daily_quota_exceeded", "今日 token 配额已用尽", 429
        )


async def _owned_resource(
    session: AsyncSession, model, raw_id: str, user: User
):
    try:
        object_id = uuid.UUID(raw_id)
    except ValueError:
        raise AppError("not_found", "资源不存在", 404) from None
    row = await session.get(model, object_id)
    if row is None or row.tenant_id != user.tenant_id:
        raise AppError("not_found", "资源不存在", 404)
    return row


async def _owned_run(
    session: AsyncSession, run_id: str, user: User
) -> AgentRun:
    return await _owned_resource(session, AgentRun, run_id, user)


async def _owned_approval(
    session: AsyncSession, approval_id: str, user: User
) -> AgentApproval:
    return await _owned_resource(
        session, AgentApproval, approval_id, user
    )


async def _context_lines(
    session: AsyncSession, user: User, payload: RunIn
) -> list[str]:
    lines: list[str] = []
    if payload.profile_id:
        profile = await _owned_resource(
            session, Profile, payload.profile_id, user
        )
        lines.append(f"当前画像：{profile.name}（id={profile.id}）")
    if payload.project_id:
        project = await _owned_resource(
            session, Project, payload.project_id, user
        )
        lines.append(f"当前项目：{project.title}（id={project.id}）")
    return lines


async def _find_by_key(
    session: AsyncSession, tenant_id: str, user_id: uuid.UUID, key: str
) -> AgentRun | None:
    """Look up the caller's own run for an idempotency key.

    The DB uniqueness is tenant-wide ``(tenant_id, idempotency_key)``, so the
    lookup is scoped to the caller: a key owned by somebody else must never
    hand that user's run to this caller.
    """
    return (
        await session.execute(
            select(AgentRun).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.user_id == user_id,
                AgentRun.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()


async def _key_owned_by_other(
    session: AsyncSession, tenant_id: str, key: str
) -> bool:
    """Whether *any* row in the tenant holds this key (existence only)."""
    return (
        await session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.idempotency_key == key,
            )
            .limit(1)
        )
    ).first() is not None


async def _new_run(
    session: AsyncSession,
    user: User,
    *,
    goal: str,
    budget: dict[str, int],
    idempotency_key: str | None = None,
) -> AgentRun:
    specs = {spec.name: spec for spec in get_tool_registry().all()}
    overrides = await load_overrides(session, user.tenant_id)
    run = AgentRun(
        tenant_id=user.tenant_id,
        user_id=user.id,
        goal=goal,
        budget=budget,
        policy=snapshot_policy(overrides, specs),
        idempotency_key=idempotency_key,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def _enqueue(queue: ArqRedis | None, run: AgentRun) -> None:
    """Hand the run to the worker, raising ``queue_unavailable`` on failure.

    The caller owns the failure compensation: a create keeps the row as a
    failure artifact but releases the idempotency key, while an approval
    decision rolls the consent back so it can be decided again.
    """
    if queue is None:
        raise AppError(
            "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503
        )
    try:
        await queue.enqueue_job("run_agent_job", str(run.id))
    except Exception:
        logger.exception("agent enqueue failed run=%s", run.id)
        raise AppError(
            "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503
        ) from None


async def _enqueue_workflow(queue: ArqRedis | None, run_id: uuid.UUID) -> None:
    if queue is None:
        raise AppError("queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503)
    try:
        await queue.enqueue_job("run_workflow_job", str(run_id))
    except Exception:
        logger.exception("workflow enqueue failed run=%s", run_id)
        raise AppError(
            "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE, 503
        ) from None


async def _restore_child_wait(
    session: AsyncSession,
    parent: WorkflowRun,
    child: AgentRun,
    approval: AgentApproval,
    *,
    previous_paused_ms: int,
    paused_at: datetime,
) -> None:
    """Undo a woke parent whose worker never saw the job.

    All three rows are restored in one transaction and only while each still
    holds the value this request wrote, so a concurrent cancel or a sibling
    decision is never resurrected. The approval wait credited to ``paused_ms``
    is removed by writing the pre-credit value back, and ``heartbeat_at`` goes
    back to the moment the parent paused, so the retry accrues the same wait
    instead of billing it to the run's wall-clock budget.

    The row identities are captured up front: the losing CAS rolls the session
    back, which expires every ORM instance, so ``child.id`` and friends are no
    longer readable when the warning is logged.
    """
    parent_id = parent.id
    parent_tenant = parent.tenant_id
    child_id = child.id
    child_tenant = child.tenant_id
    approval_id = approval.id
    approval_tenant = approval.tenant_id
    usage = dict(parent.usage or {})
    usage["paused_ms"] = previous_paused_ms
    reset_parent = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == parent_id,
            WorkflowRun.tenant_id == parent_tenant,
            WorkflowRun.status == "queued",
        )
        .values(
            status="waiting_approval",
            finished_at=None,
            error_code="",
            error_message="",
            usage=usage,
            heartbeat_at=paused_at,
        )
        .execution_options(synchronize_session=False)
    )
    reset_child = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == child_id,
            AgentRun.tenant_id == child_tenant,
            AgentRun.status == "queued",
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
        update(AgentApproval)
        .where(
            AgentApproval.id == approval_id,
            AgentApproval.tenant_id == approval_tenant,
            AgentApproval.status == "approved",
        )
        .values(status="pending", decided_by=None, decided_at=None, note="")
        .execution_options(synchronize_session=False)
    )
    if (
        reset_parent.rowcount
        and reset_child.rowcount
        and reset_approval.rowcount
    ):
        await session.commit()
        return
    await session.rollback()
    logger.warning(
        "agent approval compensation skipped (workflow moved on) run=%s approval=%s",
        child_id,
        approval_id,
    )


async def _wake_parent_workflow(
    session: AsyncSession,
    queue: ArqRedis | None,
    child: AgentRun,
    approval: AgentApproval,
) -> None:
    """Wake the parent workflow that owns ``child`` -- never the child itself.

    The parent resumes its children inline, so enqueueing the child as well
    would let two executors run the same child. While a sibling approval is
    still pending the parent keeps waiting; whoever decides the last one wakes
    it, and the parent then resumes every approved child.

    The wait is credited to the parent's ``paused_ms`` (spec §4.7/§5.3) so
    resuming does not bill the approval wait to the run's wall-clock budget.

    The parent is read with a row lock so this check-then-CAS serializes with
    ``workflow.approvals.decide_approval`` on the same run (parent run ->
    approval rows, the same order both writers use). Without it, a workflow
    review decision and a child approval decision can each see the other as
    pending and neither wakes the parent. When a sibling approval is still
    pending the lock is released before returning: the child decision was
    already committed by ``agent.runtime.decide_approval``, and the caller
    reloads the child afterwards.
    """
    parent = (
        await session.execute(
            select(WorkflowRun)
            .where(
                WorkflowRun.id == child.workflow_run_id,
                WorkflowRun.tenant_id == child.tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if parent is None:
        raise AppError("not_found", "运行不存在", 404)
    if await workflow_approvals.has_pending(
        session, tenant_id=parent.tenant_id, run_id=parent.id
    ):
        await session.rollback()
        return
    moment = datetime.now(UTC)
    paused_at = _utc(parent.heartbeat_at) or moment
    # The CAS below runs with synchronize_session=False, so the ORM instance
    # keeps the pre-credit ``paused_ms``. Capture it before adding this wait:
    # a compensation must write that value back, not subtract from a stale one
    # it never saw added.
    previous_paused_ms = int((parent.usage or {}).get("paused_ms") or 0)
    waited_ms = int(
        max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000
    )
    usage = dict(parent.usage or {})
    usage["paused_ms"] = previous_paused_ms + int(waited_ms)
    resumed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == parent.id,
            WorkflowRun.tenant_id == parent.tenant_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(
            status="queued",
            usage=usage,
            heartbeat_at=moment,
            finished_at=None,
            error_code="",
            error_message="",
        )
        .execution_options(synchronize_session=False)
    )
    if resumed.rowcount == 0:
        # 父运行已被取消、已被另一个审批唤醒或已终态：它的写入者负责子运行。
        await session.rollback()
        return
    await session.commit()
    try:
        await _enqueue_workflow(queue, parent.id)
    except AppError:
        await _restore_child_wait(
            session,
            parent,
            child,
            approval,
            previous_paused_ms=previous_paused_ms,
            paused_at=paused_at,
        )
        raise


async def _team_has_pending(
    session: AsyncSession, parent_id: uuid.UUID, tenant_id: str
) -> bool:
    """Whether any child of the team still has a pending approval."""
    row = (
        await session.execute(
            select(AgentApproval.id)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .where(
                AgentRun.parent_run_id == parent_id,
                AgentRun.tenant_id == tenant_id,
                AgentApproval.status == "pending",
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def _restore_team_wait(
    session: AsyncSession,
    parent: AgentRun,
    child: AgentRun,
    approval: AgentApproval,
    *,
    previous_paused_ms: int,
    paused_at: datetime,
) -> None:
    """Undo a woke parent team whose worker never saw the job.

    Same contract as ``_restore_child_wait``: all three rows move together and
    only while each still holds the value this request wrote, so a concurrent
    cancel or a sibling decision is never resurrected. The row identities are
    captured up front because a losing CAS rolls the session back and expires
    every ORM instance.
    """
    parent_id, child_id, approval_id = parent.id, child.id, approval.id
    tenant = parent.tenant_id
    usage = dict(parent.usage or {})
    usage["paused_ms"] = previous_paused_ms
    reset_parent = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == parent_id,
            AgentRun.tenant_id == tenant,
            AgentRun.status == "queued",
        )
        .values(
            status="waiting_approval",
            usage=usage,
            heartbeat_at=paused_at,
            finished_at=None,
            error_code="",
            error_message="",
        )
        .execution_options(synchronize_session=False)
    )
    reset_child = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == child_id,
            AgentRun.tenant_id == tenant,
            AgentRun.status == "queued",
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
        update(AgentApproval)
        .where(
            AgentApproval.id == approval_id,
            AgentApproval.tenant_id == tenant,
            AgentApproval.status == "approved",
        )
        .values(status="pending", decided_by=None, decided_at=None, note="")
        .execution_options(synchronize_session=False)
    )
    if reset_parent.rowcount and reset_child.rowcount and reset_approval.rowcount:
        await session.commit()
        return
    await session.rollback()
    logger.warning(
        "team approval compensation skipped run=%s approval=%s",
        child_id,
        approval_id,
    )


async def _wake_parent_team(
    session: AsyncSession,
    queue: ArqRedis | None,
    child: AgentRun,
    approval: AgentApproval,
) -> None:
    """Wake the parent team that owns ``child`` -- never the child itself.

    The team orchestrator resumes its children inline (``_settle_child``), so
    enqueueing the child as well would let two executors run the same child.
    While a sibling approval is still pending the parent keeps waiting;
    whoever decides the last one wakes it.

    The wait is credited to the parent's ``paused_ms`` so resuming does not
    bill the approval wait to the team's wall-clock budget. The parent is read
    with a row lock so this check-then-CAS serializes with sibling decisions
    and with cancel/fail writers on the same parent row.
    """
    parent = (
        await session.execute(
            select(AgentRun)
            .where(
                AgentRun.id == child.parent_run_id,
                AgentRun.tenant_id == child.tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if parent is None:
        raise AppError("not_found", "运行不存在", 404)
    if parent.status != "waiting_approval":
        await session.rollback()
        return
    if await _team_has_pending(session, parent.id, parent.tenant_id):
        await session.rollback()
        return
    moment = datetime.now(UTC)
    paused_at = _utc(parent.heartbeat_at) or moment
    # Capture the pre-credit value: the CAS below runs with
    # synchronize_session=False, so a compensation must write this back rather
    # than subtract from a stale total it never saw added.
    previous_paused_ms = int((parent.usage or {}).get("paused_ms") or 0)
    waited_ms = int(
        max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000
    )
    usage = dict(parent.usage or {})
    usage["paused_ms"] = previous_paused_ms + waited_ms
    resumed = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == parent.id,
            AgentRun.tenant_id == parent.tenant_id,
            AgentRun.status == "waiting_approval",
        )
        .values(
            status="queued",
            usage=usage,
            heartbeat_at=moment,
            finished_at=None,
            error_code="",
            error_message="",
        )
        .execution_options(synchronize_session=False)
    )
    if resumed.rowcount == 0:
        # 父团队已被取消、已被另一个审批唤醒或已终态：它的写入者负责子运行。
        await session.rollback()
        return
    await session.commit()
    try:
        await _enqueue(queue, parent)
    except AppError:
        await _restore_team_wait(
            session,
            parent,
            child,
            approval,
            previous_paused_ms=previous_paused_ms,
            paused_at=paused_at,
        )
        raise


async def _compensate_approval(
    session: AsyncSession, approval: AgentApproval, run: AgentRun
) -> None:
    """Undo an approval whose enqueue failed, in one transaction.

    The decision commit landed ``approval=approved`` and ``run=queued`` before
    the worker was reachable. Leaving it there would keep an "approved" consent
    for a call that never ran while making a re-decide impossible (409), so the
    pair is rolled back to ``pending`` / ``waiting_approval`` -- but only while
    each row still holds the value this request wrote, so a concurrent cancel or
    expiry is never resurrected. Both rows move together or neither does.
    """
    run_id, approval_id = run.id, approval.id
    reset_run = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.tenant_id == run.tenant_id,
            AgentRun.status == "queued",
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
        update(AgentApproval)
        .where(
            AgentApproval.id == approval_id,
            AgentApproval.tenant_id == approval.tenant_id,
            AgentApproval.status == "approved",
        )
        .values(status="pending", decided_by=None, decided_at=None, note="")
        .execution_options(synchronize_session=False)
    )
    if reset_run.rowcount and reset_approval.rowcount:
        await session.commit()
        return
    await session.rollback()
    logger.warning(
        "agent approval compensation skipped (state moved on) run=%s approval=%s",
        run_id,
        approval_id,
    )


async def _expire_source_approvals(
    session: AsyncSession, source: AgentRun
) -> None:
    """Close approvals a retried run can never resume.

    A retried source is terminal, so any approval still pending on it would be
    a decidable consent for a run nobody will continue. It is expired before the
    retry run is created; the retry itself snapshots policy afresh.
    """
    await session.execute(
        update(AgentApproval)
        .where(
            AgentApproval.tenant_id == source.tenant_id,
            AgentApproval.run_id == source.id,
            AgentApproval.status == "pending",
        )
        .values(
            status="expired", decided_at=datetime.now(UTC), note="运行已重试"
        )
        .execution_options(synchronize_session=False)
    )
    await session.commit()


@router.get("/tools", response_model=list[ToolOut])
async def list_tools(
    user: User = Depends(editor_access),  # noqa: B008
) -> list[ToolOut]:
    return [
        ToolOut(
            name=spec.name,
            description=spec.description,
            parameters=spec.input_schema,
            risk=spec.risk,
        )
        for spec in get_tool_registry().all()
    ]


@router.get("/policy", response_model=PolicyOut)
async def get_policy(
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PolicyOut:
    registry = get_tool_registry()
    specs = {spec.name: spec for spec in registry.all()}
    overrides = clamped_overrides(
        await load_overrides(session, user.tenant_id), specs
    )
    defaults = {
        f"tool:{name}": default_action(spec.risk) for name, spec in specs.items()
    }
    return PolicyOut(overrides=overrides, defaults=defaults)


@router.put("/policy", response_model=PolicyOut)
async def put_policy(
    payload: PolicyIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PolicyOut:
    registry = get_tool_registry()
    specs = {spec.name: spec for spec in registry.all()}
    overrides = validate_overrides(payload.overrides, specs)
    saved = await save_overrides(session, user.tenant_id, overrides)
    defaults = {
        f"tool:{name}": default_action(spec.risk) for name, spec in specs.items()
    }
    return PolicyOut(overrides=saved, defaults=defaults)


@router.post("/runs", response_model=RunOut, status_code=202)
async def create_run(
    payload: RunIn,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> RunOut:
    key = idempotency_key.strip() if idempotency_key else None
    if idempotency_key is not None and (not key or len(key) > 64):
        raise AppError("invalid_payload", "Idempotency-Key 不合法", 422)
    # Read once: a rollback (the insert race below) expires the ORM user, and
    # touching its attributes afterwards would attempt sync IO.
    tenant_id = user.tenant_id
    user_id = user.id
    if key:
        existing = await _find_by_key(
            session, tenant_id, user_id, key
        )
        if existing is not None:
            return _run_out(existing)

    settings = get_settings()
    await _ensure_limits(session, user, settings)
    goal = payload.goal.strip()
    if not goal:
        raise AppError("invalid_payload", "目标不能为空", 422)
    lines = await _context_lines(session, user, payload)
    if lines:
        goal = goal + "\n\n" + "\n".join(lines)
    budget = _clamp_budget(payload.budget, settings)
    try:
        run = await _new_run(
            session,
            user,
            goal=goal,
            budget=budget,
            idempotency_key=key,
        )
    except IntegrityError:
        # A concurrent request with the same key won the insert race. Only the
        # caller's own run may be replayed; a tenant-wide key collision with
        # somebody else is reported generically, without naming their run.
        await session.rollback()
        existing = (
            await _find_by_key(session, tenant_id, user_id, key)
            if key
            else None
        )
        if existing is not None:
            return _run_out(existing)
        if key and await _key_owned_by_other(session, tenant_id, key):
            raise AppError(
                "agent_idempotency_conflict",
                IDEMPOTENCY_CONFLICT_MESSAGE,
                409,
            ) from None
        raise

    try:
        await _enqueue(queue, run)
    except AppError:
        # The row stays as a failure artifact, but the key is released: a
        # replay must make a fresh attempt instead of replaying a run that
        # never reached the queue.
        run.idempotency_key = None
        await fail_run(
            session, run, "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE
        )
        raise
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="agent.run.create",
        actor=user,
        resource_type="agent_run",
        resource_id=str(run.id),
        detail={"budget": budget},
    )
    return _run_out(run)


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[RunOut]:
    stmt = select(AgentRun).where(AgentRun.tenant_id == user.tenant_id)
    if status:
        stmt = stmt.where(AgentRun.status == status)
    # ``created_at`` is second-precision on some backends; ``id`` keeps the
    # order stable (and pagination non-overlapping) for rows created together.
    stmt = (
        stmt.order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    approvals: dict[uuid.UUID, AgentApproval] = {}
    if rows:
        pending = (
            await session.execute(
                select(AgentApproval)
                .where(
                    AgentApproval.run_id.in_(
                        [run.id for run in rows]
                    ),
                    AgentApproval.status == "pending",
                )
                .order_by(AgentApproval.created_at, AgentApproval.id)
            )
        ).scalars().all()
        for approval in pending:
            # Earliest pending approval wins when a run somehow has several.
            approvals.setdefault(approval.run_id, approval)
    return [_run_out(run, approvals.get(run.id)) for run in rows]


@router.get("/runs/{run_id}", response_model=RunDetailOut)
async def get_run(
    run_id: str,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RunDetailOut:
    run = await _owned_run(session, run_id, user)
    steps = (
        await session.execute(
            select(AgentStep)
            .where(AgentStep.run_id == run.id)
            .order_by(AgentStep.step_index)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    approval = (
        await session.execute(
            select(AgentApproval).where(
                AgentApproval.run_id == run.id,
                AgentApproval.status == "pending",
            )
        )
    ).scalars().first()
    base = _run_out(run, approval)
    return RunDetailOut(
        **base.model_dump(),
        steps=[_step_out(step) for step in steps],
    )


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(
    run_id: str,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RunOut:
    run = await _owned_run(session, run_id, user)
    if user.id != run.user_id:
        raise AppError("forbidden", "只能取消自己发起的运行", 403)

    now = datetime.now(UTC)
    # A conditional update instead of read-then-write: of two concurrent
    # cancels (or a cancel racing the worker's terminal transition) exactly
    # one can match an active status, so the run is cancelled at most once.
    cancelled = await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run.id,
            AgentRun.tenant_id == user.tenant_id,
            AgentRun.status.in_(ACTIVE_STATUSES),
        )
        .values(status="cancelled", finished_at=now)
        .execution_options(synchronize_session=False)
    )
    if cancelled.rowcount == 0:
        await session.rollback()
        raise AppError(
            "agent_run_not_cancellable", "运行已结束，无法取消", 409
        )
    # Close the run's pending approvals in the same transaction, otherwise a
    # decidable consent would outlive the run it belonged to.
    await session.execute(
        update(AgentApproval)
        .where(
            AgentApproval.tenant_id == user.tenant_id,
            AgentApproval.run_id == run.id,
            AgentApproval.status == "pending",
        )
        .values(status="expired", decided_at=now, note="运行已取消")
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    await session.refresh(run)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="agent.run.cancel",
        actor=user,
        resource_type="agent_run",
        resource_id=str(run.id),
    )
    return _run_out(run)


@router.post("/runs/{run_id}/retry", response_model=RunOut, status_code=202)
async def retry_run(
    run_id: str,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> RunOut:
    source = await _owned_run(session, run_id, user)
    if source.status not in RETRYABLE_STATUSES:
        raise AppError(
            "invalid_state", "只有失败或已取消的运行可以重试", 409
        )
    settings = get_settings()
    await _ensure_limits(session, user, settings)
    # A terminal source can never resume, so its pending approvals are closed
    # before the retry exists. The retry re-snapshots the tenant's *current*
    # policy (the brief does not require inheriting the source snapshot): a
    # tool tightened since the source ran must stay tightened.
    await _expire_source_approvals(session, source)
    run = await _new_run(
        session,
        user,
        goal=source.goal,
        budget=_clamp_budget(_stored_budget(source.budget), settings),
    )
    try:
        await _enqueue(queue, run)
    except AppError:
        # The retry row stays as a failure artifact and can itself be retried.
        await fail_run(
            session, run, "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE
        )
        raise
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="agent.run.retry",
        actor=user,
        resource_type="agent_run",
        resource_id=str(run.id),
        detail={"source": str(source.id)},
    )
    return _run_out(run)


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[ApprovalOut]:
    rows = (
        await session.execute(
            select(AgentApproval)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .outerjoin(
                WorkflowRun, WorkflowRun.id == AgentRun.workflow_run_id
            )
            .where(
                AgentApproval.tenant_id == user.tenant_id,
                AgentApproval.status == "pending",
                or_(
                    AgentRun.user_id != user.id,
                    WorkflowRun.trigger == "schedule",
                ),
            )
            .order_by(AgentApproval.created_at, AgentApproval.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return [_approval_out(row) for row in rows]


@router.post("/approvals/{approval_id}/decide", response_model=RunOut)
async def decide(
    approval_id: str,
    payload: DecideIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> RunOut:
    approval = await _owned_approval(session, approval_id, user)
    run = await decide_approval(
        session,
        approval,
        actor=user,
        decision=payload.decision,
        note=payload.note,
    )
    child_id = run.id
    if payload.decision == "approve":
        if run.workflow_run_id is not None:
            # 父工作流是子运行的唯一执行者：只唤醒父运行。
            await _wake_parent_workflow(session, queue, run, approval)
            # 唤醒在父运行 CAS 落败时会 rollback，expire 掉子运行实例；
            # 重新加载后才能安全序列化响应。
            run = await session.get(AgentRun, child_id)
            return _run_out(run)
        elif run.parent_run_id is not None:
            # 父团队是子运行的唯一执行者：只唤醒父团队，子运行由编排器续跑。
            await _wake_parent_team(session, queue, run, approval)
            run = await session.get(AgentRun, child_id)
            return _run_out(run)
        try:
            await _enqueue(queue, run)
        except AppError:
            # The consent was committed before the worker could be reached.
            # Roll it back so the call did not "run" and a re-decide is
            # possible; a 503 tells the reviewer to retry.
            await _compensate_approval(session, approval, run)
            raise
    elif run.workflow_run_id is not None:
        # 子运行已由第 1 层落 failed/approval_rejected；这里终结父运行，
        # 保证没有审批或子运行比父运行活得更久。
        await lifecycle.fail_waiting_run(
            session,
            run.workflow_run_id,
            error_code="approval_rejected",
            message=APPROVAL_REJECTED,
            actor=str(user.id),
        )
        # 父运行 CAS 落败时 fail_waiting_run 会 rollback 并 expire 子运行。
        run = await session.get(AgentRun, child_id)
    elif run.parent_run_id is not None:
        # 子运行已由第 1 层落 failed/approval_rejected；这里终结父团队并
        # 保留现场（cancel_children 收敛其余子运行与 pending 审批）。
        await fail_team_run(
            session,
            run.parent_run_id,
            error_code="approval_rejected",
            message=APPROVAL_REJECTED,
        )
        run = await session.get(AgentRun, child_id)
    return _run_out(run)
