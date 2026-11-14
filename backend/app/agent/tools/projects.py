from sqlalchemy import select

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import owned_project, schema
from app.models.project import Content, Project


async def list_projects(ctx: ToolContext, args: dict) -> ToolResult:
    limit = int(args.get("limit", 20))
    statement = select(Project).where(Project.tenant_id == ctx.tenant_id)
    if args.get("status"):
        statement = statement.where(Project.status == args["status"])
    rows = (
        await ctx.session.execute(
            statement.order_by(Project.updated_at.desc()).limit(limit)
        )
    ).scalars().all()
    items = [
        {
            "id": str(project.id),
            "title": project.title,
            "topic": project.topic,
            "status": project.status,
            "platforms": list(project.platforms or []),
            "profile_id": str(project.profile_id) if project.profile_id else None,
        }
        for project in rows
    ]
    return ToolResult(summary=f"找到 {len(items)} 个项目", data={"items": items})


async def get_project(ctx: ToolContext, args: dict) -> ToolResult:
    project = await owned_project(ctx.session, ctx.tenant_id, args.get("project_id"))
    contents = (
        await ctx.session.execute(
            select(Content)
            .where(
                Content.tenant_id == ctx.tenant_id, Content.project_id == project.id
            )
            .order_by(Content.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return ToolResult(
        summary=f"项目「{project.title}」，{len(contents)} 条内容",
        data={
            "project": {
                "id": str(project.id),
                "title": project.title,
                "topic": project.topic,
                "status": project.status,
                "platforms": list(project.platforms or []),
                "profile_id": str(project.profile_id) if project.profile_id else None,
            },
            "contents": [
                {
                    "id": str(content.id),
                    "title": content.title,
                    "platform": content.platform,
                    "kind": content.kind,
                    "current_version": content.current_version,
                    "status": content.status,
                }
                for content in contents
            ],
        },
    )


SPECS = [
    ToolSpec(
        name="project.list",
        description="列出本租户的项目摘要。",
        input_schema=schema(
            {
                "status": {"type": "string", "maxLength": 32},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
        risk="read",
        handler=list_projects,
    ),
    ToolSpec(
        name="project.get",
        description="读取项目详情及其内容列表。",
        input_schema=schema(
            {"project_id": {"type": "string", "minLength": 1}}, ("project_id",)
        ),
        risk="read",
        handler=get_project,
    ),
]
