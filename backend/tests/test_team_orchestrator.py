import json as _json
import uuid as _uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent.team_contracts import (
    critic_task,
    lead_messages,
    parse_critic_verdict,
    parse_lead_decision,
)
from app.config import get_settings
from app.llm.tools import ToolTurn
from app.llm.types import Usage
from app.models.agent import AgentRun, AgentTeamEntry
from app.models.base import DEFAULT_TENANT_ID
from app.skills.registry import SkillRegistry
from app.storage import get_storage


def test_parse_delegate():
    decision = parse_lead_decision(
        '{"action":"delegate","role":"scout","task":"查数据","reason":"先调研"}',
        allowed_roles=("scout", "creator"),
    )
    assert decision.action == "delegate" and decision.role == "scout"
    assert decision.task == "查数据"


def test_parse_wrapped_json():
    decision = parse_lead_decision(
        '```json\n{"action":"finalize","summary":"完成"}\n```',
        allowed_roles=("creator",),
    )
    assert decision.action == "finalize" and decision.summary == "完成"


def test_parse_rework_only_creator():
    with pytest.raises(ValueError):
        parse_lead_decision(
            '{"action":"rework","role":"scout","task":"改"}',
            allowed_roles=("scout", "creator"),
        )
    decision = parse_lead_decision(
        '{"action":"rework","role":"creator","task":"改标题"}',
        allowed_roles=("creator",),
    )
    assert decision.action == "rework"


@pytest.mark.parametrize("text", [
    "不是 JSON",
    '{"action":"unknown"}',
    '{"action":"delegate","role":"ghost","task":"t"}',
    '{"action":"delegate","role":"scout","task":""}',
    '{"action":"finalize","summary":""}',
    '{"action":"delegate","role":"scout","task":"' + "x" * 2001 + '"}',
])
def test_parse_invalid(text):
    with pytest.raises(ValueError):
        parse_lead_decision(text, allowed_roles=("scout", "creator"))


def test_parse_critic():
    verdict = parse_critic_verdict('{"verdict":"fail","issues":["标题太长"]}')
    assert verdict.verdict == "fail" and verdict.issues == ["标题太长"]
    assert parse_critic_verdict('{"verdict":"pass","issues":[]}').verdict == "pass"
    with pytest.raises(ValueError):
        parse_critic_verdict('{"verdict":"maybe"}')
    with pytest.raises(ValueError):
        parse_critic_verdict("随便写")


def test_prompt_builders():
    messages = lead_messages(
        goal="做一篇内容", participants=("scout", "creator"),
        entries=[{"seq": 1, "kind": "goal", "role": "", "content": {"goal": "做一篇内容"}}],
        state={"turn": 1, "rework": 0},
        budget={"max_turns": 12},
    )
    assert messages[0]["role"] == "system"
    assert "做一篇内容" in messages[1]["content"]
    assert "scout" in messages[1]["content"]
    task = critic_task(goal="做一篇内容", summary="已写稿", artifacts=[], note="")
    assert "verdict" in task and "做一篇内容" in task


class ScriptedGateway:
    """Lead 用 complete()，角色子运行用 complete_tool_calls()；按脚本顺序消费。"""

    def __init__(self, script):
        self.script = list(script)
        self.last_usage = Usage(10, 5)
        self.complete_calls = []

    async def complete(self, messages, **kwargs):
        self.complete_calls.append(messages)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def complete_tool_calls(self, messages, *, tools):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def role_turn(content):
    return ToolTurn(content=content, tool_calls=[], finish_reason="stop",
                    usage=Usage(20, 10))


def lead(action, **kw):
    return _json.dumps({"action": action, **kw}, ensure_ascii=False)


@pytest_asyncio.fixture
async def team_env(db_session):
    settings = get_settings()

    class Env:
        pass

    holder = Env()
    holder.factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    holder.settings = settings
    holder.session = db_session
    return holder


async def make_parent(env, *, roles=("scout", "creator"), budget=None):
    run = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=_uuid.uuid4(),
        goal="围绕主题产出一篇内容", mode="team", status="queued",
        plan={"roles": list(roles)},
        budget=budget or {
            "max_tokens": 120000, "max_tool_calls": 40,
            "max_turns": 12, "max_seconds": 900,
        },
    )
    env.session.add(run)
    await env.session.commit()
    await env.session.refresh(run)
    return run


