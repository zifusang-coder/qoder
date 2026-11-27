import asyncio
import uuid
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import quote

import jsonschema
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.db import get_session
from app.errors import AppError
from app.models.media import MediaJob
from app.models.profile import Profile
from app.models.project import Content, ContentVersion, Project
from app.models.user import User
from app.permissions import editor_access, viewer_access
from app.publish.base import Platform
from app.queue import get_queue
from app.services.audit import record, record_best_effort
from app.services.live import snapshot_stream
from app.services.media import (
    create_media_job,
    mark_queue_failed,
    retry_media_job,
)
from app.services.repurpose import repurpose_content
from app.skills.manifest import ManifestError
from app.skills.registry import SkillRegistry, get_registry
from app.skills.runner import ScriptError
from app.storage import (
    StorageBackend,
    StorageError,
    StorageNotFound,
    get_storage,
)

router = APIRouter(tags=["projects"])

ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
]
Title300 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
ProjectTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class ProjectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: ProjectTitle
    topic: str = ""
    profile_id: str | None = None
    platforms: list[
        Annotated[str, StringConstraints(min_length=1, max_length=32)]
    ] = Field(default_factory=list, max_length=20)

    @field_validator("profile_id", mode="before")
    @classmethod
    def _blank_profile_to_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value


class ProjectOut(BaseModel):
    id: str
    title: str
    topic: str
    status: str
    profile_id: str | None
    platforms: list[str]


class ProjectList(BaseModel):
    items: list[ProjectOut]


class ContentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: ShortText
    kind: ShortText = "article"
    title: Title300 = ""
    body: str = ""


class ContentOut(BaseModel):
    id: str
    project_id: str
    platform: str
    kind: str
    title: str
    current_version: int
    status: str


class VersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Title300 = ""
    body: str = ""
    source: ShortText = "manual"


class VersionOut(BaseModel):
    id: str
    content_id: str
    version: int
    title: str
    body: str
    source: str


class VersionList(BaseModel):
    items: list[VersionOut]


class RepurposeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_content_id: str
    platform: Platform
    skill_id: ShortText = "content-repurposing"


class RepurposeOut(VersionOut):
    platform: Platform


class MediaIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(default="", max_length=500)
    text: str = Field(default="", max_length=2000)
    title: str = Field(default="", max_length=120)
    platform: Platform = "douyin"
    duration_seconds: int = Field(default=30, ge=8, le=180)
    voice: bool = True
    voice_name: Annotated[
        str, StringConstraints(min_length=1, max_length=64)
    ] = "zh-CN-XiaoxiaoNeural"
    theme: Literal["dark", "light", "warm"] = "dark"
    rate: Annotated[
        str, StringConstraints(pattern=r"^[+-]\d{1,2}%$")
    ] = "+0%"
    instructions: str = Field(default="", max_length=1000)


class MediaFileOut(BaseModel):
    name: str
    path: str
    kind: str
    size: int


class MediaOut(BaseModel):
    run_id: str
    content_id: str
    project_id: str
    version_id: str
    version: int
    platform: str
    kind: str
    title: str
    body: str
    duration_seconds: float
    files: list[MediaFileOut]
    preview: str | None
    cover: str | None
    subtitles: str | None
    audio: dict
    created_at: datetime


class MediaListOut(BaseModel):
    items: list[MediaOut]


