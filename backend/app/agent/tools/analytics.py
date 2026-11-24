from datetime import date, timedelta

from sqlalchemy import func, select

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import int_arg, schema
from app.models.analytics import AnalyticsSnapshot
from app.models.project import Content

MAX_ITEMS = 20
METRIC_FIELDS = ("views", "likes", "comments", "shares", "saves", "followers_delta")


async def analytics_summary(ctx: ToolContext, args: dict) -> ToolResult:
    days = int_arg(args, "days", default=30, minimum=1, maximum=365)
    cutoff = date.today() - timedelta(days=days)
    # 在 SQL 内按内容取时间窗内最新一条快照，避免把全部 content_id 塞进 IN (...)，
    # 大租户下也不会撞上驱动的参数上限。
    ranked = (
        select(
            AnalyticsSnapshot.id.label("snapshot_id"),
            func.row_number()
            .over(
                partition_by=AnalyticsSnapshot.content_id,
                order_by=(
                    AnalyticsSnapshot.captured_at.desc(),
                    AnalyticsSnapshot.id.desc(),
                ),
            )
            .label("rn"),
        )
        .join(
            Content,
            (Content.id == AnalyticsSnapshot.content_id)
            & (Content.tenant_id == AnalyticsSnapshot.tenant_id),
        )
        .where(
            AnalyticsSnapshot.tenant_id == ctx.tenant_id,
            AnalyticsSnapshot.captured_at >= cutoff,
        )
        .subquery()
    )
    totals_row = (
        await ctx.session.execute(
            select(
                func.count().label("content_count"),
                *(
                    func.coalesce(func.sum(getattr(AnalyticsSnapshot, field)), 0).label(
                        field
                    )
                    for field in METRIC_FIELDS
                ),
            )
            .join(ranked, AnalyticsSnapshot.id == ranked.c.snapshot_id)
            .where(ranked.c.rn == 1)
        )
    ).one()
    totals = {field: int(getattr(totals_row, field)) for field in METRIC_FIELDS}
    rows = (
        await ctx.session.execute(
            select(AnalyticsSnapshot, Content.title, Content.platform)
            .join(ranked, AnalyticsSnapshot.id == ranked.c.snapshot_id)
            .join(
                Content,
                (Content.id == AnalyticsSnapshot.content_id)
                & (Content.tenant_id == ctx.tenant_id),
            )
            .where(ranked.c.rn == 1)
            .order_by(AnalyticsSnapshot.views.desc(), AnalyticsSnapshot.id)
            .limit(MAX_ITEMS)
        )
    ).all()
    items = [
        {
            "content_id": str(snapshot.content_id),
            "title": title,
            "platform": platform,
            "captured_at": snapshot.captured_at.isoformat(),
            **{field: int(getattr(snapshot, field)) for field in METRIC_FIELDS},
        }
        for snapshot, title, platform in rows
    ]
    engagement = 0.0
    if totals["views"]:
        engagement = round(
            (totals["likes"] + totals["comments"] + totals["shares"] + totals["saves"])
            / totals["views"],
            4,
        )
    return ToolResult(
        summary=f"{int(totals_row.content_count)} 条内容有数据，总阅读 {totals['views']}",
        data={"totals": totals, "engagement_rate": engagement, "items": items},
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
