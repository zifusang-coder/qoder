import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.errors import AppError
from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.workflow import lifecycle
from app.workflow.lifecycle import _fail_approval_run, _fail_orphans

NOW = datetime.now(UTC)


async def make_run(
    db_session,
    *,
    status="running",
    attempts=1,
    resume_count=0,
    heartbeat_at=None,
    error_code="",
    error_message="",
):
    workflow = Workflow(tenant_id=DEFAULT_TENANT_ID, name="测试流", created_by=uuid.uuid4())
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=DEFAULT_TENANT_ID,
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
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status=status,
        inputs={},
        policy={},
        attempts=attempts,
        resume_count=resume_count,
        heartbeat_at=heartbeat_at,
        error_code=error_code,
        error_message=error_message,
        started_at=NOW - timedelta(minutes=5),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def add_node_run(db_session, run, node_id, status, *, attempt=1):
    row = WorkflowNodeRun(
        tenant_id=run.tenant_id,
        run_id=run.id,
        node_id=node_id,
        attempt=attempt,
        kind="tool",
        status=status,
        started_at=NOW,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def add_workflow_approval(db_session, run, *, node_id="review", expires_at=None):
    row = WorkflowApproval(
        tenant_id=run.tenant_id,
        run_id=run.id,
        node_id=node_id,
        kind="review",
        title="审核",
        payload={},
        expires_at=expires_at,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def add_waiting_child(db_session, run):
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
        tenant_id=run.tenant_id,
        run_id=child.id,
        step_index=0,
        kind="tool",
        status="running",
    )
    db_session.add(step)
    await db_session.flush()
    approval = AgentApproval(
        tenant_id=run.tenant_id,
        run_id=child.id,
        step_id=step.id,
        tool_name="content.save",
        risk="high",
        payload={},
    )
    db_session.add(approval)
    await db_session.commit()
    return child, step, approval


async def add_running_child(db_session, run, *, with_approval=True):
    child = AgentRun(
        tenant_id=run.tenant_id,
        user_id=uuid.uuid4(),
        goal="节点任务",
        mode="node",
        status="running",
        workflow_run_id=run.id,
        workflow_node_id="agent",
        budget={},
        policy={},
    )
    db_session.add(child)
    await db_session.flush()
    step = AgentStep(
        tenant_id=run.tenant_id,
        run_id=child.id,
        step_index=0,
        kind="tool",
        status="running",
    )
    db_session.add(step)
    await db_session.flush()
    approval = None
    if with_approval:
        approval = AgentApproval(
            tenant_id=run.tenant_id,
            run_id=child.id,
            step_id=step.id,
            tool_name="content.save",
            risk="high",
            payload={},
        )
        db_session.add(approval)
    await db_session.commit()
    return child, step, approval


async def latest_node(db_session, run, node_id):
    return (
        (
            await db_session.execute(
                select(WorkflowNodeRun)
                .where(
                    WorkflowNodeRun.run_id == run.id,
                    WorkflowNodeRun.node_id == node_id,
                )
                .order_by(WorkflowNodeRun.attempt.desc())
            )
        )
        .scalars()
        .first()
    )


async def test_claim_moves_queued_to_running_and_reopens_interrupted_nodes(
    db_session,
):
    run = await make_run(db_session, status="queued", attempts=0)
    await add_node_run(db_session, run, "a", "running")
    assert await lifecycle.claim_run(db_session, run.id) == "claimed"
    await db_session.refresh(run)
    assert run.status == "running"
    assert run.attempts == 1
    assert run.started_at is not None
    assert run.heartbeat_at is not None
    node = await latest_node(db_session, run, "a")
    assert node.status == "interrupted"
    # 第二次领取不再加 attempts，返回的是观察到的状态而不是 "claimed"，
    # worker 据此跳过重复投递。
    assert await lifecycle.claim_run(db_session, run.id) == "running"
    await db_session.refresh(run)
    assert run.attempts == 1


async def test_cancel_expires_approvals_children_and_nodes(db_session):
    run = await make_run(db_session, status="waiting_approval")
    approval = await add_workflow_approval(db_session, run)
    child, step, child_approval = await add_waiting_child(db_session, run)
    node = await add_node_run(db_session, run, "review", "waiting")
    assert await lifecycle.cancel_run(db_session, run, actor="tester") is True
    await db_session.refresh(run)
    await db_session.refresh(approval)
    await db_session.refresh(child)
    await db_session.refresh(step)
    await db_session.refresh(child_approval)
    await db_session.refresh(node)
    assert run.status == "cancelled"
    assert run.finished_at is not None
    assert approval.status == "expired"
    assert child.status == "cancelled"
    assert child_approval.status == "expired"
    assert step.status == "interrupted"
    assert node.status == "interrupted"


async def test_cancel_terminal_run_returns_false(db_session):
    run = await make_run(db_session, status="succeeded")
    assert await lifecycle.cancel_run(db_session, run) is False
    await db_session.refresh(run)
    assert run.status == "succeeded"


async def test_resume_reopens_failed_denied_and_skipped_nodes(db_session):
    run = await make_run(
        db_session, status="failed", error_code="node_failed", error_message="坏了"
    )
    await add_node_run(db_session, run, "ok", "ok")
    await add_node_run(db_session, run, "bad", "error")
    await add_node_run(db_session, run, "denied", "denied")
    await add_node_run(db_session, run, "skipped", "skipped")
    await lifecycle.resume_run(db_session, run, actor="tester")
    await db_session.refresh(run)
    assert run.status == "queued"
    assert run.resume_count == 1
    assert run.error_code == ""
    assert run.finished_at is None
    statuses = {}
    for node_id in ("ok", "bad", "denied", "skipped"):
        statuses[node_id] = (await latest_node(db_session, run, node_id)).status
    assert statuses == {
        "ok": "ok",
        "bad": "interrupted",
        "denied": "interrupted",
        "skipped": "interrupted",
    }


async def test_resume_rejects_running_and_exhausted_runs(db_session):
    running = await make_run(db_session, status="running")
    with pytest.raises(AppError) as exc:
        await lifecycle.resume_run(db_session, running)
    assert exc.value.code == "workflow_run_not_resumable"

    exhausted = await make_run(db_session, status="failed", resume_count=10)
    with pytest.raises(AppError) as exc:
        await lifecycle.resume_run(db_session, exhausted)
    assert exc.value.code == "workflow_run_not_resumable"


async def test_stale_run_requeues_before_second_attempt(db_session):
    run = await make_run(
        db_session, attempts=1, heartbeat_at=NOW - timedelta(minutes=10)
    )
    await add_node_run(db_session, run, "a", "running")
    assert await lifecycle.settle_stale_run(db_session, run.id) == "queued"
    await db_session.refresh(run)
    assert run.status == "queued"
    assert run.error_code == ""
    assert (await latest_node(db_session, run, "a")).status == "interrupted"


async def test_stale_run_fails_after_second_attempt(db_session):
    run = await make_run(
        db_session, attempts=2, heartbeat_at=NOW - timedelta(minutes=10)
    )
    assert await lifecycle.settle_stale_run(db_session, run.id) == "failed"
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "workflow_stale"


@pytest_asyncio.fixture
async def factory(db_session):
    return async_sessionmaker(db_session.bind, expire_on_commit=False)


async def test_reconcile_expires_overdue_approval_and_fails_run(factory, db_session):
    run = await make_run(db_session, status="waiting_approval")
    approval = await add_workflow_approval(
        db_session, run, expires_at=NOW - timedelta(hours=1)
    )
    await add_node_run(db_session, run, "review", "waiting")
    counts = await lifecycle.reconcile(
        factory, settings=get_settings()
    )
    await db_session.refresh(run)
    await db_session.refresh(approval)
    assert approval.status == "expired"
    assert run.status == "failed"
    assert run.error_code == "approval_expired"
    assert (await latest_node(db_session, run, "review")).status == "denied"
    assert counts["approval_expired"] == 1


async def test_reconcile_fails_waiting_run_without_pending_approvals(factory, db_session):
    run = await make_run(db_session, status="waiting_approval")
    await add_node_run(db_session, run, "review", "waiting")
    counts = await lifecycle.reconcile(
        factory, settings=get_settings()
    )
    await db_session.refresh(run)
    assert run.status == "failed"
    assert run.error_code == "approval_expired"
    assert counts["approval_expired"] == 1


async def test_reconcile_keeps_waiting_run_with_child_pending(factory, db_session):
    run = await make_run(db_session, status="waiting_approval")
    await add_waiting_child(db_session, run)
    counts = await lifecycle.reconcile(
        factory, settings=get_settings()
    )
    await db_session.refresh(run)
    assert run.status == "waiting_approval"
    assert counts["approval_expired"] == 0


async def test_reconcile_reports_requeued_runs_to_callback(factory, db_session):
    run = await make_run(db_session, status="running", attempts=0)
    seen = []

    async def on_requeue(run_id):
        seen.append(run_id)

    counts = await lifecycle.reconcile(
        factory, settings=get_settings(), on_requeue=on_requeue
    )
    await db_session.refresh(run)
    assert run.status == "queued"
    assert counts["requeued"] == 1
    assert seen == [run.id]


async def test_reconcile_requeues_settled_waiting_run(factory, db_session):
    run = await make_run(
        db_session,
        status="waiting_approval",
        heartbeat_at=NOW - timedelta(minutes=5),
    )
    approval = await add_workflow_approval(db_session, run)
    approval.status = "approved"
    approval.decided_at = NOW - timedelta(minutes=4)
    await db_session.commit()
    await add_node_run(db_session, run, "review", "waiting")
    seen = []

    async def on_requeue(run_id):
        seen.append(run_id)

    counts = await lifecycle.reconcile(
        factory,
        settings=get_settings(),
        now=NOW,
        on_requeue=on_requeue,
    )
    await db_session.refresh(run)
    # 最后一条审批已通过、却没人把它唤回队列：对账要重排而不是判失败。
    assert run.status == "queued"
    assert run.error_code == ""
    assert run.finished_at is None
    assert counts["requeued"] == 1
    assert counts["approval_expired"] == 0
    assert seen == [run.id]
    # 等待审批的 5 分钟必须计入 paused_ms，不能算进运行墙钟预算。
    assert int((run.usage or {}).get("paused_ms") or 0) >= 300_000


async def test_reconcile_reenqueues_lost_queued_run(factory, db_session):
    stale = await make_run(db_session, status="queued", attempts=0)
    fresh = await make_run(db_session, status="queued", attempts=0)
    await db_session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == stale.id)
        .values(updated_at=NOW - timedelta(minutes=5))
    )
    await db_session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == fresh.id)
        .values(updated_at=NOW)
    )
    await db_session.commit()
    seen = []

    async def on_requeue(run_id):
        seen.append(run_id)

    counts = await lifecycle.reconcile(
        factory, settings=get_settings(), now=NOW, on_requeue=on_requeue
    )
    await db_session.refresh(stale)
    await db_session.refresh(fresh)
    # 提交 queued 后进程死亡：超过宽限期的运行要重新投递，状态不变。
    assert counts["queued_reenqueued"] == 1
    assert seen == [stale.id]
    assert stale.status == "queued"
    # 宽限期内的运行可能还在正常入队路径上，不能重复投递。
    assert fresh.status == "queued"


