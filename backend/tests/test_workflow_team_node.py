import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.contracts import ToolResult, ToolSpec
from app.agent.registry import ToolRegistry, get_tool_registry
from app.config import get_settings
from app.db import get_session
from app.deps import get_current_user
from app.llm.tools import ToolTurn
from app.llm.types import Usage
from app.main import app
from app.models.agent import AgentApproval, AgentRun, AgentStep, AgentTeamRole
from app.models.base import DEFAULT_TENANT_ID
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import WorkflowNodeRun
from app.queue import get_queue
from app.skills.registry import SkillRegistry
from app.storage import get_storage
from app.workflow import lifecycle
from app.workflow.engine import (
    EngineDeps,
    _estimate,
    _retry_budget,
    _team_budget,
    run_workflow,
)
from app.workflow.graph import NODE_KINDS, parse_graph
from app.workflow.validate import validate_publish
from tests.test_workflow_agent_node import edge, make_run, no_sleep, node, turn


def team_graph(*, roles=None, goal="为 {{inputs.topic}} 产出内容", budget=None, retry=0):
    return {
        "nodes": [
            node(
                "in",
                "input",
                fields=[{"key": "topic", "type": "string", "required": True}],
            ),
            node(
                "tm",
                "team",
                name="内容团队",
                roles=roles or ["scout", "creator", "critic"],
                goal=goal,
                budget=budget or {},
                retry=retry,
            ),
            node("out", "output"),
        ],
        "edges": [edge("in", "tm"), edge("tm", "out")],
    }


def test_team_kind_accepted():
    assert "team" in NODE_KINDS
    graph = parse_graph(team_graph())
    assert graph.nodes["tm"].kind == "team"


def test_team_validation():
    registry = get_tool_registry()
    result = validate_publish(team_graph(), registry=registry, skill_ids=set())
    assert result.ok
    result = validate_publish(
        team_graph(roles=["scout"]), registry=registry, skill_ids=set()
    )
    assert not result.ok  # 缺 creator
    result = validate_publish(
        team_graph(roles=["ghost", "creator"]), registry=registry, skill_ids=set()
    )
    assert not result.ok
    result = validate_publish(
        team_graph(budget={"max_tokens": 0}), registry=registry, skill_ids=set()
    )
    assert not result.ok


class TeamGateway:
    """Lead 用 complete()，角色子运行用 complete_tool_calls()；按脚本顺序消费。"""

    def __init__(self, script):
        self.script = list(script)
        self.last_usage = Usage(10, 5)

    async def complete(self, messages, **kwargs):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def complete_tool_calls(self, messages, *, tools):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def rturn(content):
    return ToolTurn(
        content=content, tool_calls=[], finish_reason="stop", usage=Usage(20, 10)
    )


@pytest_asyncio.fixture
async def wf_env(db_session):
    settings = get_settings()

    class Env:
        pass

    holder = Env()
    holder.factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holder.settings = settings
    holder.session = db_session
    holder.registry = ToolRegistry()
    return holder


