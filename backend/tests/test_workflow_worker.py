import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import worker
from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID, Base
from app.models.tenant import Tenant
from app.models.workflow import (
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowSchedule,
    WorkflowVersion,
)

NOW = datetime.now(UTC)

GRAPH = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "data": {"fields": [{"key": "topic", "type": "string"}]},
        },
        {"id": "out", "type": "output", "data": {}},
    ],
    "edges": [{"source": "in", "target": "out"}],
}

CADENCE = {
    "kind": "daily",
    "weekdays": [],
    "time": "09:00",
    "timezone": "Asia/Shanghai",
}


def aware(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


class DownQueue:
    async def enqueue_job(self, name, *args):
        raise RuntimeError("redis down")


@pytest_asyncio.fixture
async def worker_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'workflow-worker.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            Tenant(
                id=DEFAULT_TENANT_ID,
                name="默认租户",
                slug="default",
                status="active",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def new_run(
    factory,
    *,
    status="queued",
    attempts=0,
    heartbeat_at=None,
):
    async with factory() as session:
        workflow = Workflow(
            tenant_id=DEFAULT_TENANT_ID, name="测试流", created_by=uuid.uuid4()
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            tenant_id=DEFAULT_TENANT_ID,
            workflow_id=workflow.id,
            version=1,
            graph=GRAPH,
            source_revision=0,
            published_by=workflow.created_by,
            published_at=datetime.now(UTC),
        )
        session.add(version)
        await session.flush()
        run = WorkflowRun(
            tenant_id=DEFAULT_TENANT_ID,
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            status=status,
            inputs={"topic": "AI 周报"},
            policy={},
            triggered_by=None,
            attempts=attempts,
            heartbeat_at=heartbeat_at,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def load_run(factory, run_id):
    async with factory() as session:
        return await session.get(WorkflowRun, run_id)


async def new_schedule(factory, *, archived=False, next_run_at=None):
    async with factory() as session:
        workflow = Workflow(
            tenant_id=DEFAULT_TENANT_ID,
            name="定时流",
            created_by=uuid.uuid4(),
            archived_at=datetime.now(UTC) if archived else None,
        )
        session.add(workflow)
        await session.flush()
        version = WorkflowVersion(
            tenant_id=DEFAULT_TENANT_ID,
            workflow_id=workflow.id,
            version=1,
            graph=GRAPH,
            source_revision=0,
            published_by=workflow.created_by,
            published_at=datetime.now(UTC),
        )
        session.add(version)
        await session.flush()
        slot = next_run_at or NOW - timedelta(minutes=5)
        schedule = WorkflowSchedule(
            tenant_id=DEFAULT_TENANT_ID,
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            cadence=dict(CADENCE),
            inputs={"topic": "定时早报"},
            enabled=True,
            next_run_at=slot,
            created_by=workflow.created_by,
        )
        session.add(schedule)
        await session.commit()
        await session.refresh(schedule)
        return schedule, slot


async def all_runs(factory):
    async with factory() as session:
        return (
            (await session.execute(select(WorkflowRun))).scalars().all()
        )


async def test_run_workflow_job_completes_queued_run(worker_factory):
    run = await new_run(worker_factory)

    await worker.run_workflow_job(
        None, str(run.id), session_factory=worker_factory
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "succeeded"
    assert row.attempts == 1
    assert row.started_at is not None
    async with worker_factory() as session:
        nodes = (
            (
                await session.execute(
                    select(WorkflowNodeRun).where(
                        WorkflowNodeRun.run_id == run.id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {(item.node_id, item.status) for item in nodes} == {
        ("in", "ok"),
        ("out", "ok"),
    }


async def test_run_workflow_job_skips_run_that_is_not_queued(worker_factory):
    run = await new_run(worker_factory, status="running", attempts=1)

    await worker.run_workflow_job(
        None, str(run.id), session_factory=worker_factory
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "running"
    assert row.attempts == 1


async def test_run_workflow_job_fails_suspended_tenant(worker_factory):
    run = await new_run(worker_factory)
    async with worker_factory() as session:
        tenant = await session.get(Tenant, DEFAULT_TENANT_ID)
        tenant.status = "suspended"
        await session.commit()

    await worker.run_workflow_job(
        None, str(run.id), session_factory=worker_factory
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "tenant_suspended"


async def test_run_workflow_job_fails_run_when_engine_crashes(
    worker_factory, monkeypatch
):
    run = await new_run(worker_factory)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "run_workflow", boom)

    await worker.run_workflow_job(
        None,
        str(run.id),
        session_factory=worker_factory,
        heartbeat_seconds=0.01,
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "workflow_internal_error"


async def test_heartbeat_loop_refreshes_active_run(worker_factory):
    run = await new_run(worker_factory)

    task = asyncio.create_task(
        worker._heartbeat_loop(worker_factory, run.id, interval=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await load_run(worker_factory, run.id)
    assert row.heartbeat_at is not None


async def test_heartbeat_loop_ignores_finished_run(worker_factory):
    run = await new_run(worker_factory, status="succeeded")

    task = asyncio.create_task(
        worker._heartbeat_loop(worker_factory, run.id, interval=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await load_run(worker_factory, run.id)
    assert row.heartbeat_at is None


async def test_fire_schedule_creates_and_enqueues_run(worker_factory):
    schedule, slot = await new_schedule(worker_factory)
    queue = FakeQueue()

    fired = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory, queue=queue, now=NOW
    )

    assert fired == 1
    runs = await all_runs(worker_factory)
    assert len(runs) == 1
    created = runs[0]
    assert created.trigger == "schedule"
    assert created.triggered_by is None
    assert created.schedule_id == schedule.id
    assert aware(created.scheduled_for) == slot
    assert created.status == "queued"
    assert created.policy
    assert queue.jobs == [("run_workflow_job", str(created.id))]
    async with worker_factory() as session:
        row = await session.get(WorkflowSchedule, schedule.id)
    assert aware(row.last_run_at) == slot
    assert aware(row.next_run_at) > NOW


async def test_fire_schedule_is_idempotent_for_the_same_slot(worker_factory):
    await new_schedule(worker_factory)
    queue = FakeQueue()

    first = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory, queue=queue, now=NOW
    )
    second = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory, queue=queue, now=NOW
    )

    assert first == 1
    assert second == 0
    assert len(await all_runs(worker_factory)) == 1
    assert len(queue.jobs) == 1


async def test_fire_schedule_skips_archived_workflow_and_advances(
    worker_factory,
):
    schedule, slot = await new_schedule(worker_factory, archived=True)

    fired = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory,
        queue=FakeQueue(),
        now=NOW,
    )

    assert fired == 0
    assert await all_runs(worker_factory) == []
    async with worker_factory() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "workflow.schedule.skipped"
                    )
                )
            )
            .scalars()
            .all()
        )
        row = await session.get(WorkflowSchedule, schedule.id)
    assert len(audits) == 1
    assert audits[0].detail["reason"] == "workflow_archived"
    assert aware(row.last_run_at) == slot
    assert aware(row.next_run_at) > NOW


async def test_fire_schedule_skips_when_workflow_has_active_run(
    worker_factory,
):
    schedule, _slot = await new_schedule(worker_factory)
    async with worker_factory() as session:
        session.add(
            WorkflowRun(
                tenant_id=DEFAULT_TENANT_ID,
                workflow_id=schedule.workflow_id,
                workflow_version_id=schedule.workflow_version_id,
                status="queued",
                inputs={},
                policy={},
            )
        )
        await session.commit()

    fired = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory,
        queue=FakeQueue(),
        now=NOW,
    )

    assert fired == 0
    assert len(await all_runs(worker_factory)) == 1
    async with worker_factory() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "workflow.schedule.skipped"
                )
            )
        ).scalars().one()
    assert audit.detail["reason"] == "active_run"


async def test_fire_schedule_fails_run_when_queue_is_unavailable(
    worker_factory,
):
    schedule, _slot = await new_schedule(worker_factory)

    fired = await worker.fire_due_workflow_schedules(
        session_factory=worker_factory,
        queue=DownQueue(),
        now=NOW,
    )

    assert fired == 0
    runs = await all_runs(worker_factory)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error_code == "queue_unavailable"
    async with worker_factory() as session:
        row = await session.get(WorkflowSchedule, schedule.id)
    assert aware(row.next_run_at) > NOW


async def test_reconcile_requeues_stale_run(worker_factory):
    run = await new_run(
        worker_factory,
        status="running",
        attempts=1,
        heartbeat_at=NOW - timedelta(minutes=10),
    )
    queue = FakeQueue()

    counts = await worker.reconcile_workflow_runs(
        session_factory=worker_factory, queue=queue, now=NOW
    )

    assert counts["requeued"] == 1
    row = await load_run(worker_factory, run.id)
    assert row.status == "queued"
    assert queue.jobs == [("run_workflow_job", str(run.id))]


async def test_reconcile_fails_run_when_requeue_cannot_enqueue(
    worker_factory,
):
    run = await new_run(
        worker_factory,
        status="running",
        attempts=1,
        heartbeat_at=NOW - timedelta(minutes=10),
    )

    await worker.reconcile_workflow_runs(
        session_factory=worker_factory, queue=DownQueue(), now=NOW
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "queue_unavailable"


async def test_reconcile_fails_run_after_second_stale_attempt(
    worker_factory,
):
    run = await new_run(
        worker_factory,
        status="running",
        attempts=2,
        heartbeat_at=NOW - timedelta(minutes=10),
    )

    counts = await worker.reconcile_workflow_runs(
        session_factory=worker_factory, queue=FakeQueue(), now=NOW
    )

    assert counts["stale_failed"] == 1
    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "workflow_stale"
