from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models.project import Content, Project
from app.models.publish import PlatformAccount, PublishJob
from app.models.user import User
from app.permissions import viewer_access

router = APIRouter(tags=["calendar"])

MAX_CALENDAR_RANGE = timedelta(days=90)
FAILED_STATUSES = {"precheck_failed", "export_failed", "publish_failed"}
MAX_ERROR_CHARS = 300


class CalendarItem(BaseModel):
    job_id: str
    content_id: str
    project_id: str
    project_title: str
    content_title: str
    platform: str
    status: str
    scheduled_at: datetime | None
    published_at: datetime | None
    account_name: str | None
    error: str | None


class CalendarOut(BaseModel):
    items: list[CalendarItem]


def _require_tz(value: datetime, field: str) -> datetime:
    if value.tzinfo is None:
        raise AppError("invalid_range", f"{field} 必须包含时区", 422)
    return value.astimezone(UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@router.get("/calendar", response_model=CalendarOut)
async def get_calendar(
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> CalendarOut:
    start_utc = _require_tz(start, "start")
    end_utc = _require_tz(end, "end")
    if start_utc >= end_utc or end_utc - start_utc > MAX_CALENDAR_RANGE:
        raise AppError("invalid_range", "时间范围不合法（最长 90 天）", 422)

    stmt = (
        select(PublishJob, Content, Project, PlatformAccount)
        .join(Content, Content.id == PublishJob.content_id)
        .join(Project, Project.id == Content.project_id)
        .outerjoin(
            PlatformAccount,
            and_(
                PlatformAccount.id == PublishJob.account_id,
                PlatformAccount.tenant_id == user.tenant_id,
            ),
        )
        .where(
            PublishJob.tenant_id == user.tenant_id,
            Content.tenant_id == user.tenant_id,
            Project.tenant_id == user.tenant_id,
            PublishJob.status != "cancelled",
            or_(
                and_(
                    PublishJob.scheduled_at >= start_utc,
                    PublishJob.scheduled_at < end_utc,
                ),
                and_(
                    PublishJob.published_at >= start_utc,
                    PublishJob.published_at < end_utc,
                ),
            ),
        )
        .order_by(func.coalesce(PublishJob.scheduled_at, PublishJob.published_at))
    )
    rows = (await session.execute(stmt)).all()
    return CalendarOut(
        items=[
            CalendarItem(
                job_id=str(job.id),
                content_id=str(content.id),
                project_id=str(project.id),
                project_title=project.title,
                content_title=content.title or content.platform,
                platform=job.platform,
                status=job.status,
                scheduled_at=_utc(job.scheduled_at),
                published_at=_utc(job.published_at),
                account_name=account.name if account is not None else None,
                error=(
                    (job.error or "")[:MAX_ERROR_CHARS] or None
                    if job.status in FAILED_STATUSES
                    else None
                ),
            )
            for job, content, project, account in rows
        ]
    )
