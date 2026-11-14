from sqlalchemy import select

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import parse_uuid, schema
from app.errors import AppError
from app.models.profile import Profile, ProfileMemory


async def list_profiles(ctx: ToolContext, args: dict) -> ToolResult:
    limit = int(args.get("limit", 20))
    rows = (
        await ctx.session.execute(
            select(Profile)
            .where(Profile.tenant_id == ctx.tenant_id)
            .order_by(Profile.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    items = [
        {
            "id": str(profile.id),
            "name": profile.name,
            "account_name": profile.account_name,
            "positioning": profile.positioning,
            "audience": profile.audience,
            "tone": profile.tone,
            "platform_focus": list(profile.platform_focus or []),
            "is_active": profile.is_active,
        }
        for profile in rows
    ]
    return ToolResult(summary=f"找到 {len(items)} 个画像", data={"items": items})


async def get_profile(ctx: ToolContext, args: dict) -> ToolResult:
    profile_id = parse_uuid(args.get("profile_id"), "画像不存在")
    profile = (
        await ctx.session.execute(
            select(Profile).where(
                Profile.id == profile_id, Profile.tenant_id == ctx.tenant_id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise AppError("not_found", "画像不存在", 404)
    memories = (
        await ctx.session.execute(
            select(ProfileMemory)
            .where(
                ProfileMemory.tenant_id == ctx.tenant_id,
                ProfileMemory.profile_id == profile.id,
            )
            .order_by(ProfileMemory.weight.desc(), ProfileMemory.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    return ToolResult(
        summary=f"画像「{profile.name}」，{len(memories)} 条记忆",
        data={
            "profile": {
                "id": str(profile.id),
                "name": profile.name,
                "positioning": profile.positioning,
                "audience": profile.audience,
                "tone": profile.tone,
                "taboos": list(profile.taboos or []),
                "goals": list(profile.goals or []),
                "platform_focus": list(profile.platform_focus or []),
            },
            "memories": [
                {"kind": item.kind, "content": item.content[:500], "weight": item.weight}
                for item in memories
            ],
        },
    )


SPECS = [
    ToolSpec(
        name="profile.list",
        description="列出本租户的账号画像，用于了解定位、受众与语气。",
        input_schema=schema(
            {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}
        ),
        risk="read",
        handler=list_profiles,
    ),
    ToolSpec(
        name="profile.get",
        description="读取单个画像详情与最近记忆片段。",
        input_schema=schema(
            {"profile_id": {"type": "string", "minLength": 1}},
            ("profile_id",),
        ),
        risk="read",
        handler=get_profile,
    ),
]
