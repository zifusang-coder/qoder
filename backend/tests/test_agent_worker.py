import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main  # noqa: F401  # 先注册全部模型，create_all 才能建全表
import app.worker as worker_module
from app.config import DEFAULT_TENANT_ID, get_settings
from app.llm.tools import ToolTurn
from app.llm.types import Usage
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import Base
from app.models.tenant import Tenant
from app.storage import get_storage
from app.worker import (
    WorkerSettings,
    reconcile_agent_runs,
    run_agent_job,
)

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


class FailingQueue:
    async def enqueue_job(self, name, *args):
        raise RuntimeError("queue unavailable")


class _SameSessionFactory:
    """Yield one prepared session so a test can hold a stale identity map."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return _SameSessionContext(self._session)


class _SameSessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


class FakeGateway:
    def __init__(self, turns=(), error=None):
        self.turns = list(turns)
        self.error = error
        self.calls = 0

    async def complete_tool_calls(self, messages, *, tools):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.turns.pop(0)


def final_turn(content="完成了"):
    return ToolTurn(
        content=content,
        tool_calls=[],
        finish_reason="stop",
        usage=Usage(10, 5),
    )


@pytest_asyncio.fixture
async def worker_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'agent-worker.db'}"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                Tenant(
                    id=DEFAULT_TENANT_ID,
                    name="默认租户",
                    slug="default",
                    status="active",
                ),
                Tenant(
                    id=OTHER_TENANT,
                    name="其他租户",
                    slug="other",
                    status="active",
                ),
            ]
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


async def new_run(factory, **overrides):
    values = {
        "tenant_id": DEFAULT_TENANT_ID,
        "user_id": uuid.uuid4(),
        "goal": "写一篇周报",
        "budget": {},
        "policy": {},
    }
    values.update(overrides)
    async with factory() as session:
        run = AgentRun(**values)
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run


async def load_run(factory, run_id):
    async with factory() as session:
        return await session.get(AgentRun, run_id)


async def test_run_agent_job_completes_run(worker_factory):
    run = await new_run(worker_factory)
    gateway = FakeGateway([final_turn()])

    await run_agent_job(
        None,
        str(run.id),
        session_factory=worker_factory,
        gateway=gateway,
        storage=get_storage(),
        queue=None,
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "succeeded"
    assert row.result_text == "完成了"
    assert row.finished_at is not None
    assert gateway.calls == 1


async def test_run_agent_job_requires_llm_provider(worker_factory):
    run = await new_run(worker_factory)

    await run_agent_job(
        None,
        str(run.id),
        session_factory=worker_factory,
        storage=get_storage(),
        queue=None,
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "llm_not_configured"


async def test_run_agent_job_ignores_finished_run(worker_factory):
    run = await new_run(worker_factory, status="succeeded")
    gateway = FakeGateway([final_turn("不应调用")])

    await run_agent_job(
        None,
        str(run.id),
        session_factory=worker_factory,
        gateway=gateway,
        storage=get_storage(),
        queue=None,
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "succeeded"
    assert gateway.calls == 0


async def test_run_agent_job_wraps_unexpected_error(worker_factory):
    run = await new_run(worker_factory)
    gateway = FakeGateway(error=RuntimeError("boom"))

    await run_agent_job(
        None,
        str(run.id),
        session_factory=worker_factory,
        gateway=gateway,
        storage=get_storage(),
        queue=None,
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "agent_internal_error"


async def test_run_agent_job_duplicate_delivery_executes_once(worker_factory):
    run = await new_run(worker_factory)
    gateway = FakeGateway([final_turn()])

    for _ in range(2):
        await run_agent_job(
            None,
            str(run.id),
            session_factory=worker_factory,
            gateway=gateway,
            storage=get_storage(),
            queue=None,
        )

    row = await load_run(worker_factory, run.id)
    assert row.status == "succeeded"
    assert gateway.calls == 1
    async with worker_factory() as session:
        steps = (
            await session.execute(
                select(AgentStep).where(AgentStep.run_id == run.id)
            )
        ).scalars().all()
    assert len(steps) == 1


async def test_run_agent_job_fails_suspended_tenant_without_tool(worker_factory):
    run = await new_run(worker_factory)
    async with worker_factory() as session:
        tenant = await session.get(Tenant, DEFAULT_TENANT_ID)
        tenant.status = "suspended"
        await session.commit()

    gateway = FakeGateway([final_turn("不应调用")])
    await run_agent_job(
        None,
        str(run.id),
        session_factory=worker_factory,
        gateway=gateway,
        storage=get_storage(),
        queue=None,
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "tenant_suspended"
    assert row.error_message == "租户已停用"
    assert gateway.calls == 0
    async with worker_factory() as session:
        steps = (
            await session.execute(
                select(AgentStep).where(AgentStep.run_id == run.id)
            )
        ).scalars().all()
    assert steps == []


async def test_run_agent_job_setup_failure_fails_queued_run(
    worker_factory, monkeypatch
):
    run = await new_run(worker_factory)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("gateway lookup crashed")

    monkeypatch.setattr("app.worker.load_gateway_default", boom)

    await run_agent_job(
        None,
        str(run.id),
        session_factory=worker_factory,
        storage=get_storage(),
        queue=None,
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "agent_internal_error"


async def test_run_agent_job_last_resort_leaves_waiting_approval(
    worker_factory, monkeypatch
):
    run = await new_run(worker_factory)

    async def crash(session, run_obj, **_kwargs):
        row = await session.get(AgentRun, run_obj.id)
        row.status = "waiting_approval"
        await session.commit()
        raise RuntimeError("boom after pausing")

    monkeypatch.setattr("app.worker.run_agent", crash)

    await run_agent_job(
        None,
        str(run.id),
        session_factory=worker_factory,
        gateway=FakeGateway([final_turn()]),
        storage=get_storage(),
        queue=None,
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "waiting_approval"
    assert row.error_code == ""


async def test_reconcile_requeues_stale_run(worker_factory):
    stale = datetime.now(UTC) - timedelta(minutes=10)
    run = await new_run(
        worker_factory, status="running", heartbeat_at=stale, attempts=0
    )
    queue = FakeQueue()

    handled = await reconcile_agent_runs(
        session_factory=worker_factory, queue=queue
    )

    row = await load_run(worker_factory, run.id)
    assert handled == 1
    assert row.status == "queued"
    assert row.attempts == 1
    assert queue.jobs == [("run_agent_job", str(run.id))]


async def test_reconcile_uses_ctx_redis_queue(worker_factory):
    stale = datetime.now(UTC) - timedelta(minutes=10)
    run = await new_run(
        worker_factory, status="running", heartbeat_at=stale, attempts=0
    )
    queue = FakeQueue()

    # arq cron 只传 ctx；生产路径必须从 ctx["redis"] 取到队列并重排。
    handled = await reconcile_agent_runs(
        {"redis": queue}, session_factory=worker_factory
    )

    row = await load_run(worker_factory, run.id)
    assert handled == 1
    assert row.status == "queued"
    assert row.attempts == 1
    assert queue.jobs == [("run_agent_job", str(run.id))]


async def test_reconcile_without_any_queue_fails_stale(worker_factory):
    stale = datetime.now(UTC) - timedelta(minutes=10)
    run = await new_run(
        worker_factory, status="running", heartbeat_at=stale, attempts=0
    )

    handled = await reconcile_agent_runs(
        None, session_factory=worker_factory
    )

    row = await load_run(worker_factory, run.id)
    assert handled == 1
    assert row.status == "failed"
    assert row.error_code == "agent_stale"


async def test_reconcile_does_not_requeue_cancelled_run(
    worker_factory, monkeypatch
):
    stale = datetime.now(UTC) - timedelta(minutes=10)
    run = await new_run(
        worker_factory, status="running", heartbeat_at=stale, attempts=0
    )
    queue = FakeQueue()

    async with worker_factory() as session:
        loaded = await session.get(AgentRun, run.id)
        assert loaded.status == "running"
        # 模拟 SELECT 之后、对账 UPDATE 之前用户取消：数据库已 cancelled，
        # 但当前会话的 identity map 仍认为它是 running。
        await session.execute(
            update(AgentRun)
            .where(AgentRun.id == run.id)
            .values(status="cancelled")
            .execution_options(synchronize_session=False)
        )
        await session.commit()

        async def stale_ids(_session, _cutoff, _limit):
            return [run.id]

        monkeypatch.setattr("app.worker._stale_agent_run_ids", stale_ids)

        handled = await reconcile_agent_runs(
            session_factory=_SameSessionFactory(session), queue=queue
        )

    assert handled == 0
    assert queue.jobs == []
    async with worker_factory() as verify:
        row = await verify.get(AgentRun, run.id)
        assert row.status == "cancelled"
        assert row.attempts == 0


async def test_reconcile_fails_run_after_two_attempts(worker_factory):
    stale = datetime.now(UTC) - timedelta(minutes=10)
    run = await new_run(
        worker_factory, status="running", heartbeat_at=stale, attempts=2
    )

    await reconcile_agent_runs(
        session_factory=worker_factory, queue=FakeQueue()
    )

    row = await load_run(worker_factory, run.id)
    assert row.status == "failed"
    assert row.error_code == "agent_stale"
    assert row.error_message == "运行超时未完成，请重试"


async def test_reconcile_leaves_stale_waiting_approval_untouched(worker_factory):
    stale = datetime.now(UTC) - timedelta(minutes=10)
    run = await new_run(
        worker_factory,
        status="waiting_approval",
        heartbeat_at=stale,
        attempts=1,
    )
    queue = FakeQueue()

    handled = await reconcile_agent_runs(
        session_factory=worker_factory, queue=queue
    )

    row = await load_run(worker_factory, run.id)
    assert handled == 0
    assert row.status == "waiting_approval"
    assert row.attempts == 1
    assert queue.jobs == []


async def test_reconcile_fails_run_when_enqueue_raises(worker_factory):
    stale = datetime.now(UTC) - timedelta(minutes=10)
    run = await new_run(
        worker_factory, status="running", heartbeat_at=stale, attempts=0
    )

    handled = await reconcile_agent_runs(
        session_factory=worker_factory, queue=FailingQueue()
    )

    row = await load_run(worker_factory, run.id)
    assert handled == 1
    assert row.status == "failed"
    assert row.error_code == "agent_stale"
    assert row.error_message == "运行超时未完成，请重试"


async def test_reconcile_isolates_failing_row(worker_factory, monkeypatch):
    now = datetime.now(UTC)
    first = await new_run(
        worker_factory,
        status="running",
        heartbeat_at=now - timedelta(minutes=20),
        attempts=2,
    )
    second = await new_run(
        worker_factory,
        status="running",
        heartbeat_at=now - timedelta(minutes=10),
        attempts=2,
    )
    real_fail_run = worker_module.fail_run

    async def flaky_fail_run(session, run, code, message):
        if run.id == first.id:
            raise RuntimeError("row-level failure")
        await real_fail_run(session, run, code, message)

    monkeypatch.setattr("app.worker.fail_run", flaky_fail_run)

    handled = await reconcile_agent_runs(
        session_factory=worker_factory, queue=FakeQueue()
    )

    assert handled == 1
    assert (await load_run(worker_factory, second.id)).status == "failed"
    # 失败行回滚后仍保持原状，等待下一次对账。
    assert (await load_run(worker_factory, first.id)).status == "running"


async def test_reconcile_expires_pending_approvals(worker_factory):
    async with worker_factory() as session:
        run = AgentRun(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=uuid.uuid4(),
            goal="等待审批",
            status="waiting_approval",
        )
        session.add(run)
        await session.flush()
        step = AgentStep(
            tenant_id=DEFAULT_TENANT_ID,
            run_id=run.id,
            step_index=1,
            kind="approval",
            tool_name="project.create",
            status="skipped",
        )
        session.add(step)
        await session.flush()
        approval = AgentApproval(
            tenant_id=DEFAULT_TENANT_ID,
            run_id=run.id,
            step_id=step.id,
            tool_name="project.create",
            risk="low",
            payload={},
            status="pending",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(approval)
        await session.commit()
        run_id = run.id
        approval_id = approval.id

    await reconcile_agent_runs(
        session_factory=worker_factory, queue=FakeQueue()
    )

    async with worker_factory() as session:
        run = await session.get(AgentRun, run_id)
        approval = await session.get(AgentApproval, approval_id)
        assert approval.status == "expired"
        assert run.status == "failed"
        assert run.error_code == "approval_expired"


async def test_approval_expiry_runs_even_if_reconcile_raises(
    worker_factory, monkeypatch
):
    async with worker_factory() as session:
        run = AgentRun(
            tenant_id=DEFAULT_TENANT_ID,
            user_id=uuid.uuid4(),
            goal="等待审批",
            status="waiting_approval",
        )
        session.add(run)
        await session.flush()
        step = AgentStep(
            tenant_id=DEFAULT_TENANT_ID,
            run_id=run.id,
            step_index=1,
            kind="approval",
            tool_name="project.create",
            status="skipped",
        )
        session.add(step)
        await session.flush()
        approval = AgentApproval(
            tenant_id=DEFAULT_TENANT_ID,
            run_id=run.id,
            step_id=step.id,
            tool_name="project.create",
            risk="low",
            payload={},
            status="pending",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        session.add(approval)
        await session.commit()
        run_id = run.id
        approval_id = approval.id

    async def boom(*_args, **_kwargs):
        raise RuntimeError("reconcile query failed")

    monkeypatch.setattr("app.worker._stale_agent_run_ids", boom)

    handled = await reconcile_agent_runs(
        session_factory=worker_factory, queue=FakeQueue()
    )

    assert handled == 0
    async with worker_factory() as session:
        run = await session.get(AgentRun, run_id)
        approval = await session.get(AgentApproval, approval_id)
        assert approval.status == "expired"
        assert run.status == "failed"
        assert run.error_code == "approval_expired"


async def test_reconcile_result_survives_expiry_failure(worker_factory, monkeypatch):
    stale = datetime.now(UTC) - timedelta(minutes=10)
    run = await new_run(
        worker_factory, status="running", heartbeat_at=stale, attempts=0
    )
    queue = FakeQueue()

    async def boom(*_args, **_kwargs):
        raise RuntimeError("expiry sweep failed")

    monkeypatch.setattr("app.worker._expire_due_agent_approvals", boom)

    handled = await reconcile_agent_runs(
        session_factory=worker_factory, queue=queue
    )

    row = await load_run(worker_factory, run.id)
    assert handled == 1
    assert row.status == "queued"
    assert row.attempts == 1
    assert queue.jobs == [("run_agent_job", str(run.id))]


def test_worker_registers_agent_job():
    assert run_agent_job in WorkerSettings.functions
    assert get_settings().agent_run_stale_seconds > 0
