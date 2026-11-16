import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.workflow import approvals

NOW = datetime.now(UTC)


async def make_run(
    db_session,
    *,
    status: str = "waiting_approval",
    heartbeat_at: datetime | None = None,
    usage: dict | None = None,
) -> WorkflowRun:
    workflow = Workflow(name="测试流", created_by=uuid.uuid4())
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        workflow_id=workflow.id,
        version=1,
        graph={"nodes": [], "edges": []},
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=NOW,
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status=status,
        inputs={},
        policy={},
        usage=dict(usage or {}),
        heartbeat_at=heartbeat_at,
        started_at=NOW - timedelta(minutes=10),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def make_approval(
    db_session,
    run: WorkflowRun,
    *,
    node_id: str = "review",
    kind: str = "review",
    payload: dict | None = None,
) -> WorkflowApproval:
    approval = await approvals.create_approval(
        db_session,
        run=run,
        node_id=node_id,
        kind=kind,
        title="标题",
        payload=payload or {"summary": "内容"},
    )
    await db_session.commit()
    await db_session.refresh(approval)
    return approval


async def add_waiting_node(db_session, run: WorkflowRun, node_id: str):
    node = WorkflowNodeRun(
        run_id=run.id, node_id=node_id, kind="review", status="waiting"
    )
    db_session.add(node)
    await db_session.commit()
    return node


async def add_child_pending(db_session, run: WorkflowRun) -> AgentRun:
    child = AgentRun(
        tenant_id=run.tenant_id,
        user_id=uuid.uuid4(),
        goal="节点任务",
        mode="node",
        status="waiting_approval",
        workflow_run_id=run.id,
        workflow_node_id="agent",
        budget={},
        policy={},
    )
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=run.tenant_id, run_id=child.id, step_index=0, kind="tool"
    )
    db_session.add(step)
    await db_session.flush()
    db_session.add(
        AgentApproval(
            tenant_id=run.tenant_id,
            run_id=child.id,
            step_id=step.id,
            tool_name="content.create_version",
            risk="high",
            payload={},
        )
    )
    await db_session.commit()
    return child


async def decided_status(db_session, approval_id) -> str:
    row = await db_session.get(WorkflowApproval, approval_id)
    await db_session.refresh(row)
    return row.status


def fresh_session(db_session):
    """同一 engine 上的独立会话，用来验证冲突来自数据库而不是会话缓存。"""
    return async_sessionmaker(db_session.bind, expire_on_commit=False)()


async def test_create_approval_is_pending_with_ttl(db_session):
    run = await make_run(db_session)
    approval = await make_approval(db_session, run)
    assert approval.status == "pending"
    assert approval.tenant_id == run.tenant_id
    assert approval.expires_at is not None
    assert approval.expires_at > NOW


async def test_node_approval_prefers_pending_then_latest_decision(db_session):
    run = await make_run(db_session)
    pending = await make_approval(db_session, run, payload={"round": 1})
    resolved = await make_approval(db_session, run, payload={"round": 2})
    resolved.status = "approved"
    resolved.decided_at = NOW + timedelta(minutes=5)
    await db_session.commit()

    found = await approvals.node_approval(
        db_session, tenant_id=run.tenant_id, run_id=run.id, node_id="review"
    )
    assert found.id == pending.id

    pending.status = "expired"
    pending.decided_at = NOW
    await db_session.commit()
    found = await approvals.node_approval(
        db_session, tenant_id=run.tenant_id, run_id=run.id, node_id="review"
    )
    assert found.id == resolved.id


async def test_has_pending_covers_both_channels(db_session):
    run = await make_run(db_session)
    args = {"tenant_id": run.tenant_id, "run_id": run.id}
    assert await approvals.has_pending(db_session, **args) is False

    approval = await make_approval(db_session, run)
    assert await approvals.has_pending(db_session, **args) is True
    await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )
    assert await approvals.has_pending(db_session, **args) is False

    await add_child_pending(db_session, run)
    assert await approvals.has_pending(db_session, **args) is True


