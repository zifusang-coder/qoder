import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.models.project import Content, Project
from app.models.publish import PublishEvent, PublishJob

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


async def _create_scheduled_job(client, *, scheduled_at, platform="wechat"):
    project = await client.post(
        "/api/projects", json={"title": f"日历测试-{uuid.uuid4().hex[:8]}"}
    )
    assert project.status_code == 201
    content = await client.post(
        f"/api/projects/{project.json()['id']}/contents",
        json={"platform": platform, "title": "日历标题", "body": "正文"},
    )
    assert content.status_code == 201
    payload = {
        "content_id": content.json()["id"],
        "platform": platform,
        "scheduled_at": scheduled_at.isoformat(),
    }
    if platform == "wechat":
        account = await client.post(
            "/api/accounts",
            json={
                "platform": "wechat",
                "name": "日历账号",
                "credential": {"app_id": "cal-id", "app_secret": "cal-secret"},
            },
        )
        assert account.status_code == 201, account.text
        payload["account_id"] = account.json()["id"]
    job = await client.post("/api/publish/jobs", json=payload)
    assert job.status_code == 201
    return project.json(), content.json(), job.json()


async def _calendar(client, start, end):
    return await client.get(
        "/api/calendar",
        params={"start": start.isoformat(), "end": end.isoformat()},
    )


async def test_calendar_returns_scheduled_job(client):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    project, content, job = await _create_scheduled_job(
        client, scheduled_at=datetime(2026, 11, 12, 10, tzinfo=UTC)
    )

    response = await _calendar(client, start, end)
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["job_id"] == job["id"]
    assert item["content_id"] == content["id"]
    assert item["project_id"] == project["id"]
    assert item["project_title"] == project["title"]
    assert item["content_title"] == "日历标题"
    assert item["platform"] == "wechat"
    assert item["status"] == "draft"
    assert item["scheduled_at"].startswith("2026-11-12T10:00:00")
    assert item["published_at"] is None
    assert item["account_name"] == "日历账号"
    assert item["error"] is None


async def test_calendar_exposes_failure_reason(client, db_session):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    _, _, job = await _create_scheduled_job(
        client, scheduled_at=datetime(2026, 11, 12, tzinfo=UTC)
    )
    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == uuid.UUID(job["id"]))
        .values(status="publish_failed", error="发布执行失败（PublishError）")
    )
    await db_session.commit()

    response = await _calendar(client, start, end)
    item = response.json()["items"][0]
    assert item["status"] == "publish_failed"
    assert item["error"] == "发布执行失败（PublishError）"


async def test_calendar_truncates_long_failure_reason(client, db_session):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    _, _, job = await _create_scheduled_job(
        client, scheduled_at=datetime(2026, 11, 12, tzinfo=UTC)
    )
    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == uuid.UUID(job["id"]))
        .values(status="export_failed", error="长" * 600)
    )
    await db_session.commit()

    response = await _calendar(client, start, end)
    assert len(response.json()["items"][0]["error"]) == 300


async def test_calendar_excludes_jobs_outside_range(client):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    await _create_scheduled_job(
        client, scheduled_at=datetime(2026, 12, 2, tzinfo=UTC)
    )

    response = await _calendar(client, start, end)
    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_calendar_excludes_cancelled_jobs(client):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    _, _, job = await _create_scheduled_job(
        client, scheduled_at=datetime(2026, 11, 12, tzinfo=UTC)
    )
    cancelled = await client.post(
        f"/api/publish/jobs/{job['id']}/actions", json={"action": "cancel"}
    )
    assert cancelled.status_code == 200

    response = await _calendar(client, start, end)
    assert response.json()["items"] == []


async def test_calendar_includes_published_job_without_schedule(
    client, db_session
):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    _, _, job = await _create_scheduled_job(
        client, scheduled_at=datetime(2026, 11, 12, tzinfo=UTC)
    )
    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == uuid.UUID(job["id"]))
        .values(
            scheduled_at=None,
            published_at=datetime(2026, 11, 20, 8, tzinfo=UTC),
            status="published",
        )
    )
    await db_session.commit()

    response = await _calendar(client, start, end)
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "published"
    assert items[0]["scheduled_at"] is None
    assert items[0]["published_at"].startswith("2026-11-20T08:00:00")


async def test_calendar_is_tenant_scoped(client, db_session):
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 12, 1, tzinfo=UTC)
    project = Project(tenant_id=OTHER_TENANT, title="外部项目")
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=OTHER_TENANT,
        project_id=project.id,
        platform="wechat",
        title="外部内容",
        current_version=1,
    )
    db_session.add(content)
    await db_session.flush()
    job = PublishJob(
        tenant_id=OTHER_TENANT,
        content_id=content.id,
        content_version=1,
        platform="wechat",
        status="draft",
        idempotency_key=f"foreign-{uuid.uuid4().hex}",
        scheduled_at=datetime(2026, 11, 12, tzinfo=UTC),
    )
    db_session.add(job)
    await db_session.flush()
    db_session.add(
        PublishEvent(
            tenant_id=OTHER_TENANT,
            job_id=job.id,
            from_status="created",
            to_status="draft",
            action="create",
        )
    )
    await db_session.commit()

    response = await _calendar(client, start, end)
    assert response.json()["items"] == []


async def test_calendar_rejects_naive_datetime(client):
    response = await client.get(
        "/api/calendar",
        params={
            "start": "2026-11-01T00:00:00",
            "end": "2026-12-01T00:00:00",
        },
    )
    assert response.status_code == 422


async def test_calendar_rejects_reversed_range(client):
    start = datetime(2026, 12, 1, tzinfo=UTC)
    end = datetime(2026, 11, 1, tzinfo=UTC)
    response = await _calendar(client, start, end)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_range"


async def test_calendar_rejects_range_over_90_days(client):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=91)
    response = await _calendar(client, start, end)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_range"
