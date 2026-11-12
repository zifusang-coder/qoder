import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models.profile import Profile
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.models.user import User
from app.permissions import admin_access, editor_access, has_role, viewer_access
from app.publish.base import Platform, PublishConfigError, PublishError
from app.publish.export import MANUAL_EXPORT_PLATFORMS, ExportAdapter
from app.publish.state import InvalidTransition
from app.publish.wechat import WeChatMPAdapter
from app.services.audit import record, record_best_effort
from app.services.precheck import run_precheck
from app.services.publish import (
    ContentChanged,
    JobNotFound,
    JobNotReschedulable,
    apply_action,
    apply_precheck,
    execute_job,
    export_job,
    reopen,
    reschedule,
)
from app.storage import StorageBackend, get_storage

router = APIRouter(prefix="/publish", tags=["publish"])

USER_ACTIONS = {"approve", "reject", "cancel", "retry"}
SYSTEM_ACTIONS = {
    "submit",
    "pass",
    "fail",
    "succeed",
    "export_done",
    "publish",
    "export",
}
INTERNAL_ACTIONS = {"reopen"}


def _require_tz(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("scheduled_at 必须包含时区")
    return value.astimezone(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_id: uuid.UUID
    platform: Platform
    account_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None

    _validate_scheduled_at = field_validator("scheduled_at")(_require_tz)


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_at: datetime | None

    _validate_scheduled_at = field_validator("scheduled_at")(_require_tz)


class ActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    note: Annotated[str, StringConstraints(max_length=500)] = ""


class JobOut(BaseModel):
    id: str
    content_id: str
    content_version: int
    platform: str
    account_id: str | None
    status: str
    scheduled_at: datetime | None
    published_at: datetime | None
    external_id: str
    external_url: str
    export_path: str
    attempts: int
    error: str
    checks: dict
    created_at: datetime
    updated_at: datetime


class JobList(BaseModel):
    items: list[JobOut]
    total: int


class PrecheckOut(BaseModel):
    job: JobOut
    passed: bool
    errors: int
    warnings: int
    issues: list[dict]


def _out(job: PublishJob) -> JobOut:
    return JobOut(
        id=str(job.id),
        content_id=str(job.content_id),
        content_version=job.content_version,
        platform=job.platform,
        account_id=str(job.account_id) if job.account_id else None,
        status=job.status,
        scheduled_at=_utc(job.scheduled_at),
        published_at=_utc(job.published_at),
        external_id=job.external_id,
        external_url=job.external_url,
        export_path=job.export_path,
        attempts=job.attempts,
        error=job.error,
        checks=job.checks or {},
        created_at=_utc(job.created_at),
        updated_at=_utc(job.updated_at),
    )


def _job_id(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except (ValueError, TypeError):
        raise AppError("not_found", "发布任务不存在", 404) from None


async def _owned_job(session: AsyncSession, job_id: str, user: User) -> PublishJob:
    job = (
        await session.execute(
            select(PublishJob).where(
                PublishJob.id == _job_id(job_id),
                PublishJob.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise AppError("not_found", "发布任务不存在", 404)
    return job


async def _find_job_by_key(
    session: AsyncSession, tenant_id: str, idempotency_key: str
) -> PublishJob | None:
    return (
        await session.execute(
            select(PublishJob)
            .where(
                PublishJob.tenant_id == tenant_id,
                PublishJob.idempotency_key == idempotency_key,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _replay_existing(
    session: AsyncSession,
    existing: PublishJob,
    payload: JobIn,
    user: User,
    response: Response,
) -> JobOut:
    tenant_id = user.tenant_id
    actor = user.email
    can_manage_schedule = has_role(user, "admin")
    for attempt in range(2):
        key = existing.idempotency_key
        try:
            if existing.status == "cancelled":
                if not can_manage_schedule:
                    raise AppError("forbidden", "权限不足", 403)
                job = await reopen(
                    session,
                    existing.id,
                    tenant_id=tenant_id,
                    actor=actor,
                    scheduled_at=payload.scheduled_at,
                )
            elif payload.scheduled_at is not None and _utc(
                payload.scheduled_at
            ) != _utc(existing.scheduled_at):
                if not can_manage_schedule:
                    raise AppError("forbidden", "权限不足", 403)
                job = await reschedule(
                    session,
                    existing.id,
                    payload.scheduled_at,
                    tenant_id=tenant_id,
                )
            else:
                job = existing
        except InvalidTransition:
            await session.rollback()
            existing = await _find_job_by_key(session, tenant_id, key)
            if existing is None:
                raise AppError("not_found", "发布任务不存在", 404) from None
            if attempt == 0:
                continue
            raise AppError(
                "invalid_transition", "发布任务状态已变化，请重试", 409
            ) from None
        except JobNotFound:
            raise AppError("not_found", "发布任务不存在", 404) from None
        except JobNotReschedulable as exc:
            raise AppError("job_not_reschedulable", str(exc), 409) from exc
        response.status_code = 200
        return _out(job)
    raise AppError("invalid_transition", "发布任务状态已变化，请重试", 409)


@router.get("/jobs", response_model=JobList)
async def list_jobs(
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobList:
    base = select(PublishJob).where(PublishJob.tenant_id == user.tenant_id)
    if status is not None:
        base = base.where(PublishJob.status == status)
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    stmt = (
        base.order_by(PublishJob.created_at.desc(), PublishJob.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return JobList(items=[_out(job) for job in rows], total=total)


@router.post("/jobs", response_model=JobOut, status_code=201)
async def create_job(
    payload: JobIn,
    response: Response,
    request: Request,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobOut:
    content = (
        await session.execute(
            select(Content).where(
                Content.id == payload.content_id,
                Content.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("not_found", "内容不存在", 404)

    idempotency_key = (
        f"{content.id}:{content.current_version}:"
        f"{payload.platform}:{payload.account_id or '-'}"
    )
    existing = await _find_job_by_key(session, user.tenant_id, idempotency_key)
    if existing is not None:
        return await _replay_existing(session, existing, payload, user, response)

    if content.current_version < 1:
        raise AppError("empty_content", "内容还没有可发布的版本", 409)

    version = (
        await session.execute(
            select(ContentVersion).where(
                ContentVersion.content_id == content.id,
                ContentVersion.version == content.current_version,
                ContentVersion.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if version is None or not version.body.strip():
        raise AppError("empty_content", "当前版本正文为空，无法发布", 409)

    if content.platform != payload.platform:
        raise AppError("platform_mismatch", "内容平台与发布平台不一致", 409)

    account = None
    if payload.account_id is not None:
        account = (
            await session.execute(
                select(PlatformAccount).where(
                    PlatformAccount.id == payload.account_id,
                    PlatformAccount.tenant_id == user.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise AppError("not_found", "账号不存在", 404)
        if account.platform != payload.platform:
            raise AppError(
                "account_platform_mismatch", "账号平台与发布平台不一致", 409
            )
        if account.status != "active":
            raise AppError("account_inactive", "账号已停用", 409)

    job = PublishJob(
        tenant_id=user.tenant_id,
        content_id=content.id,
        content_version=content.current_version,
        platform=payload.platform,
        account_id=account.id if account is not None else None,
        status="draft",
        idempotency_key=idempotency_key,
        scheduled_at=payload.scheduled_at,
    )
    session.add(job)
    try:
        await session.flush()
        session.add(
            PublishEvent(
                tenant_id=user.tenant_id,
                job_id=job.id,
                from_status="created",
                to_status="draft",
                action="create",
                actor=user.email,
            )
        )
        await record(
            session,
            tenant_id=user.tenant_id,
            action="publish.create",
            actor=user,
            resource_type="publish_job",
            resource_id=str(job.id),
            detail={"platform": job.platform},
            request=request,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await _find_job_by_key(session, user.tenant_id, idempotency_key)
        if existing is None:
            raise
        return await _replay_existing(session, existing, payload, user, response)
    await session.refresh(job)
    return _out(job)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobOut:
    return _out(await _owned_job(session, job_id, user))


@router.post("/jobs/{job_id}/precheck", response_model=PrecheckOut)
async def precheck_job(
    job_id: str,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PrecheckOut:
    job = await _owned_job(session, job_id, user)
    if job.status not in {"draft", "precheck_failed", "prechecking"}:
        raise AppError(
            "invalid_state",
            "只有草稿、检查未通过或检查中的任务可以执行检查",
            409,
        )

    content = (
        await session.execute(
            select(Content).where(
                Content.id == job.content_id,
                Content.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("not_found", "内容不存在", 404)

    version = (
        await session.execute(
            select(ContentVersion).where(
                ContentVersion.content_id == content.id,
                ContentVersion.version == job.content_version,
                ContentVersion.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise AppError("empty_content", "发布版本不存在", 409)

    project = (
        await session.execute(
            select(Project).where(
                Project.id == content.project_id,
                Project.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise AppError("not_found", "项目不存在", 404)

    taboos: list[str] = []
    if project.profile_id is not None:
        profile = (
            await session.execute(
                select(Profile).where(
                    Profile.id == project.profile_id,
                    Profile.tenant_id == user.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if profile is not None:
            taboos = profile.taboos or []

    report = run_precheck(
        version.body,
        title=version.title,
        platform=job.platform,
        taboos=taboos,
    )

    try:
        job = await apply_precheck(
            session,
            job.id,
            report,
            tenant_id=user.tenant_id,
            actor=user.email,
        )
    except InvalidTransition as exc:
        raise AppError(
            "invalid_state", "发布任务状态已变化，请刷新后重试", 409
        ) from exc
    except JobNotFound as exc:
        raise AppError("not_found", "发布任务不存在", 404) from exc

    return PrecheckOut(job=_out(job), **report)


@router.post("/jobs/{job_id}/execute", response_model=JobOut)
async def execute_publish(
    job_id: str,
    request: Request,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobOut:
    job = await _owned_job(session, job_id, user)
    if job.platform != "wechat":
        raise AppError("manual_export_required", "该平台请使用导出流程", 409)

    try:
        job = await execute_job(
            session,
            job.id,
            WeChatMPAdapter(),
            tenant_id=user.tenant_id,
            actor=user.email,
        )
    except InvalidTransition as exc:
        raise AppError(
            "invalid_state", "发布任务状态已变化，请刷新后重试", 409
        ) from exc
    except ContentChanged as exc:
        raise AppError("content_changed", str(exc), 409) from exc
    except PublishConfigError as exc:
        raise AppError("account_misconfigured", str(exc), 409) from exc
    except PublishError as exc:
        raise AppError("publish_failed", str(exc), 502) from exc
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="publish.execute",
        actor=user,
        resource_type="publish_job",
        resource_id=str(job.id),
        request=request,
    )
    return _out(job)


@router.post("/jobs/{job_id}/export", response_model=JobOut)
async def export_publish(
    job_id: str,
    request: Request,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    storage: StorageBackend = Depends(get_storage),  # noqa: B008
) -> JobOut:
    job = await _owned_job(session, job_id, user)
    if job.platform not in MANUAL_EXPORT_PLATFORMS:
        raise AppError("export_unsupported", "该平台不支持手动导出，请使用自动发布", 409)

    try:
        job = await export_job(
            session,
            job.id,
            ExportAdapter(storage),
            tenant_id=user.tenant_id,
            actor=user.email,
        )
    except JobNotFound as exc:
        raise AppError("not_found", str(exc), 404) from exc
    except InvalidTransition as exc:
        raise AppError(
            "invalid_state", "发布任务状态已变化，请刷新后重试", 409
        ) from exc
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="publish.export",
        actor=user,
        resource_type="publish_job",
        resource_id=str(job.id),
        detail={"platform": job.platform},
        request=request,
    )
    return _out(job)


@router.patch("/jobs/{job_id}/schedule", response_model=JobOut)
async def schedule_job(
    job_id: str,
    payload: ScheduleIn,
    request: Request,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobOut:
    parsed = _job_id(job_id)
    try:
        job = await reschedule(
            session, parsed, payload.scheduled_at, tenant_id=user.tenant_id
        )
    except JobNotFound as exc:
        raise AppError("not_found", str(exc), 404) from exc
    except JobNotReschedulable as exc:
        raise AppError("job_not_reschedulable", str(exc), 409) from exc
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="publish.schedule",
        actor=user,
        resource_type="publish_job",
        resource_id=str(job.id),
        detail={
            "scheduled_at": (
                payload.scheduled_at.isoformat()
                if payload.scheduled_at is not None
                else ""
            )
        },
        request=request,
    )
    return _out(job)


@router.post("/jobs/{job_id}/actions", response_model=JobOut)
async def act_on_job(
    job_id: str,
    payload: ActionIn,
    request: Request,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobOut:
    if payload.action not in USER_ACTIONS | SYSTEM_ACTIONS | INTERNAL_ACTIONS:
        raise AppError("unknown_action", "未知动作", 422)

    if payload.action in SYSTEM_ACTIONS | INTERNAL_ACTIONS:
        raise AppError("action_forbidden", "该动作不能通过此接口执行", 403)

    parsed = _job_id(job_id)
    try:
        job = await apply_action(
            session,
            parsed,
            payload.action,
            tenant_id=user.tenant_id,
            actor=user.email,
            note=payload.note,
        )
    except JobNotFound as exc:
        raise AppError("not_found", str(exc), 404) from exc
    except InvalidTransition as exc:
        raise AppError("invalid_transition", str(exc), 409) from exc
    except ContentChanged as exc:
        raise AppError("content_changed", str(exc), 409) from exc
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="publish.action",
        actor=user,
        resource_type="publish_job",
        resource_id=str(job.id),
        detail={"action": payload.action},
        request=request,
    )
    return _out(job)
