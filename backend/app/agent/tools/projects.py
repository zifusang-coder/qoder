from sqlalchemy import select

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import (
    int_arg,
    owned_project,
    parse_uuid,
    schema,
)
from app.errors import AppError
from app.models.profile import Profile
from app.models.project import Content, Project


async def list_projects(ctx: ToolContext, args: dict) -> ToolResult:
    limit = int_arg(args, "limit", default=20, minimum=1, maximum=50)
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


def _project_artifact(project: Project) -> dict:
    return {
        "type": "project",
        "id": str(project.id),
        "title": project.title,
    }


async def create_project(ctx: ToolContext, args: dict) -> ToolResult:
    key = f"{ctx.run_id}:{ctx.step_index}"
    existing = (
        await ctx.session.execute(
            select(Project).where(
                Project.tenant_id == ctx.tenant_id,
                Project.meta["agent_key"].as_string() == key,
            )
        )
    ).scalars().first()
    if existing is not None:
        return ToolResult(
            summary=f"项目已存在：{existing.title}",
            data={"project": {"id": str(existing.id), "title": existing.title}},
            artifacts=[_project_artifact(existing)],
        )
    title = str(args.get("title") or "").strip()
    if not title:
        raise AppError("invalid_arguments", "项目标题不能为空", 422)
    profile_id = None
    if args.get("profile_id"):
        profile_id = parse_uuid(args["profile_id"], "画像不存在")
        profile = (
            await ctx.session.execute(
                select(Profile).where(
                    Profile.id == profile_id,
                    Profile.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if profile is None:
            raise AppError("not_found", "画像不存在", 404)
    raw_platforms = args.get("platforms") or []
    if not isinstance(raw_platforms, list):
        raise AppError("invalid_arguments", "platforms 必须是字符串数组", 422)
    project = Project(
        tenant_id=ctx.tenant_id,
        title=title[:200],
        topic=str(args.get("topic") or "").strip()[:2000],
        profile_id=profile_id,
        platforms=[str(item)[:32] for item in raw_platforms][:20],
        meta={"agent_key": key, "created_by_agent": ctx.run_id},
    )
    ctx.session.add(project)
    await ctx.session.commit()
    await ctx.session.refresh(project)
    return ToolResult(
        summary=f"已创建项目「{project.title}」",
        data={"project": {"id": str(project.id), "title": project.title}},
        artifacts=[_project_artifact(project)],
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
    ToolSpec(
        name="project.create",
        description="新建一个内容项目，作为后续内容的容器。",
        input_schema=schema(
            {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "topic": {"type": "string", "maxLength": 2000},
                "profile_id": {"type": "string", "minLength": 1},
                "platforms": {
                    "type": "array",
                    "items": {"type": "string", "maxLength": 32},
                    "maxItems": 20,
                },
            },
            ("title",),
        ),
        risk="low",
        handler=create_project,
    ),
]
