import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.analytics import AnalyticsSnapshot
from app.models.project import Content, Project
from app.models.user import User
from app.services.analytics import import_csv

router = APIRouter(prefix="/analytics", tags=["analytics"])

MAX_CSV_BYTES = 5 * 1024 * 1024


class ImportResult(BaseModel):
    upserted: int
    errors: list[str]


class OverviewItem(BaseModel):
    content_id: str
    title: str
    platform: str
    has_data: bool
    captured_at: date | None = None
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    followers_delta: int = 0


class OverviewTotals(BaseModel):
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    followers_delta: int = 0


class OverviewOut(BaseModel):
    items: list[OverviewItem]
    totals: OverviewTotals
    engagement_rate: float


@router.post("/import", response_model=ImportResult)
async def import_analytics(
    file: Annotated[UploadFile, File()],
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ImportResult:
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_CSV_BYTES:
            raise AppError("file_too_large", "CSV 超过 5MB", 413)
        chunks.append(chunk)
    upserted, errors = await import_csv(session, user, b"".join(chunks))
    return ImportResult(upserted=upserted, errors=errors)


@router.get("/overview", response_model=OverviewOut)
async def get_overview(
    project_id: uuid.UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> OverviewOut:
    project = (
        await session.execute(
            select(Project).where(
                Project.id == project_id,
                Project.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise AppError("not_found", "项目不存在", 404)

    contents = (
        await session.execute(
            select(Content)
            .where(
                Content.project_id == project.id,
                Content.tenant_id == user.tenant_id,
            )
            .order_by(Content.created_at, Content.id)
        )
    ).scalars().all()

    latest: dict[uuid.UUID, AnalyticsSnapshot] = {}
    if contents:
        snapshots = (
            await session.execute(
                select(AnalyticsSnapshot)
                .where(
                    AnalyticsSnapshot.tenant_id == user.tenant_id,
                    AnalyticsSnapshot.content_id.in_([c.id for c in contents]),
                )
                .order_by(
                    AnalyticsSnapshot.captured_at.desc(),
                    AnalyticsSnapshot.created_at.desc(),
                )
            )
        ).scalars().all()
        for snapshot in snapshots:
            latest.setdefault(snapshot.content_id, snapshot)

    items: list[OverviewItem] = []
    totals = OverviewTotals()
    for content in contents:
        snapshot = latest.get(content.id)
        item = OverviewItem(
            content_id=str(content.id),
            title=content.title,
            platform=content.platform,
            has_data=snapshot is not None,
        )
        if snapshot is not None:
            item.captured_at = snapshot.captured_at
            item.views = snapshot.views
            item.likes = snapshot.likes
            item.comments = snapshot.comments
            item.shares = snapshot.shares
            item.saves = snapshot.saves
            item.followers_delta = snapshot.followers_delta
            totals.views += snapshot.views
            totals.likes += snapshot.likes
            totals.comments += snapshot.comments
            totals.shares += snapshot.shares
            totals.saves += snapshot.saves
            totals.followers_delta += snapshot.followers_delta
        items.append(item)

    interactions = (
        totals.likes + totals.comments + totals.shares + totals.saves
    )
    engagement_rate = (
        round(interactions / totals.views, 4) if totals.views > 0 else 0.0
    )
    return OverviewOut(
        items=items, totals=totals, engagement_rate=engagement_rate
    )
