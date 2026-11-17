import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.contracts import ToolResult, ToolSpec
from app.agent.registry import ToolRegistry
from app.config import get_settings
from app.errors import AppError
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.skills.registry import SkillRegistry
from app.storage import get_storage
from app.workflow import approvals
from app.workflow.engine import EngineDeps, _full_estimate, run_workflow
from app.workflow.graph import GraphNode


async def no_sleep(_seconds: float) -> None:
    return None


@pytest_asyncio.fixture
async def engine(db_session):
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    settings = get_settings()
    stats = {"calls": []}

    async def echo(ctx, args):
        stats["calls"].append((str(ctx.run_id), ctx.step_index, args))
        return ToolResult(
            summary="回声",
            data={"text": args["text"], "long": "x" * 3000},
            artifacts=[{"type": "content", "id": "c1"}],
        )

    async def flaky(ctx, args):
        stats["calls"].append((str(ctx.run_id), ctx.step_index, args))
        if len(stats["calls"]) == 1:
            raise AppError("conflict", "临时冲突", 409)
        return ToolResult(summary="第二次成功", data={"ok": True})

    async def bad(ctx, args):
        stats["calls"].append((str(ctx.run_id), ctx.step_index, args))
        raise AppError("invalid_arguments", "参数不合法", 422)

    async def slow(ctx, args):
        stats["calls"].append((str(ctx.run_id), ctx.step_index, args))
        await asyncio.sleep(0.8)
        return ToolResult(summary="慢节点", data={"ok": True})

    registry = ToolRegistry()
    registry.register_all(
        [
            ToolSpec(
                name="demo.echo",
                description="回声",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                risk="read",
                handler=echo,
                timeout_seconds=30,
            ),
            ToolSpec(
                name="demo.flaky",
                description="第一次失败",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                risk="read",
                handler=flaky,
                timeout_seconds=30,
            ),
            ToolSpec(
                name="demo.bad",
                description="总是参数错误",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                risk="read",
                handler=bad,
                timeout_seconds=30,
            ),
            ToolSpec(
                name="demo.slow",
                description="耗时 0.8 秒",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                risk="read",
                handler=slow,
                timeout_seconds=1.5,
            ),
        ]
    )

    class Env:
        pass

    env = Env()
    env.factory = factory
    env.stats = stats
    env.deps = EngineDeps(
        gateway=None,
        tool_registry=registry,
        skill_registry=SkillRegistry(settings.skills_dir),
        storage=get_storage(),
        queue=None,
        settings=settings,
        sleep=no_sleep,
    )
    return env


def node(node_id, kind, **data):
    return {"id": node_id, "type": kind, "data": data}


def edge(source, target, handle=None):
    value = {"id": f"{source}-{target}-{handle or ''}", "source": source, "target": target}
    if handle is not None:
        value["sourceHandle"] = handle
    return value


def linear_graph(tool="demo.echo", retry=0):
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("echo", "tool", tool=tool, args={"text": "{{inputs.topic}}"}, retry=retry),
            node("out", "output"),
        ],
        "edges": [edge("in", "echo"), edge("echo", "out")],
    }


def review_graph():
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("review", "review", name="审核", title="请确认"),
            node("out", "output"),
        ],
        "edges": [edge("in", "review"), edge("review", "out")],
    }


def condition_graph():
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node(
                "c",
                "condition",
                left="{{inputs.topic}}",
                operator="contains",
                value="AI",
            ),
            node("a", "tool", tool="demo.echo", args={"text": "命中"}),
            node("b", "tool", tool="demo.echo", args={"text": "未命中"}),
            node("out", "output"),
        ],
        "edges": [
            edge("in", "c"),
            edge("c", "a", "true"),
            edge("c", "b", "false"),
            edge("a", "out"),
            edge("b", "out"),
        ],
    }


def join_graph(tool="demo.echo"):
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("a", "tool", tool=tool, args={"text": "A"}),
            node("b", "tool", tool=tool, args={"text": "B"}),
            node("out", "output"),
        ],
        "edges": [
            edge("in", "a"),
            edge("in", "b"),
            edge("a", "out"),
            edge("b", "out"),
        ],
    }


