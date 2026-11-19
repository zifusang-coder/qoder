"""工作流执行引擎：就绪/跳过传播、节点执行、重试与时间片。

引擎不领取运行（worker 用 CAS 领取后调用），也不做取消/续跑迁移；
这些在 lifecycle.py。每轮推进读状态用一个会话，单节点执行用独立会话，
PostgreSQL 下才能真并行；SQLite 测试夹具自动串行。

设计要点：

- 每一步都从数据库重读状态，因此崩溃恢复、审批续跑、续跑都走同一条路径。
- 输出节点是唯一的结果汇总点；``raw_output`` 只用于变量解析，绝不进结果。
- 时间片余量不足时运行落回 ``queued`` 让 worker 重新入队，而不是在本任务里硬跑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import jsonschema
from arq.connections import ArqRedis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.contracts import ToolContext
from app.agent.policy import resolve_policy
from app.agent.quota import ensure_daily_quota
from app.agent.registry import ToolRegistry
from app.agent.roles import (
    EXECUTION_ROLES,
    effective_roles,
    team_budget_ceilings,
    team_budget_defaults,
)
from app.agent.runtime import run_agent
from app.agent.sanitize import fit_bytes, scrub
from app.agent.team import run_team
from app.config import Settings
from app.errors import AppError
from app.llm.gateway import LLMGateway
from app.models.agent import AgentRun
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.services.audit import is_sensitive_key, record_best_effort
from app.skills.registry import SkillRegistry
from app.storage import StorageBackend
from app.workflow import approvals
from app.workflow.graph import (
    SETTLED,
    Graph,
    GraphError,
    GraphNode,
    VariableError,
    condition_choice,
    edge_active,
    parse_graph,
    ready_nodes,
    resolve_value,
)

logger = logging.getLogger(__name__)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
ACTIVE = frozenset({"queued", "running"})
# 可以在哪个运行状态下挂起审批：waiting_approval 也要接，否则并行分支里
# 第二条审批会被 CAS 拒绝，§5.2「并行分支可能同时挂起多条审批」就落不了地。
PARKABLE = frozenset({"queued", "running", "waiting_approval"})
RETRYABLE_CODES = frozenset(
    {"conflict", "rate_limited", "queue_unavailable", "service_unavailable"}
)
# 这些节点错误直接成为运行级错误码；其余一律归为 node_failed。
RUN_LEVEL_CODES = frozenset(
    {"workflow_internal_error", "llm_not_configured", "agent_daily_quota_exceeded"}
)
MAX_RETRY = 3
# 第 index 次重试前的退避秒数（累计计入时间片判定的完整耗时）
RETRY_BACKOFF = (2.0, 4.0, 8.0)
SKIPPED_MESSAGE = "上游分支未选中"
MAX_RESULT_ITEMS = 50
# 子运行预算留出的时间片余量（Ruling 11）
SLICE_GUARD_SECONDS = 5
BUDGET_KEYS = frozenset(
    {"max_steps", "max_tokens", "max_tool_calls", "max_seconds"}
)


@dataclass(frozen=True)
class EngineDeps:
    gateway: LLMGateway | None
    tool_registry: ToolRegistry
    skill_registry: SkillRegistry
    storage: StorageBackend
    queue: ArqRedis | None
    settings: Settings
    # 测试注入 no-op，生产保持真实退避
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    # 生产 worker 必须传入：子运行靠它发心跳，否则会被第 1 层对账误杀
    session_factory: async_sessionmaker[AsyncSession] | None = None


@dataclass
class NodeState:
    node_id: str
    attempt: int
    kind: str
    status: str
    tool_step_index: int | None
    output: dict
    raw_output: dict
    error: str


@dataclass
class RunState:
    id: uuid.UUID
    tenant_id: str
    status: str
    inputs: dict
    policy: dict
    usage: dict
    triggered_by: uuid.UUID | None
    creator_id: uuid.UUID
    started_at: datetime | None
    graph: Graph
    graph_raw: dict
    node_states: dict[str, NodeState] = field(default_factory=dict)
    attempts: dict[str, int] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _duration(started_at: datetime | None, finished_at: datetime | None) -> int:
    start = _aware(started_at)
    end = _aware(finished_at)
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


def _wall_seconds(state: RunState) -> float:
    start = _aware(state.started_at)
    if start is None:
        return 0.0
    paused = float((state.usage or {}).get("paused_ms") or 0) / 1000
    return max(0.0, (_now() - start).total_seconds() - paused)


def _strip_secrets(value: Any, depth: int = 0) -> Any:
    """raw_output 的兜底清洗：只丢敏感键，不做字符串截断。

    截断由 ``output``（展示用）负责；这里截断会让下游拿到被切断的正文，
    正是第 1 层「>2000 字正文逐字传递」要避免的问题。
    """
    if depth > 8:
        return "[截断]"
    if isinstance(value, dict):
        return {
            key: _strip_secrets(item, depth + 1)
            for key, item in value.items()
            if isinstance(key, str) and not is_sensitive_key(key)
        }
    if isinstance(value, list):
        return [_strip_secrets(item, depth + 1) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)[:2000]


def _raw_payload(payload: dict) -> dict:
    cleaned = _strip_secrets(payload)
    capped, _ = fit_bytes(cleaned if isinstance(cleaned, dict) else {"data": cleaned})
    return capped if isinstance(capped, dict) else {"data": capped}


def _safe_dict(value: Any) -> dict:
    cleaned = scrub(value)
    return cleaned if isinstance(cleaned, dict) else {"data": cleaned}


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, AppError):
        return exc.message[:500]
    if isinstance(exc, TimeoutError):
        return "节点执行超时"
    return f"节点执行失败（{type(exc).__name__}）"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, AppError):
        return exc.code in RETRYABLE_CODES
    return True


def _run_error_code(exc: BaseException) -> str:
    if isinstance(exc, AppError) and exc.code in RUN_LEVEL_CODES:
        return exc.code
    return "node_failed"


def _rejection_code(node: GraphNode, approval: WorkflowApproval | None) -> str:
    """驳回/失效对应的运行级错误码（§7.7）。"""
    if approval is not None and approval.status == "rejected":
        return "review_rejected" if node.kind == "review" else "approval_rejected"
    return "approval_expired"


def _retry_budget(node: GraphNode) -> int:
    if node.kind not in {"tool", "agent", "team"}:
        return 0
    try:
        return max(0, min(MAX_RETRY, int(node.data.get("retry") or 0)))
    except (TypeError, ValueError):
        return 0


def _backoff(index: int) -> float:
    """第 index 次重试前的退避秒数；``RETRY_BACKOFF`` 比 ``MAX_RETRY`` 短时沿用末项。"""
    return RETRY_BACKOFF[min(max(0, index), len(RETRY_BACKOFF) - 1)]


def _statuses(state: RunState) -> dict[str, str]:
    return {node_id: item.status for node_id, item in state.node_states.items()}


def _raw_outputs(state: RunState) -> dict[str, dict]:
    """变量解析与条件判定用的作用域：``outputs[node_id]`` 就是节点输出本身。"""
    return {
        node_id: item.raw_output
        for node_id, item in state.node_states.items()
        if item.status == "ok"
    }


def _choices(state: RunState) -> dict[str, str | None]:
    """条件节点选中的句柄；ok 的条件节点给不出结论就是引擎错误，不能当跳过。"""
    statuses = _statuses(state)
    raws = _raw_outputs(state)
    choices: dict[str, str | None] = {}
    for node_id, node in state.graph.nodes.items():
        choice = condition_choice(node_id, statuses.get(node_id), raws)
        if (
            choice is None
            and node.kind == "condition"
            and statuses.get(node_id) == "ok"
        ):
            raise AppError(
                "workflow_internal_error",
                f"条件节点输出不合法: {node_id}",
                500,
            )
        choices[node_id] = choice
    return choices


def _node_can_run(status: str) -> bool:
    # waiting_approval 也放行：同批并行分支里别的节点可能刚挂起审批，
    # 本批其余节点仍应跑完再退出 worker。
    return status in ACTIVE or status == "waiting_approval"


async def _load(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> RunState | None:
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            return None
        workflow = await session.get(Workflow, run.workflow_id)
        version = await session.get(WorkflowVersion, run.workflow_version_id)
        if workflow is None or version is None:
            raise AppError("workflow_invalid", "工作流定义或版本不存在", 422)
        rows = (
            (
                await session.execute(
                    select(WorkflowNodeRun)
                    .where(
                        WorkflowNodeRun.tenant_id == run.tenant_id,
                        WorkflowNodeRun.run_id == run_id,
                    )
                    .order_by(WorkflowNodeRun.node_id, WorkflowNodeRun.attempt)
                )
            )
            .scalars()
            .all()
        )
        raw_graph = version.graph if isinstance(version.graph, dict) else {}
        graph = parse_graph(raw_graph)
        node_states: dict[str, NodeState] = {}
        attempts: dict[str, int] = {}
        for row in rows:
            attempts[row.node_id] = max(attempts.get(row.node_id, 0), row.attempt)
            node_states[row.node_id] = NodeState(
                node_id=row.node_id,
                attempt=row.attempt,
                kind=row.kind,
                status=row.status,
                tool_step_index=row.tool_step_index,
                output=row.output or {},
                raw_output=row.raw_output or {},
                error=row.error or "",
            )
        return RunState(
            id=run.id,
            tenant_id=run.tenant_id,
            status=run.status,
            inputs=run.inputs or {},
            policy=run.policy or {},
            usage=run.usage or {},
            triggered_by=run.triggered_by,
            creator_id=workflow.created_by,
            started_at=run.started_at,
            graph=graph,
            graph_raw=raw_graph,
            node_states=node_states,
            attempts=attempts,
        )


def _is_serial(factory: async_sessionmaker[AsyncSession]) -> bool:
    """SQLite 测试夹具用单连接 StaticPool，并行 session 会互相踩；按方言串行。"""
    bind = factory.kw.get("bind")
    return bind is not None and bind.dialect.name == "sqlite"


def _slice_window(deps: EngineDeps) -> tuple[float, float | None]:
    """返回 (时间片秒数, 本次推进的截止时刻)；``<= 0`` 表示关闭时间片。"""
    seconds = float(deps.settings.workflow_time_slice_seconds or 0)
    if seconds <= 0:
        return 0.0, None
    return seconds, asyncio.get_running_loop().time() + seconds


def _estimate(node: GraphNode, deps: EngineDeps) -> float:
    """节点单次尝试的最大可能耗时。"""
    if node.kind == "tool":
        spec = deps.tool_registry.get(str(node.data.get("tool") or ""))
        return float(spec.timeout_seconds or 0) if spec is not None else 0.0
    if node.kind == "agent":
        budget = _agent_budget(node, deps.settings)
        return float(budget.get("max_seconds") or 0)
    if node.kind == "team":
        return float(_team_budget(node, deps.settings).get("max_seconds") or 0)
    return 0.0


def _full_estimate(node: GraphNode, deps: EngineDeps) -> float:
    """整次节点执行（全部尝试 + 退避）的最大可能耗时；时间片按这个值比较。"""
    retries = _retry_budget(node)
    return _estimate(node, deps) * (retries + 1) + sum(
        _backoff(index) for index in range(retries)
    )


def _remaining(deadline: float | None) -> float | None:
    """实际剩余时间片；``None`` 表示时间片关闭（不限时）。"""
    if deadline is None:
        return None
    return deadline - asyncio.get_running_loop().time()


def _fits(
    node_id: str,
    state: RunState,
    deps: EngineDeps,
    remaining: float | None,
) -> bool:
    """给定余量是否装得下该节点整次执行（含重试与退避）。"""
    if remaining is None:
        return True
    return _full_estimate(state.graph.nodes[node_id], deps) <= remaining


def _fitting(
    state: RunState,
    ready: list[str],
    deps: EngineDeps,
    remaining: float | None,
) -> list[str]:
    return [node_id for node_id in ready if _fits(node_id, state, deps, remaining)]


async def _status_of(
    factory: async_sessionmaker[AsyncSession], run_id: uuid.UUID
) -> str:
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        return run.status if run is not None else "missing"


async def run_workflow(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    deps: EngineDeps,
) -> str:
    """推进一次运行，返回运行状态。

    返回 ``succeeded``/``failed``/``cancelled``（终态）、``waiting_approval``
    （已挂起审批）、``queued``（时间片用尽，等待重新入队）或 ``missing``。
    """
    try:
        state = await _load(factory, run_id)
    except (AppError, GraphError) as exc:
        # 已发布版本理应通过校验；读不出可执行图属引擎内部不一致。
        return await _fail(
            factory, run_id, "workflow_internal_error", str(exc)[:500]
        )
    if state is None:
        return "missing"
    if state.status in TERMINAL:
        return state.status

    await _seed_input(factory, state)

    serial = _is_serial(factory)
    slice_seconds, deadline = _slice_window(deps)
    # 首批节点在片头组装：容量按标称时间片算，不扣已流逝的余量，完整估算
    # 恰等于时间片的节点因此能在片头跑起来；之后一律只看实际剩余。
    first_batch = True

    while True:
        state = await _load(factory, run_id)
        if state is None:
            return "missing"
        if state.status in TERMINAL:
            return state.status

        if any(item.status == "waiting" for item in state.node_states.values()):
            outcome = await _resume_waiting(factory, state, deps=deps)
            if outcome == "progress":
                # 审批恢复也可能执行节点，时钟已不在片头。
                first_batch = False
                continue
            if outcome == "waiting_approval":
                return "waiting_approval"
            return await _status_of(factory, run_id)

        if state.status not in ACTIVE:
            return state.status

        limit = deps.settings.workflow_max_seconds
        if limit > 0 and _wall_seconds(state) > limit:
            return await _fail(factory, run_id, "workflow_timeout", "运行超过墙钟上限")

        try:
            choices = _choices(state)
        except AppError as exc:
            return await _fail(factory, run_id, exc.code, exc.message)

        ready, skipped = ready_nodes(state.graph, _statuses(state), choices)
        if skipped:
            await _persist_skipped(factory, state, skipped)
            continue
        if not ready:
            unsettled = [
                node_id
                for node_id in state.graph.nodes
                if _statuses(state).get(node_id) not in SETTLED
            ]
            if unsettled:
                return await _fail(
                    factory,
                    run_id,
                    "workflow_internal_error",
                    "没有可执行节点，工作流无法收敛",
                )
            return await _finalize(factory, run_id, deps=deps)

        if deadline is not None:
            # 装不进整个时间片的节点永远等不到余量，重排队就是死循环，直接判失败。
            overdue = [
                node_id
                for node_id in ready
                if _full_estimate(state.graph.nodes[node_id], deps) > slice_seconds
            ]
            if overdue:
                estimate = _full_estimate(state.graph.nodes[overdue[0]], deps)
                return await _fail(
                    factory,
                    run_id,
                    "node_failed",
                    f"节点 {overdue[0]} 预计耗时 {estimate:g} 秒（含重试与退避）"
                    f"超过时间片 {slice_seconds:g} 秒，无法在单个时间片内完成",
                )
            capacity = slice_seconds if first_batch else _remaining(deadline)
            runnable = _fitting(state, ready, deps, capacity)
            if not runnable:
                return await _requeue(factory, run_id, deps=deps)
            ready = runnable

        if serial:
            await _run_serial(
                factory,
                state,
                ready,
                deps=deps,
                deadline=deadline,
                first_batch=first_batch,
            )
        else:
            await _run_parallel(
                factory,
                state,
                ready,
                deps=deps,
                deadline=deadline,
                first_batch=first_batch,
            )
        first_batch = False


async def _run_serial(
    factory: async_sessionmaker[AsyncSession],
    state: RunState,
    node_ids: list[str],
    *,
    deps: EngineDeps,
    deadline: float | None = None,
    first_batch: bool = False,
) -> None:
    """SQLite 测试夹具：一个会话里逐节点执行，避免并行 session 抢同一连接。"""
    async with factory() as session:
        for position, node_id in enumerate(node_ids):
            # 首批首个节点已按标称时间片校验过；其余节点重查实际剩余，
            # 前一个节点实跑完可能已吃掉余量，装不下就交回外层重排队。
            if (position > 0 or not first_batch) and not _fits(
                node_id, state, deps, _remaining(deadline)
            ):
                break
            outcome = await _run_node(
                factory, state, node_id, deps=deps, session=session
            )
            if outcome in {"stopped", "stale"}:
                break


async def _run_parallel(
    factory: async_sessionmaker[AsyncSession],
    state: RunState,
    node_ids: list[str],
    *,
    deps: EngineDeps,
    deadline: float | None = None,
    first_batch: bool = False,
) -> None:
    limit = max(1, int(deps.settings.workflow_max_parallel_nodes))

    async def run_wave(wave: list[str], *, check: bool) -> None:
        gate = asyncio.Semaphore(limit)

        async def guarded(node_id: str) -> None:
            async with gate:
                # 波宽等于槽位数，门在波内不会阻塞；check 只服务于首批之后的
                # 波次：前一波已跑掉一段时间，按实际剩余再确认一次。
                if check and not _fits(node_id, state, deps, _remaining(deadline)):
                    return
                await _run_node(factory, state, node_id, deps=deps)

        await asyncio.gather(*(guarded(node_id) for node_id in wave))

    for start in range(0, len(node_ids), limit):
        await run_wave(
            node_ids[start : start + limit],
            check=start > 0 or not first_batch,
        )


async def _seed_input(
    factory: async_sessionmaker[AsyncSession], state: RunState
) -> None:
    """首次推进时为无入边的 input 节点写一条 ok 记录（输出 = 运行入参）。"""
    inputs = [
        node_id
        for node_id, node in state.graph.nodes.items()
        if node.kind == "input" and not state.graph.incoming[node_id]
    ]
    if not inputs or any(node_id in state.node_states for node_id in inputs):
        return
    moment = _now()
    async with factory() as session:
        for node_id in inputs:
            session.add(
                WorkflowNodeRun(
                    tenant_id=state.tenant_id,
                    run_id=state.id,
                    node_id=node_id,
                    attempt=1,
                    kind="input",
                    status="ok",
                    output=_safe_dict({"inputs": state.inputs}),
                    raw_output=_raw_payload({"inputs": state.inputs}),
                    started_at=moment,
                    finished_at=moment,
                )
            )
        await session.commit()


async def _persist_skipped(
    factory: async_sessionmaker[AsyncSession], state: RunState, node_ids: list[str]
) -> None:
    moment = _now()
    async with factory() as session:
        for node_id in node_ids:
            session.add(
                WorkflowNodeRun(
                    tenant_id=state.tenant_id,
                    run_id=state.id,
                    node_id=node_id,
                    attempt=state.attempts.get(node_id, 0) + 1,
                    kind=state.graph.nodes[node_id].kind,
                    status="skipped",
                    error=SKIPPED_MESSAGE,
                    started_at=moment,
                    finished_at=moment,
                )
            )
        await session.commit()


async def _latest_node_run(
    session: AsyncSession,
    *,
    tenant_id: str,
    run_id: uuid.UUID,
    node_id: str,
) -> WorkflowNodeRun | None:
    return (
        (
            await session.execute(
                select(WorkflowNodeRun)
                .where(
                    WorkflowNodeRun.tenant_id == tenant_id,
                    WorkflowNodeRun.run_id == run_id,
                    WorkflowNodeRun.node_id == node_id,
                )
                .order_by(WorkflowNodeRun.attempt.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _allocate_step_index(session: AsyncSession, run: WorkflowRun) -> int:
    """原子分配稳定的 ``tool_step_index``；同事务内读回的就是自己的值。"""
    await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
        )
        .values(step_counter=WorkflowRun.step_counter + 1)
        .execution_options(synchronize_session=False)
    )
    return int(
        (
            await session.execute(
                select(WorkflowRun.step_counter).where(
                    WorkflowRun.id == run.id,
                    WorkflowRun.tenant_id == run.tenant_id,
                )
            )
        ).scalar_one()
    )


async def _run_node(
    factory: async_sessionmaker[AsyncSession],
    state: RunState,
    node_id: str,
    *,
    deps: EngineDeps,
    session: AsyncSession | None = None,
    resume_attempt: int | None = None,
):
    """执行一个节点（含重试）；返回 ok/denied/waiting/error/stopped/stale。"""
    if session is None:
        async with factory() as own:
            return await _run_node(
                factory,
                state,
                node_id,
                deps=deps,
                session=own,
                resume_attempt=resume_attempt,
            )
    node = state.graph.nodes.get(node_id)
    if node is None:
        return "stopped"

    session.expire_all()
    run = await session.get(WorkflowRun, state.id)
    if run is None or not _node_can_run(run.status):
        return "stopped"

    retry = _retry_budget(node)
    approved = resume_attempt is not None
    node_run: WorkflowNodeRun | None = None
    attempt = state.attempts.get(node_id, 0)
    previous = state.node_states.get(node_id)
    step_index = previous.tool_step_index if previous is not None else None

    if approved:
        node_run = await _latest_node_run(
            session,
            tenant_id=run.tenant_id,
            run_id=run.id,
            node_id=node_id,
        )
        if node_run is None or node_run.status != "waiting":
            return "stale"
        node_run.status = "running"
        node_run.started_at = _now()
        if node.kind == "tool" and node_run.tool_step_index is None:
            node_run.tool_step_index = await _allocate_step_index(session, run)
        await session.commit()
        attempt = node_run.attempt
        step_index = node_run.tool_step_index

    # team 节点重试沿用同一父运行断点：旧尝试的 agent_run_id 继承到新行。
    team_parent_id: uuid.UUID | None = None
    for index in range(retry + 1):
        if node_run is None or index > 0:
            attempt += 1
            node_run = WorkflowNodeRun(
                tenant_id=run.tenant_id,
                run_id=run.id,
                node_id=node_id,
                attempt=attempt,
                kind=node.kind,
                status="running",
                tool_step_index=step_index,
                agent_run_id=team_parent_id,
                started_at=_now(),
            )
            session.add(node_run)
            await session.commit()
            if node.kind == "tool" and step_index is None:
                step_index = await _allocate_step_index(session, run)
                node_run.tool_step_index = step_index
                await session.commit()
        node_run_id = node_run.id
        try:
            return await _execute_node(
                session, run, node, node_run, state, deps, resume=approved
            )
        except Exception as exc:  # 节点级失败：记录、按需重试、最终落运行失败
            # 回滚会过期本事务里的一切对象，之后只能用主键重读，不能摸旧实例。
            logger.exception(
                "workflow node failed run=%s node=%s attempt=%s",
                state.id,
                node_id,
                node_run.attempt,
            )
            await session.rollback()
            moment = _now()
            row = await session.get(WorkflowNodeRun, node_run_id)
            if row is not None:
                # 回滚后从库里重读旧尝试已提交的父运行 id，供新尝试继承。
                if node.kind == "team":
                    team_parent_id = row.agent_run_id
                row.status = "error"
                row.error = _error_message(exc)
                row.finished_at = moment
                row.duration_ms = _duration(row.started_at, moment)
                await session.commit()
            run = await session.get(WorkflowRun, state.id)
            if run is None or not _node_can_run(run.status):
                return "stopped"
            if _retryable(exc) and index < retry:
                await deps.sleep(_backoff(index))
                # 退避期间运行可能被取消：上一次尝试的回滚已过期本会话实例，
                # 重读后才能决定是否还要写新尝试行并执行工具。
                session.expire_all()
                run = await session.get(WorkflowRun, state.id)
                if run is None or not _node_can_run(run.status):
                    return "stopped"
                continue
            await _fail_run(session, run, _run_error_code(exc), _error_message(exc))
            await session.commit()
            return "error"
    return "error"


async def _execute_node(
    session: AsyncSession,
    run: WorkflowRun,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
    deps: EngineDeps,
    *,
    resume: bool = False,
) -> str:
    if node.kind == "tool":
        return await _execute_tool(
            session, run, node, node_run, state, deps, resume=resume
        )
    if node.kind == "agent":
        return await _execute_agent(
            session, run, node, node_run, state, deps, resume=resume
        )
    if node.kind == "team":
        return await _execute_team(
            session, run, node, node_run, state, deps, resume=resume
        )
    if node.kind == "condition":
        return await _execute_condition(session, node, node_run, state)
    if node.kind == "review":
        return await _execute_review(session, run, node, node_run, state, deps)
    if node.kind == "output":
        return await _execute_output(session, node, node_run, state)
    raise AppError("workflow_internal_error", f"未知节点类型: {node.kind}", 500)


def _resolve_args(node: GraphNode, state: RunState) -> dict:
    args = node.data.get("args") or {}
    try:
        resolved = resolve_value(
            args, inputs=state.inputs, outputs=_raw_outputs(state)
        )
    except VariableError as exc:
        raise AppError(
            "invalid_arguments", f"节点 {node.id} 参数解析失败：{exc}", 422
        ) from None
    if not isinstance(resolved, dict):
        raise AppError(
            "invalid_arguments", f"节点 {node.id} 工具参数必须是对象", 422
        )
    return resolved


def _agent_budget(node: GraphNode, settings: Settings) -> dict[str, int]:
    raw = node.data.get("budget")
    if not isinstance(raw, dict):
        raw = {}
    budget = {
        key: value
        for key, value in raw.items()
        if key in BUDGET_KEYS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    }
    cap = int(settings.workflow_time_slice_seconds or 0)
    if cap > 0:
        # 整次执行（含重试与退避）得装进一个时间片，否则引擎会在开跑前判死。
        # 于是每个尝试只分到（时间片 - 守卫 - 全部退避）/ 尝试次数；
        # retry=0 时正好退化为 Ruling 11 的「时间片 - 5」。
        attempts = _retry_budget(node) + 1
        backoffs = int(sum(_backoff(index) for index in range(attempts - 1)))
        ceiling = max(1, (cap - SLICE_GUARD_SECONDS - backoffs) // attempts)
        budget["max_seconds"] = min(budget.get("max_seconds", ceiling), ceiling)
    return budget


def _team_budget(node: GraphNode, settings: Settings) -> dict[str, int]:
    budget = team_budget_defaults(settings)
    ceilings = team_budget_ceilings(settings)
    raw = node.data.get("budget")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if (
                key in budget
                and isinstance(value, int)
                and not isinstance(value, bool)
                and value > 0
            ):
                budget[key] = min(value, ceilings[key])
    cap = int(settings.workflow_time_slice_seconds or 0)
    if cap > 0:
        attempts = _retry_budget(node) + 1
        backoffs = int(sum(_backoff(index) for index in range(attempts - 1)))
        ceiling = max(1, (cap - SLICE_GUARD_SECONDS - backoffs) // attempts)
        budget["max_seconds"] = min(budget["max_seconds"], ceiling)
    return budget


def _agent_goal(node: GraphNode, state: RunState) -> str:
    raw = node.data.get("goal") or ""
    try:
        resolved = resolve_value(
            raw, inputs=state.inputs, outputs=_raw_outputs(state)
        )
    except VariableError as exc:
        raise AppError(
            "invalid_arguments", f"Agent 目标解析失败：{exc}", 422
        ) from None
    if not isinstance(resolved, str) or not resolved.strip():
        raise AppError("invalid_arguments", "Agent 目标不能为空", 422)
    return resolved[:4000]


async def _abort_node(
    session: AsyncSession,
    run: WorkflowRun,
    node_run: WorkflowNodeRun,
    code: str,
    message: str,
) -> None:
    node_run.status = "error"
    node_run.error = message[:500]
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await _fail_run(session, run, code, message)
    await session.commit()


async def _agent_succeeded(
    session: AsyncSession, node_run: WorkflowNodeRun, child: AgentRun
) -> str:
    payload = {
        "result_text": child.result_text,
        "result": child.result if isinstance(child.result, dict) else {},
    }
    node_run.output = _safe_dict(payload)
    node_run.raw_output = _raw_payload(payload)
    node_run.status = "ok"
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await session.commit()
    return "ok"


async def _agent_failed(
    session: AsyncSession,
    run: WorkflowRun,
    node_run: WorkflowNodeRun,
    child: AgentRun,
) -> str:
    code = child.error_code or "node_failed"
    message = (child.error_message or "Agent 节点执行失败")[:500]
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    if code == "approval_rejected":
        node_run.status = "denied"
        node_run.error = message
        await _fail_run(session, run, "approval_rejected", message)
        await session.commit()
        return "error"
    if code in {"agent_daily_quota_exceeded", "llm_not_configured"}:
        node_run.status = "error"
        node_run.error = message
        await _fail_run(session, run, code, message)
        await session.commit()
        return "error"
    # 其余失败交给 _run_node 的重试循环，耗尽后落 node_failed
    raise AppError("service_unavailable", f"Agent 子运行失败：{message}")


async def _execute_agent(
    session: AsyncSession,
    run: WorkflowRun,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
    deps: EngineDeps,
    *,
    resume: bool = False,
) -> str:
    if deps.gateway is None:
        await _abort_node(
            session, run, node_run, "llm_not_configured", "未配置可用的大模型"
        )
        return "error"
    try:
        await ensure_daily_quota(session, run.tenant_id, deps.settings)
    except AppError as exc:
        await _abort_node(session, run, node_run, exc.code, exc.message)
        return "error"

    patterns = [str(item) for item in (node.data.get("tools") or [])]
    child: AgentRun | None = None
    if resume and node_run.agent_run_id is not None:
        child = await session.get(AgentRun, node_run.agent_run_id)
    if child is None:
        goal = _agent_goal(node, state)
        child = AgentRun(
            tenant_id=run.tenant_id,
            user_id=run.triggered_by or state.creator_id,
            goal=goal,
            mode="node",
            status="queued",
            budget=_agent_budget(node, deps.settings),
            policy=dict(run.policy or {}),
            workflow_run_id=run.id,
            workflow_node_id=node.id,
        )
        session.add(child)
        await session.commit()
        await session.refresh(child)
        node_run.agent_run_id = child.id
        node_run.input = _safe_dict({"goal": goal, "tools": patterns})
        node_run.status = "running"
        await session.commit()
    elif child.status == "succeeded":
        return await _agent_succeeded(session, node_run, child)
    elif child.status in {"failed", "cancelled"}:
        return await _agent_failed(session, run, node_run, child)
    elif child.status not in {"queued", "waiting_approval"}:
        await _abort_node(
            session,
            run,
            node_run,
            "workflow_internal_error",
            f"子运行状态异常: {child.status}",
        )
        return "error"

    await run_agent(
        session,
        child,
        gateway=deps.gateway,
        tool_registry=deps.tool_registry.subset(patterns),
        skill_registry=deps.skill_registry,
        storage=deps.storage,
        queue=deps.queue,
        settings=deps.settings,
        session_factory=deps.session_factory,
    )
    await session.refresh(child)
    if child.status == "succeeded":
        return await _agent_succeeded(session, node_run, child)
    if child.status == "waiting_approval":
        node_run.status = "waiting"
        # 与工具/审核节点同一套挂起口径：盖心跳并接受并行分支已挂起的状态。
        if not await _park(session, run, _now()):
            await session.rollback()
            return "stopped"
        await session.commit()
        return "waiting"
    return await _agent_failed(session, run, node_run, child)


async def _settle_team_parent(
    session: AsyncSession,
    run: WorkflowRun,
    node_run: WorkflowNodeRun,
    parent: AgentRun,
) -> str | None:
    """把已有团队父运行领回可续跑状态。

    返回 ``None`` 表示父运行已就绪（queued），调用方接着 ``run_team``；
    否则返回终态结果（ok/error）。CAS 落败说明别的写入者改了状态，refresh
    后按新状态重新分支（cancelled 不复活）。
    """
    while True:
        if parent.status == "succeeded":
            return await _agent_succeeded(session, node_run, parent)
        if parent.status == "waiting_approval":
            # 审批通过后续跑：等待时长按 heartbeat_at 锚点计入 paused_ms
            # （公式镜像 api/agent.py _wake_parent_team），再 CAS 回 queued。
            moment = _now()
            paused_at = _aware(parent.heartbeat_at) or moment
            waited_ms = int(
                max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000
            )
            usage = dict(parent.usage or {})
            usage["paused_ms"] = int(usage.get("paused_ms") or 0) + waited_ms
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
            if resumed.rowcount:
                await session.commit()
                await session.refresh(parent)
                return None
            await session.refresh(parent)
            continue
        if parent.status == "failed" and node_run.attempt > 1:
            # 重试沿用断点：保留 team_state 与 usage，清错误后 CAS 回 queued。
            moment = _now()
            resumed = await session.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == parent.id,
                    AgentRun.tenant_id == parent.tenant_id,
                    AgentRun.status == "failed",
                )
                .values(
                    status="queued",
                    error_code="",
                    error_message="",
                    finished_at=None,
                    heartbeat_at=moment,
                )
                .execution_options(synchronize_session=False)
            )
            if resumed.rowcount:
                await session.commit()
                await session.refresh(parent)
                return None
            await session.refresh(parent)
            continue
        if parent.status in {"failed", "cancelled"}:
            return await _agent_failed(session, run, node_run, parent)
        if parent.status == "queued":
            return None
        await _abort_node(
            session,
            run,
            node_run,
            "workflow_internal_error",
            f"团队运行状态异常: {parent.status}",
        )
        return "error"


async def _execute_team(
    session: AsyncSession,
    run: WorkflowRun,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
    deps: EngineDeps,
    *,
    resume: bool = False,
) -> str:
    if deps.gateway is None:
        await _abort_node(
            session, run, node_run, "llm_not_configured", "未配置可用的大模型"
        )
        return "error"
    try:
        await ensure_daily_quota(session, run.tenant_id, deps.settings)
    except AppError as exc:
        await _abort_node(session, run, node_run, exc.code, exc.message)
        return "error"

    # 租户角色收窄：被禁用的执行角色直接中止，不创建团队父运行。
    # 错误码与 roles.validate_participants 一致；未知角色维持静默过滤。
    roles_cfg = await effective_roles(session, run.tenant_id)
    for role in node.data.get("roles") or []:
        if role in EXECUTION_ROLES and not roles_cfg[role].enabled:
            await _abort_node(
                session, run, node_run, "team_roles_invalid",
                f"角色已被租户禁用: {role}",
            )
            return "error"

    roles = [r for r in (node.data.get("roles") or []) if r in EXECUTION_ROLES]
    if "creator" not in roles:
        roles.append("creator")
    parent: AgentRun | None = None
    if node_run.agent_run_id is not None:
        parent = await session.get(AgentRun, node_run.agent_run_id)
    if parent is None:
        goal = _agent_goal(node, state)
        parent = AgentRun(
            tenant_id=run.tenant_id,
            user_id=run.triggered_by or state.creator_id,
            goal=goal,
            mode="team",
            status="queued",
            plan={"roles": roles},
            budget=_team_budget(node, deps.settings),
            policy=dict(run.policy or {}),
            workflow_run_id=run.id,
            workflow_node_id=node.id,
        )
        session.add(parent)
        await session.commit()
        await session.refresh(parent)
        node_run.agent_run_id = parent.id
        node_run.input = _safe_dict({"goal": goal, "roles": roles})
        node_run.status = "running"
        await session.commit()
    else:
        outcome = await _settle_team_parent(session, run, node_run, parent)
        if outcome is not None:
            return outcome

    await run_team(
        session,
        parent,
        gateway=deps.gateway,
        tool_registry=deps.tool_registry,
        skill_registry=deps.skill_registry,
        storage=deps.storage,
        queue=deps.queue,
        settings=deps.settings,
        session_factory=deps.session_factory,
    )
    await session.refresh(parent)
    if parent.status == "succeeded":
        return await _agent_succeeded(session, node_run, parent)
    if parent.status == "waiting_approval":
        node_run.status = "waiting"
        # 与 agent 节点同一套挂起口径：盖心跳并接受并行分支已挂起的状态。
        if not await _park(session, run, _now()):
            await session.rollback()
            return "stopped"
        await session.commit()
        return "waiting"
    return await _agent_failed(session, run, node_run, parent)


async def _park(
    session: AsyncSession, run: WorkflowRun, moment: datetime
) -> bool:
    """把运行挂到等待审批，并在此刻盖上心跳（暂停计时锚点）。"""
    parked = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status.in_(PARKABLE),
        )
        .values(status="waiting_approval", heartbeat_at=moment)
        .execution_options(synchronize_session=False)
    )
    return parked.rowcount > 0


async def _execute_tool(
    session: AsyncSession,
    run: WorkflowRun,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
    deps: EngineDeps,
    *,
    resume: bool = False,
) -> str:
    name = str(node.data.get("tool") or "")
    spec = deps.tool_registry.get(name)
    if spec is None:
        raise AppError("tool_unknown", f"未知工具: {name}", 422)
    args = _resolve_args(node, state)
    try:
        jsonschema.Draft202012Validator(spec.input_schema).validate(args)
    except jsonschema.ValidationError as exc:
        raise AppError(
            "invalid_arguments",
            f"节点 {node.id} 工具参数不合法：{exc.message}",
            422,
        ) from None
    node_run.input = _safe_dict(args)
    # 审批恢复时用同一组上游 raw_output 重新解析参数，结果与暂停前一致，
    # 因此直接执行、不再看策略（否则会二次挂起同一条审批）。
    action = "auto" if resume else resolve_policy(run.policy or {}, spec)

    if action == "deny":
        node_run.status = "denied"
        node_run.error = "策略拒绝执行该工具"
        node_run.finished_at = _now()
        node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
        await _fail_run(session, run, "policy_denied", "策略拒绝执行该工具")
        await session.commit()
        return "denied"

    if action == "confirm":
        moment = _now()
        await approvals.create_approval(
            session,
            run=run,
            node_id=node.id,
            kind="tool_confirm",
            title=f"确认执行 {spec.name}",
            payload=_safe_dict(args),
            tool_name=spec.name,
            risk=spec.risk,
            ttl_hours=deps.settings.workflow_approval_ttl_hours,
            now=moment,
        )
        node_run.status = "waiting"
        if not await _park(session, run, moment):
            await session.rollback()
            return "stopped"
        await session.commit()
        return "waiting"

    ctx = ToolContext(
        session=session,
        tenant_id=run.tenant_id,
        user_id=run.triggered_by or state.creator_id,
        run_id=str(run.id),
        step_index=int(node_run.tool_step_index or 0),
        gateway=deps.gateway,
        storage=deps.storage,
        registry=deps.skill_registry,
        queue=deps.queue,
        settings=deps.settings,
    )
    result = await asyncio.wait_for(
        spec.handler(ctx, args), timeout=spec.timeout_seconds
    )
    # 工具执行期间运行可能已被取消/落败：不把结果写成 ok，免得把已终结的
    # 运行拉回活动态（与第 1 层每次调用前的取消检查同一口径）。
    await session.refresh(run)
    if run.status in TERMINAL:
        await session.rollback()
        return "stopped"
    payload = {
        "summary": result.summary,
        "data": result.data,
        "artifacts": result.artifacts,
    }
    display, truncated = fit_bytes(scrub(payload))
    if not isinstance(display, dict):
        display = {"data": display}
    if truncated:
        display["truncated"] = True
    node_run.output = display
    node_run.raw_output = _raw_payload(payload)
    node_run.status = "ok"
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await session.commit()
    if spec.risk in {"low", "high"}:
        await record_best_effort(
            session,
            tenant_id=run.tenant_id,
            action="workflow.node.execute",
            actor=str(run.triggered_by) if run.triggered_by else "scheduler",
            resource_type="workflow_run",
            resource_id=str(run.id),
            detail={"node_id": node.id, "tool": spec.name},
        )
    return "ok"


def _compare(left: Any, operator: str, value: Any) -> bool:
    if operator == "not_empty":
        return left not in (None, "", [], {})
    if operator == "equals":
        return left == value
    if operator == "not_equals":
        return left != value
    if operator == "contains":
        if isinstance(left, str):
            return isinstance(value, str) and value in left
        if isinstance(left, list):
            return value in left
        return False
    if operator in {"gt", "lt"}:
        numeric = (int, float)
        if (
            isinstance(left, numeric)
            and not isinstance(left, bool)
            and isinstance(value, numeric)
            and not isinstance(value, bool)
        ):
            return left > value if operator == "gt" else left < value
        return False
    raise AppError("workflow_internal_error", f"未知条件运算符: {operator}", 500)


async def _execute_condition(
    session: AsyncSession,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
) -> str:
    raws = _raw_outputs(state)
    try:
        left = resolve_value(node.data.get("left"), inputs=state.inputs, outputs=raws)
        value = (
            resolve_value(node.data.get("value"), inputs=state.inputs, outputs=raws)
            if "value" in node.data
            else None
        )
    except VariableError as exc:
        raise AppError(
            "invalid_arguments", f"节点 {node.id} 条件解析失败：{exc}", 422
        ) from None
    result = _compare(left, str(node.data.get("operator") or ""), value)
    node_run.input = _safe_dict({"left": left, "value": value})
    node_run.output = {"result": result}
    node_run.raw_output = {"result": result}
    node_run.status = "ok"
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await session.commit()
    return "ok"


def _upstream_payload(state: RunState, node_id: str) -> list[dict]:
    payload: list[dict] = []
    for item in state.graph.incoming[node_id]:
        source = state.node_states.get(item.source)
        if source is None or source.status != "ok":
            continue
        payload.append(
            {
                "node_id": item.source,
                "summary": str(source.output.get("summary") or ""),
                "artifacts": list(source.output.get("artifacts") or [])[:10],
            }
        )
    return payload


async def _execute_review(
    session: AsyncSession,
    run: WorkflowRun,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
    deps: EngineDeps,
) -> str:
    title = str(node.data.get("title") or "审核")
    payload = {
        "node_id": node.id,
        "title": title,
        "note": str(node.data.get("note") or ""),
        "upstream": _upstream_payload(state, node.id),
    }
    moment = _now()
    node_run.input = _safe_dict({"title": title})
    await approvals.create_approval(
        session,
        run=run,
        node_id=node.id,
        kind="review",
        title=title,
        payload=payload,
        ttl_hours=deps.settings.workflow_approval_ttl_hours,
        now=moment,
    )
    node_run.status = "waiting"
    if not await _park(session, run, moment):
        await session.rollback()
        return "stopped"
    await session.commit()
    return "waiting"


def _collect_output(
    state: RunState, node: GraphNode
) -> tuple[list[dict], list[dict], list]:
    """汇总本输出节点的激活上游：展示副本给 output，原始副本给 raw_output。"""
    statuses = _statuses(state)
    choices = _choices(state)
    items: list[dict] = []
    raw_items: list[dict] = []
    artifacts: list = []
    for item in state.graph.incoming[node.id]:
        if not edge_active(item, statuses, choices):
            continue
        source = state.node_states.get(item.source)
        if source is None or source.status != "ok":
            continue
        items.append({"node_id": item.source, "output": source.output})
        raw_items.append({"node_id": item.source, "output": source.raw_output})
        artifacts.extend(source.output.get("artifacts") or [])
    return items[:MAX_RESULT_ITEMS], raw_items[:MAX_RESULT_ITEMS], artifacts


async def _execute_output(
    session: AsyncSession,
    node: GraphNode,
    node_run: WorkflowNodeRun,
    state: RunState,
) -> str:
    items, raw_items, artifacts = _collect_output(state, node)
    node_run.output = {"items": items, "artifacts": artifacts[:MAX_RESULT_ITEMS]}
    node_run.raw_output = {"items": raw_items, "artifacts": artifacts[:MAX_RESULT_ITEMS]}
    node_run.status = "ok"
    node_run.finished_at = _now()
    node_run.duration_ms = _duration(node_run.started_at, node_run.finished_at)
    await session.commit()
    return "ok"


async def _resume_waiting(
    factory: async_sessionmaker[AsyncSession],
    state: RunState,
    *,
    deps: EngineDeps,
) -> str:
    """结算等待中的节点与审批，返回 progress / waiting_approval / stopped。"""
    active: list[str] = []
    async with factory() as session:
        run = await session.get(WorkflowRun, state.id)
        if run is None:
            return "stopped"
        if run.status in TERMINAL:
            return "stopped"
        if await approvals.has_pending(
            session, tenant_id=run.tenant_id, run_id=run.id
        ):
            if run.status != "waiting_approval" and not await _park(
                session, run, _now()
            ):
                await session.rollback()
                return "stopped"
            await session.commit()
            return "waiting_approval"

        moment = _now()
        resumed = run.status == "waiting_approval"
        # 防御：审批通道为空但运行仍挂着（决策与运行 CAS 未同批落库时）。
        # 正常路径由 decide_approval 在同事务内把运行置回 queued。
        if resumed and not await _settle_pause(session, run, moment):
            await session.rollback()
            return "stopped"

        for item in list(state.node_states.values()):
            if item.status != "waiting":
                continue
            node = state.graph.nodes.get(item.node_id)
            if node is None:
                await _fail_run(
                    session,
                    run,
                    "workflow_internal_error",
                    f"无法恢复节点: {item.node_id}",
                )
                await session.commit()
                return "stopped"
            if node.kind in {"agent", "team"}:
                # 子运行的审批在 agent_approvals，没有 workflow_approvals 行；
                # team 节点的 role 子审批同样在 agent_approvals（继承外键后
                # 已被 has_pending 看见），这里一并交给子运行续跑通道。
                active.append(item.node_id)
                continue
            node_run = await _latest_node_run(
                session,
                tenant_id=run.tenant_id,
                run_id=run.id,
                node_id=item.node_id,
            )
            approval = await approvals.node_approval(
                session,
                tenant_id=run.tenant_id,
                run_id=run.id,
                node_id=item.node_id,
            )
            if approval is None or approval.status in {"rejected", "expired"}:
                if resumed:
                    code = _rejection_code(node, approval)
                    if node_run is not None and node_run.status == "waiting":
                        node_run.status = "denied"
                        node_run.error = "审批未通过"
                        node_run.finished_at = moment
                        node_run.duration_ms = _duration(node_run.started_at, moment)
                    await _fail_run(session, run, code, "审批未通过")
                    await session.commit()
                    return "stopped"
                # 运行已被续跑/重新入队：兄弟节点遗留的 waiting 要重置，
                # 交给就绪循环以 attempt+1 重新执行（审核节点会重新建审批）。
                if node_run is not None and node_run.status == "waiting":
                    node_run.status = "interrupted"
                    node_run.error = "审批已失效，等待重新执行"
                    node_run.finished_at = moment
                    node_run.duration_ms = _duration(node_run.started_at, moment)
                continue
            if approval.status == "pending":
                await session.commit()
                return "waiting_approval"
            if node.kind == "review":
                if node_run is None:
                    await _fail_run(
                        session,
                        run,
                        "workflow_internal_error",
                        f"审核节点缺少运行记录: {item.node_id}",
                    )
                    await session.commit()
                    return "stopped"
                if node_run.status == "waiting":
                    node_run.status = "ok"
                    node_run.output = {"approved": True}
                    node_run.raw_output = {"approved": True}
                    node_run.finished_at = moment
                    node_run.duration_ms = _duration(node_run.started_at, moment)
            elif node.kind == "tool":
                active.append(item.node_id)
            else:
                await _fail_run(
                    session,
                    run,
                    "workflow_internal_error",
                    f"无法恢复节点类型: {node.kind}",
                )
                await session.commit()
                return "stopped"
        await session.commit()

    for node_id in active:
        outcome = await _run_node(
            factory,
            state,
            node_id,
            deps=deps,
            resume_attempt=state.node_states[node_id].attempt,
        )
        if outcome in {"error", "denied", "stopped"}:
            return "stopped"
    return "progress"


async def _settle_pause(
    session: AsyncSession, run: WorkflowRun, moment: datetime
) -> bool:
    """把审批等待时长记入 usage.paused_ms，并把运行从 waiting_approval 领回 running。"""
    usage = dict(run.usage or {})
    paused_at = _aware(run.heartbeat_at) or moment
    usage["paused_ms"] = int(usage.get("paused_ms") or 0) + _duration(paused_at, moment)
    claimed = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status == "waiting_approval",
        )
        .values(status="running", usage=usage, heartbeat_at=moment)
        .execution_options(synchronize_session=False)
    )
    return claimed.rowcount > 0


async def _refresh_usage(session: AsyncSession, run: WorkflowRun) -> dict:
    usage = dict(run.usage or {})
    rows = (
        (
            await session.execute(
                select(WorkflowNodeRun).where(
                    WorkflowNodeRun.tenant_id == run.tenant_id,
                    WorkflowNodeRun.run_id == run.id,
                )
            )
        )
        .scalars()
        .all()
    )
    latest: dict[str, WorkflowNodeRun] = {}
    for row in rows:
        current = latest.get(row.node_id)
        if current is None or row.attempt >= current.attempt:
            latest[row.node_id] = row
    usage["nodes"] = sum(1 for row in latest.values() if row.status in SETTLED)
    # 工具调用按节点去重：重试失败的尝试不算一次成功调用。
    usage["tool_calls"] = len(
        {row.node_id for row in rows if row.kind == "tool" and row.status == "ok"}
    )
    children = (
        (
            await session.execute(
                select(AgentRun).where(
                    AgentRun.tenant_id == run.tenant_id,
                    AgentRun.workflow_run_id == run.id,
                )
            )
        )
        .scalars()
        .all()
    )
    usage["prompt_tokens"] = sum(
        int((child.usage or {}).get("prompt_tokens") or 0) for child in children
    )
    usage["completion_tokens"] = sum(
        int((child.usage or {}).get("completion_tokens") or 0)
        for child in children
    )
    usage["elapsed_ms"] = _duration(run.started_at, _now())
    usage.setdefault("paused_ms", 0)
    return usage


async def _fail_run(
    session: AsyncSession,
    run: WorkflowRun,
    code: str,
    message: str,
) -> None:
    moment = _now()
    await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run.id,
            WorkflowRun.tenant_id == run.tenant_id,
            WorkflowRun.status.in_(ACTIVE | {"waiting_approval"}),
        )
        .values(
            status="failed",
            error_code=code[:100],
            error_message=message[:500],
            finished_at=moment,
            heartbeat_at=moment,
        )
        .execution_options(synchronize_session=False)
    )


async def _fail(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    code: str,
    message: str,
) -> str:
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            return "missing"
        if run.status in TERMINAL:
            return run.status
        await _fail_run(session, run, code, message)
        await session.commit()
    return "failed"


def _merge_outputs(rows: list[WorkflowNodeRun]) -> tuple[list[dict], list]:
    """多个 output 节点都参与结果汇总；按 node_id 去重，保留首次出现。"""
    items: list[dict] = []
    artifacts: list = []
    seen_items: set[Any] = set()
    seen_artifacts: set[str] = set()
    for row in rows:
        payload = row.output if isinstance(row.output, dict) else {}
        for item in payload.get("items") or []:
            if not isinstance(item, dict):
                continue
            key = item.get("node_id")
            if key in seen_items:
                continue
            seen_items.add(key)
            items.append(item)
        for artifact in payload.get("artifacts") or []:
            key = json.dumps(artifact, sort_keys=True, ensure_ascii=False, default=str)
            if key in seen_artifacts:
                continue
            seen_artifacts.add(key)
            artifacts.append(artifact)
    return items[:MAX_RESULT_ITEMS], artifacts[:MAX_RESULT_ITEMS]


async def _finalize(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    deps: EngineDeps,
) -> str:
    async with factory() as session:
        run = await session.get(WorkflowRun, run_id)
        if run is None:
            return "missing"
        if run.status in TERMINAL:
            return run.status
        rows = (
            (
                await session.execute(
                    select(WorkflowNodeRun)
                    .where(
                        WorkflowNodeRun.tenant_id == run.tenant_id,
                        WorkflowNodeRun.run_id == run.id,
                        WorkflowNodeRun.kind == "output",
                        WorkflowNodeRun.status == "ok",
                    )
                    .order_by(WorkflowNodeRun.node_id, WorkflowNodeRun.attempt)
                )
            )
            .scalars()
            .all()
        )
        latest: dict[str, WorkflowNodeRun] = {}
        for row in rows:
            latest[row.node_id] = row
        items, artifacts = _merge_outputs(list(latest.values()))
        usage = await _refresh_usage(session, run)
        moment = _now()
        completed = await session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run.id,
                WorkflowRun.tenant_id == run.tenant_id,
                WorkflowRun.status.in_(ACTIVE | {"waiting_approval"}),
            )
            .values(
                status="succeeded",
                result={"items": items, "artifacts": artifacts},
                usage=usage,
                error_code="",
                error_message="",
                finished_at=moment,
                heartbeat_at=moment,
            )
            .execution_options(synchronize_session=False)
        )
        if completed.rowcount == 0:
            await session.rollback()
            return await _status_of(factory, run_id)
        await session.commit()
        await record_best_effort(
            session,
            tenant_id=run.tenant_id,
            action="workflow.run.complete",
            actor=str(run.triggered_by) if run.triggered_by else "scheduler",
            resource_type="workflow_run",
            resource_id=str(run.id),
            detail={"nodes": usage.get("nodes", 0)},
        )
    return "succeeded"


async def _requeue(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    *,
    deps: EngineDeps,
) -> str:
    """时间片用尽：落回 queued 并自我重新入队，不在本任务里硬跑。"""
    async with factory() as session:
        result = await session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.status.in_(ACTIVE),
            )
            .values(status="queued", heartbeat_at=_now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            await session.rollback()
            return await _status_of(factory, run_id)
        await session.commit()
    if deps.queue is not None:
        await deps.queue.enqueue_job("run_workflow_job", str(run_id))
    return "queued"
