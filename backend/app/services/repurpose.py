import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.llm.gateway import LLMGateway, load_gateway_default
from app.models.profile import Profile
from app.models.project import Content, ContentVersion, Project
from app.services.profiles import profile_block_from_profile
from app.services.skill_settings import require_enabled
from app.skills.executor import SkillExecutor
from app.skills.manifest import ManifestError
from app.skills.registry import SkillRegistry


def _parse_uuid(value: str, message: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        raise AppError("not_found", message, 404) from None


async def repurpose_content(
    *,
    session: AsyncSession,
    registry: SkillRegistry,
    tenant_id: str,
    project: Project,
    source_content_id: str,
    platform: str,
    skill_id: str = "content-repurposing",
    gateway: LLMGateway | None = None,
    created_by: uuid.UUID | None = None,
) -> ContentVersion:
    source_uuid = _parse_uuid(source_content_id, "源内容不存在")
    source = (
        await session.execute(
            select(Content).where(
                Content.id == source_uuid,
                Content.project_id == project.id,
                Content.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise AppError("not_found", "源内容不存在", 404)

    # 提前固化 ORM 属性：executor.run 内部会 commit，避免依赖实例过期行为的可移植性假设
    project_id = project.id
    profile_id = project.profile_id
    source_kind = source.kind
    source_title = source.title

    version = (
        await session.execute(
            select(ContentVersion)
            .where(
                ContentVersion.content_id == source.id,
                ContentVersion.tenant_id == tenant_id,
            )
            .order_by(ContentVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if version is None or not version.body.strip():
        raise AppError("empty_content", "源内容没有正文，无法改写", 409)
    version_title = version.title
    version_body = version.body

    try:
        registry.get(skill_id)
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc
    await require_enabled(session, tenant_id, skill_id)

    profile_block = ""
    if profile_id:
        profile = (
            await session.execute(
                select(Profile).where(
                    Profile.id == profile_id,
                    Profile.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if profile:
            profile_block = await profile_block_from_profile(
                session,
                profile,
                tenant_id,
                query=f"{version_title}\n{version_body[:1500]}",
            )

    # 延迟到 404/409 校验之后再解析网关，避免缺少 LLM 配置掩盖真实错误
    if gateway is None:
        gateway = await load_gateway_default(session, tenant_id)

    executor = SkillExecutor(
        registry=registry,
        gateway=gateway,
        session=session,
        tenant_id=tenant_id,
        profile_block=profile_block,
        created_by=created_by,
    )
    result = await executor.run(
        skill_id,
        {
            "topic": version_title or source_title,
            "source": version_body,
            "platform": platform,
            "text": version_body,
        },
    )

    # 串行化同一项目内的目标内容选择与版本号分配，避免并发下重复目标与重复版本；Postgres 默认 READ COMMITTED，SQLite 会忽略 FOR UPDATE
    await session.execute(
        select(Project.id)
        .where(Project.id == project_id, Project.tenant_id == tenant_id)
        .with_for_update()
    )

    target = (
        await session.execute(
            select(Content)
            .where(
                Content.project_id == project_id,
                Content.platform == platform,
                Content.tenant_id == tenant_id,
            )
            .order_by(Content.created_at, Content.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if target is None:
        target = Content(
            tenant_id=tenant_id,
            project_id=project_id,
            platform=platform,
            kind=source_kind,
            title=source_title,
        )
        session.add(target)
        await session.flush()
    elif not target.title:
        target.title = source_title

    current = (
        await session.execute(
            select(func.max(ContentVersion.version)).where(
                ContentVersion.content_id == target.id,
                ContentVersion.tenant_id == tenant_id,
            )
        )
    ).scalar()
    next_no = int(current or 0) + 1
    new_version = ContentVersion(
        tenant_id=tenant_id,
        content_id=target.id,
        version=next_no,
        title=target.title or source_title,
        body=result.text,
        source="skill",
        skill_run_id=result.run_id,
    )
    session.add(new_version)
    target.current_version = next_no
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("version_conflict", "版本冲突，请重试", 409) from None
    await session.refresh(new_version)
    return new_version
