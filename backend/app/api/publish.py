import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.project import Content, ContentVersion
from app.models.publish import PlatformAccount, PublishJob
from app.models.user import User
from app.publish.state import InvalidTransition
from app.services.publish import apply_action

router = APIRouter(prefix="/publish", tags=["publish"])

Platform = Literal[
    "wechat", "xiaohongshu", "douyin", "zhihu", "kuaishou", "channels", "bilibili"
]

USER_ACTIONS = {"submit", "approve", "reject", "cancel", "retry"}


class JobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_id: uuid.UUID
    platform: Platform
    account_id: uuid.UUID | None = None
    scheduled_at: datetime | None = None


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


class JobList(BaseModel):
    items: list[JobOut]


def _out(job: PublishJob) -> JobOut:
    return JobOut(
        id=str(job.id),
        content_id=str(job.content_id),
        content_version=job.content_version,
        platform=job.platform,
        account_id=str(job.account_id) if job.account_id else None,
        status=job.status,
        scheduled_at=job.scheduled_at,
        published_at=job.published_at,
        external_id=job.external_id,
        external_url=job.external_url,
        export_path=job.export_path,
        error=job.error,
        checks=job.checks or {},
        created_at=job.created_at,
    )


async def _owned_job(
    session: AsyncSession,
    job_id: str,
    user: User,
    *,
    for_update: bool = False,
) -> PublishJob:
    try:
        parsed = uuid.UUID(job_id)
    except (ValueError, TypeError):
        raise AppError("not_found", "发布任务不存在", 404) from None
    stmt = select(PublishJob).where(
        PublishJob.id == parsed, PublishJob.tenant_id == user.tenant_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    job = (await session.execute(stmt)).scalar_one_or_none()
    if job is None:
        raise AppError("not_found", "发布任务不存在", 404)
    return job


@router.get("/jobs", response_model=JobList)
async def list_jobs(
    status: str | None = None,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobList:
    stmt = select(PublishJob).where(PublishJob.tenant_id == user.tenant_id)
    if status is not None:
        stmt = stmt.where(PublishJob.status == status)
    stmt = stmt.order_by(PublishJob.created_at.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return JobList(items=[_out(job) for job in rows])


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

    idempotency_key = (
        f"{content.id}:{content.current_version}:"
        f"{payload.platform}:{payload.account_id or '-'}"
    )
    existing = (
        await session.execute(
            select(PublishJob).where(
                PublishJob.tenant_id == user.tenant_id,
                PublishJob.idempotency_key == idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        response.status_code = 200
        return _out(existing)

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
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = (
            await session.execute(
                select(PublishJob).where(
                    PublishJob.tenant_id == user.tenant_id,
                    PublishJob.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        response.status_code = 200
        return _out(existing)
    await session.refresh(job)
    return _out(job)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobOut:
    return _out(await _owned_job(session, job_id, user))


@router.post("/jobs/{job_id}/actions", response_model=JobOut)
async def act_on_job(
    job_id: str,
    payload: ActionIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> JobOut:
    if payload.action not in USER_ACTIONS:
        raise AppError("action_forbidden", "该动作由系统执行", 403)

    job = await _owned_job(session, job_id, user, for_update=True)
    try:
        job = await apply_action(
            session, job, payload.action, actor=user.email, note=payload.note
        )
    except InvalidTransition as exc:
        raise AppError("invalid_transition", str(exc), 409) from exc
    return _out(job)
