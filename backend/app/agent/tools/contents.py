from sqlalchemy import select

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import owned_content, owned_project, schema
from app.errors import AppError
from app.models.project import Content, ContentVersion


async def list_contents(ctx: ToolContext, args: dict) -> ToolResult:
    limit = int(args.get("limit", 20))
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
    version_number = int(args.get("version") or content.current_version)
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
]
