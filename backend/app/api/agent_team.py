import uuid
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.agent.policy import load_overrides, snapshot_policy
from app.agent.registry import get_tool_registry
from app.agent.roles import (
    ROLE_KEYS,
    ROLES,
    clamp_team_budget,
    effective_roles,
    validate_participants,
    validate_role_patch,
)
from app.agent.team import cancel_team_run
from app.api import agent as agent_api
from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentTeamEntry, AgentTeamRole
from app.models.user import User
from app.permissions import admin_access, editor_access, viewer_access
from app.queue import get_queue
from app.services.audit import record_best_effort
from app.services.live import snapshot_stream

router = APIRouter(prefix="/agent/team", tags=["agent-team"])


class RolePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    enabled: bool | None = None
    tools: list[str] | None = None
    budget: dict[str, Any] | None = None


class RolesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[RolePatch] = Field(default_factory=list)


class TeamRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal: str = Field(min_length=1, max_length=4000)
    roles: list[str]
    budget: dict[str, Any] = Field(default_factory=dict)


def _role_out(role) -> dict:
    spec = ROLES[role.key]
    return {
        "role": role.key, "label": role.label, "enabled": role.enabled,
        "tools": list(role.tools), "default_tools": list(spec.tools),
        "budget": dict(role.budget), "limits": dict(spec.budget),
    }


@router.get("/roles")
async def get_roles(
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
):
    roles = await effective_roles(session, user.tenant_id)
    return [_role_out(roles[key]) for key in ROLE_KEYS]


@router.put("/roles")
async def put_roles(
    payload: RolesIn,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
):
    for patch in payload.roles:
        validate_role_patch(patch.role, enabled=patch.enabled,
                            tools=patch.tools, budget=patch.budget)
    for patch in payload.roles:
        spec = ROLES[patch.role]
        row = (
            await session.execute(
                select(AgentTeamRole).where(
                    AgentTeamRole.tenant_id == user.tenant_id,
                    AgentTeamRole.role == patch.role,
                )
            )
        ).scalar_one_or_none()
        enabled = spec.required if patch.enabled is None else patch.enabled
        if spec.required:
            enabled = True
        tools = list(patch.tools) if patch.tools is not None else list(spec.tools)
        budget = dict(spec.budget)
        if patch.budget is not None:
            budget.update(patch.budget)
        is_default = (
            enabled and tools == list(spec.tools) and budget == spec.budget
        )
        if is_default:
            if row is not None:
                await session.delete(row)
            continue
        if row is None:
            row = AgentTeamRole(tenant_id=user.tenant_id, role=patch.role)
            session.add(row)
        row.enabled = enabled
        row.tools = tools
        row.budget = budget
    await session.commit()
    await record_best_effort(
        session, tenant_id=user.tenant_id, action="agent.team.roles.update",
        actor=user, resource_type="agent_team_roles", resource_id=user.tenant_id,
    )
    roles = await effective_roles(session, user.tenant_id)
    return [_role_out(roles[key]) for key in ROLE_KEYS]


@router.post("/runs", status_code=202)
async def create_team_run(
    payload: TeamRunIn,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
):
    settings = get_settings()
    goal = payload.goal.strip()
    if not goal:
        raise AppError("invalid_payload", "目标不能为空", 422)
    effective = await effective_roles(session, user.tenant_id)
    participants = validate_participants(payload.roles, effective)
    budget = clamp_team_budget(payload.budget, settings)
    await agent_api._ensure_limits(session, user, settings)
    specs = {spec.name: spec for spec in get_tool_registry().all()}
    overrides = await load_overrides(session, user.tenant_id)
    run = AgentRun(
        tenant_id=user.tenant_id, user_id=user.id, goal=goal,
        mode="team", status="queued", plan={"roles": list(participants)},
        budget=budget, policy=snapshot_policy(overrides, specs),
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    try:
        await agent_api._enqueue(queue, run)
    except AppError:
        from app.agent.runtime import fail_run
        await fail_run(session, run, "queue_unavailable",
                       agent_api.QUEUE_UNAVAILABLE_MESSAGE)
        raise
    await record_best_effort(
        session, tenant_id=user.tenant_id, action="agent.team.create",
        actor=user, resource_type="agent_run", resource_id=str(run.id),
        detail={"roles": list(participants), "budget": budget},
    )
    return agent_api._run_out(run)


@router.get("/runs/{run_id}")
async def get_team_run(
    run_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
):
    return await _team_detail(session, run_id, user)


@router.get("/runs/{run_id}/stream")
async def stream_team_run(
    run_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> EventSourceResponse:
    run = await agent_api._owned_run(session, run_id, user)
    if run.mode != "team":
        raise AppError("not_found", "资源不存在", 404)

    async def load(read: AsyncSession) -> dict[str, Any]:
        return await _team_detail(read, run_id, user)

    return EventSourceResponse(
        snapshot_stream(load, status_of=lambda payload: payload["run"]["status"])
    )


async def _team_detail(
    session: AsyncSession, run_id: str, user: User
) -> dict[str, Any]:
    run = await agent_api._owned_run(session, run_id, user)
    if run.mode != "team":
        raise AppError("not_found", "资源不存在", 404)
    children = (
        await session.execute(
            select(AgentRun).where(
                AgentRun.parent_run_id == run.id,
                AgentRun.tenant_id == user.tenant_id,
                AgentRun.mode == "role",
            )
            .order_by(AgentRun.created_at, AgentRun.id)
        )
    ).scalars().all()
    approvals: dict[uuid.UUID, AgentApproval] = {}
    if children:
        pending = (
            await session.execute(
                select(AgentApproval)
                .where(
                    AgentApproval.run_id.in_([child.id for child in children]),
                    AgentApproval.status == "pending",
                )
                .order_by(AgentApproval.created_at, AgentApproval.id)
            )
        ).scalars().all()
        for approval in pending:
            approvals.setdefault(approval.run_id, approval)
    entries = (
        await session.execute(
            select(AgentTeamEntry).where(
                AgentTeamEntry.run_id == run.id,
                AgentTeamEntry.tenant_id == user.tenant_id,
            )
            .order_by(AgentTeamEntry.seq)
        )
    ).scalars().all()
    return {
        "run": agent_api._run_out(run).model_dump(),
        "children": [
            {
                "id": str(child.id), "role": child.role, "status": child.status,
                "attempts": child.attempts, "usage": child.usage or {},
                "result_text": child.result_text[:500],
                "error_code": child.error_code,
                "created_at": child.created_at, "finished_at": child.finished_at,
                "pending_approval": (
                    agent_api._approval_out(approvals[child.id]).model_dump()
                    if child.id in approvals else None
                ),
            }
            for child in children
        ],
        "entries": [
            {"seq": e.seq, "kind": e.kind, "role": e.role,
             "content": e.content, "created_at": e.created_at}
            for e in entries
        ],
        "artifacts": (run.result or {}).get("artifacts")
        or (run.team_state or {}).get("artifact_index") or [],
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_team(
    run_id: str,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
):
    run = await agent_api._owned_run(session, run_id, user)
    if run.mode != "team":
        raise AppError("not_found", "资源不存在", 404)
    if user.role not in {"admin", "owner"} and user.id != run.user_id:
        raise AppError("forbidden", "只能取消自己发起的团队运行", 403)
    if not await cancel_team_run(session, run):
        raise AppError("run_not_cancellable", "运行已结束，无法取消", 409)
    await session.refresh(run)
    return agent_api._run_out(run)