async def make_run(db_session, graph, *, inputs=None, status="running", **overrides):
    workflow = Workflow(name="流程", created_by=uuid.uuid4())
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        workflow_id=workflow.id,
        version=1,
        graph=graph,
        source_revision=1,
        published_by=uuid.uuid4(),
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    values = {
        "workflow_id": workflow.id,
        "workflow_version_id": version.id,
        "status": status,
        "inputs": inputs if inputs is not None else {"topic": "AI 写作"},
        "policy": {},
        "usage": {},
        "started_at": datetime.now(UTC) - timedelta(seconds=1),
    }
    values.update(overrides)
    run = WorkflowRun(**values)
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def node_rows(db_session, run_id):
    return (
        (
            await db_session.execute(
                select(WorkflowNodeRun)
                .where(WorkflowNodeRun.run_id == run_id)
                .order_by(WorkflowNodeRun.node_id, WorkflowNodeRun.attempt)
            )
        )
        .scalars()
        .all()
    )


async def test_linear_tool_flow_succeeds(engine, db_session):
    run = await make_run(db_session, linear_graph())
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"

    await db_session.refresh(run)
    assert run.status == "succeeded"
    assert len(engine.stats["calls"]) == 1
    assert engine.stats["calls"][0][1] == 1
    rows = {row.node_id: row for row in await node_rows(db_session, run.id)}
    assert set(rows) == {"in", "echo", "out"}
    assert rows["echo"].status == "ok"
    assert len(rows["echo"].raw_output["data"]["long"]) == 3000
    assert len(rows["echo"].output["data"]["long"]) == 2000
    assert run.result["items"][0]["output"]["summary"] == "回声"
    assert run.result["artifacts"] == [{"type": "content", "id": "c1"}]


async def test_condition_skips_unselected_branch(engine, db_session):
    run = await make_run(db_session, condition_graph())
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    rows = {row.node_id: row for row in await node_rows(db_session, run.id)}
    assert rows["a"].status == "ok"
    assert rows["b"].status == "skipped"
    assert rows["b"].error == "上游分支未选中"
    await db_session.refresh(run)
    assert [item["node_id"] for item in run.result["items"]] == ["a"]


async def test_join_waits_for_both_branches_and_merges_output(engine, db_session):
    run = await make_run(db_session, join_graph())
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    await db_session.refresh(run)
    assert {item["node_id"] for item in run.result["items"]} == {"a", "b"}
    assert len(engine.stats["calls"]) == 2


async def test_tool_confirm_parks_then_approval_executes_once(engine, db_session):
    run = await make_run(
        db_session, linear_graph(), policy={"tool:demo.echo": "confirm"}
    )
    assert (
        await run_workflow(engine.factory, run.id, deps=engine.deps)
        == "waiting_approval"
    )
    approval = (
        await db_session.execute(
            select(WorkflowApproval).where(WorkflowApproval.run_id == run.id)
        )
    ).scalars().one()
    assert approval.kind == "tool_confirm"
    assert engine.stats["calls"] == []

    status = await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="approve",
        actor_id=uuid.uuid4(),
    )
    assert status == "queued"
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    assert len(engine.stats["calls"]) == 1
    call_run_id, step_index, _ = engine.stats["calls"][0]
    assert call_run_id == str(run.id)
    assert step_index == 1
    await db_session.refresh(run)
    assert run.step_counter == 1


async def test_tool_confirm_reject_never_executes(engine, db_session):
    run = await make_run(
        db_session, linear_graph(), policy={"tool:demo.echo": "confirm"}
    )
    assert (
        await run_workflow(engine.factory, run.id, deps=engine.deps)
        == "waiting_approval"
    )
    approval = (
        await db_session.execute(
            select(WorkflowApproval).where(WorkflowApproval.run_id == run.id)
        )
    ).scalars().one()
    await approvals.decide_approval(
        db_session,
        run=run,
        approval=approval,
        decision="reject",
        actor_id=uuid.uuid4(),
    )
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "approval_rejected"
    assert engine.stats["calls"] == []


