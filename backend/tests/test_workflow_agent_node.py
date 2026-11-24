import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.contracts import ToolResult, ToolSpec
from app.agent.registry import ToolRegistry
from app.agent.runtime import decide_approval
from app.config import get_settings
from app.llm.tools import ToolCall, ToolTurn
from app.llm.types import Usage
from app.models.agent import AgentApproval, AgentRun
from app.models.base import DEFAULT_TENANT_ID
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.skills.registry import SkillRegistry
from app.storage import get_storage
from app.workflow.engine import EngineDeps, run_workflow


async def no_sleep(_seconds: float) -> None:
    return None


class FakeGateway:
    def __init__(self) -> None:
        self.turns: list = []
        self.tools: list[list[str]] = []

    async def complete_tool_calls(self, messages, *, tools):
        self.tools.append([item["name"] for item in tools])
        item = self.turns.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def turn(content="", calls=(), prompt=10, completion=5):
    return ToolTurn(
        content=content,
        tool_calls=[
            ToolCall(
                id=call_id,
                name=name,
                arguments=args if isinstance(args, str) else json.dumps(args),
            )
            for call_id, name, args in calls
        ],
        finish_reason="tool_calls" if calls else "stop",
        usage=Usage(prompt, completion),
    )


@pytest_asyncio.fixture
async def env(db_session):
    settings = get_settings()
    calls: list[tuple[str, dict]] = []

    async def echo(ctx, args):
        calls.append((str(ctx.run_id), args))
        return ToolResult(summary="回声", data={"text": args["text"]})

    async def publish(ctx, args):
        calls.append((str(ctx.run_id), args))
        return ToolResult(
            summary="已建草稿",
            data={"draft": True},
            artifacts=[{"type": "content", "id": "d1"}],
        )

    schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    registry = ToolRegistry()
    registry.register_all(
        [
            ToolSpec(
                name="demo.echo",
                description="回声",
                input_schema=schema,
                risk="read",
                handler=echo,
                timeout_seconds=30,
            ),
            ToolSpec(
                name="demo.publish",
                description="建草稿",
                input_schema=schema,
                risk="high",
                handler=publish,
                timeout_seconds=30,
            ),
        ]
    )

    class Env:
        pass

    holder = Env()
    holder.factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holder.settings = settings
    holder.calls = calls
    holder.gateway = FakeGateway()
    holder.deps = EngineDeps(
        gateway=holder.gateway,
        tool_registry=registry,
        skill_registry=SkillRegistry(settings.skills_dir),
        storage=get_storage(),
        queue=None,
        settings=settings,
        sleep=no_sleep,
    )
    return holder


def node(node_id, kind, **data):
    return {"id": node_id, "type": kind, "data": data}


def edge(source, target):
    return {"id": f"{source}-{target}", "source": source, "target": target}


def agent_graph(*, tools=None, budget=None, retry=0):
    data = {
        "goal": "围绕 {{inputs.topic}} 写一条笔记",
        "budget": budget or {"max_steps": 4},
        "retry": retry,
    }
    if tools is not None:
        data["tools"] = tools
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node("writer", "agent", **data),
            node("out", "output"),
        ],
        "edges": [edge("in", "writer"), edge("writer", "out")],
    }


async def make_run(db_session, *, graph, policy=None, triggered_by=None):
    owner = triggered_by or uuid.uuid4()
    workflow = Workflow(
        tenant_id=DEFAULT_TENANT_ID, name="测试工作流", created_by=owner
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        version=1,
        graph=graph,
        source_revision=0,
        published_by=owner,
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        tenant_id=DEFAULT_TENANT_ID,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status="queued",
        inputs={"topic": "AI 选题"},
        policy=dict(policy or {}),
        triggered_by=triggered_by,
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def children_of(db_session, run):
    return (
        (
            await db_session.execute(
                select(AgentRun).where(AgentRun.workflow_run_id == run.id)
            )
        )
        .scalars()
        .all()
    )


def approver() -> User:
    return User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email="approver@local",
        password_hash="x",
        display_name="Approver",
        role="admin",
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )


async def test_registry_subset_matches_exact_and_prefix_names(env):
    assert env.deps.tool_registry.subset(None).names() == [
        "demo.echo",
        "demo.publish",
    ]
    assert env.deps.tool_registry.subset(["demo.*"]).names() == [
        "demo.echo",
        "demo.publish",
    ]
    assert env.deps.tool_registry.subset(["demo.echo"]).names() == ["demo.echo"]
    assert env.deps.tool_registry.subset(["missing.tool"]).names() == []


async def test_agent_node_succeeds_and_feeds_output(env, db_session):
    env.gateway.turns = [
        turn(content="先查一下", calls=[("c1", "demo.echo", {"text": "你好"})]),
        turn(content="已经完成"),
    ]
    run = await make_run(db_session, graph=agent_graph(tools=["demo.echo"]))
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"

    children = await children_of(db_session, run)
    assert len(children) == 1
    child = children[0]
    assert child.mode == "node"
    assert child.workflow_node_id == "writer"
    assert child.status == "succeeded"
    assert child.goal == "围绕 AI 选题 写一条笔记"
    assert env.calls and env.calls[0][0] == str(child.id)

    node_run = (
        await db_session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "writer",
            )
        )
    ).scalars().one()
    assert node_run.status == "ok"
    assert node_run.agent_run_id == child.id
    assert node_run.output["result_text"] == "已经完成"

    await db_session.refresh(run)
    assert run.result["items"][0]["output"]["result_text"] == "已经完成"
    assert run.usage["prompt_tokens"] == 20
    assert run.usage["completion_tokens"] == 10


