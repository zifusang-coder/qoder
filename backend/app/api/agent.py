import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
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
from app.agent.runtime import decide_approval, fail_run
from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.profile import Profile
from app.models.project import Project
from app.models.user import User
from app.permissions import admin_access, editor_access, viewer_access
from app.queue import get_queue
from app.services.audit import record_best_effort

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

ACTIVE_STATUSES = ("queued", "running", "waiting_approval")
RETRYABLE_STATUSES = ("failed", "cancelled")
ACTIVE_RUNS_PER_USER = 2
BUDGET_CEILINGS = {
    "max_steps": 30,
    "max_tokens": 200_000,
    "max_tool_calls": 40,
    "max_seconds": 900,
}


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overrides: dict[str, Any] = {}


class PolicyOut(BaseModel):
    overrides: dict[str, str]
    defaults: dict[str, str]


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=4000)
    profile_id: str | None = None
    project_id: str | None = None
    budget: dict[str, int] = Field(default_factory=dict)


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


def _clamp_budget(
    raw: dict[str, int] | None, settings: Settings
) -> dict[str, int]:
    """Validate explicit values, clamp them to ceilings and fill omissions.

    Omitted dimensions use the ceiling as their default so a run always carries
    a complete budget. Explicit non-positive or non-integer values are rejected
    rather than silently replaced.
    """
    cleaned: dict[str, int] = {}
    for key, value in (raw or {}).items():
        ceiling = BUDGET_CEILINGS.get(key)
        if ceiling is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AppError(
                "agent_budget_invalid", f"预算 {key} 必须是正整数", 422
            )
        cleaned[key] = min(value, ceiling)
    for key, ceiling in BUDGET_CEILINGS.items():
        cleaned.setdefault(key, ceiling)
    return cleaned


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
                AgentRun.status.in_(ACTIVE_STATUSES),
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
    if used >= settings.agent_daily_token_quota:
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
    session: AsyncSession, tenant_id: str, key: str
) -> AgentRun | None:
    return (
        await session.execute(
            select(AgentRun).where(
                AgentRun.tenant_id == tenant_id,
                AgentRun.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()


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


async def _enqueue(
    session: AsyncSession, queue: ArqRedis | None, run: AgentRun
) -> None:
    if queue is None:
        await fail_run(
            session, run, "queue_unavailable", "队列不可用，请稍后重试"
        )
        raise AppError("queue_unavailable", "队列不可用，请稍后重试", 503)
    try:
        await queue.enqueue_job("run_agent_job", str(run.id))
    except Exception:
        logger.exception("agent enqueue failed run=%s", run.id)
        await fail_run(
            session, run, "queue_unavailable", "队列不可用，请稍后重试"
        )
        raise AppError(
            "queue_unavailable", "队列不可用，请稍后重试", 503
        ) from None


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
    if key:
        existing = await _find_by_key(session, user.tenant_id, key)
        if existing is not None:
            # The unique key is tenant-wide; never hand one user's run to
            # another user who happens to reuse the same key.
            if existing.user_id != user.id:
                raise AppError(
                    "agent_idempotency_conflict",
                    "该 Idempotency-Key 已被其他用户使用",
                    409,
                )
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
        # A concurrent request with the same key won the insert race.
        await session.rollback()
        existing = await _find_by_key(session, user.tenant_id, key or "")
        if existing is None:
            raise
        if existing.user_id != user.id:
            raise AppError(
                "agent_idempotency_conflict",
                "该 Idempotency-Key 已被其他用户使用",
                409,
            ) from None
        return _run_out(existing)

    await _enqueue(session, queue, run)
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
    stmt = (
        stmt.order_by(AgentRun.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [_run_out(run) for run in rows]


@router.get("/runs/{run_id}", response_model=RunDetailOut)
async def get_run(
    run_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RunDetailOut:
    run = await _owned_run(session, run_id, user)
    steps = (
        await session.execute(
            select(AgentStep)
            .where(AgentStep.run_id == run.id)
            .order_by(AgentStep.step_index)
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
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> RunOut:
    run = await _owned_run(session, run_id, user)
    if run.status not in ACTIVE_STATUSES:
        raise AppError(
            "agent_run_not_cancellable", "运行已结束，无法取消", 409
        )
    if user.id != run.user_id and user.role not in {"admin", "owner"}:
        raise AppError("forbidden", "只能取消自己发起的运行", 403)

    now = datetime.now(UTC)
    run.status = "cancelled"
    run.finished_at = now
    approvals = (
        await session.execute(
            select(AgentApproval).where(
                AgentApproval.run_id == run.id,
                AgentApproval.status == "pending",
            )
        )
    ).scalars().all()
    for approval in approvals:
        approval.status = "expired"
        approval.decided_at = now
        approval.note = "运行已取消"
    await session.commit()
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
    run = await _new_run(
        session,
        user,
        goal=source.goal,
        budget=_clamp_budget(source.budget, settings),
    )
    await _enqueue(session, queue, run)
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
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[ApprovalOut]:
    rows = (
        await session.execute(
            select(AgentApproval)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .where(
                AgentApproval.tenant_id == user.tenant_id,
                AgentApproval.status == "pending",
                AgentRun.user_id != user.id,
            )
            .order_by(AgentApproval.created_at)
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
    if payload.decision == "approve":
        await _enqueue(session, queue, run)
    return _run_out(run)
