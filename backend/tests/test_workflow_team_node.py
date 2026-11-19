import json

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.registry import ToolRegistry, get_tool_registry
from app.config import get_settings
from app.llm.tools import ToolTurn
from app.llm.types import Usage
from app.models.agent import AgentRun
from app.models.workflow import WorkflowNodeRun
from app.skills.registry import SkillRegistry
from app.storage import get_storage
from app.workflow.engine import EngineDeps, run_workflow
from app.workflow.graph import NODE_KINDS, parse_graph
from app.workflow.validate import validate_publish
from tests.test_workflow_agent_node import edge, make_run, no_sleep, node


def team_graph(*, roles=None, goal="为 {{inputs.topic}} 产出内容", budget=None):
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
                retry=0,
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