def deps(env, gateway):
    from app.agent.registry import get_tool_registry
    return {
        "gateway": gateway, "tool_registry": get_tool_registry(),
        "skill_registry": SkillRegistry(env.settings.skills_dir),
        "storage": get_storage(), "queue": None, "settings": env.settings,
        "session_factory": env.factory,
    }


@pytest.mark.asyncio
async def test_delegate_then_finalize(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env)
    gateway = ScriptedGateway([
        lead("delegate", role="scout", task="调研选题"),
        role_turn("调研结论：主题可行"),
        lead("finalize", summary="已交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    assert parent.result["summary"] == "已交付"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
    )).scalars().all()
    assert [(c.role, c.status) for c in children] == [("scout", "succeeded")]
    entries = (await team_env.session.execute(
        select(AgentTeamEntry).where(AgentTeamEntry.run_id == parent.id)
        .order_by(AgentTeamEntry.seq)
    )).scalars().all()
    kinds = [e.kind for e in entries]
    assert kinds[0] == "goal" and "delegation" in kinds and "result" in kinds
    assert kinds[-1] == "final"
    # 父 usage 只记 Lead 用量，不并入子运行
    assert parent.usage["prompt_tokens"] == 20  # 两次 Lead 调用 × 10


@pytest.mark.asyncio
async def test_turn_budget_exhausted(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env, budget={
        "max_tokens": 120000, "max_tool_calls": 40,
        "max_turns": 1, "max_seconds": 900,
    })
    gateway = ScriptedGateway([
        lead("delegate", role="scout", task="调研"),
        role_turn("完成"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "team_budget_exhausted"


@pytest.mark.asyncio
async def test_lead_invalid_twice_fails(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env)
    gateway = ScriptedGateway(["不是 JSON", "还不是 JSON"])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "team_lead_invalid"


@pytest.mark.asyncio
async def test_resume_settled_child_no_redelegate(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env)
    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="调研",
        mode="role", role="scout", parent_run_id=parent.id,
        status="succeeded", result_text="调研完成",
        result={"artifacts": []},
    )
    team_env.session.add(child)
    await team_env.session.commit()
    await team_env.session.refresh(child)
    parent.team_state = {
        "turn": 1, "phase": "role",
        "current_child_id": str(child.id), "current_role": "scout",
    }
    parent.status = "queued"
    await team_env.session.commit()
    gateway = ScriptedGateway([lead("finalize", summary="交付")])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
    )).scalars().all()
    assert len(children) == 1  # 没有重复委派


@pytest.mark.asyncio
async def test_forced_critic_pass(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("稿件完成"),
        role_turn('{"verdict":"pass","issues":[]}'),
        lead("finalize", summary="交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
        .order_by(AgentRun.created_at)
    )).scalars().all()
    assert [c.role for c in children] == ["creator", "critic"]
    assert parent.result["critic"]["verdict"] == "pass"


@pytest.mark.asyncio
async def test_critic_fail_forces_rework_then_pass(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("稿件 v1"),
        role_turn('{"verdict":"fail","issues":["标题太长"]}'),
        role_turn("稿件 v2"),
        role_turn('{"verdict":"pass","issues":[]}'),
        lead("finalize", summary="交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
        .order_by(AgentRun.created_at)
    )).scalars().all()
    assert [c.role for c in children] == ["creator", "critic", "creator", "critic"]
    assert children[2].goal.find("标题太长") >= 0
    assert parent.result["critic"]["rework_rounds"] == 1


@pytest.mark.asyncio
async def test_critic_rejected_after_two_reworks(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("v1"),
        role_turn('{"verdict":"fail","issues":["问题"]}'),
        role_turn("v2"),
        role_turn('{"verdict":"fail","issues":["问题"]}'),
        role_turn("v3"),
        role_turn('{"verdict":"fail","issues":["问题"]}'),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "critic_rejected"
    # 现场保留：黑板与子运行仍在
    entries = (await team_env.session.execute(
        select(AgentTeamEntry).where(AgentTeamEntry.run_id == parent.id)
    )).scalars().all()
    assert len(entries) > 0


@pytest.mark.asyncio
async def test_critic_invalid_twice(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("稿件"),
        role_turn("不是 JSON"),
        role_turn("还不是 JSON"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "critic_invalid"


@pytest.mark.asyncio
async def test_loop_detected_same_task_three_times(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env)
    gateway = ScriptedGateway([
        lead("delegate", role="scout", task="同样的任务"),
        role_turn("r1"),
        lead("delegate", role="scout", task="同样的任务"),
        role_turn("r2"),
        lead("delegate", role="scout", task="同样的任务"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "failed"
    assert parent.error_code == "team_loop_detected"


@pytest.mark.asyncio
async def test_loop_guard_consecutive_alternating_ok(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env)
    gateway = ScriptedGateway([
        lead("delegate", role="scout", task="任务甲"),
        role_turn("r1"),
        lead("delegate", role="scout", task="任务乙"),
        role_turn("r2"),
        lead("delegate", role="scout", task="任务甲"),
        role_turn("r3"),
        lead("delegate", role="scout", task="任务乙"),
        role_turn("r4"),
        lead("delegate", role="scout", task="任务甲"),
        role_turn("r5"),
        lead("finalize", summary="交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded", parent.error_code


@pytest.mark.asyncio
async def test_critic_invalid_retry_resets_per_incident(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("稿件 v1"),
        role_turn("不是 JSON"),
        role_turn('{"verdict":"fail","issues":["标题太长"]}'),
        role_turn("稿件 v2"),
        role_turn("还不是 JSON"),
        role_turn('{"verdict":"pass","issues":[]}'),
        lead("finalize", summary="交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded", parent.error_code
    assert parent.result["critic"]["verdict"] == "pass"


@pytest.mark.asyncio
async def test_critic_disabled_skips_review(team_env):
    from app.agent.team import run_team
    from app.models.agent import AgentTeamRole

    team_env.session.add(AgentTeamRole(
        tenant_id=DEFAULT_TENANT_ID, role="critic", enabled=False,
    ))
    await team_env.session.commit()
    parent = await make_parent(team_env, roles=("creator", "critic"))
    gateway = ScriptedGateway([
        lead("delegate", role="creator", task="写稿"),
        role_turn("稿件"),
        lead("finalize", summary="交付"),
    ])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    assert parent.status == "succeeded"
    children = (await team_env.session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
    )).scalars().all()
    assert [c.role for c in children] == ["creator"]


@pytest.mark.asyncio
async def test_child_failure_propagates_and_closes_siblings(team_env):
    from app.agent.team import run_team

    parent = await make_parent(team_env)
    # 手工构造：一个失败子运行 + 一个排队子运行，父 running
    bad = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="t",
        mode="role", role="scout", parent_run_id=parent.id,
        status="failed", error_code="agent_budget_exhausted",
        error_message="预算用尽",
    )
    pending = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="t2",
        mode="role", role="creator", parent_run_id=parent.id, status="queued",
    )
    team_env.session.add_all([bad, pending])
    await team_env.session.commit()
    await team_env.session.refresh(bad)
    parent.status = "running"
    parent.team_state = {"turn": 1, "phase": "role",
                         "current_child_id": str(bad.id)}
    await team_env.session.commit()
    gateway = ScriptedGateway([])
    await run_team(team_env.session, parent, **deps(team_env, gateway))
    await team_env.session.refresh(parent)
    await team_env.session.refresh(pending)
    assert parent.status == "failed"
    assert parent.error_code == "agent_budget_exhausted"
    assert pending.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_team_run(team_env):
    from app.agent.team import cancel_team_run
    parent = await make_parent(team_env)
    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="t",
        mode="role", role="scout", parent_run_id=parent.id, status="running",
    )
    team_env.session.add(child)
    parent.status = "running"
    await team_env.session.commit()
    assert await cancel_team_run(team_env.session, parent) is True
    await team_env.session.refresh(parent)
    await team_env.session.refresh(child)
    assert parent.status == "cancelled"
    assert child.status == "cancelled"
    assert await cancel_team_run(team_env.session, parent) is False