"""arq worker：定时发布、媒体任务、Agent 运行与心跳对账。

定时执行覆盖「平台 + 内容类型」能力为自动发布的任务：公众号图文、
抖音视频、头条视频。人工导出平台与待接入平台不进入扫描。worker 作为
系统进程跨租户扫描到期任务是有意为之，与按当前用户过滤的 API 端点不同。
Agent 运行由 API 以 ``run_agent_job`` 入队执行，心跳对账与审批过期由定时
任务兜底。
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from arq import cron
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.agent.policy import load_overrides, snapshot_policy
from app.agent.registry import ToolRegistry, get_tool_registry
from app.agent.runtime import (
    APPROVAL_EXPIRED,
    INTERNAL_ERROR,
    _expire_approval,
    fail_run,
    run_agent,
)
from app.agent.team import cancel_children, fail_team_run
from app.config import get_settings
from app.db import SessionLocal
from app.errors import AppError
from app.llm.gateway import LLMGateway, load_gateway_default
from app.models.agent import AgentApproval, AgentRun
from app.models.project import Content
from app.models.publish import PublishJob
from app.models.tenant import Tenant
from app.models.workflow import (
    Workflow,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowVersion,
)
from app.publish.adapters import adapter_for
from app.publish.base import PublishConfigError, PublisherAdapter, PublishError
from app.publish.registry import AUTO_PUBLISH_PAIRS
from app.publish.state import InvalidTransition
from app.services.audit import record_best_effort
from app.services.audit_retention import archive_expired_audit_logs
from app.services.media import run_media_job
from app.services.publish import (
    ContentChanged,
    JobNotFound,
    apply_action,
    execute_job,
)
from app.skills.registry import SkillRegistry, get_registry
from app.storage import StorageBackend, get_storage
from app.workflow import lifecycle
from app.workflow.engine import EngineDeps, run_workflow
from app.workflow.schedule import next_run_at, parse_cadence

logger = logging.getLogger(__name__)

STALE_PUBLISH_SECONDS = 900  # > worker job_timeout(600s)，避免误伤仍在执行的发布
STALE_PUBLISH_MESSAGE = "发布超时，结果未知；请到平台核对已有草稿后重试"
# 单批上限保证整批耗时远低于 job_timeout(600s)，剩余任务留到下一次扫描
MAX_BATCH_SIZE = 10
# Agent 对账与审批过期同样分批执行，避免一次定时任务处理无上限的行；
# 未处理完的留到下一次 cron。
AGENT_RECONCILE_BATCH_SIZE = 50
AGENT_APPROVAL_EXPIRY_BATCH_SIZE = 100
AGENT_STALE_MESSAGE = "运行超时未完成，请重试"
AGENT_INTERNAL_MESSAGE = "运行异常，请稍后重试"
# 只有这两个状态属于「仍在执行」，兜底失败只能改写它们；等待审批/终态
# 由对应的写入者负责，不能被崩溃兜底覆盖。
ACTIVE_AGENT_STATUSES = {"queued", "running"}
# 这些错误重试也不会成功，必须把任务落为失败，避免永远停留在 approved
BLOCKED_ERROR_CODES = {
    "too_many_attempts",
    "empty_content",
    "account_missing",
    "account_inactive",
    "account_platform_mismatch",
    "platform_content_mismatch",
    "platform_not_available",
}

WORKFLOW_INTERNAL_ERROR = "workflow_internal_error"
WORKFLOW_INTERNAL_MESSAGE = "工作流运行异常，请稍后重试"
WORKFLOW_QUEUE_MESSAGE = "队列暂不可用，请稍后重试"
WORKFLOW_HEARTBEAT_SECONDS = 15
WORKFLOW_SCHEDULE_BATCH_SIZE = 100
# 只有这两个状态属于「仍在执行」，心跳与崩溃兜底都只碰它们。
WORKFLOW_ACTIVE_STATUSES = {"queued", "running"}
WORKFLOW_RUNNING_STATUSES = ("queued", "running", "waiting_approval")


async def list_due_jobs(
    session: AsyncSession,
    now: datetime,
    limit: int = MAX_BATCH_SIZE,
) -> list[PublishJob]:
    auto_pairs = [
        and_(
            PublishJob.platform == platform,
            Content.kind == kind,
        )
        for platform, kind in AUTO_PUBLISH_PAIRS
    ]
    rows = (
        await session.execute(
            select(PublishJob)
            .join(
                Content,
                (Content.id == PublishJob.content_id)
                & (Content.tenant_id == PublishJob.tenant_id),
            )
            .join(Tenant, Tenant.id == PublishJob.tenant_id)
            .where(
                Tenant.status == "active",
                PublishJob.status == "approved",
                or_(*auto_pairs),
                PublishJob.scheduled_at.is_not(None),
                PublishJob.scheduled_at <= now,
            )
            .order_by(PublishJob.scheduled_at, PublishJob.created_at)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def reconcile_stale_publishing(
    session: AsyncSession,
    now: datetime,
    *,
    stale_after_seconds: int = STALE_PUBLISH_SECONDS,
) -> int:
    cutoff = now - timedelta(seconds=stale_after_seconds)
    rows = (
        await session.execute(
            select(PublishJob).where(
                PublishJob.status == "publishing",
                PublishJob.updated_at <= cutoff,
            )
        )
    ).scalars().all()
    targets = [(job.id, job.tenant_id) for job in rows]
    reconciled = 0
    for job_id, tenant_id in targets:
        try:
            await apply_action(
                session,
                job_id,
                "fail",
                tenant_id=tenant_id,
                actor="scheduler",
                note=STALE_PUBLISH_MESSAGE,
                fields={"error": STALE_PUBLISH_MESSAGE},
            )
            reconciled += 1
            await record_best_effort(
                session,
                tenant_id=tenant_id,
                action="publish.auto_fail",
                actor="scheduler",
                resource_type="publish_job",
                resource_id=str(job_id),
                detail={"reason": "stale_publish"},
            )
        except (InvalidTransition, JobNotFound):
            # 任务在 SELECT 与 fail 之间被其他执行者推进，属于正常竞态
            await session.rollback()
        except Exception:
            logger.exception("悬挂发布任务对账失败 job=%s", job_id)
            await session.rollback()
    return reconciled


async def _fail_blocked_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    tenant_id: str,
    message: str,
) -> None:
    """把确定无法执行的任务落为 publish_failed，避免永久停留在 approved。"""
    try:
        await apply_action(
            session,
            job_id,
            "fail",
            tenant_id=tenant_id,
            actor="scheduler",
            note=message[:500],
            fields={"error": message[:1000]},
        )
    except (InvalidTransition, JobNotFound):
        # 用户取消或另一执行者已推进状态，静默跳过
        await session.rollback()
        return
    except Exception:  # noqa: BLE001 - 状态写入失败只记录日志，不能阻断整批任务
        logger.warning("定时发布阻塞任务状态写入失败 job=%s", job_id)
        await session.rollback()
        return
    await record_best_effort(
        session,
        tenant_id=tenant_id,
        action="publish.auto_fail",
        actor="scheduler",
        resource_type="publish_job",
        resource_id=str(job_id),
        detail={"reason": message[:200]},
    )
    logger.warning("定时发布任务无法执行，已标记失败 job=%s: %s", job_id, message)


async def publish_due_jobs(
    _ctx: dict | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = SessionLocal,
    adapter_resolver: Callable[[str], PublisherAdapter] = adapter_for,
    storage: StorageBackend | None = None,
    now: datetime | None = None,
) -> int:
    current = now or datetime.now(UTC)
    async with session_factory() as session:
        try:
            reconciled = await reconcile_stale_publishing(session, current)
            if reconciled:
                logger.warning("对账悬挂发布任务 %s 个", reconciled)
        except Exception:
            logger.exception("悬挂发布任务对账失败")
            await session.rollback()
        # 多取一条用于判断是否仍有剩余任务，避免恰好一批时误报截断
        due = await list_due_jobs(session, current, limit=MAX_BATCH_SIZE + 1)
        truncated = len(due) > MAX_BATCH_SIZE
        if truncated:
            logger.warning(
                "到期发布任务达到单批上限 %s，剩余任务将在下一次扫描执行",
                MAX_BATCH_SIZE,
            )
        targets = [
            (job.id, job.tenant_id, job.platform)
            for job in due[:MAX_BATCH_SIZE]
        ]

    backend = storage or get_storage()
    executed = 0
    for job_id, tenant_id, platform in targets:
        # 每个任务独立会话；状态机保证并发/重复扫描时只有一个执行者成功
        async with session_factory() as session:
            try:
                adapter = adapter_resolver(platform)
            except PublishConfigError as exc:
                await _fail_blocked_job(
                    session, job_id, tenant_id=tenant_id, message=str(exc)
                )
                continue
            try:
                await execute_job(
                    session,
                    job_id,
                    adapter,
                    tenant_id=tenant_id,
                    actor="scheduler",
                    storage=backend,
                    require_due=current,
                )
                executed += 1
                await record_best_effort(
                    session,
                    tenant_id=tenant_id,
                    action="publish.auto_execute",
                    actor="scheduler",
                    resource_type="publish_job",
                    resource_id=str(job_id),
                )
            except ContentChanged as exc:
                await _fail_blocked_job(
                    session, job_id, tenant_id=tenant_id, message=str(exc)
                )
            except AppError as exc:
                if exc.code == "invalid_state":
                    # 用户取消或另一执行者已抢占，属于正常竞态
                    logger.info("定时发布任务已被其他路径处理，跳过 job=%s", job_id)
                    await session.rollback()
                    continue
                if exc.code == "not_due":
                    # 选中后用户重排发布时间，留待下一次扫描
                    logger.info("定时发布任务已改期，跳过 job=%s", job_id)
                    await session.rollback()
                    continue
                if exc.code in BLOCKED_ERROR_CODES:
                    await _fail_blocked_job(
                        session, job_id, tenant_id=tenant_id, message=exc.message
                    )
                    continue
                logger.warning("定时发布任务执行失败 job=%s: %s", job_id, exc.message)
                await session.rollback()
            except Exception as exc:
                # 未预期异常不能阻断同批后续任务
                if isinstance(exc, PublishError):
                    # 适配器失败已由 execute_job 落库 publish_failed，只记录即可
                    logger.warning("定时发布失败 job=%s error=%s", job_id, exc)
                else:
                    logger.exception("定时发布异常 job=%s", job_id)
                await session.rollback()
    return executed


async def process_media_job(_ctx: dict | None, job_id: str) -> None:
    async with SessionLocal() as session:
        try:
            await run_media_job(
                session=session,
                job_id=job_id,
                registry=get_registry(),
                storage=get_storage(),
            )
        except Exception:
            # run_media_job 已把业务失败写入任务行；这里只兜底基础设施异常
            logger.exception("媒体任务执行异常 job=%s", job_id)


async def _fail_active_agent_run(
    session: AsyncSession,
    run_uuid: uuid.UUID,
    run_id: str,
) -> None:
    """Best-effort terminal state for a crash outside the main run loop.

    Only ``queued``/``running`` rows are rewritten: a run that paused for
    approval or already reached a terminal state belongs to another writer and
    must keep its state. Failures here are logged, never re-raised, so the job
    itself ends cleanly.
    """
    try:
        await session.rollback()
        row = await session.get(AgentRun, run_uuid)
        if row is not None and row.status in ACTIVE_AGENT_STATUSES:
            await fail_run(session, row, INTERNAL_ERROR, AGENT_INTERNAL_MESSAGE)
    except Exception:
        logger.exception("agent run failure could not be recorded run=%s", run_id)


async def _enqueue_workflow(queue: ArqRedis | None, run_id: uuid.UUID) -> None:
    if queue is None:
        raise AppError("queue_unavailable", WORKFLOW_QUEUE_MESSAGE, 503)
    try:
        await queue.enqueue_job("run_workflow_job", str(run_id))
    except Exception:
        logger.exception("workflow enqueue failed run=%s", run_id)
        raise AppError(
            "queue_unavailable", WORKFLOW_QUEUE_MESSAGE, 503
        ) from None


async def run_agent_job(
    _ctx: dict | None,
    run_id: str,
    *,
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ] = SessionLocal,
    gateway: LLMGateway | None = None,
    tool_registry: ToolRegistry | None = None,
    skill_registry: SkillRegistry | None = None,
    storage: StorageBackend | None = None,
    queue: ArqRedis | None = None,
) -> None:
    """执行一个已入队的 Agent 运行。

    只有 ``queued`` 的运行会被执行：``run_agent`` 内部以 ``queued → running``
    的条件更新抢占，重复投递或已被其他执行者接手的运行在这里直接返回，保证
    arq 至少一次投递下的幂等。``running`` 的运行不在这里重入，交给心跳对账
    决定重排或失败，避免重复执行与尝试次数被重复消耗。

    任何逃逸异常都把仍处于 ``queued``/``running`` 的运行落为
    ``agent_internal_error``；等待审批或已终态的运行绝不被兜底改写。租户
    已停用时同样落失败且不执行任何工具。
    """
    # arq 运行时把 Redis 连接放在 ctx["redis"]；生产路径必须把它交给工具，
    # 否则媒体类工具无法二次入队。
    worker_queue = queue or (_ctx or {}).get("redis")
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (AttributeError, TypeError, ValueError):
        logger.warning("agent run id 不合法，跳过 run=%s", run_id)
        return
    async with session_factory() as session:
        # 租户校验、状态读取和网关解析都在主循环之前完成；任何非 AppError
        # 的失败也必须把仍处于 queued 的运行落为内部错误，不能让它永远排队。
        try:
            run = await session.get(AgentRun, run_uuid)
            if run is None or run.status != "queued":
                return
            tenant = await session.get(Tenant, run.tenant_id)
            if tenant is None or tenant.status != "active":
                await fail_run(session, run, "tenant_suspended", "租户已停用")
                return
        except Exception:
            logger.exception("agent run setup failed run=%s", run_id)
            await _fail_active_agent_run(session, run_uuid, run_id)
            return
        try:
            active_gateway = gateway or await load_gateway_default(
                session, run.tenant_id
            )
        except AppError as exc:
            await fail_run(session, run, exc.code, exc.message)
            return
        except Exception:
            logger.exception("agent gateway setup failed run=%s", run_id)
            await _fail_active_agent_run(session, run_uuid, run_id)
            return
        try:
            await run_agent(
                session,
                run,
                gateway=active_gateway,
                tool_registry=tool_registry or get_tool_registry(),
                skill_registry=skill_registry or get_registry(),
                storage=storage or get_storage(),
                queue=worker_queue,
                settings=get_settings(),
                session_factory=session_factory,
            )
        except Exception:
            logger.exception("agent run crashed run=%s", run.id)
            await _fail_active_agent_run(session, run_uuid, run_id)


async def _heartbeat_loop(
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ],
    run_id: uuid.UUID,
    *,
    interval: float,
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            async with session_factory() as session:
                await session.execute(
                    update(WorkflowRun)
                    .where(
                        WorkflowRun.id == run_id,
                        WorkflowRun.status.in_(WORKFLOW_ACTIVE_STATUSES),
                    )
                    .values(heartbeat_at=datetime.now(UTC))
                    .execution_options(synchronize_session=False)
                )
                await session.commit()
        except Exception:  # noqa: BLE001 - 心跳失败不能打断运行本身
            logger.warning("workflow heartbeat failed run=%s", run_id)


async def _fail_active_workflow_run(
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ],
    run_uuid: uuid.UUID,
    run_id: str,
) -> None:
    """把崩溃后仍处于 queued/running 的运行落为失败。

    ``waiting_approval`` 与其他终态属于别的写入者，``fail_active_run``
    只改 queued/running，因此这里可以无条件调用。
    """
    try:
        async with session_factory() as session:
            await lifecycle.fail_active_run(
                session,
                run_uuid,
                error_code=WORKFLOW_INTERNAL_ERROR,
                message=WORKFLOW_INTERNAL_MESSAGE,
                actor="worker",
            )
    except Exception:  # 兜底写入失败只记录日志
        logger.exception(
            "workflow run failure could not be recorded run=%s", run_id
        )


async def run_workflow_job(
    _ctx: dict | None = None,
    run_id: str | None = None,
    *,
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ] = SessionLocal,
    gateway: LLMGateway | None = None,
    tool_registry: ToolRegistry | None = None,
    skill_registry: SkillRegistry | None = None,
    storage: StorageBackend | None = None,
    queue: ArqRedis | None = None,
    heartbeat_seconds: float = WORKFLOW_HEARTBEAT_SECONDS,
) -> None:
    """执行一个已入队的工作流运行。

    只有 ``queued`` 会被领取：``lifecycle.claim_run`` 的条件更新保证 arq
    至少一次投递下的幂等，重复投递或已被其他执行者接手的运行直接返回。
    网关缺失不终结运行（没有 Agent 节点的工作流不需要网关），任何逃逸
    异常把仍处于 queued/running 的运行落 ``workflow_internal_error``。
    """
    worker_queue = queue or (_ctx or {}).get("redis")
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (AttributeError, TypeError, ValueError):
        logger.warning("workflow run id 不合法，跳过 run=%s", run_id)
        return
    tenant_id: str | None = None
    async with session_factory() as session:
        try:
            run = await session.get(WorkflowRun, run_uuid)
            if run is None or run.status != "queued":
                return
            tenant_id = run.tenant_id
            tenant = await session.get(Tenant, run.tenant_id)
            if tenant is None or tenant.status != "active":
                await lifecycle.fail_active_run(
                    session,
                    run_uuid,
                    error_code="tenant_suspended",
                    message="租户已停用",
                    actor="worker",
                )
                return
            if await lifecycle.claim_run(session, run_uuid) != "claimed":
                return
        except Exception:
            logger.exception("workflow run setup failed run=%s", run_id)
            # Release this session's transaction before the fallback opens a
            # second one; otherwise the failed write can keep the row locked
            # and the fallback never records the failure.
            with contextlib.suppress(Exception):
                await session.rollback()
            await _fail_active_workflow_run(
                session_factory, run_uuid, str(run_id)
            )
            return

    active_gateway = gateway
    if active_gateway is None and tenant_id is not None:
        try:
            async with session_factory() as session:
                active_gateway = await load_gateway_default(
                    session, tenant_id
                )
        except AppError:
            # 没有 Agent 节点的工作流不需要网关；需要时由 Agent 节点自己
            # 落 llm_not_configured。
            active_gateway = None
        except Exception:
            logger.exception("workflow gateway setup failed run=%s", run_id)
            await _fail_active_workflow_run(
                session_factory, run_uuid, str(run_id)
            )
            return

    deps = EngineDeps(
        gateway=active_gateway,
        tool_registry=tool_registry or get_tool_registry(),
        skill_registry=skill_registry or get_registry(),
        storage=storage or get_storage(),
        queue=worker_queue,
        settings=get_settings(),
        session_factory=session_factory,
    )
    beat = asyncio.create_task(
        _heartbeat_loop(
            session_factory, run_uuid, interval=heartbeat_seconds
        )
    )
    try:
        await run_workflow(session_factory, run_uuid, deps=deps)
    except Exception:
        logger.exception("workflow run crashed run=%s", run_uuid)
        await _fail_active_workflow_run(session_factory, run_uuid, str(run_uuid))
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat


async def _schedule_skip_reason(
    session: AsyncSession, schedule: WorkflowSchedule
) -> str | None:
    tenant = await session.get(Tenant, schedule.tenant_id)
    if tenant is None or tenant.status != "active":
        return "tenant_suspended"
    workflow = await session.get(Workflow, schedule.workflow_id)
    if workflow is None or workflow.archived_at is not None:
        return "workflow_archived"
    version = await session.get(WorkflowVersion, schedule.workflow_version_id)
    if version is None:
        return "version_missing"
    active = (
        await session.execute(
            select(WorkflowRun.id)
            .where(
                WorkflowRun.tenant_id == schedule.tenant_id,
                WorkflowRun.workflow_id == schedule.workflow_id,
                WorkflowRun.status.in_(WORKFLOW_RUNNING_STATUSES),
            )
            .limit(1)
        )
    ).first()
    return "active_run" if active is not None else None


async def _audit_schedule_skip(
    session: AsyncSession, schedule: WorkflowSchedule, reason: str
) -> None:
    await record_best_effort(
        session,
        tenant_id=schedule.tenant_id,
        action="workflow.schedule.skipped",
        actor="scheduler",
        resource_type="workflow_schedule",
        resource_id=str(schedule.id),
        detail={"workflow": str(schedule.workflow_id), "reason": reason},
    )


async def _fire_schedule(
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ],
    schedule_id: uuid.UUID,
    *,
    queue: ArqRedis | None,
    moment: datetime,
) -> bool:
    """消费一个到期的定时：先推进计划时刻，再决定建不建运行。"""
    async with session_factory() as session:
        schedule = await session.get(WorkflowSchedule, schedule_id)
        if (
            schedule is None
            or not schedule.enabled
            or schedule.next_run_at is None
        ):
            return False
        try:
            following = next_run_at(parse_cadence(schedule.cadence), moment)
        except AppError as exc:
            # API 已校验 cadence；坏数据只记录，不阻断同批其他定时。
            logger.warning(
                "workflow schedule cadence invalid schedule=%s: %s",
                schedule.id,
                exc.message,
            )
            await session.rollback()
            return False
        slot = schedule.next_run_at
        advanced = await session.execute(
            update(WorkflowSchedule)
            .where(
                WorkflowSchedule.id == schedule.id,
                WorkflowSchedule.tenant_id == schedule.tenant_id,
                WorkflowSchedule.enabled.is_(True),
                WorkflowSchedule.next_run_at == slot,
            )
            .values(next_run_at=following, last_run_at=slot)
            .execution_options(synchronize_session=False)
        )
        if advanced.rowcount == 0:
            # 另一个 worker 已消费该触发时刻。
            await session.rollback()
            return False
        await session.commit()

        skip = await _schedule_skip_reason(session, schedule)
        if skip is not None:
            await _audit_schedule_skip(session, schedule, skip)
            return False

        run = WorkflowRun(
            tenant_id=schedule.tenant_id,
            workflow_id=schedule.workflow_id,
            workflow_version_id=schedule.workflow_version_id,
            trigger="schedule",
            triggered_by=None,
            schedule_id=schedule.id,
            scheduled_for=slot,
            status="queued",
            inputs=dict(schedule.inputs or {}),
            policy=snapshot_policy(
                await load_overrides(session, schedule.tenant_id),
                {spec.name: spec for spec in get_tool_registry().all()},
            ),
            heartbeat_at=moment,
        )
        session.add(run)
        try:
            await session.commit()
        except IntegrityError:
            # 同一 (schedule_id, scheduled_for) 已存在：并发触发，保留先到者。
            await session.rollback()
            return False
        await session.refresh(run)

        try:
            await _enqueue_workflow(queue, run.id)
        except AppError:
            await session.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run.id,
                    WorkflowRun.status == "queued",
                )
                .values(
                    status="failed",
                    error_code="queue_unavailable",
                    error_message=WORKFLOW_QUEUE_MESSAGE,
                    finished_at=moment,
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            return False

        await record_best_effort(
            session,
            tenant_id=schedule.tenant_id,
            action="workflow.schedule.fire",
            actor="scheduler",
            resource_type="workflow_run",
            resource_id=str(run.id),
            detail={
                "schedule": str(schedule.id),
                "scheduled_for": slot.isoformat(),
            },
        )
        return True


async def fire_due_workflow_schedules(
    _ctx: dict | None = None,
    *,
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ] = SessionLocal,
    queue: ArqRedis | None = None,
    now: datetime | None = None,
) -> int:
    """每分钟触发一次：消费所有到期的定时并创建运行。

    单条定时失败不阻断同批其他定时；每条定时独立会话，推进计划时刻与
    创建运行不共享事务（进程在两者之间崩溃只会漏掉一次，不会重复触发）。
    """
    moment = now or datetime.now(UTC)
    worker_queue = queue or (_ctx or {}).get("redis")
    async with session_factory() as session:
        due = (
            (
                await session.execute(
                    select(WorkflowSchedule.id)
                    .where(
                        WorkflowSchedule.enabled.is_(True),
                        WorkflowSchedule.next_run_at.is_not(None),
                        WorkflowSchedule.next_run_at <= moment,
                    )
                    .order_by(
                        WorkflowSchedule.next_run_at, WorkflowSchedule.id
                    )
                    .limit(WORKFLOW_SCHEDULE_BATCH_SIZE)
                )
            )
            .scalars()
            .all()
        )
    fired = 0
    for schedule_id in due:
        try:
            if await _fire_schedule(
                session_factory, schedule_id, queue=worker_queue, moment=moment
            ):
                fired += 1
        except Exception:  # 单条失败不阻断同批
            logger.exception("workflow schedule fire failed id=%s", schedule_id)
    return fired


async def reconcile_workflow_runs(
    _ctx: dict | None = None,
    *,
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ] = SessionLocal,
    queue: ArqRedis | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """心跳对账：重排超时运行、终结无望运行、过期审批。

    状态迁移全部在 ``lifecycle.reconcile`` 内完成，worker 只负责把重排回
    ``queued`` 的运行投递出去。入队失败（含没有队列）立即落
    ``queue_unavailable``，避免运行停在 queued 永远没人执行。
    """
    worker_queue = queue or (_ctx or {}).get("redis")

    async def wake(run_id: uuid.UUID) -> None:
        if worker_queue is None:
            async with session_factory() as session:
                await lifecycle.fail_active_run(
                    session,
                    run_id,
                    error_code="queue_unavailable",
                    message=WORKFLOW_QUEUE_MESSAGE,
                    actor="worker",
                )
            return
        try:
            await worker_queue.enqueue_job("run_workflow_job", str(run_id))
        except Exception:
            logger.exception("workflow requeue failed run=%s", run_id)
            async with session_factory() as session:
                await lifecycle.fail_active_run(
                    session,
                    run_id,
                    error_code="queue_unavailable",
                    message=WORKFLOW_QUEUE_MESSAGE,
                    actor="worker",
                )

    return await lifecycle.reconcile(
        session_factory,
        settings=get_settings(),
        now=now,
        on_requeue=wake,
    )


async def _stale_agent_run_ids(
    session: AsyncSession,
    cutoff: datetime,
    limit: int,
) -> list[uuid.UUID]:
    rows = await session.execute(
        select(AgentRun.id)
        .where(
            AgentRun.status == "running",
            # role 子运行由团队编排器驱动，心跳对账只处理独立/团队运行，
            # 避免把等待 Lead 调度的子运行误判为崩溃并重排。
            AgentRun.mode != "role",
            or_(
                AgentRun.heartbeat_at.is_(None),
                AgentRun.heartbeat_at < cutoff,
            ),
        )
        .order_by(AgentRun.heartbeat_at, AgentRun.created_at)
        .limit(limit)
    )
    return list(rows.scalars().all())


async def _reconcile_agent_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    moment: datetime,
    cutoff: datetime,
    queue: ArqRedis | None,
) -> bool:
    """Requeue or fail one stale run; return whether this sweep handled it.

    The requeue is a compare-and-set (``WHERE status='running' AND heartbeat
    still stale``): a user cancel or a fresh heartbeat that lands between the
    scan and this UPDATE makes ``rowcount`` zero, in which case the row is left
    untouched and nothing is enqueued.
    """
    run = await session.get(AgentRun, run_id)
    if run is None or run.status != "running":
        return False
    stale = or_(
        AgentRun.heartbeat_at.is_(None), AgentRun.heartbeat_at < cutoff
    )
    if queue is not None and run.attempts < 2:
        requeued = await session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.status == "running",
                stale,
                AgentRun.attempts < 2,
            )
            .values(
                status="queued",
                attempts=AgentRun.attempts + 1,
                heartbeat_at=moment,
            )
            .execution_options(synchronize_session=False)
        )
        if requeued.rowcount == 0:
            # 用户取消或新的心跳先到：不要覆盖既有状态，也不要入队。
            await session.rollback()
            return False
        await session.commit()
        await session.refresh(run)
        try:
            await queue.enqueue_job("run_agent_job", str(run.id))
        except Exception:
            logger.exception("agent requeue failed run=%s", run.id)
            await session.refresh(run)
            if run.status == "queued":
                await fail_run(
                    session, run, "agent_stale", AGENT_STALE_MESSAGE
                )
        return True

    conditions = [
        AgentRun.id == run_id,
        AgentRun.status == "running",
        stale,
    ]
    if queue is not None:
        # 队列存在但重排次数已用尽。
        conditions.append(AgentRun.attempts >= 2)
    claimed = await session.execute(
        update(AgentRun)
        .where(*conditions)
        .values(status="failed")
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount == 0:
        # 同一竞态：运行已不是待对账的 running 行，交给它的写入者。
        await session.rollback()
        return False
    await session.refresh(run)
    await fail_run(session, run, "agent_stale", AGENT_STALE_MESSAGE)
    return True


async def _expire_due_agent_approvals(
    session: AsyncSession,
    *,
    moment: datetime,
    limit: int = AGENT_APPROVAL_EXPIRY_BATCH_SIZE,
) -> int:
    """Expire a bounded batch of pending approvals, isolating each row.

    Reuses the runtime's per-approval compare-and-set/orchestration instead of
    duplicating its semantics; the worker only supplies the bounded selection
    and the per-row error boundary.
    """
    settings = get_settings()
    cutoff = moment - timedelta(hours=settings.agent_approval_ttl_hours)
    targets = (
        await session.execute(
            select(AgentApproval.id)
            .where(
                AgentApproval.status == "pending",
                or_(
                    AgentApproval.expires_at < moment,
                    and_(
                        AgentApproval.expires_at.is_(None),
                        AgentApproval.created_at < cutoff,
                    ),
                ),
            )
            .order_by(AgentApproval.expires_at, AgentApproval.created_at)
            .limit(limit)
        )
    ).scalars().all()
    expired = 0
    for approval_id in targets:
        try:
            approval = await session.get(AgentApproval, approval_id)
            if approval is None:
                continue
            if await _expire_approval(session, approval, moment):
                expired += 1
        except Exception:
            logger.exception("agent approval expiry failed approval=%s", approval_id)
            await session.rollback()
    return expired


async def _team_pending_count(session: AsyncSession, parent_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AgentApproval)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .where(
                AgentRun.parent_run_id == parent_id,
                AgentApproval.status == "pending",
            )
        )
    ).scalar_one()


async def _team_approved_count(session: AsyncSession, parent_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AgentApproval)
            .join(AgentRun, AgentRun.id == AgentApproval.run_id)
            .where(
                AgentRun.parent_run_id == parent_id,
                AgentApproval.status == "approved",
            )
        )
    ).scalar_one()


async def _settle_waiting_team_parents(
    session: AsyncSession,
    *,
    moment: datetime,
    queue: ArqRedis | None,
    limit: int = 100,
) -> int:
    """结算零 pending 的等待团队父运行：有 approved 重排，否则判审批过期。

    与 ``lifecycle.requeue_settled_orphan`` 同一口径：行锁 + 谓词重评估，
    等待时长按 ``heartbeat_at``（挂起时刻）计入 ``paused_ms``；CAS 落败或
    仍有 pending 时回滚释放锁，交给下一次对账。
    """
    ids = (
        await session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.mode == "team",
                AgentRun.status == "waiting_approval",
            )
            .order_by(AgentRun.updated_at)
            .limit(limit)
        )
    ).scalars().all()
    handled = 0
    for run_id in ids:
        parent = (
            await session.execute(
                select(AgentRun)
                .where(AgentRun.id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if parent is None or parent.status != "waiting_approval":
            await session.rollback()
            continue
        # 先取标量：rollback 会过期 session 内全部实例，之后读 ORM 属性
        # 会触发 MissingGreenlet（与 lifecycle._fail_orphans 同一口径）。
        parent_id = parent.id
        if await _team_pending_count(session, parent_id) > 0:
            await session.rollback()
            continue
        if await _team_approved_count(session, parent_id) == 0:
            await session.rollback()
            await fail_team_run(
                session, parent_id,
                error_code="approval_expired",
                message=APPROVAL_EXPIRED,
            )
            handled += 1
            continue
        paused_at = parent.heartbeat_at or moment
        if paused_at.tzinfo is None:
            paused_at = paused_at.replace(tzinfo=UTC)
        waited_ms = int(
            max(0.0, (moment - min(paused_at, moment)).total_seconds()) * 1000
        )
        usage = dict(parent.usage or {})
        usage["paused_ms"] = int(usage.get("paused_ms") or 0) + waited_ms
        resumed = await session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == parent.id,
                AgentRun.status == "waiting_approval",
            )
            .values(status="queued", usage=usage, heartbeat_at=moment)
            .execution_options(synchronize_session=False)
        )
        if resumed.rowcount == 0:
            await session.rollback()
            continue
        await session.commit()
        if queue is not None:
            try:
                await queue.enqueue_job("run_agent_job", str(parent.id))
            except Exception:
                logger.exception("team requeue enqueue failed run=%s", parent.id)
        handled += 1
    return handled


async def _close_terminal_team_stragglers(
    session: AsyncSession,
    *,
    limit: int = 100,
) -> int:
    """终态团队仍有活跃子运行时，复用 cancel_children 收敛残留。"""
    child = aliased(AgentRun)
    ids = (
        await session.execute(
            select(AgentRun.id)
            .where(
                AgentRun.mode == "team",
                AgentRun.status.in_(("succeeded", "failed", "cancelled")),
                exists().where(
                    child.parent_run_id == AgentRun.id,
                    child.status.in_(("queued", "running", "waiting_approval")),
                ),
            )
            .limit(limit)
        )
    ).scalars().all()
    handled = 0
    for run_id in ids:
        parent = await session.get(AgentRun, run_id)
        if parent is None:
            continue
        await cancel_children(session, parent.id, parent.tenant_id)
        handled += 1
    return handled


async def reconcile_agent_runs(
    _ctx: dict | None = None,
    *,
    session_factory: Callable[
        [], AbstractAsyncContextManager[AsyncSession]
    ] = SessionLocal,
    queue: ArqRedis | None = None,
    now: datetime | None = None,
    stale_after_seconds: int | None = None,
) -> int:
    """心跳对账：重排超时运行、终结无望运行、过期审批。

    扫描 ``running`` 且心跳早于 ``agent_run_stale_seconds`` 的运行，``attempts``
    未达 2 次时重排（``attempts`` 记录对账重入队次数）并再次入队 ``run_agent_job``，
    否则落 ``agent_stale``。``waiting_approval`` 是等人工决策，不在扫描范围；
    已取消/已完成的运行也不会被这里改写。对账跨租户扫描是 worker 的职责，
    但每条运行只写自己的行，不做跨租户写入。

    arq 只会传入 ``ctx``，因此队列优先取 ``ctx["redis"]``（与 ``run_agent_job``
    一致）；只有确实拿不到队列时才直接落 ``agent_stale``。运行对账与审批
    过期各自使用独立会话和错误边界，任一阶段失败都不会阻断另一阶段。
    """
    settings = get_settings()
    moment = now or datetime.now(UTC)
    stale_after = (
        settings.agent_run_stale_seconds
        if stale_after_seconds is None
        else stale_after_seconds
    )
    cutoff = moment - timedelta(seconds=stale_after)
    worker_queue = queue or (_ctx or {}).get("redis")
    handled = 0
    try:
        async with session_factory() as session:
            targets = await _stale_agent_run_ids(
                session, cutoff, AGENT_RECONCILE_BATCH_SIZE
            )
            for run_id in targets:
                try:
                    if await _reconcile_agent_run(
                        session,
                        run_id,
                        moment=moment,
                        cutoff=cutoff,
                        queue=worker_queue,
                    ):
                        handled += 1
                except Exception:
                    # 单行失败不能中断其余运行；回滚后继续下一行。
                    logger.exception("agent reconcile failed run=%s", run_id)
                    await session.rollback()
    except Exception:
        logger.exception("agent reconciliation sweep failed")
    # 审批过期是独立阶段：即使上面的扫描/重排抛出，也仍会执行；它自己的
    # 异常也不会影响已经完成的运行对账。
    try:
        async with session_factory() as session:
            await _expire_due_agent_approvals(session, moment=moment)
    except Exception:
        logger.exception("agent approval expiry sweep failed")
    # 团队对账同样是独立阶段：结算零 pending 的等待父运行，并收敛终态
    # 团队遗留的活跃子运行；任一失败不影响前面两个阶段的结果。
    try:
        async with session_factory() as session:
            settled = await _settle_waiting_team_parents(
                session, moment=moment, queue=worker_queue,
            )
            closed = await _close_terminal_team_stragglers(session)
            if settled or closed:
                logger.info("团队对账：结算 %s 个等待父运行，收敛 %s 个残留", settled, closed)
    except Exception:
        logger.exception("team reconciliation sweep failed")
    return handled


async def archive_audit_logs(
    _ctx: dict | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = SessionLocal,
    storage: StorageBackend | None = None,
    retention_days: int | None = None,
) -> int:
    settings = get_settings()
    days = settings.audit_retention_days if retention_days is None else retention_days
    if days <= 0:
        return 0
    backend = storage or get_storage()
    async with session_factory() as session:
        try:
            archived = await archive_expired_audit_logs(
                session, backend, retention_days=days
            )
        except Exception:
            # 归档失败不影响次日重试；过期数据保留比丢失更安全
            logger.exception("审计日志归档失败")
            await session.rollback()
            return 0
    if archived:
        logger.info("已归档过期审计日志 %s 条，保留期 %s 天", archived, days)
    return archived


class WorkerSettings:
    functions: ClassVar[list] = [
        process_media_job,
        run_agent_job,
        run_workflow_job,
    ]
    cron_jobs: ClassVar[list] = [
        cron(publish_due_jobs, second=0, job_id="publish-due-jobs"),
        cron(
            fire_due_workflow_schedules,
            second=15,
            job_id="workflow-schedules",
        ),
        cron(reconcile_agent_runs, second=30, job_id="agent-reconcile"),
        cron(
            reconcile_workflow_runs,
            second=45,
            job_id="workflow-reconcile",
        ),
        cron(archive_audit_logs, hour=3, minute=15, job_id="audit-archive"),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    job_timeout = 600
