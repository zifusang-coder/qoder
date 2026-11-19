"""团队编排器：Lead 结构化决策驱动，串行调用既有 run_agent。"""

import asyncio
import contextlib
import hashlib
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.blackboard import append_entry, recent_entries, render_entries
from app.agent.quota import ensure_daily_quota
from app.agent.roles import EXECUTION_ROLES, clamp_team_budget, effective_roles
from app.agent.runtime import (
    INTERNAL_ERROR,
    INTERNAL_MESSAGE,
    _heartbeat_loop,
    run_agent,
)
from app.agent.team_contracts import (
    critic_task,
    lead_messages,
    parse_critic_verdict,
    parse_lead_decision,
)
from app.config import Settings
from app.errors import AppError
from app.models.agent import AgentRun, AgentTeamEntry
from app.models.user import User
from app.services.audit import record_best_effort

logger = logging.getLogger(__name__)

TEAM_BUDGET_EXHAUSTED = "team_budget_exhausted"
TEAM_LEAD_INVALID = "team_lead_invalid"
CRITIC_REJECTED = "critic_rejected"
CRITIC_INVALID = "critic_invalid"
TEAM_LOOP_DETECTED = "team_loop_detected"
ROLE_FAILED = "role_failed"
TERMINAL = ("succeeded", "failed", "cancelled")
ACTIVE = ("queued", "running", "waiting_approval")


class _TeamStop(Exception):
    """编排器已落终态， unwind 主循环。"""


def _now() -> datetime:
    return datetime.now(UTC)


def _load_state(run: AgentRun) -> dict:
    state = dict(run.team_state or {})
    state.setdefault("turn", 0)
    state.setdefault("phase", "lead")
    state.setdefault("rework", 0)
    state.setdefault("roles_run", [])
    state.setdefault("artifact_index", [])
    state.setdefault("critic_verdicts", [])
    state.setdefault("critic", {})
    state.setdefault("loop_guard", {})
    return state


def _save_state(run: AgentRun, state: dict) -> None:
    run.team_state = dict(state)


def _participants(run: AgentRun) -> tuple[str, ...]:
    raw = (run.plan or {}).get("roles") or []
    roles = tuple(r for r in EXECUTION_ROLES if r in raw)
    return roles or EXECUTION_ROLES


async def team_usage(session: AsyncSession, parent: AgentRun) -> dict:
    totals = {
        "prompt_tokens": 0, "completion_tokens": 0, "tool_calls": 0,
        "steps": 0, "elapsed_ms": 0, "paused_ms": 0, "children": 0,
    }
    for key in totals:
        totals[key] = int((parent.usage or {}).get(key) or 0)
    rows = (
        await session.execute(
            select(AgentRun.usage).where(AgentRun.parent_run_id == parent.id)
        )
    ).scalars().all()
    totals["children"] = len(rows)
    for usage in rows:
        if not isinstance(usage, dict):
            continue
        for key in ("prompt_tokens", "completion_tokens", "tool_calls", "steps"):
            totals[key] += int(usage.get(key) or 0)
    return totals


def _budget_of(run: AgentRun, settings: Settings) -> dict:
    budget = clamp_team_budget({}, settings)
    for key, value in (run.budget or {}).items():
        if (
            key in budget and isinstance(value, int)
            and not isinstance(value, bool) and value > 0
        ):
            budget[key] = value
    return budget


async def _budget_stop(session, run, settings, state) -> str | None:
    budget = _budget_of(run, settings)
    usage = await team_usage(session, run)
    if int(state.get("turn", 0)) >= budget["max_turns"]:
        return "团队轮次已用尽"
    if usage["prompt_tokens"] + usage["completion_tokens"] >= budget["max_tokens"]:
        return "团队 token 预算已用尽"
    if usage["tool_calls"] >= budget["max_tool_calls"]:
        return "团队工具调用预算已用尽"
    started = run.started_at
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        elapsed = (_now() - started).total_seconds() - usage["paused_ms"] / 1000
        if elapsed >= budget["max_seconds"]:
            return "团队时间预算已用尽"
    return None


