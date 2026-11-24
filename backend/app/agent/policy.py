from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contracts import Risk, ToolSpec
from app.errors import AppError
from app.models.agent import AgentPolicy

DEFAULTS: dict[Risk, str] = {"read": "auto", "low": "auto", "high": "confirm"}
RANK = {"auto": 0, "confirm": 1, "deny": 2}
TOOL_PREFIX = "tool:"


def default_action(risk: Risk) -> str:
    return DEFAULTS[risk]


def _clamp_action(action: object, tool: ToolSpec) -> str:
    """把存储的动作收敛到该工具当前 risk 默认值允许的范围。

    存储的覆盖可能是旧值：工具后来被重新分类、动作不是字符串，或动作未知。
    这些情况一律回退到风险默认值，保证持久化数据永远不会放宽策略。
    """
    default = default_action(tool.risk)
    if (
        isinstance(action, str)
        and action in RANK
        and RANK[action] >= RANK[default]
    ):
        return action
    return default


def resolve_policy(overrides: dict, tool: ToolSpec) -> str:
    return _clamp_action(overrides.get(f"{TOOL_PREFIX}{tool.name}"), tool)


def clamped_overrides(
    overrides: dict, specs: dict[str, ToolSpec]
) -> dict[str, str]:
    """返回按当前注册表校验后的有效覆盖。

    丢弃不存在工具的键、非字符串或未知动作，以及低于当前风险默认值的条目，
    因此返回值只包含真实生效的收紧策略，且一定是 ``dict[str, str]``。
    """
    if not isinstance(overrides, dict):
        return {}
    effective: dict[str, str] = {}
    for key, action in overrides.items():
        if not isinstance(key, str) or not key.startswith(TOOL_PREFIX):
            continue
        spec = specs.get(key[len(TOOL_PREFIX) :])
        if spec is None:
            continue
        clamped = _clamp_action(action, spec)
        if clamped == action:
            effective[key] = clamped
    return effective


def validate_overrides(overrides: dict, specs: dict[str, ToolSpec]) -> dict[str, str]:
    if not isinstance(overrides, dict):
        raise AppError("policy_invalid", "策略覆盖必须是对象", 422)
    cleaned: dict[str, str] = {}
    for key, action in overrides.items():
        if not isinstance(key, str) or not key.startswith(TOOL_PREFIX):
            raise AppError("policy_invalid", f"策略键不合法: {key}", 422)
        name = key[len(TOOL_PREFIX) :]
        spec = specs.get(name)
        if spec is None:
            raise AppError("policy_invalid", f"未知工具: {name}", 422)
        if not isinstance(action, str) or action not in RANK:
            raise AppError("policy_invalid", f"未知策略动作: {action}", 422)
        if RANK[action] < RANK[default_action(spec.risk)]:
            raise AppError(
                "policy_not_loosenable",
                f"{name} 的策略只能收紧，不能放宽为 {action}",
                422,
            )
        cleaned[key] = action
    return cleaned


def snapshot_policy(overrides: dict, specs: dict[str, ToolSpec]) -> dict[str, str]:
    return {name: resolve_policy(overrides, spec) for name, spec in specs.items()}


async def load_overrides(session: AsyncSession, tenant_id: str) -> dict:
    row = (
        await session.execute(
            select(AgentPolicy).where(AgentPolicy.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    return dict(row.overrides or {}) if row is not None else {}


async def save_overrides(
    session: AsyncSession, tenant_id: str, overrides: dict
) -> dict:
    row = (
        await session.execute(
            select(AgentPolicy).where(AgentPolicy.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(AgentPolicy(tenant_id=tenant_id, overrides=overrides))
    else:
        row.overrides = overrides
    await session.commit()
    return overrides