async def test_fail_waiting_run_denies_node_and_expires_the_rest(db_session):
    run = await make_run(db_session, status="waiting_approval")
    workflow_approval = await add_workflow_approval(
        db_session, run, node_id="other"
    )
    node = await add_node_run(db_session, run, "agent", "waiting")
    child, _step, child_approval = await add_waiting_child(db_session, run)
    assert (
        await lifecycle.fail_waiting_run(
            db_session,
            run.id,
            error_code="approval_rejected",
            message="审批已驳回",
            actor="tester",
        )
        is True
    )
    for row in (run, node, workflow_approval, child, child_approval):
        await db_session.refresh(row)
    assert run.status == "failed"
    assert run.error_code == "approval_rejected"
    assert run.finished_at is not None
    assert node.status == "denied"
    assert workflow_approval.status == "expired"
    assert child.status == "cancelled"
    assert child_approval.status == "expired"
    # 已经是终态的运行不会被再次改写
    assert (
        await lifecycle.fail_waiting_run(
            db_session,
            run.id,
            error_code="approval_rejected",
            message="审批已驳回",
        )
        is False
    )


async def test_fail_active_run_closes_in_flight_work_only(db_session):
    run = await make_run(db_session, status="running", attempts=1)
    workflow_approval = await add_workflow_approval(db_session, run)
    node = await add_node_run(db_session, run, "agent", "running")
    child, step, child_approval = await add_waiting_child(db_session, run)
    assert (
        await lifecycle.fail_active_run(
            db_session,
            run.id,
            error_code="workflow_internal_error",
            message="运行异常，请稍后重试",
            actor="worker",
        )
        is True
    )
    for row in (run, workflow_approval, node, child, step, child_approval):
        await db_session.refresh(row)
    assert run.status == "failed"
    assert run.error_code == "workflow_internal_error"
    assert run.finished_at is not None
    assert workflow_approval.status == "expired"
    assert node.status == "interrupted"
    assert child.status == "cancelled"
    assert step.status == "interrupted"
    assert child_approval.status == "expired"
    # waiting_approval 属于审批决策者，崩溃兜底不能覆盖它
    waiting = await make_run(db_session, status="waiting_approval")
    assert (
        await lifecycle.fail_active_run(
            db_session,
            waiting.id,
            error_code="workflow_internal_error",
            message="运行异常，请稍后重试",
        )
        is False
    )
    await db_session.refresh(waiting)
    assert waiting.status == "waiting_approval"


