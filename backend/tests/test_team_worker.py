import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


async def make_parent(db_session, status):
    run = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=uuid.uuid4(), goal="g",
        mode="team", status=status,
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def make_child(db_session, parent, status, *, approval_status=None):
    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="t",
        mode="role", role="creator", parent_run_id=parent.id, status=status,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(child)
    if approval_status:
        step = AgentStep(
            tenant_id=DEFAULT_TENANT_ID, run_id=child.id, step_index=0,
            kind="tool", tool_name="demo.write", status="waiting",
        )
        db_session.add(step)
        await db_session.commit()
        await db_session.refresh(step)
        approval = AgentApproval(
            tenant_id=DEFAULT_TENANT_ID, run_id=child.id, step_id=step.id,
            tool_name="demo.write", risk="low", payload={},
            status=approval_status,
        )
        db_session.add(approval)
        await db_session.commit()
    return child


@pytest.mark.asyncio
async def test_waiting_team_with_approved_requeues(db_session):
    parent = await make_parent(db_session, "waiting_approval")
    await make_child(db_session, parent, "waiting_approval",
                     approval_status="approved")
    queue = FakeQueue()
    from app.worker import _settle_waiting_team_parents
    handled = await _settle_waiting_team_parents(
        db_session, moment=datetime.now(UTC), queue=queue,
    )
    assert handled == 1
    await db_session.refresh(parent)
    assert parent.status == "queued"
    assert queue.jobs == [("run_agent_job", str(parent.id))]


@pytest.mark.asyncio
async def test_waiting_team_zero_pending_no_approved_fails(db_session):
    parent = await make_parent(db_session, "waiting_approval")
    await make_child(db_session, parent, "waiting_approval",
                     approval_status="expired")
    from app.worker import _settle_waiting_team_parents
    handled = await _settle_waiting_team_parents(
        db_session, moment=datetime.now(UTC), queue=None,
    )
    assert handled == 1
    await db_session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "approval_expired"


@pytest.mark.asyncio
async def test_terminal_team_closes_stragglers(db_session):
    parent = await make_parent(db_session, "failed")
    child = await make_child(db_session, parent, "queued")
    from app.worker import _close_terminal_team_stragglers
    handled = await _close_terminal_team_stragglers(db_session)
    assert handled == 1
    await db_session.refresh(child)
    assert child.status == "cancelled"


@pytest.mark.asyncio
async def test_stale_scan_skips_role_children(db_session):
    from app.worker import _stale_agent_run_ids
    parent = await make_parent(db_session, "running")
    await make_child(db_session, parent, "running")
    cutoff = datetime.now(UTC)
    ids = await _stale_agent_run_ids(db_session, cutoff, 100)
    assert parent.id in ids
    children = (await db_session.execute(
        select(AgentRun.id).where(AgentRun.mode == "role")
    )).scalars().all()
    assert not set(children) & set(ids)