class MediaJobOut(BaseModel):
    id: str
    project_id: str
    skill_id: str
    status: str
    content_id: str | None
    error_code: str
    error_message: str
    attempts: int
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _parse_uuid(value: str, message: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        raise AppError("not_found", message, 404) from None


def _project_out(p: Project) -> ProjectOut:
    return ProjectOut(
        id=str(p.id), title=p.title, topic=p.topic, status=p.status,
        profile_id=str(p.profile_id) if p.profile_id else None,
        platforms=p.platforms or [],
    )


def _content_out(c: Content) -> ContentOut:
    return ContentOut(
        id=str(c.id), project_id=str(c.project_id), platform=c.platform, kind=c.kind,
        title=c.title, current_version=c.current_version, status=c.status,
    )


def _version_out(v: ContentVersion) -> VersionOut:
    return VersionOut(
        id=str(v.id), content_id=str(v.content_id), version=v.version,
        title=v.title, body=v.body, source=v.source,
    )


def _media_job_out(job: MediaJob) -> MediaJobOut:
    return MediaJobOut(
        id=str(job.id),
        project_id=str(job.project_id),
        skill_id=job.skill_id,
        status=job.status,
        content_id=str(job.content_id) if job.content_id else None,
        error_code=job.error_code,
        error_message=job.error_message,
        attempts=job.attempts,
        started_at=job.started_at,
        finished_at=job.finished_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _media_out(content: Content, version: ContentVersion) -> MediaOut:
    meta = version.meta if isinstance(version.meta, dict) else {}
    raw_files = meta.get("files")
    files = [
        MediaFileOut(
            name=str(item["name"]),
            path=str(item["path"]),
            kind=str(item.get("kind") or "text"),
            size=int(item.get("size") or 0),
        )
        for item in (raw_files if isinstance(raw_files, list) else [])
        if isinstance(item, dict) and item.get("name") and item.get("path")
    ]
    try:
        duration = float(meta.get("duration_seconds") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    audio = meta.get("audio")
    return MediaOut(
        run_id=str(meta.get("run_id") or ""),
        content_id=str(content.id),
        project_id=str(content.project_id),
        version_id=str(version.id),
        version=version.version,
        platform=content.platform,
        kind=content.kind,
        title=content.title,
        body=version.body,
        duration_seconds=duration,
        files=files,
        preview=meta.get("preview") if isinstance(meta.get("preview"), str) else None,
        cover=meta.get("cover") if isinstance(meta.get("cover"), str) else None,
        subtitles=(
            meta.get("subtitles") if isinstance(meta.get("subtitles"), str) else None
        ),
        audio=audio if isinstance(audio, dict) else {},
        created_at=version.created_at,
    )


async def _owned_project(session: AsyncSession, project_id: str, user: User) -> Project:
    parsed = _parse_uuid(project_id, "项目不存在")
    project = (
        await session.execute(
            select(Project).where(Project.id == parsed, Project.tenant_id == user.tenant_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise AppError("not_found", "项目不存在", 404)
    return project


async def _owned_content(session: AsyncSession, content_id: str, user: User) -> Content:
    parsed = _parse_uuid(content_id, "内容不存在")
    content = (
        await session.execute(
            select(Content).where(Content.id == parsed, Content.tenant_id == user.tenant_id)
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("not_found", "内容不存在", 404)
    return content


async def _next_version(session: AsyncSession, content_id: uuid.UUID) -> int:
    current = (
        await session.execute(
            select(func.max(ContentVersion.version)).where(
                ContentVersion.content_id == content_id
            )
        )
    ).scalar()
    return int(current or 0) + 1


MAX_UPLOAD_BYTES = 10 * 1024 * 1024

PREVIEW_TYPES = {
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".srt": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


def _safe_upload_name(name: str | None) -> str:
    raw = (name or "upload.bin").replace("\\", "/")
    base = PurePosixPath(raw).name
    if (
        not base
        or base in {".", ".."}
        or "\x00" in base
        or any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in base)
        or len(base) > 200
        or len(base.encode("utf-8")) > 200
    ):
        raise AppError("invalid_filename", "文件名不合法", 422)
    return base


@router.get("/projects", response_model=ProjectList)
async def list_projects(
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProjectList:
    rows = (
        await session.execute(
            select(Project)
            .where(Project.tenant_id == user.tenant_id)
            .order_by(Project.created_at.desc())
        )
    ).scalars().all()
    return ProjectList(items=[_project_out(p) for p in rows])


@router.post("/projects", response_model=ProjectOut, status_code=201)
async def create_project(
    payload: ProjectIn,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProjectOut:
    data = payload.model_dump()
    profile_id = data.pop("profile_id")
    profile_uuid = None
    if profile_id:
        profile_uuid = _parse_uuid(profile_id, "账号画像不存在")
        profile = (
            await session.execute(
                select(Profile).where(
                    Profile.id == profile_uuid,
                    Profile.tenant_id == user.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            raise AppError("not_found", "账号画像不存在", 404)
    project = Project(tenant_id=user.tenant_id, profile_id=profile_uuid, **data)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    return _project_out(project)


@router.get("/projects/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProjectOut:
    return _project_out(await _owned_project(session, project_id, user))


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    request: Request,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    project = await _owned_project(session, project_id, user)
    await record(
        session,
        tenant_id=user.tenant_id,
        action="project.delete",
        actor=user,
        resource_type="project",
        resource_id=str(project.id),
        detail={"title": project.title},
        request=request,
    )
    await session.delete(project)
    await session.commit()
    return Response(status_code=204)


@router.get("/projects/{project_id}/contents", response_model=list[ContentOut])
async def list_contents(
    project_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[ContentOut]:
    project = await _owned_project(session, project_id, user)
    rows = (
        await session.execute(
            select(Content)
            .where(Content.project_id == project.id, Content.tenant_id == user.tenant_id)
            .order_by(Content.created_at)
        )
    ).scalars().all()
    return [_content_out(c) for c in rows]


@router.post("/projects/{project_id}/contents", response_model=ContentOut, status_code=201)
async def create_content(
    project_id: str,
    payload: ContentIn,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ContentOut:
    project = await _owned_project(session, project_id, user)
    content = Content(
        tenant_id=user.tenant_id,
        project_id=project.id,
        platform=payload.platform,
        kind=payload.kind,
        title=payload.title,
        current_version=0,
    )
    session.add(content)
    await session.flush()
    if payload.body or payload.title:
        version = ContentVersion(
            tenant_id=user.tenant_id,
            content_id=content.id,
            version=1,
            title=payload.title,
            body=payload.body,
            source="manual",
        )
        session.add(version)
        content.current_version = 1
    await session.commit()
    await session.refresh(content)
    return _content_out(content)


@router.get("/contents/{content_id}/versions", response_model=VersionList)
async def list_versions(
    content_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> VersionList:
    content = await _owned_content(session, content_id, user)
    rows = (
        await session.execute(
            select(ContentVersion)
            .where(ContentVersion.content_id == content.id)
            .order_by(ContentVersion.version.desc())
        )
    ).scalars().all()
    return VersionList(items=[_version_out(v) for v in rows])


@router.post("/contents/{content_id}/versions", response_model=VersionOut, status_code=201)
async def create_version(
    content_id: str,
    payload: VersionIn,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> VersionOut:
    content = await _owned_content(session, content_id, user)
    version_no = await _next_version(session, content.id)
    version = ContentVersion(
        tenant_id=user.tenant_id,
        content_id=content.id,
        version=version_no,
        title=payload.title or content.title,
        body=payload.body,
        source=payload.source,
    )
    session.add(version)
    content.current_version = version_no
    if payload.title:
        content.title = payload.title
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("version_conflict", "版本冲突，请重试", 409) from None
    await session.refresh(version)
    return _version_out(version)


@router.delete("/contents/{content_id}", status_code=204)
async def delete_content(
    content_id: str,
    request: Request,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    content = await _owned_content(session, content_id, user)
    await record(
        session,
        tenant_id=user.tenant_id,
        action="content.delete",
        actor=user,
        resource_type="content",
        resource_id=str(content.id),
        detail={"title": content.title, "platform": content.platform},
        request=request,
    )
    await session.delete(content)
    await session.commit()
    return Response(status_code=204)


@router.get("/projects/{project_id}/files")
async def list_project_files(
    project_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    storage: StorageBackend = Depends(get_storage),  # noqa: B008
) -> dict:
    project = await _owned_project(session, project_id, user)
    return {"items": await asyncio.to_thread(storage.list_files, str(project.id))}


@router.get("/projects/{project_id}/files/download")
async def download_project_file(
    project_id: str,
    path: Annotated[str, Query(min_length=1, max_length=500)],
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    storage: StorageBackend = Depends(get_storage),  # noqa: B008
) -> Response:
    project = await _owned_project(session, project_id, user)
    try:
        data = await asyncio.to_thread(storage.read_bytes, str(project.id), path)
    except StorageNotFound:
        raise AppError("not_found", "文件不存在", 404) from None
    except StorageError:
        raise AppError("invalid_path", "文件路径不合法", 422) from None
    filename = PurePosixPath(path).name
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"
        },
    )


@router.get("/projects/{project_id}/files/preview")
async def preview_project_file(
    project_id: str,
    path: Annotated[str, Query(min_length=1, max_length=500)],
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    storage: StorageBackend = Depends(get_storage),  # noqa: B008
) -> Response:
    project = await _owned_project(session, project_id, user)
    suffix = PurePosixPath(path).suffix.lower()
    media_type = PREVIEW_TYPES.get(suffix)
    if media_type is None:
        raise AppError("unsupported_media_type", "不支持在线预览该文件类型", 415)
    try:
        data = await asyncio.to_thread(storage.read_bytes, str(project.id), path)
    except StorageNotFound:
        raise AppError("not_found", "文件不存在", 404) from None
    except StorageError:
        raise AppError("invalid_path", "文件路径不合法", 422) from None
    filename = PurePosixPath(path).name
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=60",
        },
    )


@router.post("/projects/{project_id}/files", status_code=201)
async def upload_project_file(
    project_id: str,
    file: Annotated[UploadFile, File()],
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    storage: StorageBackend = Depends(get_storage),  # noqa: B008
) -> dict:
    project = await _owned_project(session, project_id, user)
    chunks: list[bytes] = []
    size = 0
    while chunk := await file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise AppError("file_too_large", "文件超过 10MB", 413)
        chunks.append(chunk)
    name = _safe_upload_name(file.filename)
    relpath = await asyncio.to_thread(
        storage.save_bytes, str(project.id), f"files/{name}", b"".join(chunks)
    )
    return {"path": relpath, "size": size}


@router.get("/projects/{project_id}/media", response_model=MediaListOut)
async def list_project_media(
    project_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MediaListOut:
    project = await _owned_project(session, project_id, user)
    rows = (
        await session.execute(
            select(Content, ContentVersion)
            .join(
                ContentVersion,
                and_(
                    ContentVersion.content_id == Content.id,
                    ContentVersion.version == Content.current_version,
                ),
            )
            .where(
                Content.project_id == project.id,
                Content.tenant_id == user.tenant_id,
                Content.kind.in_(sorted({"video", "audio"})),
            )
            .order_by(Content.created_at.desc())
        )
    ).all()
    return MediaListOut(
        items=[_media_out(content, version) for content, version in rows]
    )


async def _owned_media_job(
    session: AsyncSession, project: Project, job_id: str, user: User
) -> MediaJob:
    job = (
        await session.execute(
            select(MediaJob).where(
                MediaJob.id == _parse_uuid(job_id, "媒体任务不存在"),
                MediaJob.project_id == project.id,
                MediaJob.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise AppError("not_found", "媒体任务不存在", 404)
    return job


async def _enqueue_media_job(
    queue: ArqRedis | None, session: AsyncSession, job: MediaJob
) -> None:
    if queue is None:
        await mark_queue_failed(session, job)
        raise AppError("queue_unavailable", "任务队列暂不可用，请稍后重试", 503)
    try:
        await queue.enqueue_job("process_media_job", str(job.id))
    except Exception:  # noqa: BLE001 - 队列不可用需落库并返回可重试错误
        await mark_queue_failed(session, job)
        raise AppError(
            "queue_unavailable", "任务队列暂不可用，请稍后重试", 503
        ) from None


@router.post(
    "/projects/{project_id}/media/{skill_id}",
    response_model=MediaJobOut,
    status_code=202,
)
async def create_project_media(
    project_id: str,
    skill_id: str,
    payload: MediaIn,
    request: Request,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> MediaJobOut:
    project = await _owned_project(session, project_id, user)
    job = await create_media_job(
        session=session,
        registry=registry,
        tenant_id=user.tenant_id,
        project=project,
        skill_id=skill_id,
        payload=payload.model_dump(),
        created_by=user.id,
    )
    await _enqueue_media_job(queue, session, job)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="media.create",
        actor=user,
        resource_type="media_job",
        resource_id=str(job.id),
        detail={"skill_id": skill_id},
        request=request,
    )
    return _media_job_out(job)


@router.get(
    "/projects/{project_id}/media/jobs/{job_id}",
    response_model=MediaJobOut,
)
async def get_project_media_job(
    project_id: str,
    job_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MediaJobOut:
    project = await _owned_project(session, project_id, user)
    job = await _owned_media_job(session, project, job_id, user)
    return _media_job_out(job)


@router.get("/projects/{project_id}/media/jobs/{job_id}/stream")
async def stream_project_media_job(
    project_id: str,
    job_id: str,
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> EventSourceResponse:
    project = await _owned_project(session, project_id, user)
    await _owned_media_job(session, project, job_id, user)

    async def load(read: AsyncSession) -> dict[str, Any]:
        owned = await _owned_project(read, project_id, user)
        job = await _owned_media_job(read, owned, job_id, user)
        return _media_job_out(job).model_dump(mode="json")

    return EventSourceResponse(snapshot_stream(load))


@router.post(
    "/projects/{project_id}/media/jobs/{job_id}/retry",
    response_model=MediaJobOut,
    status_code=202,
)
async def retry_project_media_job(
    project_id: str,
    job_id: str,
    request: Request,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    queue: ArqRedis | None = Depends(get_queue),  # noqa: B008
) -> MediaJobOut:
    project = await _owned_project(session, project_id, user)
    await _owned_media_job(session, project, job_id, user)
    job = await retry_media_job(
        session=session, job_id=job_id, tenant_id=user.tenant_id
    )
    await _enqueue_media_job(queue, session, job)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="media.retry",
        actor=user,
        resource_type="media_job",
        resource_id=str(job.id),
        request=request,
    )
    return _media_job_out(job)


@router.post(
    "/projects/{project_id}/repurpose",
    response_model=RepurposeOut,
    status_code=201,
)
async def repurpose(
    project_id: str,
    payload: RepurposeIn,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> RepurposeOut:
    project = await _owned_project(session, project_id, user)
    try:
        version = await repurpose_content(
            session=session,
            registry=registry,
            tenant_id=user.tenant_id,
            project=project,
            source_content_id=payload.source_content_id,
            platform=payload.platform,
            skill_id=payload.skill_id,
            created_by=user.id,
        )
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc
    except jsonschema.ValidationError as exc:
        raise AppError("skill_inputs_invalid", "技能输入参数校验失败", 422) from exc
    except ScriptError as exc:
        raise AppError("skill_script_failed", "后处理脚本执行失败", 500) from exc
    except ValueError as exc:
        raise AppError("skill_output_invalid", "模型输出格式异常", 502) from exc
    return RepurposeOut(
        **_version_out(version).model_dump(), platform=payload.platform
    )
