import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import select
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
from app.workflow.engine import EngineDeps, run_workflow


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


def join_graph():
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("a", "tool", tool="demo.echo", args={"text": "A"}),
            node("b", "tool", tool="demo.echo", args={"text": "B"}),
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
    deps = replace(
        engine.deps,
        settings=engine.deps.settings.model_copy(
            update={"workflow_time_slice_seconds": 1}
        ),
    )
    run = await make_run(db_session, linear_graph())
    assert await run_workflow(engine.factory, run.id, deps=deps) == "queued"
    await db_session.refresh(run)
    assert run.status == "queued"
    rows = await node_rows(db_session, run.id)
    assert [row.node_id for row in rows] == ["in"]
    assert engine.stats["calls"] == []
