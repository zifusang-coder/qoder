from datetime import date, timedelta

from sqlalchemy import select

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import schema
from app.models.project import Content
from app.services.analytics import latest_snapshots


async def analytics_summary(ctx: ToolContext, args: dict) -> ToolResult:
    days = int(args.get("days", 30))
    cutoff = date.today() - timedelta(days=days)
    contents = (
        await ctx.session.execute(
            select(Content).where(Content.tenant_id == ctx.tenant_id)
        )
    ).scalars().all()
    snapshots = await latest_snapshots(
        ctx.session, ctx.tenant_id, [content.id for content in contents]
    )
    items = []
    totals = {
        "views": 0,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "saves": 0,
        "followers_delta": 0,
    }
    for content in contents:
        snapshot = snapshots.get(content.id)
        if snapshot is None or snapshot.captured_at < cutoff:
            continue
        row = {
            "content_id": str(content.id),
            "title": content.title,
            "platform": content.platform,
            "captured_at": snapshot.captured_at.isoformat(),
            "views": snapshot.views,
            "likes": snapshot.likes,
            "comments": snapshot.comments,
            "shares": snapshot.shares,
            "saves": snapshot.saves,
            "followers_delta": snapshot.followers_delta,
        }
        items.append(row)
        for key in totals:
            totals[key] += int(row[key])
    items.sort(key=lambda item: item["views"], reverse=True)
    engagement = 0.0
    if totals["views"]:
        engagement = round(
            (totals["likes"] + totals["comments"] + totals["shares"] + totals["saves"])
            / totals["views"],
            4,
        )
    return ToolResult(
        summary=f"{len(items)} 条内容有数据，总阅读 {totals['views']}",
        data={"totals": totals, "engagement_rate": engagement, "items": items[:20]},
    )


SPECS = [
    ToolSpec(
        name="analytics.summary",
        description="汇总最近若干天已导入的阅读、互动与涨粉数据。",
        input_schema=schema(
            {"days": {"type": "integer", "minimum": 1, "maximum": 365}}
        ),
        risk="read",
        handler=analytics_summary,
    )
]
