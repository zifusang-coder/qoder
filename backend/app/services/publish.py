import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.project import Content, ContentVersion
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.publish.base import (
    PublishConfigError,
    PublishContent,
    PublisherAdapter,
    PublishError,
    PublishMedia,
)
from app.publish.credentials import merge_credential_patch
from app.publish.export import MANUAL_EXPORT_PLATFORMS, ExportAdapter
from app.publish.state import InvalidTransition, next_status
from app.storage import StorageBackend

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
EXPORTABLE_STATUSES = {"draft", "awaiting_review", "approved"}
EXPORT_FAILED_MESSAGE = "导出失败，请稍后重试"
EXPORT_IN_FLIGHT_MESSAGE = "导出请求已提交，等待完成"
EXPORT_LEASE_SECONDS = 60


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


async def _reload_job(
    session: AsyncSession, job_id: uuid.UUID, tenant_id: str
) -> PublishJob | None:
    try:
        return (
            await session.execute(
                select(PublishJob)
                .where(PublishJob.id == job_id, PublishJob.tenant_id == tenant_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    except Exception:
        logger.exception("导出任务状态回读失败 job=%s", job_id)
        return None


async def _mark_export_failed(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    tenant_id: str,
    actor: str,
) -> PublishJob | None:
    try:
        await apply_action(
            session,
            job_id,
            "fail",
            tenant_id=tenant_id,
            actor=actor,
            note=EXPORT_FAILED_MESSAGE,
            fields={"error": EXPORT_FAILED_MESSAGE},
        )
    except InvalidTransition:
        # 回读与 fail 之间另一路径已落库 exported，返回真实结果而不是 500
        await session.rollback()
        current = await _reload_job(session, job_id, tenant_id)
        if current is not None and current.status == "exported":
            return current
    except Exception:  # 状态写入失败只记录日志，响应仍归一化
        logger.exception("导出失败状态写入失败 job=%s", job_id)
        await session.rollback()
    return None


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


async def _persist_failure(
    session: AsyncSession,
    job_id: uuid.UUID,
    message: str,
    *,
    tenant_id: str,
    actor: str,
    attempts: int,
) -> None:
    try:
        await apply_action(
            session,
            job_id,
            "fail",
            tenant_id=tenant_id,
            actor=actor,
            note=message[:500],
            fields={"error": message[:1000], "attempts": attempts},
        )
    except Exception:
        logger.exception("发布失败状态写入失败 job=%s", job_id)
        await session.rollback()


async def execute_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    adapter: PublisherAdapter,
    *,
    tenant_id: str,
    actor: str,
    storage: StorageBackend,
    require_due: datetime | None = None,
) -> PublishJob:
    job = await _locked_job(session, job_id, tenant_id)
    if job.status != "approved":
        raise AppError("invalid_state", "只有审核通过的任务可以执行发布", 409)
    if require_due is not None:
        scheduled = _as_utc(job.scheduled_at)
        if scheduled is None or scheduled > _as_utc(require_due):
            # worker 选中任务后用户可能已重排发布时间，锁内必须再次确认
            raise AppError("not_due", "发布任务尚未到执行时间", 409)
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

    content = (
        await session.execute(
            select(Content).where(
                Content.id == job.content_id,
                Content.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("empty_content", "内容不存在", 409)

    meta = version.meta if isinstance(version.meta, dict) else {}
    raw_files = meta.get("files")
    media: list[PublishMedia] = []
    if isinstance(raw_files, list):
        for item in raw_files:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            path = item.get("path")
            if isinstance(name, str) and name and isinstance(path, str) and path:
                media.append(
                    PublishMedia(
                        name=name,
                        kind=str(item.get("kind") or "text"),
                        path=path,
                    )
                )
    cover = meta.get("cover")
    payload = PublishContent(
        project_id=str(content.project_id),
        title=version.title,
        body=version.body,
        kind=content.kind,
        media=tuple(media),
        cover_path=cover if isinstance(cover, str) and cover else None,
    )

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
    previous_attempts = job.attempts
    job = await apply_action(
        session,
        job.id,
        "publish",
        tenant_id=tenant_id,
        actor=actor,
        fields={
            "attempts": previous_attempts + 1,
            "error": "发布请求已提交，等待平台确认",
        },
    )

    try:
        result = await adapter.publish(
            account=account, content=payload, storage=storage
        )
    except PublishConfigError as exc:
        # 配置问题可修复，回滚本次尝试次数，避免用户改好配置前先被限流
        await _persist_failure(
            session,
            job.id,
            str(exc),
            tenant_id=tenant_id,
            actor=actor,
            attempts=previous_attempts,
        )
        raise
    except Exception as exc:  # noqa: BLE001 - 任何适配器异常都必须归一化后才能返回
        # 未预期异常只暴露类名，避免把内部细节或密钥带进响应
        failure = (
            exc
            if isinstance(exc, PublishError)
            else PublishError(f"发布执行失败（{type(exc).__name__}）")
        )
        message = str(failure)[:1000]
        await _persist_failure(
            session,
            job.id,
            message,
            tenant_id=tenant_id,
            actor=actor,
            attempts=previous_attempts + 1,
        )
        raise failure from None

    published = await apply_action(
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
    if result.credential_patch:
        published_id = published.id
        try:
            merge_credential_patch(account, result.credential_patch)
            await session.commit()
        except Exception:
            # 刷新令牌回写失败不影响已确认的发布结果
            logger.exception("发布凭据回写失败 job=%s", published_id)
            await session.rollback()
            reloaded = await _reload_job(session, published_id, tenant_id)
            if reloaded is not None:
                return reloaded
    return published


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        # 部分驱动（SQLite）会把带时区的 UTC 时间读成 naive
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def export_job(
    session: AsyncSession,
    job_id: uuid.UUID,
    exporter: ExportAdapter,
    *,
    tenant_id: str,
    actor: str,
) -> PublishJob:
    job = await _locked_job(session, job_id, tenant_id)
    if job.platform not in MANUAL_EXPORT_PLATFORMS:
        raise AppError("export_unsupported", "该平台不支持手动导出，请使用自动发布", 409)
    if job.status == "exported":
        return job
    if job.status not in EXPORTABLE_STATUSES and job.status != "exporting":
        raise AppError("invalid_state", "当前状态不允许导出；导出失败请先重试", 409)

    content = (
        await session.execute(
            select(Content).where(
                Content.id == job.content_id,
                Content.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("empty_content", "内容不存在", 409)
    version = (
        await session.execute(
            select(ContentVersion).where(
                ContentVersion.content_id == content.id,
                ContentVersion.version == job.content_version,
                ContentVersion.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise AppError("empty_content", "发布版本不存在", 409)

    if job.status == "exporting":
        age = (datetime.now(UTC) - _as_utc(job.updated_at)).total_seconds()
        if job.error == EXPORT_IN_FLIGHT_MESSAGE and age < EXPORT_LEASE_SECONDS:
            raise AppError("export_in_progress", "导出进行中，请稍后刷新", 409)
        job.error = EXPORT_IN_FLIGHT_MESSAGE
        # 显式续租：只改 error 时值未变不会触发 UPDATE，updated_at 不会刷新
        job.updated_at = datetime.now(UTC)
        await session.commit()
    else:
        job = await apply_action(
            session,
            job.id,
            "export",
            tenant_id=tenant_id,
            actor=actor,
            fields={"error": EXPORT_IN_FLIGHT_MESSAGE, "export_path": ""},
        )

    project_id = str(content.project_id)
    try:
        relpath = await asyncio.to_thread(
            lambda: exporter.export(
                project_id=project_id,
                platform=job.platform,
                job_id=str(job.id),
                content_id=str(content.id),
                content_version=job.content_version,
                title=version.title,
                body=version.body,
            )
        )
    except Exception:  # 原始异常不能进入响应或 job.error
        logger.exception("导出失败 job=%s", job_id)
        # 会话可能因异常处于失效事务；回读前必须先回滚
        await session.rollback()
        current = await _reload_job(session, job_id, tenant_id)
        # 慢请求租约过期后，另一路径可能已成功落库，不能把 exported 改成失败
        if current is not None and current.status == "exported":
            return current
        failed = await _mark_export_failed(
            session, job_id, tenant_id=tenant_id, actor=actor
        )
        if failed is not None:
            return failed
        raise AppError("export_failed", EXPORT_FAILED_MESSAGE, 500) from None

    try:
        return await apply_action(
            session,
            job_id,
            "export_done",
            tenant_id=tenant_id,
            actor=actor,
            fields={"export_path": relpath, "error": ""},
        )
    except Exception:
        logger.exception("导出完成状态写入失败 job=%s", job_id)
        await session.rollback()
        # commit 已成功但后续 refresh/ack 失败时，任务其实已经导出
        current = await _reload_job(session, job_id, tenant_id)
        if current is not None and current.status == "exported":
            return current
        # 确定性导出的产物可被后续重试复用，失败时保留而不是删除
        failed = await _mark_export_failed(
            session, job_id, tenant_id=tenant_id, actor=actor
        )
        if failed is not None:
            return failed
        raise AppError("export_failed", EXPORT_FAILED_MESSAGE, 500) from None
