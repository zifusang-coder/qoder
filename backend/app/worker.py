"""arq 定时发布 worker。

定时执行只覆盖审核通过的公众号任务；小红书、知乎、快手、视频号等
手动导出平台仍由用户在导出流程中处理。worker 作为系统进程跨租户扫描
到期任务是有意为之，与按当前用户过滤的 API 端点不同。
"""

import logging
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
from app.models.publish import PublishJob
from app.publish.base import PublisherAdapter
from app.publish.wechat import WeChatMPAdapter
from app.services.publish import apply_action, execute_job

logger = logging.getLogger(__name__)

STALE_PUBLISH_SECONDS = 900  # > worker job_timeout(300s)，避免误伤仍在执行的发布
STALE_PUBLISH_MESSAGE = "发布超时，结果未知；请到平台核对已有草稿后重试"


async def list_due_jobs(session: AsyncSession, now: datetime) -> list[PublishJob]:
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
            except Exception:
                logger.exception("定时发布失败 job=%s", job_id)
                await session.rollback()
    return executed


class WorkerSettings:
    functions: ClassVar[list] = [publish_due_jobs]
    cron_jobs: ClassVar[list] = [cron(publish_due_jobs, second=0)]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = 3
    job_timeout = 300
