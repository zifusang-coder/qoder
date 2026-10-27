import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import DEFAULT_TENANT_ID, get_settings
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PublishEvent, PublishJob
from app.publish.base import PublishResult
from app.worker import (
    STALE_PUBLISH_MESSAGE,
    WorkerSettings,
    list_due_jobs,
    publish_due_jobs,
    reconcile_stale_publishing,
)


async def _seed_content(db_session, *, platform="wechat", title="定时内容", body="正文"):
    project = Project(
        tenant_id=DEFAULT_TENANT_ID, title=f"项目-{uuid.uuid4().hex[:8]}"
    )
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=DEFAULT_TENANT_ID,
        project_id=project.id,
        platform=platform,
        title=title,
        current_version=1,
    )
    db_session.add(content)
    await db_session.flush()
    db_session.add(
        ContentVersion(
            tenant_id=DEFAULT_TENANT_ID,
            content_id=content.id,
            version=1,
            title=title,
            body=body,
        )
    )
    await db_session.flush()
    return content


def _job(content, *, key, status, scheduled_at, **extra):
    return PublishJob(
        tenant_id=DEFAULT_TENANT_ID,
        content_id=content.id,
        content_version=1,
        platform=content.platform,
        status=status,
        idempotency_key=key,
        scheduled_at=scheduled_at,
        **extra,
    )


async def test_list_due_jobs_only_approved_wechat_and_due(db_session):
    now = datetime.now(UTC)
    wechat = await _seed_content(db_session, platform="wechat")
    xiaohongshu = await _seed_content(
        db_session, platform="xiaohongshu", title="小红书内容"
    )

    db_session.add_all(
        [
            _job(
                wechat,
                key="k1",
                status="approved",
                scheduled_at=now - timedelta(minutes=1),
            ),
            _job(
                wechat,
                key="k2",
                status="approved",
                scheduled_at=now + timedelta(hours=1),
            ),
            _job(
                wechat,
                key="k3",
                status="draft",
                scheduled_at=now - timedelta(minutes=5),
            ),
            _job(
                xiaohongshu,
                key="k4",
                status="approved",
                scheduled_at=now - timedelta(minutes=2),
            ),
        ]
    )
    await db_session.commit()

    due = await list_due_jobs(db_session, now)

    assert [job.idempotency_key for job in due] == ["k1"]


async def test_reconcile_stale_publishing_only_stale(db_session):
    now = datetime.now(UTC)
    content = await _seed_content(db_session)

    stale = _job(
        content,
        key="stale",
        status="publishing",
        scheduled_at=now - timedelta(minutes=5),
        attempts=2,
        error="发布请求已提交，等待平台确认",
        updated_at=now - timedelta(seconds=1000),
    )
    fresh = _job(
        content,
        key="fresh",
        status="publishing",
        scheduled_at=now - timedelta(minutes=4),
        updated_at=now,
    )
    published = _job(
        content,
        key="published",
        status="published",
        scheduled_at=now - timedelta(minutes=3),
        updated_at=now - timedelta(seconds=1000),
    )
    db_session.add_all([stale, fresh, published])
    await db_session.commit()
    stale_id, fresh_id, published_id = stale.id, fresh.id, published.id

    assert await reconcile_stale_publishing(db_session, now) == 1

    db_session.expire_all()
    stale_job = (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == stale_id)
        )
    ).scalar_one()
    fresh_job = (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == fresh_id)
        )
    ).scalar_one()
    published_job = (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == published_id)
        )
    ).scalar_one()

    assert stale_job.status == "publish_failed"
    assert stale_job.error == STALE_PUBLISH_MESSAGE
    assert stale_job.attempts == 2
    assert fresh_job.status == "publishing"
    assert published_job.status == "published"

    event = (
        await db_session.execute(
            select(PublishEvent).where(
                PublishEvent.job_id == stale_id,
                PublishEvent.action == "fail",
            )
        )
    ).scalar_one()
    assert event.actor == "scheduler"
    assert event.from_status == "publishing"
    assert event.to_status == "publish_failed"


class FakeAdapter:
    platform = "wechat"
    capability = "auto"
    calls = 0

    async def publish(self, *, account, title, body):
        self.calls += 1
        return PublishResult(external_id="draft-42")


class _SharedSession:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _factory(session):
    return lambda: _SharedSession(session)


async def test_publish_due_jobs_executes_once(client, db_session):
    account_response = await client.post(
        "/api/accounts",
        json={
            "platform": "wechat",
            "name": "定时账号",
            "credential": {"app_id": "wx123", "app_secret": "secret"},
        },
    )
    assert account_response.status_code == 201, account_response.text
    account = account_response.json()

    project_response = await client.post(
        "/api/projects", json={"title": f"定时项目-{uuid.uuid4().hex[:8]}"}
    )
    assert project_response.status_code == 201
    body = "定时发布正文。" * 50
    assert len(body) >= 300
    content_response = await client.post(
        f"/api/projects/{project_response.json()['id']}/contents",
        json={"platform": "wechat", "title": "定时发布标题", "body": body},
    )
    assert content_response.status_code == 201, content_response.text
    content = content_response.json()

    job_response = await client.post(
        "/api/publish/jobs",
        json={
            "content_id": content["id"],
            "platform": "wechat",
            "account_id": account["id"],
        },
    )
    assert job_response.status_code == 201, job_response.text
    job_id = job_response.json()["id"]

    precheck = await client.post(f"/api/publish/jobs/{job_id}/precheck")
    assert precheck.status_code == 200, precheck.text
    assert precheck.json()["passed"] is True
    assert precheck.json()["job"]["status"] == "awaiting_review"

    approve = await client.post(
        f"/api/publish/jobs/{job_id}/actions", json={"action": "approve"}
    )
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == "approved"

    scheduled_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    scheduled = await client.patch(
        f"/api/publish/jobs/{job_id}/schedule",
        json={"scheduled_at": scheduled_at},
    )
    assert scheduled.status_code == 200, scheduled.text

    adapter = FakeAdapter()
    executed = await publish_due_jobs(
        session_factory=_factory(db_session),
        adapter_factory=lambda: adapter,
        now=datetime.now(UTC),
    )
    assert executed == 1

    db_session.expire_all()
    stored = (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == uuid.UUID(job_id))
        )
    ).scalar_one()
    assert stored.status == "published"
    assert stored.external_id == "draft-42"
    assert stored.attempts == 1

    again = await publish_due_jobs(
        session_factory=_factory(db_session),
        adapter_factory=lambda: adapter,
        now=datetime.now(UTC),
    )
    assert again == 0
    assert adapter.calls == 1

    db_session.expire_all()
    events = (
        await db_session.execute(
            select(PublishEvent).where(
                PublishEvent.job_id == uuid.UUID(job_id),
                PublishEvent.action.in_(["publish", "succeed"]),
            )
        )
    ).scalars().all()
    assert len(events) == 2
    assert {event.action for event in events} == {"publish", "succeed"}
    assert all(event.actor == "scheduler" for event in events)


def test_worker_settings_wire_cron_and_redis():
    assert WorkerSettings.functions
    assert publish_due_jobs in WorkerSettings.functions
    assert WorkerSettings.cron_jobs
    assert WorkerSettings.cron_jobs[0].coroutine is publish_due_jobs

    parsed = WorkerSettings.redis_settings
    expected = type(parsed).from_dsn(get_settings().redis_url)
    assert (parsed.host, parsed.port, parsed.database) == (
        expected.host,
        expected.port,
        expected.database,
    )
