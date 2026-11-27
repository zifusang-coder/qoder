import uuid
from datetime import UTC, date, datetime

import pytest

from app.config import DEFAULT_TENANT_ID
from app.models.analytics import AnalyticsSnapshot
from app.models.project import Project
from app.models.publish import PublishJob

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


async def _create_project(client, *, title="洞察项目"):
    response = await client.post("/api/projects", json={"title": title})
    assert response.status_code == 201
    return response.json()["id"]


async def _create_content(client, project_id, *, title, platform="wechat"):
    response = await client.post(
        f"/api/projects/{project_id}/contents",
        json={"platform": platform, "title": title},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _seed_snapshot(
    db_session,
    content_id,
    *,
    platform="wechat",
    captured_at=date(2026, 10, 1),
    views=0,
    likes=0,
    comments=0,
    shares=0,
    saves=0,
    tenant_id=DEFAULT_TENANT_ID,
):
    db_session.add(
        AnalyticsSnapshot(
            tenant_id=tenant_id,
            content_id=uuid.UUID(content_id),
            platform=platform,
            captured_at=captured_at,
            views=views,
            likes=likes,
            comments=comments,
            shares=shares,
            saves=saves,
        )
    )
    await db_session.commit()


async def _seed_job(
    db_session,
    content_id,
    *,
    platform="wechat",
    status="published",
    published_at=None,
    tenant_id=DEFAULT_TENANT_ID,
):
    db_session.add(
        PublishJob(
            tenant_id=tenant_id,
            content_id=uuid.UUID(content_id),
            content_version=1,
            platform=platform,
            status=status,
            idempotency_key=f"insight-{uuid.uuid4().hex}",
            published_at=published_at,
        )
    )
    await db_session.commit()


async def _insights(client, project_id):
    response = await client.get(
        "/api/analytics/insights", params={"project_id": project_id}
    )
    assert response.status_code == 200
    return response.json()


async def test_channel_breakdown_and_content_ranking(client, db_session):
    project_id = await _create_project(client)
    wechat_a = await _create_content(client, project_id, title="公众号爆款")
    wechat_b = await _create_content(client, project_id, title="公众号日常")
    zhihu = await _create_content(client, project_id, title="知乎长文", platform="zhihu")
    await _create_content(client, project_id, title="暂无数据")

    await _seed_snapshot(
        db_session, wechat_a, views=1000, likes=100, comments=10, shares=5, saves=5
    )
    await _seed_snapshot(db_session, wechat_b, views=500, likes=40)
    await _seed_snapshot(
        db_session,
        zhihu,
        platform="zhihu",
        views=100,
        likes=35,
        comments=10,
        saves=5,
    )

    body = await _insights(client, project_id)

    channels = {item["platform"]: item for item in body["channels"]}
    assert set(channels) == {"wechat", "zhihu"}
    assert channels["wechat"]["contents"] == 2
    assert channels["wechat"]["views"] == 1500
    assert channels["wechat"]["interactions"] == 160
    assert channels["wechat"]["engagement_rate"] == pytest.approx(0.1067)
    assert channels["zhihu"]["contents"] == 1
    assert channels["zhihu"]["views"] == 100
    assert channels["zhihu"]["interactions"] == 50
    assert channels["zhihu"]["engagement_rate"] == pytest.approx(0.5)
    # 按阅读量降序
    assert [item["platform"] for item in body["channels"]] == ["wechat", "zhihu"]

    ranked = body["contents"]
    assert [item["title"] for item in ranked] == ["公众号爆款", "知乎长文", "公众号日常"]
    assert ranked[0]["content_id"] == wechat_a
    assert ranked[0]["interactions"] == 120
    assert ranked[0]["engagement_rate"] == pytest.approx(0.12)
    assert ranked[1]["interactions"] == 50
    assert ranked[2]["interactions"] == 40


async def test_time_buckets_from_published_jobs(client, db_session):
    project_id = await _create_project(client)
    fast = await _create_content(client, project_id, title="早高峰")
    slow = await _create_content(client, project_id, title="晚高峰")
    repeat = await _create_content(client, project_id, title="重复发布")

    await _seed_snapshot(db_session, fast, views=1000, likes=100, comments=10, shares=5, saves=5)
    await _seed_snapshot(db_session, slow, views=500, likes=40)
    await _seed_snapshot(db_session, repeat, views=100, likes=35, comments=10, saves=5)

    # UTC 02:00 即上海时间 10:00（周一）；UTC 10:30 即上海时间 18:30（周一）
    await _seed_job(db_session, fast, published_at=datetime(2026, 10, 5, 2, 0, tzinfo=UTC))
    await _seed_job(db_session, slow, published_at=datetime(2026, 10, 5, 10, 30, tzinfo=UTC))
    await _seed_job(db_session, repeat, published_at=datetime(2026, 10, 12, 2, 0, tzinfo=UTC))
    # 同一内容多条已发布记录只取最早一条
    await _seed_job(db_session, fast, published_at=datetime(2026, 10, 19, 6, 0, tzinfo=UTC))
    # 未发布任务不参与时段统计
    await _seed_job(
        db_session,
        repeat,
        status="draft",
        published_at=datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
    )

    body = await _insights(client, project_id)

    assert body["has_time_data"] is True
    assert body["published_contents"] == 3

    buckets = {(item["weekday"], item["hour"]): item for item in body["time_buckets"]}
    assert set(buckets) == {(0, 10), (0, 18)}
    morning = buckets[(0, 10)]
    assert morning["samples"] == 2
    assert morning["avg_views"] == 550
    assert morning["avg_interactions"] == pytest.approx(85.0)
    assert morning["engagement_rate"] == pytest.approx(0.1545)

    best = body["best_slots"]
    assert len(best) == 2
    assert (best[0]["weekday"], best[0]["hour"]) == (0, 10)
    assert best[0]["source"] == "history"
    assert best[0]["samples"] == 2
    assert (best[1]["weekday"], best[1]["hour"]) == (0, 18)
    assert best[1]["source"] == "history"


async def test_falls_back_to_default_slots_without_history(client, db_session):
    project_id = await _create_project(client)
    content_id = await _create_content(client, project_id, title="只有数据")
    await _seed_snapshot(db_session, content_id, views=200, likes=10)

    body = await _insights(client, project_id)

    assert body["has_time_data"] is False
    assert body["published_contents"] == 0
    assert body["time_buckets"] == []
    assert [item["hour"] for item in body["best_slots"]] == [18, 21, 12]
    for slot in body["best_slots"]:
        assert slot["weekday"] is None
        assert slot["source"] == "fallback"
        assert slot["samples"] == 0


async def test_empty_project_returns_empty_insights(client, db_session):
    project_id = await _create_project(client, title="空项目")

    body = await _insights(client, project_id)
    assert body["channels"] == []
    assert body["contents"] == []
    assert body["time_buckets"] == []
    assert body["has_time_data"] is False
    assert len(body["best_slots"]) == 3


async def test_insights_project_lookup(client, db_session):
    missing = await client.get(
        "/api/analytics/insights", params={"project_id": uuid.uuid4()}
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

    foreign_project_id = uuid.uuid4()
    db_session.add(
        Project(id=foreign_project_id, tenant_id=OTHER_TENANT, title="其他租户项目")
    )
    await db_session.commit()
    foreign = await client.get(
        "/api/analytics/insights", params={"project_id": foreign_project_id}
    )
    assert foreign.status_code == 404

    malformed = await client.get(
        "/api/analytics/insights", params={"project_id": "not-a-uuid"}
    )
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "validation_error"


async def test_other_tenant_jobs_and_snapshots_are_ignored(client, db_session):
    project_id = await _create_project(client)
    content_id = await _create_content(client, project_id, title="本租户内容")
    await _seed_snapshot(db_session, content_id, views=100, likes=10)

    # 同 id 内容在其他租户下的快照与发布任务不应进入统计
    await _seed_snapshot(
        db_session,
        content_id,
        views=9999,
        likes=999,
        tenant_id=OTHER_TENANT,
    )
    await _seed_job(
        db_session,
        content_id,
        published_at=datetime(2026, 10, 5, 2, 0, tzinfo=UTC),
        tenant_id=OTHER_TENANT,
    )

    body = await _insights(client, project_id)
    assert body["published_contents"] == 0
    assert body["time_buckets"] == []
    assert body["channels"][0]["views"] == 100
    assert body["channels"][0]["interactions"] == 10


async def test_contents_without_snapshots_are_excluded(client, db_session):
    project_id = await _create_project(client)
    await _create_content(client, project_id, title="没有数据")

    body = await _insights(client, project_id)
    assert body["channels"] == []
    assert body["contents"] == []