async def test_review_approval_resumes_after_worker_claim(engine, db_session):
    from app.workflow import lifecycle

    run = await make_run(db_session, review_graph(), status="queued", attempts=0)
    assert (
        await run_workflow(engine.factory, run.id, deps=engine.deps)
        == "waiting_approval"
    )
    approval = (
        await db_session.execute(
            select(WorkflowApproval).where(WorkflowApproval.run_id == run.id)
        )
    ).scalars().one()
    assert (
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=approval,
            decision="approve",
            actor_id=uuid.uuid4(),
        )
        == "queued"
    )

    # 生产路径：worker 先领取再推进。领取不得清掉等待审批的节点，
    # 否则审核节点会重新执行并建一条新的 pending 审批。
    assert await lifecycle.claim_run(db_session, run.id) == "claimed"
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    await db_session.refresh(run)
    assert run.status == "succeeded"
    assert (
        await db_session.execute(
            select(WorkflowApproval).where(
                WorkflowApproval.run_id == run.id,
                WorkflowApproval.status == "pending",
            )
        )
    ).scalars().all() == []


async def test_tool_confirm_approval_resumes_after_worker_claim(engine, db_session):
    from app.workflow import lifecycle

    run = await make_run(
        db_session,
        linear_graph(),
        status="queued",
        attempts=0,
        policy={"tool:demo.echo": "confirm"},
    )
    assert (
        await run_workflow(engine.factory, run.id, deps=engine.deps)
        == "waiting_approval"
    )
    approval = (
        await db_session.execute(
            select(WorkflowApproval).where(WorkflowApproval.run_id == run.id)
        )
    ).scalars().one()
    assert (
        await approvals.decide_approval(
            db_session,
            run=run,
            approval=approval,
            decision="approve",
            actor_id=uuid.uuid4(),
        )
        == "queued"
    )
    assert await lifecycle.claim_run(db_session, run.id) == "claimed"
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    assert len(engine.stats["calls"]) == 1
    assert engine.stats["calls"][0][1] == 1


async def test_policy_deny_fails_without_executing(engine, db_session):
    run = await make_run(
        db_session, linear_graph(), policy={"tool:demo.echo": "deny"}
    )
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "policy_denied"
    rows = {row.node_id: row for row in await node_rows(db_session, run.id)}
    assert rows["echo"].status == "denied"
    assert engine.stats["calls"] == []


async def test_conflict_retries_with_new_attempt_and_same_step_index(engine, db_session):
    run = await make_run(db_session, linear_graph(tool="demo.flaky", retry=1))
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "succeeded"
    assert len(engine.stats["calls"]) == 2
    assert engine.stats["calls"][0][1] == engine.stats["calls"][1][1] == 1
    attempts = [
        row
        for row in await node_rows(db_session, run.id)
        if row.node_id == "echo"
    ]
    assert [row.attempt for row in attempts] == [1, 2]
    assert [row.status for row in attempts] == ["error", "ok"]


async def test_invalid_arguments_are_not_retried(engine, db_session):
    run = await make_run(db_session, linear_graph(tool="demo.bad", retry=3))
    assert await run_workflow(engine.factory, run.id, deps=engine.deps) == "failed"
    assert len(engine.stats["calls"]) == 1
    await db_session.refresh(run)
    assert run.error_code == "node_failed"
    attempts = [
        row
        for row in await node_rows(db_session, run.id)
        if row.node_id == "echo"
    ]
    assert len(attempts) == 1