async def test_engine_team_node_success(wf_env):
    run = await make_run(wf_env.session, graph=team_graph(roles=["creator"]))
    gateway = TeamGateway(
        [
            json.dumps({"action": "delegate", "role": "creator", "task": "写稿"}),
            rturn("稿件完成"),
            # critic 默认启用：creator 交付后强制审稿，裁决通过才回到 Lead
            rturn('{"verdict":"pass","issues":[]}'),
            json.dumps({"action": "finalize", "summary": "已交付"}),
        ]
    )
    deps = EngineDeps(
        gateway=gateway,
        tool_registry=wf_env.registry,
        skill_registry=SkillRegistry(wf_env.settings.skills_dir),
        storage=get_storage(),
        queue=None,
        settings=wf_env.settings,
        sleep=no_sleep,
        session_factory=wf_env.factory,
    )
    assert await run_workflow(wf_env.factory, run.id, deps=deps) == "succeeded"
    await wf_env.session.refresh(run)
    assert run.status == "succeeded"
    teams = (
        await wf_env.session.execute(
            select(AgentRun).where(
                AgentRun.mode == "team", AgentRun.workflow_run_id == run.id
            )
        )
    ).scalars().all()
    assert len(teams) == 1
    parent = teams[0]
    assert parent.status == "succeeded"
    assert parent.workflow_node_id == "tm"
    assert parent.goal == "为 AI 选题 产出内容"
    # 时间片开启时 max_seconds 被夹到「时间片 - 守卫」（retry=0）
    settings = wf_env.settings
    expected_seconds = settings.agent_team_max_seconds
    if settings.workflow_time_slice_seconds > 0:
        expected_seconds = min(
            expected_seconds, settings.workflow_time_slice_seconds - 5
        )
    assert parent.budget["max_seconds"] == expected_seconds

    node_run = (
        await wf_env.session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "tm",
            )
        )
    ).scalars().one()
    assert node_run.status == "ok"
    assert node_run.agent_run_id == parent.id
    assert node_run.output["result_text"] == "已交付"


# --- Fix Round 1：审批挂起/恢复、重试断点复用、租户角色收窄 ---


class FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


@pytest.fixture
def queue():
    fake = FakeQueue()
    app.dependency_overrides[get_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_queue, None)


@pytest_asyncio.fixture
async def api(db_session):
    state = {"user": None}

    async def current_user():
        return state["user"]

    app.dependency_overrides[get_session] = lambda: db_session
    app.dependency_overrides[get_current_user] = current_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http, state
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_session, None)


async def seed_tenant(db_session):
    db_session.add(
        Tenant(
            id=DEFAULT_TENANT_ID, name="默认租户", slug="default", status="active"
        )
    )
    await db_session.commit()


async def make_approver(db_session):
    user = User(
        id=uuid.uuid4(), tenant_id=DEFAULT_TENANT_ID,
        email=f"{uuid.uuid4().hex[:8]}@example.com", password_hash="x",
        display_name="approver", role="owner", is_active=True,
        must_change_password=False, is_platform_admin=False, token_version=0,
    )
    db_session.add(user)
    await db_session.commit()
    return user


def register_publish_tool(registry, calls):
    async def publish(ctx, args):
        calls.append((str(ctx.run_id), args))
        return ToolResult(
            summary="已建草稿", data={"draft": True},
            artifacts=[{"type": "content", "id": "d1"}],
        )

    registry.register_all(
        [
            ToolSpec(
                name="content.create_version",
                description="建草稿",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
                risk="high",
                handler=publish,
                timeout_seconds=30,
            )
        ]
    )


def make_deps(wf_env, gateway):
    return EngineDeps(
        gateway=gateway,
        tool_registry=wf_env.registry,
        skill_registry=SkillRegistry(wf_env.settings.skills_dir),
        storage=get_storage(),
        queue=None,
        settings=wf_env.settings,
        sleep=no_sleep,
        session_factory=wf_env.factory,
    )


async def team_parent_of(session, run):
    return (
        await session.execute(
            select(AgentRun).where(
                AgentRun.mode == "team", AgentRun.workflow_run_id == run.id
            )
        )
    ).scalars().one()


async def team_count(session, run):
    return (
        await session.execute(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.mode == "team", AgentRun.workflow_run_id == run.id)
        )
    ).scalar_one()


async def park_team(wf_env):
    """脚本化网关让 creator 子运行触发需审批工具而挂起；返回续跑所需 deps。"""
    calls = []
    register_publish_tool(wf_env.registry, calls)
    gateway = TeamGateway(
        [
            json.dumps({"action": "delegate", "role": "creator", "task": "写稿"}),
            turn(
                content="准备建草稿",
                calls=[("c1", "content.create_version", {"text": "稿件"})],
            ),
            rturn("稿件完成"),
            rturn('{"verdict":"pass","issues":[]}'),
            json.dumps({"action": "finalize", "summary": "已交付"}),
        ]
    )
    deps = make_deps(wf_env, gateway)
    run = await make_run(wf_env.session, graph=team_graph(roles=["creator"]))
    outcome = await run_workflow(wf_env.factory, run.id, deps=deps)
    return run, deps, calls, outcome