async def _fail_team(session, run, code: str, message: str) -> None:
    """Task 7 会在此追加子运行收敛；本任务先落父运行终态。"""
    moment = _now()
    await session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run.id,
            AgentRun.tenant_id == run.tenant_id,
            AgentRun.status.in_(ACTIVE),
        )
        .values(status="failed", error_code=code, error_message=message[:500],
                finished_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    # record() 需要 User | str 作为 actor；与 runtime._audit 同一口径，
    # 找不到用户时退化为 user_id 字符串，避免审计静默丢失。
    user = await session.get(User, run.user_id)
    actor: User | str = user if user is not None else str(run.user_id)
    await record_best_effort(
        session, tenant_id=run.tenant_id, action="agent.team.fail",
        actor=actor, resource_type="agent_run", resource_id=str(run.id),
        detail={"error_code": code},
    )


async def _park_parent(session, run) -> None:
    moment = _now()
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run.id, AgentRun.status.in_(("queued", "running")))
        .values(status="waiting_approval", heartbeat_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _lead_decide(session, run, *, gateway, settings, state, participants):
    entries = render_entries(
        await recent_entries(session, run.id, settings.agent_team_blackboard_inject)
    )
    messages = lead_messages(
        goal=run.goal, participants=participants, entries=entries,
        state=state, budget=_budget_of(run, settings),
    )
    for _attempt in range(2):
        text = await gateway.complete(messages, temperature=0.2, max_tokens=800)
        usage = dict(run.usage or {})
        last = getattr(gateway, "last_usage", None)
        usage["prompt_tokens"] = int(usage.get("prompt_tokens") or 0) + int(
            getattr(last, "prompt_tokens", 0) or 0)
        usage["completion_tokens"] = int(usage.get("completion_tokens") or 0) + int(
            getattr(last, "completion_tokens", 0) or 0)
        usage["steps"] = int(usage.get("steps") or 0) + 1
        run.usage = usage
        await session.commit()
        try:
            return parse_lead_decision(text, allowed_roles=participants)
        except ValueError as exc:
            messages = messages + [
                {"role": "assistant", "content": str(text)},
                {"role": "user", "content": f"输出无效：{exc}。请只输出一个 JSON 对象。"},
            ]
    await _fail_team(session, run, TEAM_LEAD_INVALID, "Lead 连续两次输出无效决策")
    raise _TeamStop()


async def _drive_child(session, parent, child, *, roles, gateway, tool_registry,
                       skill_registry, storage, queue, settings, session_factory):
    if child.status in {"running", "interrupted"}:
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == child.id,
                   AgentRun.status.in_(("running", "interrupted")))
            .values(status="queued", finished_at=None, error_code="", error_message="")
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        await session.refresh(child)
    if child.status == "queued":
        await run_agent(
            session, child,
            gateway=gateway,
            tool_registry=tool_registry.subset(roles[child.role].tools),
            skill_registry=skill_registry, storage=storage, queue=queue,
            settings=settings, session_factory=session_factory,
        )
        await session.refresh(child)


async def _start_role(session, run, *, role, task, reason, state, roles,
                      gateway, tool_registry, skill_registry, storage, queue,
                      settings, session_factory) -> None:
    normalized = " ".join(task.split())[:500]
    key = f"{role}|{hashlib.sha256(normalized.encode()).hexdigest()[:16]}"
    guard = state.setdefault("loop_guard", {})
    count = int(guard.get("count", 0)) + 1 if guard.get("last_key") == key else 1
    state["loop_guard"] = {"last_key": key, "count": count}
    if count >= 3:
        await _fail_team(session, run, TEAM_LOOP_DETECTED, "同一任务连续委派三次")
        raise _TeamStop()
    child = AgentRun(
        tenant_id=run.tenant_id, user_id=run.user_id, goal=task[:4000],
        mode="role", role=role, parent_run_id=run.id, status="queued",
        budget=dict(roles[role].budget), policy=dict(run.policy or {}),
    )
    session.add(child)
    await session.commit()
    await session.refresh(child)
    state["current_child_id"] = str(child.id)
    state["current_role"] = role
    state["phase"] = "role"
    await append_entry(session, run=run, kind="delegation", role=role,
                       content={"task": task[:1000], "reason": reason[:1000]})
    _save_state(run, state)
    await session.commit()
    await _drive_child(session, run, child, roles=roles, gateway=gateway,
                       tool_registry=tool_registry, skill_registry=skill_registry,
                       storage=storage, queue=queue, settings=settings,
                       session_factory=session_factory)


async def _start_critic(session, run, state, *, roles, gateway, tool_registry,
                        skill_registry, storage, queue, settings, session_factory):
    state["turn"] = int(state.get("turn", 0)) + 1
    _save_state(run, state)
    await session.commit()
    artifacts = state.get("artifact_index") or []
    summary = ""
    last = await recent_entries(session, run.id, settings.agent_team_blackboard_inject)
    for entry in reversed(last):
        if entry.kind == "result" and entry.role == "creator":
            summary = str((entry.content or {}).get("summary") or "")
            break
    task = critic_task(
        goal=run.goal, summary=summary, artifacts=artifacts,
        note=state.get("critic_note", ""),
    )
    await _start_role(
        session, run, role="critic", task=task, reason="强制审稿", state=state,
        roles=roles, gateway=gateway, tool_registry=tool_registry,
        skill_registry=skill_registry, storage=storage, queue=queue,
        settings=settings, session_factory=session_factory,
    )