async def test_approve_last_one_requeues_and_records_pause(db_session):
    run = await make_run(
        db_session,
        heartbeat_at=NOW - timedelta(minutes=3),
        usage={"paused_ms": 0},
    )
    approval = await make_approval(db_session, run)
    status = await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )
    assert status == "queued"
    await db_session.refresh(run)
    assert run.status == "queued"
    assert run.usage["paused_ms"] == 180_000
    assert await decided_status(db_session, approval.id) == "approved"


async def test_approve_with_sibling_pending_keeps_waiting(db_session):
    run = await make_run(
        db_session,
        heartbeat_at=NOW - timedelta(minutes=3),
        usage={"paused_ms": 0},
    )
    first = await make_approval(db_session, run, node_id="a")
    await make_approval(db_session, run, node_id="b")

    status = await approvals.decide_approval(
        db_session,
        run=run,
        approval=first,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )
    await db_session.refresh(run)
    assert status == "waiting_approval"
    assert run.status == "waiting_approval"
    assert run.usage["paused_ms"] == 0


async def test_two_parallel_approvals_requeue_once(db_session):
    """两条并行审批顺序决策：中间保持等待，最后一条只结算一次等待时长。"""
    run = await make_run(
        db_session,
        heartbeat_at=NOW - timedelta(minutes=3),
        usage={"paused_ms": 0},
    )
    first = await make_approval(db_session, run, node_id="a")
    second = await make_approval(db_session, run, node_id="b")
    actor = uuid.uuid4()

    assert (
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=first,
            decision="approve",
            actor_id=actor,
            now=NOW,
        )
        == "waiting_approval"
    )
    await db_session.refresh(run)
    assert run.status == "waiting_approval"
    assert run.usage["paused_ms"] == 0

    assert (
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=second,
            decision="approve",
            actor_id=actor,
            now=NOW,
        )
        == "queued"
    )
    await db_session.refresh(run)
    assert run.status == "queued"
    # 只有最后一条结算，不会把同一段等待累计两次。
    assert run.usage["paused_ms"] == 180_000
    assert first.status == "approved"
    assert second.status == "approved"


async def test_decide_reloads_run_state_under_lock(db_session):
    """决策必须先重读运行行：内存里的旧 heartbeat 不能参与等待时长计算。"""
    run = await make_run(
        db_session,
        heartbeat_at=NOW - timedelta(minutes=3),
        usage={"paused_ms": 0},
    )
    approval = await make_approval(db_session, run)
    async with fresh_session(db_session) as other:
        await other.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run.id)
            .values(heartbeat_at=NOW - timedelta(minutes=10))
        )
        await other.commit()

    status = await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )
    assert status == "queued"
    await db_session.refresh(run)
    assert run.usage["paused_ms"] == 600_000


async def test_non_waiting_run_rolls_back_decision(db_session):
    """运行已不在等待审批：审批决定整体回滚，什么都不改变。"""
    run = await make_run(
        db_session,
        status="running",
        heartbeat_at=NOW - timedelta(minutes=3),
        usage={"paused_ms": 5_000},
    )
    approval = await make_approval(db_session, run)
    approval_id = approval.id
    with pytest.raises(AppError) as excinfo:
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=approval,
            decision="approve",
            actor_id=uuid.uuid4(),
            now=NOW,
        )
    assert excinfo.value.code == "workflow_approval_not_pending"
    await db_session.refresh(approval)
    await db_session.refresh(run)
    assert approval.status == "pending"
    assert run.status == "running"
    assert run.usage["paused_ms"] == 5_000
    assert await decided_status(db_session, approval_id) == "pending"


