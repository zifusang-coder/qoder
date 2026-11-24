import pytest

from app.agent.roles import (
    ROLES,
    clamp_team_budget,
    effective_roles,
    team_budget_defaults,
    validate_participants,
    validate_role_patch,
)
from app.config import get_settings
from app.errors import AppError
from app.models.agent import AgentTeamRole
from app.models.base import DEFAULT_TENANT_ID


@pytest.mark.asyncio
async def test_defaults_without_rows(db_session):
    roles = await effective_roles(db_session, DEFAULT_TENANT_ID)
    assert roles["scout"].enabled is True
    assert roles["scout"].tools == ROLES["scout"].tools
    assert roles["planner"].enabled and roles["creator"].enabled


@pytest.mark.asyncio
async def test_tenant_narrowing(db_session):
    db_session.add(AgentTeamRole(
        tenant_id=DEFAULT_TENANT_ID, role="scout", enabled=False,
        tools=["profile.get"], budget={"max_tokens": 1000},
    ))
    db_session.add(AgentTeamRole(
        tenant_id=DEFAULT_TENANT_ID, role="creator", enabled=False,
    ))
    await db_session.commit()
    roles = await effective_roles(db_session, DEFAULT_TENANT_ID)
    assert roles["scout"].enabled is False
    assert roles["scout"].tools == ("profile.get",)
    assert roles["scout"].budget["max_tokens"] == 1000
    assert roles["creator"].enabled is True  # 必选角色强制启用


def test_validate_participants():
    # 用纯函数路径：构造默认 effective 需要 DB，这里直接测校验规则
    from app.agent.roles import EXECUTION_ROLES

    class Fake:
        enabled = True

    effective = {key: Fake() for key in EXECUTION_ROLES}
    assert validate_participants(["creator", "scout"], effective) == ("scout", "creator")
    with pytest.raises(AppError) as exc:
        validate_participants(["scout"], effective)
    assert exc.value.code == "team_roles_invalid"
    with pytest.raises(AppError):
        validate_participants(["creator", "planner"], effective)
    with pytest.raises(AppError):
        validate_participants([], effective)
    effective["scout"].enabled = False
    with pytest.raises(AppError):
        validate_participants(["scout", "creator"], effective)


def test_validate_role_patch():
    validate_role_patch("scout", enabled=False, tools=["profile.get"],
                        budget={"max_tokens": 1000})
    with pytest.raises(AppError) as exc:
        validate_role_patch("creator", enabled=False)
    assert exc.value.code == "policy_not_loosenable"
    with pytest.raises(AppError):
        validate_role_patch("planner", enabled=False)
    with pytest.raises(AppError):
        validate_role_patch("scout", tools=["publish.execute"])
    with pytest.raises(AppError):
        validate_role_patch("scout", budget={"max_tokens": 999999})
    with pytest.raises(AppError) as exc:
        validate_role_patch("ghost")
    assert exc.value.code == "team_roles_invalid"


def test_clamp_team_budget():
    settings = get_settings()
    defaults = team_budget_defaults(settings)
    assert defaults == {
        "max_tokens": 120000, "max_tool_calls": 40,
        "max_turns": 12, "max_seconds": 900,
    }
    assert clamp_team_budget({}, settings) == defaults
    assert clamp_team_budget({"max_tokens": 200000}, settings)["max_tokens"] == 200000
    with pytest.raises(AppError) as exc:
        clamp_team_budget({"max_tokens": 200001}, settings)
    assert exc.value.code == "budget_invalid"
    with pytest.raises(AppError):
        clamp_team_budget({"max_tokens": 0}, settings)
    with pytest.raises(AppError):
        clamp_team_budget({"max_steps": 5}, settings)