async def _on_child_succeeded(session, run, child, state, *, settings) -> None:
    result = child.result if isinstance(child.result, dict) else {}
    artifacts = [a for a in result.get("artifacts") or [] if isinstance(a, dict)]
    entry = await append_entry(
        session, run=run, kind="result", role=child.role,
        content={"summary": child.result_text[:1000], "artifacts": artifacts},
    )
    for item in artifacts:
        state["artifact_index"].append({
            "kind": str(item.get("type") or item.get("kind") or ""),
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "role": child.role, "seq": entry.seq,
        })
    state["roles_run"] = sorted(set(state["roles_run"]) | {child.role})
    state["current_child_id"] = None
    state["current_role"] = ""
    if child.role == "creator":
        state["creator_task"] = state.get("creator_task") or child.goal
        critic_enabled = (await effective_roles(session, run.tenant_id))["critic"].enabled
        state["phase"] = "critic" if critic_enabled else "lead"
    elif child.role == "critic":
        try:
            verdict = parse_critic_verdict(child.result_text)
        except ValueError:
            if int(state.get("critic_invalid", 0)) >= 1:
                await _fail_team(session, run, CRITIC_INVALID, "Critic 连续两次输出无效")
                raise _TeamStop()
            state["critic_invalid"] = int(state.get("critic_invalid", 0)) + 1
            state["critic_note"] = "上次输出无效，只输出 JSON 裁决"
            state["phase"] = "critic"
            _save_state(run, state)
            await session.commit()
            return
        # 纠错重试按事件计数：裁决有效即清零，避免跨返工轮次误判与 note 残留
        state["critic_invalid"] = 0
        state["critic_note"] = ""
        await append_entry(session, run=run, kind="critic", role="critic",
                           content={"verdict": verdict.verdict, "issues": verdict.issues})
        state["critic_verdicts"].append({
            "child_id": str(child.id), "verdict": verdict.verdict,
            "issues": verdict.issues,
        })
        state["critic"] = {
            "verdict": verdict.verdict,
            "rework_rounds": int(state.get("rework", 0)),
            "issues": verdict.issues,
        }
        if verdict.verdict == "pass":
            state["phase"] = "lead"
        else:
            if int(state.get("rework", 0)) >= settings.agent_team_rework_limit:
                await _fail_team(session, run, CRITIC_REJECTED,
                                 "Critic 返工上限仍未通过")
                raise _TeamStop()
            state["rework"] = int(state.get("rework", 0)) + 1
            state["critic"]["rework_rounds"] = state["rework"]
            state["turn"] = int(state.get("turn", 0)) + 1
            issues = "\n".join(f"- {item}" for item in verdict.issues)
            state["forced_role"] = "creator"
            state["forced_task"] = (
                f"按审稿问题清单返工：\n{issues}\n原任务：{state.get('creator_task', '')}"
            )[:2000]
            state["phase"] = "role"
    else:
        state["phase"] = "lead"
    _save_state(run, state)
    await session.commit()


async def _settle_child(session, run, state, *, roles, gateway, tool_registry,
                        skill_registry, storage, queue, settings, session_factory) -> str:
    child = await session.get(AgentRun, uuid.UUID(state["current_child_id"]))
    if child is None:
        await _fail_team(session, run, ROLE_FAILED, "子运行记录丢失")
        return "failed"
    if child.status in {"queued", "running", "interrupted"}:
        await _drive_child(session, run, child, roles=roles, gateway=gateway,
                           tool_registry=tool_registry, skill_registry=skill_registry,
                           storage=storage, queue=queue, settings=settings,
                           session_factory=session_factory)
    if child.status == "waiting_approval":
        _save_state(run, state)
        await _park_parent(session, run)
        return "waiting"
    if child.status in {"failed", "cancelled"}:
        await _fail_team(session, run, child.error_code or ROLE_FAILED,
                         child.error_message or "角色子运行失败")
        return "failed"
    if child.status != "succeeded":
        await _fail_team(session, run, INTERNAL_ERROR, INTERNAL_MESSAGE)
        return "failed"
    await _on_child_succeeded(session, run, child, state, settings=settings)
    return "ok"