@pytest.mark.asyncio
async def test_team_node_approval_park_and_resume(wf_env, api, queue):
    http, state = api
    session = wf_env.session
    await seed_tenant(session)
    state["user"] = await make_approver(session)
    run, deps, calls, outcome = await park_team(wf_env)
    assert outcome == "waiting_approval"
    await session.refresh(run)
    assert run.status == "waiting_approval"
    parent = await team_parent_of(session, run)
    assert parent.status == "waiting_approval"
    child = (
        await session.execute(
            select(AgentRun).where(AgentRun.parent_run_id == parent.id)
        )
    ).scalars().one()
    assert child.status == "waiting_approval"
    # 裁定 1：role 子运行继承工作流外键，L1 机制据此看见团队审批。
    assert child.workflow_run_id == run.id
    assert child.workflow_node_id == "tm"
    approval = (
        await session.execute(
            select(AgentApproval).where(AgentApproval.run_id == child.id)
        )
    ).scalars().one()
    assert approval.status == "pending"
    node_run = (
        await session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "tm",
            )
        )
    ).scalars().one()
    assert node_run.status == "waiting"
    assert calls == []

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "approve"},
    )
    assert resp.status_code == 200
    await session.refresh(run)
    assert run.status == "queued"
    assert queue.jobs == [("run_workflow_job", str(run.id))]

    assert await run_workflow(wf_env.factory, run.id, deps=deps) == "succeeded"
    assert await team_count(session, run) == 1
    await session.refresh(parent)
    await session.refresh(child)
    await session.refresh(node_run)
    await session.refresh(run)
    assert parent.status == "succeeded"
    assert child.status == "succeeded"
    assert node_run.status == "ok"
    assert node_run.output["result_text"] == "已交付"
    assert run.status == "succeeded"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_team_node_pending_approval_survives_reconcile(wf_env):
    session = wf_env.session
    run, _deps, _calls, outcome = await park_team(wf_env)
    assert outcome == "waiting_approval"
    parent = await team_parent_of(session, run)
    counts = await lifecycle.reconcile(
        wf_env.factory,
        settings=wf_env.settings,
        now=datetime.now(UTC) + timedelta(seconds=60),
    )
    await session.refresh(run)
    await session.refresh(parent)
    assert run.status == "waiting_approval"
    assert parent.status == "waiting_approval"
    assert counts["approval_expired"] == 0


