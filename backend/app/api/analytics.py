import uuid
from datetime import date
from typing import Annotated

import jsonschema
from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models.project import Content, Project
from app.models.user import User
from app.permissions import editor_access, viewer_access
from app.services.analytics import import_csv, latest_snapshots
from app.services.postmortem import generate_postmortem
from app.skills.manifest import ManifestError
from app.skills.registry import SkillRegistry, get_registry
from app.skills.runner import ScriptError

router = APIRouter(prefix="/analytics", tags=["analytics"])

MAX_CSV_BYTES = 5 * 1024 * 1024
# multipart 边界与表单字段的容差，超过该声明长度直接拒绝
CONTENT_LENGTH_SLACK = 1024 * 1024


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


class PostmortemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ] = "postmortem"


class PostmortemOut(BaseModel):
    report: dict
    memory_ids: list[str]
    run_id: str


@router.post("/import", response_model=ImportResult)
async def import_analytics(
    request: Request,
    file: Annotated[UploadFile, File()],
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ImportResult:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            declared_size = 0
        if declared_size > MAX_CSV_BYTES + CONTENT_LENGTH_SLACK:
            raise AppError("file_too_large", "CSV 超过 5MB", 413)

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
    user: User = Depends(viewer_access),  # noqa: B008
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

    latest = await latest_snapshots(
        session, user.tenant_id, [content.id for content in contents]
    )

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


@router.post("/contents/{content_id}/postmortem", response_model=PostmortemOut)
async def postmortem(
    content_id: uuid.UUID,
    payload: PostmortemIn,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> PostmortemOut:
    try:
        result = await generate_postmortem(
            session=session,
            registry=registry,
            tenant_id=user.tenant_id,
            content_id=content_id,
            skill_id=payload.skill_id,
            created_by=user.id,
        )
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc
    except jsonschema.ValidationError as exc:
        raise AppError(
            "skill_inputs_invalid", "技能输入参数校验失败", 422
        ) from exc
    except ScriptError as exc:
        raise AppError("skill_script_failed", "后处理脚本执行失败", 500) from exc
    except ValueError as exc:
        raise AppError(
            "skill_output_invalid",
            "模型输出格式异常",
            502,
        ) from exc
    return PostmortemOut(**result)