async def test_approval_expired_elsewhere_conflicts_and_changes_nothing(db_session):
    """审批已被别处置为 expired：CAS 落空后回滚，运行状态与 usage 不动。"""
    run = await make_run(
        db_session,
        heartbeat_at=NOW - timedelta(minutes=3),
        usage={"paused_ms": 5_000},
    )
    approval = await make_approval(db_session, run)
    approval_id = approval.id
    async with fresh_session(db_session) as other:
        await other.execute(
            update(WorkflowApproval)
            .where(WorkflowApproval.id == approval_id)
            .values(status="expired", decided_at=NOW)
        )
        await other.commit()

    with pytest.raises(AppError) as excinfo:
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=approval,
            decision="approve",
            actor_id=uuid.uuid4(),
            now=NOW,
        )
    assert excinfo.value.code == "workflow_approval_not_pending"
    await db_session.refresh(approval)
    await db_session.refresh(run)
    assert approval.status == "expired"
    assert run.status == "waiting_approval"
    assert run.usage["paused_ms"] == 5_000
    assert await decided_status(db_session, approval_id) == "expired"


async def test_decided_approval_conflicts_across_sessions(db_session):
    """另一个会话读到的是已决策的审批：决策返回冲突而不是重放。"""
    run = await make_run(db_session)
    approval = await make_approval(db_session, run)
    await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )

    async with fresh_session(db_session) as other:
        other_run = await other.get(WorkflowRun, run.id)
        other_approval = await other.get(WorkflowApproval, approval.id)
        assert other_approval.status == "approved"
        with pytest.raises(AppError) as excinfo:
            await approvals.decide_approval(
                other,
                run=other_run,
                approval=other_approval,
                decision="approve",
                actor_id=uuid.uuid4(),
                now=NOW,
            )
        assert excinfo.value.code == "workflow_approval_not_pending"

    assert await decided_status(db_session, approval.id) == "approved"
    await db_session.refresh(run)
    assert run.status == "queued"


@pytest.mark.parametrize(
    ("kind", "error_code"),
    [("review", "review_rejected"), ("tool_confirm", "approval_rejected")],
)
async def test_reject_fails_run_and_denies_node(db_session, kind, error_code):
    run = await make_run(db_session, heartbeat_at=NOW)
    approval = await make_approval(db_session, run, node_id="gate", kind=kind)
    node = await add_waiting_node(db_session, run, "gate")
    sibling = await make_approval(db_session, run, node_id="other")
    child = await add_child_pending(db_session, run)

    status = await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="reject",
        actor_id=uuid.uuid4(),
        note="不合适",
        now=NOW,
    )
    assert status == "failed"
    await db_session.refresh(run)
    await db_session.refresh(node)
    await db_session.refresh(child)
    assert run.status == "failed"
    assert run.error_code == error_code
    assert node.status == "denied"
    assert await decided_status(db_session, approval.id) == "rejected"
    assert await decided_status(db_session, sibling.id) == "expired"
    # 驳回后子运行与它的待处理审批不再可决策。
    assert child.status == "cancelled"
    child_approvals = (
        (
            await db_session.execute(
                select(AgentApproval).where(AgentApproval.run_id == child.id)
            )
        )
        .scalars()
        .all()
    )
    assert [row.status for row in child_approvals] == ["expired"]


async def test_decide_twice_conflicts(db_session):
    run = await make_run(db_session)
    approval = await make_approval(db_session, run)
    await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
        now=NOW,
    )
    await db_session.refresh(run)
    with pytest.raises(AppError) as excinfo:
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=approval,
            decision="approve",
            actor_id=uuid.uuid4(),
            now=NOW,
        )
    assert excinfo.value.code == "workflow_approval_not_pending"


async def test_expired_approval_conflicts_and_is_marked(db_session):
    run = await make_run(db_session)
    approval = await make_approval(db_session, run)
    approval.expires_at = NOW - timedelta(minutes=1)
    await db_session.commit()

    with pytest.raises(AppError) as excinfo:
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=approval,
            decision="approve",
            actor_id=uuid.uuid4(),
            now=NOW,
        )
    assert excinfo.value.code == "workflow_approval_not_pending"
    # 过期标记提交后，调用方手里的对象也要反映真相（refresh 发生在 raise 之前）。
    assert approval.status == "expired"
    assert await decided_status(db_session, approval.id) == "expired"
