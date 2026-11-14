"""arq worker：定时发布、媒体任务、Agent 运行与心跳对账。

定时执行覆盖「平台 + 内容类型」能力为自动发布的任务：公众号图文、
抖音视频、头条视频。人工导出平台与待接入平台不进入扫描。worker 作为
系统进程跨租户扫描到期任务是有意为之，与按当前用户过滤的 API 端点不同。
Agent 运行由 API 以 ``run_agent_job`` 入队执行，心跳对账与审批过期由定时
任务兜底。
"""

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from arq import cron
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.registry import ToolRegistry, get_tool_registry
from app.agent.runtime import expire_approvals, fail_run, run_agent
from app.config import get_settings
from app.db import SessionLocal
from app.errors import AppError
from app.llm.gateway import LLMGateway, load_gateway_default
from app.models.agent import AgentRun
from app.models.project import Content
from app.models.publish import PublishJob
from app.models.tenant import Tenant
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

logger = logging.getLogger(__name__)

STALE_PUBLISH_SECONDS = 900  # > worker job_timeout(600s)，避免误伤仍在执行的发布
STALE_PUBLISH_MESSAGE = "发布超时，结果未知；请到平台核对已有草稿后重试"
# 单批上限保证整批耗时远低于 job_timeout(600s)，剩余任务留到下一次扫描
MAX_BATCH_SIZE = 10
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

    任何逃逸异常都把运行落为 ``agent_internal_error``，绝不让行停留在
    ``running``；租户已停用时同样落失败且不执行任何工具。
    """
    # arq 运行时把 Redis 连接放在 ctx["redis"]；生产路径必须把它交给工具，
    # 否则媒体类工具无法二次入队。
    worker_queue = queue or (_ctx or {}).get("redis")
    async with session_factory() as session:
        run = await session.get(AgentRun, uuid.UUID(str(run_id)))
        if run is None or run.status != "queued":
            return
        tenant = await session.get(Tenant, run.tenant_id)
        if tenant is None or tenant.status != "active":
            await fail_run(session, run, "tenant_suspended", "租户已停用")
            return
        try:
            active_gateway = gateway or await load_gateway_default(
                session, run.tenant_id
            )
        except AppError as exc:
            await fail_run(session, run, exc.code, exc.message)
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
            await session.rollback()
            row = await session.get(AgentRun, uuid.UUID(str(run_id)))
            if row is not None and row.status in {
                "queued",
                "running",
                "waiting_approval",
            }:
                await fail_run(
                    session,
                    row,
                    "agent_internal_error",
                    "运行异常，请稍后重试",
                )


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
    """
    settings = get_settings()
    moment = now or datetime.now(UTC)
    stale_after = (
        settings.agent_run_stale_seconds
        if stale_after_seconds is None
        else stale_after_seconds
    )
    cutoff = moment - timedelta(seconds=stale_after)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(AgentRun).where(
                    AgentRun.status == "running",
                    or_(
                        AgentRun.heartbeat_at.is_(None),
                        AgentRun.heartbeat_at < cutoff,
                    ),
                )
            )
        ).scalars().all()
        targets = [row.id for row in rows]
        handled = 0
        for target_id in targets:
            run = await session.get(AgentRun, target_id)
            if run is None or run.status != "running":
                continue
            if run.attempts < 2 and queue is not None:
                run.attempts += 1
                run.status = "queued"
                run.heartbeat_at = moment
                await session.commit()
                try:
                    await queue.enqueue_job("run_agent_job", str(run.id))
                except Exception:
                    logger.exception("agent requeue failed run=%s", run.id)
                    await fail_run(
                        session,
                        run,
                        "agent_stale",
                        "运行超时未完成，请重试",
                    )
            else:
                await fail_run(
                    session,
                    run,
                    "agent_stale",
                    "运行超时未完成，请重试",
                )
            handled += 1
        # 审批过期与运行对账同一次定时任务完成：过期审批会终结等待中的运行。
        await expire_approvals(session)
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
    functions: ClassVar[list] = [process_media_job, run_agent_job]
    cron_jobs: ClassVar[list] = [
        cron(publish_due_jobs, second=0, job_id="publish-due-jobs"),
        cron(
            reconcile_agent_runs,
            second=30,
            job_id="agent-reconcile",
        ),
        cron(archive_audit_logs, hour=3, minute=15, job_id="audit-archive"),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    job_timeout = 600
