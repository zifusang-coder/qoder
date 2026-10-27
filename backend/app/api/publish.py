import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.profile import Profile
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.models.user import User
from app.publish.state import InvalidTransition
from app.services.precheck import run_precheck
from app.services.publish import (
    ContentChanged,
    JobNotFound,
    JobNotReschedulable,
    apply_action,
    reopen,
    reschedule,
)

router = APIRouter(prefix="/publish", tags=["publish"])

Platform = Literal[
    "wechat", "xiaohongshu", "douyin", "zhihu", "kuaishou", "channels", "bilibili"
]

USER_ACTIONS = {"submit", "approve", "reject", "cancel", "retry"}
SYSTEM_ACTIONS = {"pass", "fail", "succeed", "export_done", "publish", "export"}
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
    for attempt in range(2):
        key = existing.idempotency_key
        try:
            if existing.status == "cancelled":
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
    user: User = Depends(get_current_user),  # noqa: B008
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
    user: User = Depends(get_current_user),  # noqa: B008
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
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobOut:
    return _out(await _owned_job(session, job_id, user))


@router.post("/jobs/{job_id}/precheck", response_model=PrecheckOut)
async def precheck_job(
    job_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> PrecheckOut:
    job = await _owned_job(session, job_id, user)
    if job.status not in {"draft", "precheck_failed"}:
        raise AppError(
            "invalid_state", "只有草稿或检查未通过的任务可以执行检查", 409
        )

    version = (
        await session.execute(
            select(ContentVersion).where(
                ContentVersion.content_id == job.content_id,
                ContentVersion.version == job.content_version,
                ContentVersion.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise AppError("empty_content", "发布版本不存在", 409)

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

    text = f"# {version.title}\n{version.body}" if version.title else version.body
    report = run_precheck(text, platform=job.platform, taboos=taboos)

    try:
        job = await apply_action(
            session,
            job.id,
            "submit",
            tenant_id=user.tenant_id,
            actor=user.email,
            note="发布前检查",
        )
        job = await apply_action(
            session,
            job.id,
            "pass" if report["passed"] else "fail",
            tenant_id=user.tenant_id,
            actor=user.email,
            note="检查通过" if report["passed"] else "检查未通过",
        )
    except InvalidTransition as exc:
        raise AppError(
            "invalid_state", "发布任务状态已变化，请刷新后重试", 409
        ) from exc
    except JobNotFound as exc:
        raise AppError("not_found", "发布任务不存在", 404) from exc

    job.checks = report
    await session.commit()
    await session.refresh(job)
    return PrecheckOut(job=_out(job), **report)


@router.patch("/jobs/{job_id}/schedule", response_model=JobOut)
async def schedule_job(
    job_id: str,
    payload: ScheduleIn,
    user: User = Depends(get_current_user),  # noqa: B008
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
    return _out(job)


@router.post("/jobs/{job_id}/actions", response_model=JobOut)
async def act_on_job(
    job_id: str,
    payload: ActionIn,
    user: User = Depends(get_current_user),  # noqa: B008
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
    return _out(job)
