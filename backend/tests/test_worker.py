import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main  # noqa: F401  # 先注册全部模型，create_all 才能建全表
from app.config import DEFAULT_TENANT_ID
from app.models.base import Base
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.publish.base import PublishError, PublishResult
from app.publish.state import InvalidTransition
from app.services.publish import MAX_PUBLISH_ATTEMPTS
from app.worker import (
    MAX_BATCH_SIZE,
    STALE_PUBLISH_MESSAGE,
    WorkerSettings,
    list_due_jobs,
    publish_due_jobs,
    reconcile_stale_publishing,
)

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"
MISSING_ACCOUNT_MESSAGE = "请先选择并授权发布账号"
CONTENT_CHANGED_MESSAGE = "内容已更新，请基于最新版本重新创建发布任务"


@pytest_asyncio.fixture
async def worker_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_content(
    session,
    *,
    tenant_id=DEFAULT_TENANT_ID,
    platform="wechat",
    title="定时内容",
    body="正文",
):
    project = Project(tenant_id=tenant_id, title=f"项目-{uuid.uuid4().hex[:8]}")
    session.add(project)
    await session.flush()
    content = Content(
        tenant_id=tenant_id,
        project_id=project.id,
        platform=platform,
        title=title,
        current_version=1,
    )
    session.add(content)
    await session.flush()
    session.add(
        ContentVersion(
            tenant_id=tenant_id,
            content_id=content.id,
            version=1,
            title=title,
            body=body,
        )
    )
    await session.commit()
    return content


async def _seed_account(
    session,
    *,
    tenant_id=DEFAULT_TENANT_ID,
    platform="wechat",
    status="active",
):
    account = PlatformAccount(
        tenant_id=tenant_id,
        platform=platform,
        name=f"账号-{uuid.uuid4().hex[:8]}",
        status=status,
    )
    session.add(account)
    await session.commit()
    return account


def _job(
    content,
    *,
    key,
    status,
    scheduled_at,
    tenant_id=None,
    content_version=1,
    **extra,
):
    return PublishJob(
        tenant_id=tenant_id or content.tenant_id,
        content_id=content.id,
        content_version=content_version,
        platform=content.platform,
        status=status,
        idempotency_key=key,
        scheduled_at=scheduled_at,
        **extra,
    )


async def _job_state(factory, job_id):
    """用全新会话读取已提交状态，避免共享会话掩盖事务边界。"""
    async with factory() as session:
        job = (
            await session.execute(select(PublishJob).where(PublishJob.id == job_id))
        ).scalar_one()
        events = (
            await session.execute(
                select(PublishEvent).where(PublishEvent.job_id == job_id)
            )
        ).scalars().all()
        return {
            "status": job.status,
            "attempts": job.attempts,
            "error": job.error,
            "tenant_id": job.tenant_id,
            "external_id": job.external_id,
            "events": [
                (
                    event.action,
                    event.from_status,
                    event.to_status,
                    event.actor,
                    event.tenant_id,
                )
                for event in events
            ],
        }


async def test_list_due_jobs_only_approved_wechat_and_due(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        wechat = await _seed_content(session, platform="wechat")
        xiaohongshu = await _seed_content(
            session, platform="xiaohongshu", title="小红书内容"
        )
        session.add_all(
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
                _job(
                    wechat,
                    key="k5-no-schedule",
                    status="approved",
                    scheduled_at=None,
                ),
            ]
        )
        await session.commit()

        due = await list_due_jobs(session, now)

    assert [job.idempotency_key for job in due] == ["k1"]


async def test_list_due_jobs_respects_batch_limit(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session)
        session.add_all(
            [
                _job(
                    content,
                    key=f"limit-{index}",
                    status="approved",
                    scheduled_at=now - timedelta(minutes=index + 1),
                )
                for index in range(MAX_BATCH_SIZE + 3)
            ]
        )
        await session.commit()

        due = await list_due_jobs(session, now)

    assert len(due) == MAX_BATCH_SIZE
    # 按 scheduled_at 升序只取最早的 10 个（limit-12 最早）
    assert [job.idempotency_key for job in due] == [
        f"limit-{index}" for index in range(MAX_BATCH_SIZE + 2, 2, -1)
    ]


