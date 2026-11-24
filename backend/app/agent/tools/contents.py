import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import int_arg, owned_content, owned_project, schema
from app.errors import AppError
from app.models.project import Content, ContentVersion


async def list_contents(ctx: ToolContext, args: dict) -> ToolResult:
    limit = int_arg(args, "limit", default=20, minimum=1, maximum=50)
    statement = select(Content).where(Content.tenant_id == ctx.tenant_id)
    if args.get("project_id"):
        project = await owned_project(
            ctx.session, ctx.tenant_id, args["project_id"]
        )
        statement = statement.where(Content.project_id == project.id)
    if args.get("platform"):
        statement = statement.where(Content.platform == args["platform"])
    rows = (
        await ctx.session.execute(
            statement.order_by(Content.updated_at.desc()).limit(limit)
        )
    ).scalars().all()
    items = [
        {
            "id": str(content.id),
            "project_id": str(content.project_id),
            "title": content.title,
            "platform": content.platform,
            "kind": content.kind,
            "current_version": content.current_version,
            "status": content.status,
        }
        for content in rows
    ]
    return ToolResult(summary=f"找到 {len(items)} 条内容", data={"items": items})


async def get_content(ctx: ToolContext, args: dict) -> ToolResult:
    content = await owned_content(ctx.session, ctx.tenant_id, args.get("content_id"))
    version_number = int_arg(
        args, "version", default=content.current_version, minimum=1
    )
    version = (
        await ctx.session.execute(
            select(ContentVersion).where(
                ContentVersion.tenant_id == ctx.tenant_id,
                ContentVersion.content_id == content.id,
                ContentVersion.version == version_number,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise AppError("not_found", "内容版本不存在", 404)
    return ToolResult(
        summary=f"内容「{content.title}」v{version_number}",
        data={
            "content": {
                "id": str(content.id),
                "project_id": str(content.project_id),
                "platform": content.platform,
                "kind": content.kind,
                "current_version": content.current_version,
                "status": content.status,
            },
            "version": {
                "version": version.version,
                "title": version.title,
                "body": version.body[:8000],
                "source": version.source,
            },
        },
    )


def _content_artifact(
    version: ContentVersion, *, content_id: uuid.UUID, title: str
) -> dict:
    return {
        "type": "content",
        "id": str(content_id),
        "title": title,
        "version_id": str(version.id),
        "version": version.version,
    }


def _version_result(version: ContentVersion, *, replay: bool) -> ToolResult:
    summary = (
        f"版本已存在：v{version.version}"
        if replay
        else f"已追加「{version.title}」v{version.version}"
    )
    return ToolResult(
        summary=summary,
        data={
            "content_id": str(version.content_id),
            "version": {"id": str(version.id), "version": version.version},
        },
        artifacts=[
            _content_artifact(
                version, content_id=version.content_id, title=version.title
            )
        ],
    )


def _created_result(
    content: Content, version: ContentVersion | None, *, replay: bool
) -> ToolResult:
    title = content.title or "未命名内容"
    summary = f"内容已存在：「{title}」" if replay else f"已创建「{title}」"
    artifact: dict = {"type": "content", "id": str(content.id), "title": title}
    if version is not None:
        artifact["version_id"] = str(version.id)
        artifact["version"] = version.version
    return ToolResult(
        summary=summary,
        data={
            "content": {
                "id": str(content.id),
                "project_id": str(content.project_id),
                "platform": content.platform,
                "kind": content.kind,
                "title": content.title,
                "current_version": content.current_version,
                "status": content.status,
            }
        },
        artifacts=[artifact],
    )


async def _created_replay(
    session: AsyncSession, tenant_id: str, key: str
) -> ToolResult | None:
    content = (
        await session.execute(
            select(Content).where(
                Content.tenant_id == tenant_id,
                Content.meta["agent_key"].as_string() == key,
            )
        )
    ).scalars().first()
    if content is None:
        return None
    version = None
    if content.current_version:
        version = (
            await session.execute(
                select(ContentVersion).where(
                    ContentVersion.tenant_id == tenant_id,
                    ContentVersion.content_id == content.id,
                    ContentVersion.version == content.current_version,
                )
            )
        ).scalar_one_or_none()
    return _created_result(content, version, replay=True)


async def create_content(ctx: ToolContext, args: dict) -> ToolResult:
    key = f"{ctx.run_id}:{ctx.step_index}"
    replayed = await _created_replay(ctx.session, ctx.tenant_id, key)
    if replayed is not None:
        return replayed
    project = await owned_project(
        ctx.session, ctx.tenant_id, args.get("project_id")
    )
    platform = str(args.get("platform") or "").strip()[:32]
    if not platform:
        raise AppError("invalid_arguments", "platform 不能为空", 422)
    kind = str(args.get("kind") or "article").strip()[:32] or "article"
    title = str(args.get("title") or "").strip()[:300]
    body = str(args.get("body") or "")
    content = Content(
        tenant_id=ctx.tenant_id,
        project_id=project.id,
        platform=platform,
        kind=kind,
        title=title,
        current_version=0,
        meta={"agent_key": key},
    )
    ctx.session.add(content)
    version = None
    if body or title:
        await ctx.session.flush()
        version = ContentVersion(
            tenant_id=ctx.tenant_id,
            content_id=content.id,
            version=1,
            title=title,
            body=body,
            source="agent",
            meta={"agent_key": key},
        )
        ctx.session.add(version)
        content.current_version = 1
    try:
        await ctx.session.commit()
    except IntegrityError:
        await ctx.session.rollback()
        replayed = await _created_replay(ctx.session, ctx.tenant_id, key)
        if replayed is None:
            raise AppError("conflict", "内容创建冲突，请重试", 409) from None
        return replayed
    await ctx.session.refresh(content)
    if version is not None:
        await ctx.session.refresh(version)
    return _created_result(content, version, replay=False)


async def create_version(ctx: ToolContext, args: dict) -> ToolResult:
    key = f"{ctx.run_id}:{ctx.step_index}"
    existing = (
        await ctx.session.execute(
            select(ContentVersion).where(
                ContentVersion.tenant_id == ctx.tenant_id,
                ContentVersion.meta["agent_key"].as_string() == key,
            )
        )
    ).scalars().first()
    if existing is not None:
        return _version_result(existing, replay=True)
    content = await owned_content(
        ctx.session, ctx.tenant_id, args.get("content_id")
    )
    body = str(args.get("body") or "")
    if not body.strip():
        raise AppError("invalid_arguments", "正文不能为空", 422)
    latest = (
        await ctx.session.execute(
            select(func.max(ContentVersion.version)).where(
                ContentVersion.content_id == content.id,
                ContentVersion.tenant_id == ctx.tenant_id,
            )
        )
    ).scalar()
    number = int(latest or 0) + 1
    title = str(args.get("title") or content.title).strip()[:300]
    content_id = content.id
    version = ContentVersion(
        tenant_id=ctx.tenant_id,
        content_id=content_id,
        version=number,
        title=title,
        body=body,
        source="agent",
        meta={"agent_key": key},
    )
    ctx.session.add(version)
    content.current_version = number
    if title:
        content.title = title
    try:
        await ctx.session.commit()
    except IntegrityError:
        # 版本号竞态：回滚后按幂等键回读，命中即复放，否则返回可重试冲突
        await ctx.session.rollback()
        existing = (
            await ctx.session.execute(
                select(ContentVersion).where(
                    ContentVersion.tenant_id == ctx.tenant_id,
                    ContentVersion.content_id == content_id,
                    ContentVersion.meta["agent_key"].as_string() == key,
                )
            )
        ).scalars().first()
        if existing is None:
            raise AppError(
                "conflict", "内容版本已被其他操作更新，请重试", 409
            ) from None
        return _version_result(existing, replay=True)
    await ctx.session.refresh(version)
    return _version_result(version, replay=False)


SPECS = [
    ToolSpec(
        name="content.list",
        description="列出内容（可按项目与平台过滤）。",
        input_schema=schema(
            {
                "project_id": {"type": "string", "minLength": 1},
                "platform": {"type": "string", "maxLength": 32},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
        risk="read",
        handler=list_contents,
    ),
    ToolSpec(
        name="content.get",
        description="读取内容正文；不传 version 时读取当前版本。",
        input_schema=schema(
            {
                "content_id": {"type": "string", "minLength": 1},
                "version": {"type": "integer", "minimum": 1},
            },
            ("content_id",),
        ),
        risk="read",
        handler=get_content,
    ),
    ToolSpec(
        name="content.create",
        description="在项目下新建内容；传 title/body 时同时写入第 1 版。",
        input_schema=schema(
            {
                "project_id": {"type": "string", "minLength": 1},
                "platform": {"type": "string", "minLength": 1, "maxLength": 32},
                "kind": {"type": "string", "minLength": 1, "maxLength": 32},
                "title": {"type": "string", "maxLength": 300},
                "body": {"type": "string", "maxLength": 100000},
            },
            ("project_id", "platform"),
        ),
        risk="low",
        handler=create_content,
    ),
    ToolSpec(
        name="content.create_version",
        description="向已有内容追加一个新版本，不修改历史版本。",
        input_schema=schema(
            {
                "content_id": {"type": "string", "minLength": 1},
                "title": {"type": "string", "maxLength": 300},
                "body": {"type": "string", "minLength": 1, "maxLength": 100000},
            },
            ("content_id", "body"),
        ),
        risk="low",
        handler=create_version,
    ),
]