async def test_reconcile_expiry_cleans_up_children_and_running_nodes(
    factory, db_session
):
    run = await make_run(db_session, status="waiting_approval")
    overdue = await add_workflow_approval(
        db_session, run, expires_at=NOW - timedelta(hours=1)
    )
    pending_other = await add_workflow_approval(
        db_session, run, node_id="other", expires_at=NOW + timedelta(hours=1)
    )
    waiting_node = await add_node_run(db_session, run, "review", "waiting")
    running_node = await add_node_run(db_session, run, "parallel", "running")
    child, step, child_approval = await add_running_child(db_session, run)
    counts = await lifecycle.reconcile(
        factory, settings=get_settings(), now=NOW
    )
    for row in (
        run,
        overdue,
        pending_other,
        waiting_node,
        running_node,
        child,
        step,
        child_approval,
    ):
        await db_session.refresh(row)
    assert run.status == "failed"
    assert run.error_code == "approval_expired"
    assert overdue.status == "expired"
    assert pending_other.status == "expired"
    assert waiting_node.status == "denied"
    assert running_node.status == "interrupted"
    assert child.status == "cancelled"
    assert child_approval.status == "expired"
    assert step.status == "interrupted"
    assert counts["approval_expired"] == 1


async def test_reconcile_orphan_waiting_run_closes_children(factory, db_session):
    run = await make_run(db_session, status="waiting_approval")
    node = await add_node_run(db_session, run, "parallel", "running")
    child, step, _approval = await add_running_child(
        db_session, run, with_approval=False
    )
    counts = await lifecycle.reconcile(
        factory, settings=get_settings(), now=NOW
    )
    for row in (run, node, child, step):
        await db_session.refresh(row)
    assert run.status == "failed"
    assert run.error_code == "approval_expired"
    assert node.status == "interrupted"
    assert child.status == "cancelled"
    assert step.status == "interrupted"
    assert counts["approval_expired"] == 1


