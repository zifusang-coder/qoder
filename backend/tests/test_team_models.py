import uuid

import pytest
from sqlalchemy import select

from app.agent.roles import EXECUTION_ROLES, ROLE_KEYS, ROLES
from app.config import get_settings
from app.models.agent import AgentRun, AgentTeamEntry, AgentTeamRole
from app.models.base import DEFAULT_TENANT_ID


def test_role_constants():
    assert ROLE_KEYS == ("planner", "scout", "creator", "critic", "publisher")
    assert EXECUTION_ROLES == ("scout", "creator", "critic", "publisher")
    assert ROLES["planner"].required and ROLES["creator"].required
    assert not ROLES["scout"].required
    # scout = planner 白名单 + media.job.get
    assert ROLES["scout"].tools == ROLES["planner"].tools + ("media.job.get",)
    for spec in ROLES.values():
        assert set(spec.budget) == {
            "max_steps", "max_tokens", "max_tool_calls", "max_seconds"
        }
        assert all(v > 0 for v in spec.budget.values())
    # 任何角色都拿不到排期/真发布/导出工具
    forbidden = ("publish.schedule", "publish.execute", "publish.export")
    for spec in ROLES.values():
        assert not set(spec.tools) & set(forbidden)


def test_team_settings_defaults():
    settings = get_settings()
    assert settings.agent_team_max_turns == 12
    assert settings.agent_team_rework_limit == 2
    assert settings.agent_team_blackboard_inject == 12
    assert settings.agent_team_max_tokens == 120000
    assert settings.agent_team_max_tool_calls == 40
    assert settings.agent_team_max_seconds == 900
    assert settings.agent_team_token_ceiling == 200000
    assert settings.agent_team_seconds_ceiling == 1800


@pytest.mark.asyncio
async def test_team_models(db_session):
    parent = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=uuid.uuid4(), goal="团队目标",
        mode="team",
    )
    db_session.add(parent)
    await db_session.commit()
    await db_session.refresh(parent)
    assert parent.role == "" and parent.team_state == {}
    assert parent.parent_run_id is None

    child = AgentRun(
        tenant_id=DEFAULT_TENANT_ID, user_id=parent.user_id, goal="子任务",
        mode="role", role="scout", parent_run_id=parent.id,
    )
    entry = AgentTeamEntry(
        tenant_id=DEFAULT_TENANT_ID, run_id=parent.id, seq=1, kind="goal",
        content={"goal": "团队目标"},
    )
    config = AgentTeamRole(tenant_id=DEFAULT_TENANT_ID, role="scout")
    db_session.add_all([child, entry, config])
    await db_session.commit()

    rows = (await db_session.execute(
        select(AgentRun).where(AgentRun.parent_run_id == parent.id)
    )).scalars().all()
    assert [row.role for row in rows] == ["scout"]
    assert config.enabled is True and config.tools == [] and config.budget == {}