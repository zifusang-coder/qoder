import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main  # noqa: F401  # 先注册全部模型，create_all 才能建全表
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


def test_worker_registers_agent_job():
    assert run_agent_job in WorkerSettings.functions
    assert get_settings().agent_run_stale_seconds > 0
