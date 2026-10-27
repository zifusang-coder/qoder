import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.project import Content, ContentVersion
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.publish.base import PublisherAdapter, PublishError
from app.publish.state import next_status

logger = logging.getLogger(__name__)

RESCHEDULABLE = {"draft", "prechecking", "precheck_failed", "awaiting_review", "approved"}
MUTABLE_JOB_FIELDS = {
    "attempts",
    "error",
    "external_id",
    "external_url",
    "published_at",
    "export_path",
}
FIELD_LIMITS = {
    "error": 1000,
    "external_id": 200,
    "external_url": 1000,
    "export_path": 1000,
}
MAX_PUBLISH_ATTEMPTS = 5


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


def _validate_fields(fields: dict) -> None:
    for key, value in fields.items():
        if key == "attempts":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("attempts 必须是非负整数")
        elif key == "published_at":
            if value is not None and not isinstance(value, datetime):
                raise ValueError("published_at 必须是时间或空值")
        else:
            limit = FIELD_LIMITS[key]
            if not isinstance(value, str):
                raise ValueError(f"{key} 必须是字符串")
            if len(value) > limit:
                raise ValueError(f"{key} 长度不能超过 {limit} 个字符")


async def apply_action(
    session: AsyncSession,
    job_id: uuid.UUID,
    action: str,
    *,
    tenant_id: str,
    actor: str = "user",
    note: str = "",
    fields: dict | None = None,
) -> PublishJob:
    pending = fields or {}
    unknown = set(pending) - MUTABLE_JOB_FIELDS
    if unknown:
        raise ValueError(f"不允许修改的字段：{', '.join(sorted(unknown))}")
    _validate_fields(pending)
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
    # 白名单仅允许可变字段，避免调用方借 fields 改写 status/tenant 等敏感列
    for key, value in pending.items():
        setattr(job, key, value)
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


async def execute_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    adapter: PublisherAdapter,
    *,
    tenant_id: str,
    actor: str,
) -> PublishJob:
    job = await _locked_job(session, job_id, tenant_id)
    if job.status != "approved":
        raise AppError("invalid_state", "只有审核通过的任务可以执行发布", 409)
    if job.attempts >= MAX_PUBLISH_ATTEMPTS:
        raise AppError(
            "too_many_attempts", "发布尝试次数已达上限，请人工确认平台状态", 409
        )

    version = (
        await session.execute(
            select(ContentVersion).where(
                ContentVersion.content_id == job.content_id,
                ContentVersion.version == job.content_version,
                ContentVersion.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise AppError("empty_content", "发布版本不存在", 409)

    if job.account_id is None:
        raise AppError("account_missing", "请先选择并授权发布账号", 409)
    account = (
        await session.execute(
            select(PlatformAccount).where(
                PlatformAccount.id == job.account_id,
                PlatformAccount.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise AppError("account_missing", "请先选择并授权发布账号", 409)
    if account.status != "active":
        raise AppError("account_inactive", "账号已停用", 409)
    if account.platform != job.platform:
        raise AppError("account_platform_mismatch", "账号平台与发布平台不一致", 409)

    # 先落库次数与进行中标记：进程崩溃后仍能看出请求已提交平台
    job = await apply_action(
        session,
        job.id,
        "publish",
        tenant_id=tenant_id,
        actor=actor,
        fields={
            "attempts": job.attempts + 1,
            "error": "发布请求已提交，等待平台确认",
        },
    )

    try:
        result = await adapter.publish(
            account=account, title=version.title, body=version.body
        )
    except Exception as exc:  # noqa: BLE001 - 任何适配器异常都必须归一化后才能返回
        # 未预期异常只暴露类名，避免把内部细节或密钥带进响应
        failure = (
            exc
            if isinstance(exc, PublishError)
            else PublishError(f"发布执行失败（{type(exc).__name__}）")
        )
        message = str(failure)[:1000]
        try:
            await apply_action(
                session,
                job.id,
                "fail",
                tenant_id=tenant_id,
                actor=actor,
                note=message[:500],
                fields={"error": message},
            )
        except Exception:
            logger.exception("发布失败状态写入失败 job=%s", job.id)
            await session.rollback()
        raise failure from None

    return await apply_action(
        session,
        job.id,
        "succeed",
        tenant_id=tenant_id,
        actor=actor,
        fields={
            "external_id": result.external_id,
            "external_url": result.url,
            "published_at": datetime.now(UTC),
            "error": "",
        },
    )
