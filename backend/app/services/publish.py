import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Content
from app.models.publish import PublishEvent, PublishJob
from app.publish.state import next_status

RESCHEDULABLE = {"draft", "prechecking", "precheck_failed", "awaiting_review", "approved"}


class JobNotFound(LookupError):
    pass


class ContentChanged(ValueError):
    pass


class JobNotReschedulable(ValueError):
    pass


async def _locked_job(
    session: AsyncSession, job_id: uuid.UUID, tenant_id: str
) -> PublishJob:
    job = (
        await session.execute(
            select(PublishJob)
            .where(PublishJob.id == job_id, PublishJob.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        raise JobNotFound("发布任务不存在")
    return job


async def apply_action(
    session: AsyncSession,
    job_id: uuid.UUID,
    action: str,
    *,
    tenant_id: str,
    actor: str = "user",
    note: str = "",
) -> PublishJob:
    job = await _locked_job(session, job_id, tenant_id)
    if action == "publish":
        content = (
            await session.execute(
                select(Content)
                .where(Content.id == job.content_id, Content.tenant_id == tenant_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if content is None or content.current_version != job.content_version:
            raise ContentChanged("内容已更新，请基于最新版本重新创建发布任务")
    current = job.status
    target = next_status(current, action)
    job.status = target
    session.add(
        PublishEvent(
            tenant_id=tenant_id,
            job_id=job.id,
            from_status=current,
            to_status=target,
            action=action,
            note=note,
            actor=actor,
        )
    )
    await session.commit()
    await session.refresh(job)
    return job


async def apply_precheck(
    session: AsyncSession,
    job_id: uuid.UUID,
    report: dict,
    *,
    tenant_id: str,
    actor: str,
) -> PublishJob:
    job = await _locked_job(session, job_id, tenant_id)
    status = job.status
    if status != "prechecking":
        target = next_status(status, "submit")
        session.add(
            PublishEvent(
                tenant_id=job.tenant_id,
                job_id=job.id,
                from_status=status,
                to_status=target,
                action="submit",
                note="发布前检查",
                actor=actor,
            )
        )
        status = target
    action = "pass" if report["passed"] else "fail"
    target = next_status(status, action)
    session.add(
        PublishEvent(
            tenant_id=job.tenant_id,
            job_id=job.id,
            from_status=status,
            to_status=target,
            action=action,
            note="检查通过" if report["passed"] else "检查未通过",
            actor=actor,
        )
    )
    job.status = target
    job.checks = report
    await session.commit()
    await session.refresh(job)
    return job


async def reopen(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    tenant_id: str,
    actor: str,
    scheduled_at: datetime | None,
) -> PublishJob:
    job = await _locked_job(session, job_id, tenant_id)
    current = job.status
    job.status = next_status(current, "reopen")
    job.attempts = 0
    job.error = ""
    job.export_path = ""
    job.external_id = ""
    job.external_url = ""
    job.published_at = None
    job.checks = {}
    job.scheduled_at = scheduled_at
    session.add(
        PublishEvent(
            tenant_id=tenant_id,
            job_id=job.id,
            from_status=current,
            to_status=job.status,
            action="reopen",
            actor=actor,
        )
    )
    await session.commit()
    await session.refresh(job)
    return job


async def reschedule(
    session: AsyncSession,
    job_id: uuid.UUID,
    scheduled_at: datetime | None,
    *,
    tenant_id: str,
) -> PublishJob:
    job = await _locked_job(session, job_id, tenant_id)
    if job.status not in RESCHEDULABLE:
        raise JobNotReschedulable("任务已开始执行，无法修改定时时间")
    job.scheduled_at = scheduled_at
    await session.commit()
    await session.refresh(job)
    return job