async def test_wall_clock_limit_fails_before_execution(engine, db_session):
    deps = replace(
        engine.deps,
        settings=engine.deps.settings.model_copy(
            update={"workflow_max_seconds": 1}
        ),
    )
    run = await make_run(
        db_session,
        linear_graph(),
        started_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    assert await run_workflow(engine.factory, run.id, deps=deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "workflow_timeout"
    assert engine.stats["calls"] == []


async def test_time_slice_requeues_when_next_node_cannot_fit(engine, db_session):
    # 单节点预计 1.5 秒能装进 2 秒时间片；首节点实跑 0.8 秒后余量不足，第二个必须顺延。
    deps = replace(
        engine.deps,
        settings=engine.deps.settings.model_copy(
            update={"workflow_time_slice_seconds": 2}
        ),
    )
    run = await make_run(db_session, join_graph(tool="demo.slow"))
    assert await run_workflow(engine.factory, run.id, deps=deps) == "queued"
    await db_session.refresh(run)
    assert run.status == "queued"
    rows = await node_rows(db_session, run.id)
    assert sorted(row.node_id for row in rows) == ["a", "in"]
    assert len(engine.stats["calls"]) == 1
    assert engine.stats["calls"][0][2] == {"text": "A"}


async def test_node_equal_to_slice_still_runs(engine, db_session):
    # 完整估算恰好等于时间片：首批从片头开始，必须能跑，而不是永远重排队。
    deps = replace(
        engine.deps,
        settings=engine.deps.settings.model_copy(
            update={"workflow_time_slice_seconds": 1.5}
        ),
    )
    run = await make_run(db_session, linear_graph(tool="demo.slow"))
    assert await run_workflow(engine.factory, run.id, deps=deps) == "succeeded"
    assert len(engine.stats["calls"]) == 1


async def test_retry_budget_counts_against_slice(engine, db_session):
    # demo.slow 单次 1.5 秒、retry=1，完整估算 1.5*2 + 2 = 5 秒 > 3 秒切片。
    deps = replace(
        engine.deps,
        settings=engine.deps.settings.model_copy(
            update={"workflow_time_slice_seconds": 3}
        ),
    )
    run = await make_run(db_session, linear_graph(tool="demo.slow", retry=1))
    assert await run_workflow(engine.factory, run.id, deps=deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "node_failed"
    assert "时间片" in run.error_message
    assert engine.stats["calls"] == []


async def test_node_longer_than_slice_fails_instead_of_requeueing(engine, db_session):
    deps = replace(
        engine.deps,
        settings=engine.deps.settings.model_copy(
            update={"workflow_time_slice_seconds": 1}
        ),
    )
    run = await make_run(db_session, linear_graph())
    assert await run_workflow(engine.factory, run.id, deps=deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "node_failed"
    assert "时间片" in run.error_message
    assert engine.stats["calls"] == []


async def test_cancel_during_retry_backoff_does_not_execute_again(engine, db_session):
    run = await make_run(db_session, linear_graph(tool="demo.flaky", retry=1))

    async def cancel_during_backoff(_seconds: float) -> None:
        async with engine.factory() as session:
            await session.execute(
                update(WorkflowRun)
                .where(
                    WorkflowRun.id == run.id,
                    WorkflowRun.status.in_({"running", "queued"}),
                )
                .values(status="cancelled")
                .execution_options(synchronize_session=False)
            )
            await session.commit()

    deps = replace(engine.deps, sleep=cancel_during_backoff)
    assert await run_workflow(engine.factory, run.id, deps=deps) == "cancelled"
    assert len(engine.stats["calls"]) == 1
    attempts = [
        row for row in await node_rows(db_session, run.id) if row.node_id == "echo"
    ]
    assert [row.attempt for row in attempts] == [1]


async def test_full_estimate_counts_retries_and_backoff(engine):
    def tool_node(retry):
        data = {"tool": "demo.echo"} | ({"retry": retry} if retry else {})
        return GraphNode(id="echo", kind="tool", data=data, position={})

    # demo.echo 单次上限 30 秒；退避 2/4/8 秒也计入时间片。
    assert _full_estimate(tool_node(0), engine.deps) == 30.0
    assert _full_estimate(tool_node(1), engine.deps) == 62.0
    assert _full_estimate(tool_node(3), engine.deps) == 134.0