async def _finish_team(session, run, state, summary: str) -> None:
    moment = _now()
    usage = dict(run.usage or {})
    started = run.started_at
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        usage["elapsed_ms"] = int(max(0.0, (moment - started).total_seconds()) * 1000)
    result = {
        "summary": summary,
        "roles": state.get("roles_run") or [],
        "artifacts": state.get("artifact_index") or [],
        "critic": state.get("critic") or {
            "verdict": "", "rework_rounds": state.get("rework", 0), "issues": [],
        },
        "team": {"turns": state.get("turn", 0),
                 "children": len(state.get("roles_run") or [])},
    }
    children = (await session.execute(
        select(func.count()).select_from(AgentRun)
        .where(AgentRun.parent_run_id == run.id)
    )).scalar_one()
    result["team"]["children"] = children
    await append_entry(session, run=run, kind="final",
                       content={"summary": summary, "artifacts": result["artifacts"]})
    await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run.id, AgentRun.status.in_(("queued", "running")))
        .values(status="succeeded", result_text=summary[:4000], result=result,
                usage=usage, finished_at=moment, error_code="", error_message="")
        .execution_options(synchronize_session=False)
    )
    await session.commit()


async def _team_loop(session, run, *, gateway, tool_registry, skill_registry,
                     storage, queue, settings, session_factory) -> None:
    if run.status != "queued":
        return
    moment = _now()
    claimed = await session.execute(
        update(AgentRun)
        .where(AgentRun.id == run.id, AgentRun.status == "queued",
               AgentRun.mode == "team")
        .values(status="running", started_at=run.started_at or moment,
                heartbeat_at=moment)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if claimed.rowcount == 0:
        await session.refresh(run)
        return
    await session.refresh(run)
    try:
        await ensure_daily_quota(session, run.tenant_id, settings)
    except AppError as exc:
        await _fail_team(session, run, exc.code, exc.message)
        return
    roles = await effective_roles(session, run.tenant_id)
    participants = _participants(run)
    state = _load_state(run)
    has_goal = (await session.execute(
        select(AgentTeamEntry.id).where(
            AgentTeamEntry.run_id == run.id, AgentTeamEntry.kind == "goal"
        ).limit(1)
    )).first()
    if has_goal is None:
        await append_entry(session, run=run, kind="goal",
                           content={"goal": run.goal[:2000]})
        _save_state(run, state)
        await session.commit()

    while True:
        await session.refresh(run)
        if run.status == "cancelled":
            return
        state = _load_state(run)
        stop = await _budget_stop(session, run, settings, state)
        if stop is not None:
            await _fail_team(session, run, TEAM_BUDGET_EXHAUSTED, stop)
            return
        if state.get("current_child_id"):
            outcome = await _settle_child(
                session, run, state, roles=roles, gateway=gateway,
                tool_registry=tool_registry, skill_registry=skill_registry,
                storage=storage, queue=queue, settings=settings,
                session_factory=session_factory,
            )
            if outcome in {"waiting", "failed"}:
                return
            continue
        if state.get("forced_role"):
            role = state.pop("forced_role")
            task = state.pop("forced_task", "")
            await _start_role(
                session, run, role=role, task=task, reason="Critic 否决后强制返工",
                state=state, roles=roles, gateway=gateway,
                tool_registry=tool_registry, skill_registry=skill_registry,
                storage=storage, queue=queue, settings=settings,
                session_factory=session_factory,
            )
            continue
        if state.get("phase") == "critic":
            await _start_critic(
                session, run, state, roles=roles, gateway=gateway,
                tool_registry=tool_registry, skill_registry=skill_registry,
                storage=storage, queue=queue, settings=settings,
                session_factory=session_factory,
            )
            continue
        decision = await _lead_decide(
            session, run, gateway=gateway, settings=settings,
            state=state, participants=participants,
        )
        state["turn"] = int(state.get("turn", 0)) + 1
        _save_state(run, state)
        await session.commit()
        if decision.action == "finalize":
            await _finish_team(session, run, state, decision.summary)
            return
        if decision.action == "rework":
            state["creator_task"] = state.get("creator_task") or decision.task
        await _start_role(
            session, run, role=decision.role, task=decision.task,
            reason=decision.reason, state=state, roles=roles, gateway=gateway,
            tool_registry=tool_registry, skill_registry=skill_registry,
            storage=storage, queue=queue, settings=settings,
            session_factory=session_factory,
        )


async def run_team(
    session: AsyncSession,
    run: AgentRun,
    *,
    gateway,
    tool_registry,
    skill_registry,
    storage,
    queue,
    settings: Settings,
    session_factory=None,
) -> None:
    heartbeat = (
        asyncio.create_task(_heartbeat_loop(session_factory, run.id))
        if session_factory is not None
        else None
    )
    try:
        try:
            await _team_loop(
                session, run, gateway=gateway, tool_registry=tool_registry,
                skill_registry=skill_registry, storage=storage, queue=queue,
                settings=settings, session_factory=session_factory,
            )
        except _TeamStop:
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("team run crashed run=%s", run.id)
            await _fail_team(session, run, INTERNAL_ERROR, INTERNAL_MESSAGE)
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat