import uuid
from pathlib import PurePosixPath
from typing import Annotated, Literal

import jsonschema
from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.profile import Profile
from app.models.project import Content, ContentVersion, Project
from app.models.user import User
from app.services.repurpose import repurpose_content
from app.skills.manifest import ManifestError
from app.skills.registry import SkillRegistry, get_registry
from app.skills.runner import ScriptError
from app.storage import Storage, get_storage

router = APIRouter(tags=["projects"])

ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)
]
Title300 = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]
ProjectTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
Platform = Literal[
    "wechat", "xiaohongshu", "douyin", "zhihu", "kuaishou", "channels", "bilibili"
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


def _safe_upload_name(name: str | None) -> str:
    raw = (name or "upload.bin").replace("\\", "/")
    base = PurePosixPath(raw).name
    if (
        not base
        or base in {".", ".."}
        or "\x00" in base
        or any(ord(ch) < 32 for ch in base)
        or len(base) > 200
        or len(base.encode("utf-8")) > 200
    ):
        raise AppError("invalid_filename", "文件名不合法", 422)
    return base


@router.get("/projects", response_model=ProjectList)
async def list_projects(
    user: User = Depends(get_current_user),  # noqa: B008
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
    user: User = Depends(get_current_user),  # noqa: B008
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
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProjectOut:
    return _project_out(await _owned_project(session, project_id, user))


@router.get("/projects/{project_id}/contents", response_model=list[ContentOut])
async def list_contents(
    project_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
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
    user: User = Depends(get_current_user),  # noqa: B008
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
    user: User = Depends(get_current_user),  # noqa: B008
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
    user: User = Depends(get_current_user),  # noqa: B008
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


@router.get("/projects/{project_id}/files")
async def list_project_files(
    project_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008
) -> dict:
    project = await _owned_project(session, project_id, user)
    return {"items": storage.list_files(str(project.id))}


@router.post("/projects/{project_id}/files", status_code=201)
async def upload_project_file(
    project_id: str,
    file: Annotated[UploadFile, File()],
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    storage: Storage = Depends(get_storage),  # noqa: B008
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
    relpath = storage.save_bytes(str(project.id), f"files/{name}", b"".join(chunks))
    return {"path": relpath, "size": size}


@router.post(
    "/projects/{project_id}/repurpose",
    response_model=RepurposeOut,
    status_code=201,
)
async def repurpose(
    project_id: str,
    payload: RepurposeIn,
    user: User = Depends(get_current_user),  # noqa: B008
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