@pytest.mark.asyncio
async def test_team_node_reject_fails_team_and_workflow(wf_env, api, queue):
    http, state = api
    session = wf_env.session
    await seed_tenant(session)
    state["user"] = await make_approver(session)
    run, _deps, _calls, outcome = await park_team(wf_env)
    assert outcome == "waiting_approval"
    parent = await team_parent_of(session, run)
    child = (
        await session.execute(
            select(AgentRun).where(AgentRun.parent_run_id == parent.id)
        )
    ).scalars().one()
    approval = (
        await session.execute(
            select(AgentApproval).where(AgentApproval.run_id == child.id)
        )
    ).scalars().one()
    # 兄弟子运行 + pending 审批：驳回后必须被收敛（expired/cancelled）。
    sibling = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="兄弟任务",
        mode="role", role="publisher", parent_run_id=parent.id,
        status="waiting_approval", workflow_run_id=run.id, workflow_node_id="tm",
    )
    session.add(sibling)
    await session.flush()
    step = AgentStep(
        tenant_id=DEFAULT_TENANT_ID, run_id=sibling.id, step_index=0,
        kind="tool", tool_name="content.create_version", status="waiting",
    )
    session.add(step)
    await session.commit()
    await session.refresh(sibling)
    await session.refresh(step)
    sib_approval = AgentApproval(
        tenant_id=DEFAULT_TENANT_ID, run_id=sibling.id, step_id=step.id,
        tool_name="content.create_version", risk="high", payload={},
        status="pending", expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    session.add(sib_approval)
    await session.commit()
    await session.refresh(sib_approval)

    resp = await http.post(
        f"/api/agent/approvals/{approval.id}/decide",
        json={"decision": "reject", "note": "不行"},
    )
    assert resp.status_code == 200
    for row in (run, parent, child, approval, sibling, sib_approval):
        await session.refresh(row)
    assert child.status == "failed"
    assert child.error_code == "approval_rejected"
    assert approval.status == "rejected"
    # 裁定 6：团队父先落 failed（保留现场），工作流再落 failed。
    assert parent.status == "failed"
    assert parent.error_code == "approval_rejected"
    assert run.status == "failed"
    assert run.error_code == "approval_rejected"
    assert sib_approval.status == "expired"
    assert sibling.status == "cancelled"
    assert queue.jobs == []


@pytest.mark.asyncio
async def test_team_node_retry_reuses_parent(wf_env):
    session = wf_env.session
    gateway = TeamGateway(
        [
            "无效输出1", "无效输出2",  # 第一轮 Lead 连续两次无效 → team_lead_invalid
            json.dumps({"action": "delegate", "role": "creator", "task": "写稿"}),
            rturn("稿件完成"),
            rturn('{"verdict":"pass","issues":[]}'),
            json.dumps({"action": "finalize", "summary": "已交付"}),
        ]
    )
    deps = make_deps(wf_env, gateway)
    run = await make_run(
        session, graph=team_graph(roles=["creator"], retry=1)
    )
    assert await run_workflow(wf_env.factory, run.id, deps=deps) == "succeeded"
    assert await team_count(session, run) == 1
    parent = await team_parent_of(session, run)
    assert parent.status == "succeeded"
    assert "creator" in (parent.team_state or {}).get("roles_run", [])
    rows = (
        await session.execute(
            select(WorkflowNodeRun)
            .where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "tm",
            )
            .order_by(WorkflowNodeRun.attempt)
        )
    ).scalars().all()
    assert len(rows) == 2
    # 裁定 3/4：重试沿用同一父运行断点，不重头创建。
    assert rows[0].agent_run_id == parent.id
    assert rows[1].agent_run_id == parent.id
    assert rows[1].attempt == 2
    assert rows[1].status == "ok"


@pytest.mark.asyncio
async def test_team_node_disabled_role_aborts(wf_env):
    session = wf_env.session
    session.add(
        AgentTeamRole(tenant_id=DEFAULT_TENANT_ID, role="scout", enabled=False)
    )
    await session.commit()
    gateway = TeamGateway(
        [
            json.dumps({"action": "delegate", "role": "creator", "task": "写稿"}),
            rturn("稿件完成"),
            rturn('{"verdict":"pass","issues":[]}'),
            json.dumps({"action": "finalize", "summary": "已交付"}),
        ]
    )
    deps = make_deps(wf_env, gateway)
    run = await make_run(
        session, graph=team_graph(roles=["scout", "creator", "critic"])
    )
    assert await run_workflow(wf_env.factory, run.id, deps=deps) == "failed"
    await session.refresh(run)
    assert run.error_code == "team_roles_invalid"
    node_run = (
        await session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "tm",
            )
        )
    ).scalars().one()
    assert node_run.status == "error"
    assert "scout" in node_run.error
    # 裁定 9：角色被禁用时不创建团队父运行。
    assert await team_count(session, run) == 0


def test_team_retry_budget_and_estimate():
    settings = get_settings()
    graph = parse_graph(team_graph(retry=2))
    tm = graph.nodes["tm"]
    assert _retry_budget(tm) == 2
    deps = EngineDeps(
        gateway=None,
        tool_registry=ToolRegistry(),
        skill_registry=SkillRegistry(settings.skills_dir),
        storage=get_storage(),
        queue=None,
        settings=settings,
    )
    expected = float(_team_budget(tm, settings).get("max_seconds") or 0)
    assert expected > 0
    assert _estimate(tm, deps) == expected