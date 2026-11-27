import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import AnalyticsSnapshot
from app.models.project import Content
from app.models.publish import PublishJob
from app.services.analytics import latest_snapshots

# 发布时间按产品使用地（中国）作息理解，而不是按 UTC 小时归档
INSIGHT_TIMEZONE = ZoneInfo("Asia/Shanghai")
TOP_CONTENT_LIMIT = 10
BEST_SLOT_LIMIT = 3
# 缺少发布历史时给出的通用建议时段：下班通勤、睡前、午休
FALLBACK_SLOT_HOURS = (18, 21, 12)


def _interactions(snapshot: AnalyticsSnapshot) -> int:
    return snapshot.likes + snapshot.comments + snapshot.shares + snapshot.saves


def _rate(interactions: int, views: int) -> float:
    return round(interactions / views, 4) if views > 0 else 0.0


def _to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(INSIGHT_TIMEZONE)


async def _earliest_publish_times(
    session: AsyncSession,
    tenant_id: str,
    content_ids: list[uuid.UUID],
) -> dict[uuid.UUID, datetime]:
    """每个内容取最早一次成功发布的时间，避免重试记录重复计入时段。"""
    if not content_ids:
        return {}

    rows = (
        await session.execute(
            select(PublishJob.content_id, PublishJob.published_at)
            .where(
                PublishJob.tenant_id == tenant_id,
                PublishJob.content_id.in_(content_ids),
                PublishJob.status == "published",
                PublishJob.published_at.is_not(None),
            )
            .order_by(PublishJob.published_at, PublishJob.id)
        )
    ).all()
    times: dict[uuid.UUID, datetime] = {}
    for content_id, published_at in rows:
        if content_id not in times and published_at is not None:
            times[content_id] = published_at
    return times


def summarize(
    contents: list[Content],
    latest: dict[uuid.UUID, AnalyticsSnapshot],
    publish_times: dict[uuid.UUID, datetime],
) -> dict:
    channels: dict[str, dict] = {}
    content_items: list[dict] = []
    samples: list[tuple[int, int, AnalyticsSnapshot, int]] = []

    for content in contents:
        snapshot = latest.get(content.id)
        if snapshot is None:
            continue

        interactions = _interactions(snapshot)
        channel = channels.setdefault(
            content.platform,
            {
                "platform": content.platform,
                "contents": 0,
                "views": 0,
                "interactions": 0,
            },
        )
        channel["contents"] += 1
        channel["views"] += snapshot.views
        channel["interactions"] += interactions

        content_items.append(
            {
                "content_id": str(content.id),
                "title": content.title,
                "platform": content.platform,
                "views": snapshot.views,
                "interactions": interactions,
                "engagement_rate": _rate(interactions, snapshot.views),
            }
        )

        published_at = publish_times.get(content.id)
        if published_at is not None:
            local = _to_local(published_at)
            samples.append((local.weekday(), local.hour, snapshot, interactions))

    channel_list = [
        {**channel, "engagement_rate": _rate(channel["interactions"], channel["views"])}
        for channel in channels.values()
    ]
    channel_list.sort(key=lambda item: (-item["views"], item["platform"]))

    content_items.sort(
        key=lambda item: (-item["interactions"], -item["views"], item["content_id"])
    )

    buckets: dict[tuple[int, int], dict] = {}
    for weekday, hour, snapshot, interactions in samples:
        bucket = buckets.setdefault(
            (weekday, hour),
            {"weekday": weekday, "hour": hour, "samples": 0, "views": 0, "interactions": 0},
        )
        bucket["samples"] += 1
        bucket["views"] += snapshot.views
        bucket["interactions"] += interactions

    time_buckets: list[dict] = []
    for key in sorted(buckets):
        bucket = buckets[key]
        count = bucket["samples"]
        time_buckets.append(
            {
                "weekday": bucket["weekday"],
                "hour": bucket["hour"],
                "samples": count,
                "avg_views": round(bucket["views"] / count),
                "avg_interactions": round(bucket["interactions"] / count, 1),
                "engagement_rate": _rate(bucket["interactions"], bucket["views"]),
            }
        )

    ranked = sorted(
        time_buckets,
        key=lambda item: (
            -item["avg_interactions"],
            -item["engagement_rate"],
            item["weekday"],
            item["hour"],
        ),
    )
    best_slots: list[dict] = [
        {
            "weekday": item["weekday"],
            "hour": item["hour"],
            "samples": item["samples"],
            "avg_views": item["avg_views"],
            "avg_interactions": item["avg_interactions"],
            "source": "history",
        }
        for item in ranked[:BEST_SLOT_LIMIT]
    ]
    if not best_slots:
        best_slots = [
            {
                "weekday": None,
                "hour": hour,
                "samples": 0,
                "avg_views": 0,
                "avg_interactions": 0.0,
                "source": "fallback",
            }
            for hour in FALLBACK_SLOT_HOURS
        ]

    return {
        "channels": channel_list,
        "contents": content_items[:TOP_CONTENT_LIMIT],
        "time_buckets": time_buckets,
        "best_slots": best_slots,
        "has_time_data": bool(samples),
        "published_contents": len(samples),
    }


async def collect_insights(
    session: AsyncSession,
    tenant_id: str,
    project_id: uuid.UUID,
) -> dict:
    contents = (
        await session.execute(
            select(Content)
            .where(Content.project_id == project_id, Content.tenant_id == tenant_id)
            .order_by(Content.created_at, Content.id)
        )
    ).scalars().all()
    content_ids = [content.id for content in contents]
    latest = await latest_snapshots(session, tenant_id, content_ids)
    publish_times = await _earliest_publish_times(session, tenant_id, content_ids)
    return summarize(contents, latest, publish_times)
