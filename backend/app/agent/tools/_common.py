import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.media import MediaJob
from app.models.project import Content, Project
from app.models.publish import PublishJob


def schema(properties: dict | None = None, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": list(required),
        "additionalProperties": False,
    }


def parse_uuid(value: object, message: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise AppError("not_found", message, 404) from None


async def owned_project(
    session: AsyncSession, tenant_id: str, project_id: object
) -> Project:
    project = (
        await session.execute(
            select(Project).where(
                Project.id == parse_uuid(project_id, "项目不存在"),
                Project.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise AppError("not_found", "项目不存在", 404)
    return project


async def owned_content(
    session: AsyncSession, tenant_id: str, content_id: object
) -> Content:
    content = (
        await session.execute(
            select(Content).where(
                Content.id == parse_uuid(content_id, "内容不存在"),
                Content.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("not_found", "内容不存在", 404)
    return content


async def owned_job(
    session: AsyncSession, tenant_id: str, job_id: object
) -> PublishJob:
    job = (
        await session.execute(
            select(PublishJob).where(
                PublishJob.id == parse_uuid(job_id, "发布任务不存在"),
                PublishJob.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise AppError("not_found", "发布任务不存在", 404)
    return job


async def owned_media_job(
    session: AsyncSession, tenant_id: str, job_id: object
) -> MediaJob:
    job = (
        await session.execute(
            select(MediaJob).where(
                MediaJob.id == parse_uuid(job_id, "媒体任务不存在"),
                MediaJob.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise AppError("not_found", "媒体任务不存在", 404)
    return job