@pytest.mark.parametrize(
    ("job_count", "expect_truncated"),
    [(MAX_BATCH_SIZE, False), (MAX_BATCH_SIZE + 1, True)],
)
async def test_publish_due_jobs_batch_truncation(
    worker_factory, caplog, job_count, expect_truncated
):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session)
        account = await _seed_account(session)
        jobs = [
            _job(
                content,
                key=f"batch-{index}",
                status="approved",
                scheduled_at=now - timedelta(minutes=index + 1),
                account_id=account.id,
            )
            for index in range(job_count)
        ]
        session.add_all(jobs)
        await session.commit()
        job_ids = [job.id for job in jobs]

    adapter = FakeAdapter()
    with caplog.at_level(logging.WARNING, logger="app.worker"):
        executed = await publish_due_jobs(
            session_factory=worker_factory,
            adapter_factory=lambda: adapter,
            now=now,
        )

    expected = min(job_count, MAX_BATCH_SIZE)
    assert executed == expected
    assert adapter.calls == expected

    truncation_warnings = [
        record for record in caplog.records if "单批上限" in record.getMessage()
    ]
    assert bool(truncation_warnings) is expect_truncated

    states = [await _job_state(worker_factory, job_id) for job_id in job_ids]
    assert [state["status"] for state in states].count("published") == expected
    if expect_truncated:
        # index 0 的 scheduled_at 最晚，属于第 11 个任务，留到下一次扫描
        assert states[0]["status"] == "approved"
        assert states[0]["events"] == []


async def test_reconcile_stale_publishing_only_stale(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session)
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
        session.add_all([stale, fresh, published])
        await session.commit()
        stale_id, fresh_id, published_id = stale.id, fresh.id, published.id

        assert await reconcile_stale_publishing(session, now) == 1

    stale_state = await _job_state(worker_factory, stale_id)
    assert stale_state["status"] == "publish_failed"
    assert stale_state["error"] == STALE_PUBLISH_MESSAGE
    assert stale_state["attempts"] == 2
    assert stale_state["events"] == [
        ("fail", "publishing", "publish_failed", "scheduler", DEFAULT_TENANT_ID)
    ]

    assert (await _job_state(worker_factory, fresh_id))["status"] == "publishing"
    assert (await _job_state(worker_factory, published_id))["status"] == "published"


async def test_reconcile_stale_publishing_skips_concurrent_transition(
    worker_factory, monkeypatch, caplog
):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session)
        stale = _job(
            content,
            key="raced",
            status="publishing",
            scheduled_at=now - timedelta(minutes=5),
            updated_at=now - timedelta(seconds=1000),
        )
        session.add(stale)
        await session.commit()
        stale_id = stale.id

    async def already_published(*_args, **_kwargs):
        # 模拟 SELECT 之后另一执行者已把任务推进到 published
        raise InvalidTransition("状态 published 不允许动作 fail")

    monkeypatch.setattr("app.worker.apply_action", already_published)
    with caplog.at_level(logging.ERROR, logger="app.worker"):
        async with worker_factory() as session:
            assert await reconcile_stale_publishing(session, now) == 0

    # 竞态属于正常路径，不应记录异常堆栈
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors == []

    state = await _job_state(worker_factory, stale_id)
    assert state["status"] == "publishing"
    assert state["events"] == []


async def test_publish_due_jobs_reconciles_stale_before_scan(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session)
        account = await _seed_account(session)
        stale = _job(
            content,
            key="stale-in-scan",
            status="publishing",
            scheduled_at=now - timedelta(minutes=5),
            attempts=2,
            error="发布请求已提交，等待平台确认",
            updated_at=now - timedelta(seconds=1000),
        )
        due = _job(
            content,
            key="due-in-scan",
            status="approved",
            scheduled_at=now - timedelta(minutes=1),
            account_id=account.id,
        )
        session.add_all([stale, due])
        await session.commit()
        stale_id, due_id = stale.id, due.id

    adapter = FakeAdapter()
    executed = await publish_due_jobs(
        session_factory=worker_factory,
        adapter_factory=lambda: adapter,
        now=now,
    )

    assert executed == 1
    assert adapter.calls == 1

    stale_state = await _job_state(worker_factory, stale_id)
    assert stale_state["status"] == "publish_failed"
    assert stale_state["attempts"] == 2
    assert stale_state["error"] == STALE_PUBLISH_MESSAGE
    assert stale_state["events"] == [
        ("fail", "publishing", "publish_failed", "scheduler", DEFAULT_TENANT_ID)
    ]

    due_state = await _job_state(worker_factory, due_id)
    assert due_state["status"] == "published"