async def test_fail_approval_run_lost_cas_writes_nothing(db_session):
    # running 状态让 waiting_approval → failed 的 CAS 必然失败
    run = await make_run(db_session, status="running")
    approval = await add_workflow_approval(db_session, run)
    node = await add_node_run(db_session, run, "review", "waiting")
    assert await _fail_approval_run(db_session, run.id, NOW) == 0
    await db_session.refresh(approval)
    await db_session.refresh(node)
    assert approval.status == "pending"
    assert approval.decided_at is None
    assert node.status == "waiting"
    assert node.finished_at is None


async def test_fail_orphans_survives_lost_cas_and_processes_rest(db_session):
    first = await make_run(db_session, status="waiting_approval")
    first_node = await add_node_run(db_session, first, "review", "waiting")
    second = await make_run(db_session, status="waiting_approval")
    second_node = await add_node_run(db_session, second, "review", "waiting")
    first_id, second_id = first.id, second.id

    real_has_pending = lifecycle.approvals.has_pending
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    calls = 0
    lost_run_id = None

    async def has_pending(session, *, tenant_id, run_id):
        nonlocal calls, lost_run_id
        calls += 1
        if calls == 1:
            # 模拟并发决策者在 select 与 CAS 之间把该运行改成 cancelled
            lost_run_id = run_id
            async with factory() as other:
                await other.execute(
                    update(WorkflowRun)
                    .where(WorkflowRun.id == run_id)
                    .values(status="cancelled")
                )
                await other.commit()
            return False
        return await real_has_pending(
            session, tenant_id=tenant_id, run_id=run_id
        )

    with patch.object(lifecycle.approvals, "has_pending", has_pending):
        failed = await _fail_orphans(db_session, NOW)

    assert failed == 1
    assert lost_run_id == first_id
    for row in (first, first_node, second, second_node):
        await db_session.refresh(row)
    assert first.status == "cancelled"
    assert first_node.status == "waiting"
    assert second.id == second_id
    assert second.status == "failed"
    assert second.error_code == "approval_expired"
    assert second_node.status == "denied"