async def test_agent_tool_whitelist_supports_prefix(env, db_session):
    env.gateway.turns = [turn(content="完成")]
    run = await make_run(db_session, graph=agent_graph(tools=["demo.*"]))
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"
    assert env.gateway.tools[0] == ["demo.echo", "demo.publish"]


async def test_agent_without_whitelist_uses_all_tools(env, db_session):
    env.gateway.turns = [turn(content="完成")]
    run = await make_run(db_session, graph=agent_graph())
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"
    assert env.gateway.tools[0] == ["demo.echo", "demo.publish"]


async def test_agent_budget_seconds_clamped_to_time_slice(env, db_session):
    settings = env.settings.model_copy(update={"workflow_time_slice_seconds": 60})
    deps = replace(env.deps, settings=settings)
    env.gateway.turns = [turn(content="完成")]
    run = await make_run(
        db_session,
        graph=agent_graph(budget={"max_steps": 4, "max_seconds": 600}),
    )
    assert await run_workflow(env.factory, run.id, deps=deps) == "succeeded"
    child = (await children_of(db_session, run))[0]
    assert child.budget == {"max_steps": 4, "max_seconds": 55}


async def test_agent_budget_accounts_for_retry_budget(env, db_session):
    # 重试节点的整次估算要装进时间片：每个尝试只能拿到
    # (slice - guard - backoff_sum) / (retry + 1)，否则会在跑之前被硬判失败。
    settings = env.settings.model_copy(update={"workflow_time_slice_seconds": 60})
    deps = replace(env.deps, settings=settings)
    env.gateway.turns = [turn(content="完成")]
    run = await make_run(db_session, graph=agent_graph(budget={"max_steps": 4}, retry=1))
    assert await run_workflow(env.factory, run.id, deps=deps) == "succeeded"
    child = (await children_of(db_session, run))[0]
    assert child.budget == {"max_steps": 4, "max_seconds": 26}


async def test_agent_high_risk_tool_waits_for_approval_and_resumes(env, db_session):
    env.gateway.turns = [
        turn(content="准备建草稿", calls=[("c1", "demo.publish", {"text": "稿件"})]),
        turn(content="已经完成"),
    ]
    run = await make_run(db_session, graph=agent_graph())
    assert (
        await run_workflow(env.factory, run.id, deps=env.deps)
        == "waiting_approval"
    )
    child = (await children_of(db_session, run))[0]
    approval = (
        await db_session.execute(
            select(AgentApproval).where(AgentApproval.run_id == child.id)
        )
    ).scalars().one()
    assert approval.status == "pending"
    assert env.calls == []

    await decide_approval(
        db_session, approval, actor=approver(), decision="approve"
    )
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"
    assert len(env.calls) == 1
    await db_session.refresh(run)
    assert run.status == "succeeded"
    assert run.error_code == ""


async def test_agent_rejected_approval_fails_run(env, db_session):
    env.gateway.turns = [
        turn(content="准备建草稿", calls=[("c1", "demo.publish", {"text": "稿件"})]),
    ]
    run = await make_run(db_session, graph=agent_graph())
    assert (
        await run_workflow(env.factory, run.id, deps=env.deps)
        == "waiting_approval"
    )
    child = (await children_of(db_session, run))[0]
    approval = (
        await db_session.execute(
            select(AgentApproval).where(AgentApproval.run_id == child.id)
        )
    ).scalars().one()
    await decide_approval(
        db_session, approval, actor=approver(), decision="reject"
    )
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "approval_rejected"
    assert env.calls == []
    node_run = (
        await db_session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "writer",
            )
        )
    ).scalars().one()
    assert node_run.status == "denied"


async def test_agent_child_failure_retries_with_new_child(env, db_session):
    env.gateway.turns = [RuntimeError("boom"), turn(content="完成")]
    run = await make_run(db_session, graph=agent_graph(retry=1))
    assert await run_workflow(env.factory, run.id, deps=env.deps) == "succeeded"
    children = await children_of(db_session, run)
    assert len(children) == 2
    assert {child.status for child in children} == {"failed", "succeeded"}


async def test_agent_daily_quota_exceeded_fails_run(env, db_session):
    settings = env.settings.model_copy(update={"agent_daily_token_quota": 1})
    deps = replace(env.deps, settings=settings)
    seed = AgentRun(
        tenant_id=DEFAULT_TENANT_ID,
        user_id=uuid.uuid4(),
        goal="旧运行",
        status="succeeded",
        usage={"prompt_tokens": 5},
        created_at=datetime.now(UTC),
    )
    db_session.add(seed)
    await db_session.commit()

    env.gateway.turns = [turn(content="不应执行")]
    run = await make_run(db_session, graph=agent_graph())
    assert await run_workflow(env.factory, run.id, deps=deps) == "failed"
    await db_session.refresh(run)
    assert run.error_code == "agent_daily_quota_exceeded"
    count = (
        await db_session.execute(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.workflow_run_id == run.id)
        )
    ).scalar_one()
    assert count == 0
    assert env.gateway.tools == []
