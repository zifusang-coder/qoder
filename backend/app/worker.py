"""arq 定时发布 worker。

定时执行只覆盖审核通过的公众号任务；小红书、知乎、快手、视频号等
手动导出平台仍由用户在导出流程中处理。worker 作为系统进程跨租户扫描
到期任务是有意为之，与按当前用户过滤的 API 端点不同。
"""

import logging
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import ClassVar

from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionLocal
from app.errors import AppError
from app.models.publish import PublishJob
from app.publish.base import PublisherAdapter
from app.publish.state import InvalidTransition
from app.publish.wechat import WeChatMPAdapter
from app.services.publish import (
    ContentChanged,
    JobNotFound,
    apply_action,
    execute_job,
)

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
}


async def list_due_jobs(
    session: AsyncSession,
    now: datetime,
    limit: int = MAX_BATCH_SIZE,
) -> list[PublishJob]:
    rows = (
        await session.execute(
            select(PublishJob)
            .where(
                PublishJob.status == "approved",
                PublishJob.platform == "wechat",
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
    logger.warning("定时发布任务无法执行，已标记失败 job=%s: %s", job_id, message)


async def publish_due_jobs(
    _ctx: dict | None = None,
    *,
    session_factory: Callable[[], AbstractAsyncContextManager[AsyncSession]] = SessionLocal,
    adapter_factory: Callable[[], PublisherAdapter] = WeChatMPAdapter,
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
        due = await list_due_jobs(session, current)
        if len(due) >= MAX_BATCH_SIZE:
            logger.warning(
                "到期发布任务达到单批上限 %s，剩余任务将在下一次扫描执行",
                MAX_BATCH_SIZE,
            )
        targets = [(job.id, job.tenant_id) for job in due]

    adapter = adapter_factory()
    executed = 0
    for job_id, tenant_id in targets:
        # 每个任务独立会话；状态机保证并发/重复扫描时只有一个执行者成功
        async with session_factory() as session:
            try:
                await execute_job(
                    session,
                    job_id,
                    adapter,
                    tenant_id=tenant_id,
                    actor="scheduler",
                )
                executed += 1
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
                if exc.code in BLOCKED_ERROR_CODES:
                    await _fail_blocked_job(
                        session, job_id, tenant_id=tenant_id, message=exc.message
                    )
                    continue
                logger.warning("定时发布任务执行失败 job=%s: %s", job_id, exc.message)
                await session.rollback()
            except Exception as exc:  # noqa: BLE001 - 未预期异常不能阻断同批后续任务
                # 适配器失败已由 execute_job 落库 publish_failed，这里只记录并继续
                logger.warning("定时发布任务执行失败 job=%s: %s", job_id, exc)
                await session.rollback()
    return executed


class WorkerSettings:
    # cron_jobs 会自动注册协程，functions 留空即可
    functions: ClassVar[list] = []
    cron_jobs: ClassVar[list] = [
        cron(publish_due_jobs, second=0, job_id="publish-due-jobs")
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    job_timeout = 600