class FakeAdapter:
    platform = "wechat"
    capability = "auto"
    calls = 0

    async def publish(self, *, account, title, body):
        self.calls += 1
        return PublishResult(external_id="draft-42")


async def test_publish_due_jobs_executes_once(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session, body="定时发布正文。" * 50)
        account = await _seed_account(session)
        job = _job(
            content,
            key="happy",
            status="approved",
            scheduled_at=now - timedelta(minutes=1),
            account_id=account.id,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    adapter = FakeAdapter()
    executed = await publish_due_jobs(
        session_factory=worker_factory,
        adapter_factory=lambda: adapter,
        now=now,
    )
    assert executed == 1

    state = await _job_state(worker_factory, job_id)
    assert state["status"] == "published"
    assert state["external_id"] == "draft-42"
    assert state["attempts"] == 1

    again = await publish_due_jobs(
        session_factory=worker_factory,
        adapter_factory=lambda: adapter,
        now=datetime.now(UTC),
    )
    assert again == 0
    assert adapter.calls == 1

    assert state["events"] == [
        ("publish", "approved", "publishing", "scheduler", DEFAULT_TENANT_ID),
        ("succeed", "publishing", "published", "scheduler", DEFAULT_TENANT_ID),
    ]


async def test_missing_account_fails_job_and_continues_batch(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session)
        account = await _seed_account(session)
        blocked = _job(
            content,
            key="missing-account",
            status="approved",
            scheduled_at=now - timedelta(minutes=2),
        )
        healthy = _job(
            content,
            key="healthy",
            status="approved",
            scheduled_at=now - timedelta(minutes=1),
            account_id=account.id,
        )
        session.add_all([blocked, healthy])
        await session.commit()
        blocked_id, healthy_id = blocked.id, healthy.id

    assert (await _job_state(worker_factory, blocked_id))["status"] == "approved"

    adapter = FakeAdapter()
    executed = await publish_due_jobs(
        session_factory=worker_factory,
        adapter_factory=lambda: adapter,
        now=now,
    )

    assert executed == 1
    assert adapter.calls == 1

    blocked_state = await _job_state(worker_factory, blocked_id)
    assert blocked_state["status"] == "publish_failed"
    assert blocked_state["attempts"] == 0
    assert blocked_state["error"] == MISSING_ACCOUNT_MESSAGE
    assert blocked_state["events"] == [
        ("fail", "approved", "publish_failed", "scheduler", DEFAULT_TENANT_ID)
    ]

    healthy_state = await _job_state(worker_factory, healthy_id)
    assert healthy_state["status"] == "published"
    assert healthy_state["attempts"] == 1


async def test_content_changed_fails_job(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session)
        account = await _seed_account(session)
        job = _job(
            content,
            key="content-changed",
            status="approved",
            scheduled_at=now - timedelta(minutes=1),
            account_id=account.id,
        )
        session.add(job)
        await session.commit()
        job_id = job.id
        content.current_version = 2
        await session.commit()

    adapter = FakeAdapter()
    executed = await publish_due_jobs(
        session_factory=worker_factory,
        adapter_factory=lambda: adapter,
        now=now,
    )

    assert executed == 0
    assert adapter.calls == 0

    state = await _job_state(worker_factory, job_id)
    assert state["status"] == "publish_failed"
    assert state["attempts"] == 0
    assert state["error"] == CONTENT_CHANGED_MESSAGE
    assert state["events"] == [
        ("fail", "approved", "publish_failed", "scheduler", DEFAULT_TENANT_ID)
    ]


async def test_publish_due_jobs_uses_job_tenant(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session, tenant_id=OTHER_TENANT)
        account = await _seed_account(session, tenant_id=OTHER_TENANT)
        job = _job(
            content,
            key="other-tenant",
            status="approved",
            scheduled_at=now - timedelta(minutes=1),
            tenant_id=OTHER_TENANT,
            account_id=account.id,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    adapter = FakeAdapter()
    executed = await publish_due_jobs(
        session_factory=worker_factory,
        adapter_factory=lambda: adapter,
        now=now,
    )
    assert executed == 1
    assert adapter.calls == 1

    state = await _job_state(worker_factory, job_id)
    assert state["tenant_id"] == OTHER_TENANT
    assert state["status"] == "published"
    assert len(state["events"]) == 2
    assert all(event[4] == OTHER_TENANT for event in state["events"])
    assert {(event[0], event[3]) for event in state["events"]} == {
        ("publish", "scheduler"),
        ("succeed", "scheduler"),
    }


BLOCKED_JOB_CASES = [
    ("account_missing", MISSING_ACCOUNT_MESSAGE, 0),
    ("account_inactive", "账号已停用", 0),
    ("account_platform_mismatch", "账号平台与发布平台不一致", 0),
    (
        "too_many_attempts",
        "发布尝试次数已达上限，请人工确认平台状态",
        MAX_PUBLISH_ATTEMPTS,
    ),
    ("empty_content", "发布版本不存在", 0),
]


@pytest.mark.parametrize(("code", "message", "attempts"), BLOCKED_JOB_CASES)
async def test_blocked_conditions_fail_job_deterministically(
    worker_factory, code, message, attempts
):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        content = await _seed_content(session)
        account = await _seed_account(
            session,
            platform="zhihu" if code == "account_platform_mismatch" else "wechat",
            status="inactive" if code == "account_inactive" else "active",
        )
        job = _job(
            content,
            key=f"blocked-{code}",
            status="approved",
            scheduled_at=now - timedelta(minutes=1),
            content_version=2 if code == "empty_content" else 1,
            account_id=None if code == "account_missing" else account.id,
            attempts=attempts,
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    adapter = FakeAdapter()
    executed = await publish_due_jobs(
        session_factory=worker_factory,
        adapter_factory=lambda: adapter,
        now=now,
    )

    assert executed == 0
    assert adapter.calls == 0

    state = await _job_state(worker_factory, job_id)
    assert state["status"] == "publish_failed"
    assert state["attempts"] == attempts
    assert state["error"] == message
    assert state["events"] == [
        ("fail", "approved", "publish_failed", "scheduler", DEFAULT_TENANT_ID)
    ]


class FailingAdapter:
    platform = "wechat"
    capability = "auto"
    failed_body = "平台错误正文"

    def __init__(self):
        self.calls = 0

    async def publish(self, *, account, title, body):
        self.calls += 1
        if body == self.failed_body:
            raise PublishError("平台错误")
        return PublishResult(external_id="draft-ok")


async def test_adapter_error_isolated_to_failing_job(worker_factory):
    now = datetime.now(UTC)
    async with worker_factory() as session:
        failing_content = await _seed_content(
            session, title="失败内容", body="平台错误正文"
        )
        healthy_content = await _seed_content(
            session, title="正常内容", body="正常正文"
        )
        account = await _seed_account(session)
        failing = _job(
            failing_content,
            key="adapter-failing",
            status="approved",
            scheduled_at=now - timedelta(minutes=2),
            account_id=account.id,
        )
        healthy = _job(
            healthy_content,
            key="adapter-healthy",
            status="approved",
            scheduled_at=now - timedelta(minutes=1),
            account_id=account.id,
        )
        session.add_all([failing, healthy])
        await session.commit()
        failing_id, healthy_id = failing.id, healthy.id

    adapter = FailingAdapter()
    executed = await publish_due_jobs(
        session_factory=worker_factory,
        adapter_factory=lambda: adapter,
        now=now,
    )

    assert executed == 1
    assert adapter.calls == 2

    failing_state = await _job_state(worker_factory, failing_id)
    assert failing_state["status"] == "publish_failed"
    assert failing_state["attempts"] == 1
    assert failing_state["error"] == "平台错误"
    assert failing_state["events"] == [
        ("publish", "approved", "publishing", "scheduler", DEFAULT_TENANT_ID),
        ("fail", "publishing", "publish_failed", "scheduler", DEFAULT_TENANT_ID),
    ]

    healthy_state = await _job_state(worker_factory, healthy_id)
    assert healthy_state["status"] == "published"
    assert healthy_state["external_id"] == "draft-ok"


def test_worker_settings_wire_cron():
    assert WorkerSettings.functions == []
    assert len(WorkerSettings.cron_jobs) == 1
    cron_job = WorkerSettings.cron_jobs[0]
    assert cron_job.job_id == "publish-due-jobs"
    assert cron_job.coroutine is publish_due_jobs
    # 每分钟整点执行，不保留结果（worker 每次独立扫描数据库）
    assert cron_job.minute is None
    assert cron_job.second == 0
    assert cron_job.keep_result_s == 0
    assert WorkerSettings.job_timeout == 600
