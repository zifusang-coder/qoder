"""内置团队角色：提示词、工具白名单与默认预算。代码管理，租户只能收窄。"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError
from app.models.agent import AgentTeamRole

ROLE_BUDGET_KEYS = ("max_steps", "max_tokens", "max_tool_calls", "max_seconds")
TEAM_BUDGET_KEYS = ("max_tokens", "max_tool_calls", "max_turns", "max_seconds")
EXECUTION_ROLES = ("scout", "creator", "critic", "publisher")
ROLE_KEYS = ("planner",) + EXECUTION_ROLES

PLANNER_TOOLS = (
    "project.get",
    "project.list",
    "content.get",
    "content.list",
    "profile.get",
    "profile.list",
    "platform.capabilities",
    "analytics.summary",
    "calendar.list",
    "publish.job.get",
    "publish.job.list",
)


@dataclass(frozen=True)
class RoleSpec:
    key: str
    label: str
    system_prompt: str
    tools: tuple[str, ...]
    budget: dict[str, int]
    required: bool = False


ROLES: dict[str, RoleSpec] = {
    "planner": RoleSpec(
        key="planner",
        label="Lead 规划",
        system_prompt=(
            "你是内容团队的 Lead。根据目标与黑板，每轮决定：委派一个执行角色、"
            "要求 creator 返工、或 finalize 交付。只输出一个 JSON 对象，不要其他文字。"
        ),
        tools=PLANNER_TOOLS,
        budget={"max_steps": 4, "max_tokens": 12000, "max_tool_calls": 4, "max_seconds": 180},
        required=True,
    ),
    "scout": RoleSpec(
        key="scout",
        label="Scout 调研",
        system_prompt=(
            "你是内容团队的调研角色。查画像、历史内容、数据表现与平台能力，"
            "输出事实与约束；不要创作内容。"
        ),
        tools=PLANNER_TOOLS + ("media.job.get",),
        budget={"max_steps": 6, "max_tokens": 20000, "max_tool_calls": 8, "max_seconds": 240},
    ),
    "creator": RoleSpec(
        key="creator",
        label="Creator 创作",
        system_prompt=(
            "你是内容团队的创作角色。建项目、写内容版本、跑创作技能、生成媒体；"
            "发布相关操作只能创建草稿，不能声称已经发布。"
        ),
        tools=(
            "project.create", "project.get", "project.list",
            "content.get", "content.list", "content.create_version",
            "profile.get", "profile.list",
            "skill.run", "media.generate", "media.job.get",
            "platform.capabilities",
        ),
        budget={"max_steps": 8, "max_tokens": 40000, "max_tool_calls": 10, "max_seconds": 360},
        required=True,
    ),
    "critic": RoleSpec(
        key="critic",
        label="Critic 审稿",
        system_prompt=(
            "你是内容团队的审稿角色。只读评审交付内容，"
            '最终只输出 JSON：{"verdict":"pass|fail","issues":["..."]}。'
        ),
        tools=(
            "content.get", "content.list", "platform.capabilities",
            "publish.job.get", "publish.job.list", "analytics.summary",
        ),
        budget={"max_steps": 4, "max_tokens": 15000, "max_tool_calls": 4, "max_seconds": 180},
    ),
    "publisher": RoleSpec(
        key="publisher",
        label="Publisher 交付",
        system_prompt=(
            "你是内容团队的交付角色。创建发布草稿并预检；不排期、不发布。"
        ),
        tools=(
            "content.get", "content.list", "platform.capabilities",
            "publish.create_draft_job", "publish.precheck",
            "publish.job.get", "publish.job.list",
        ),
        budget={"max_steps": 5, "max_tokens": 12000, "max_tool_calls": 6, "max_seconds": 180},
    ),
}


@dataclass(frozen=True)
class EffectiveRole:
    key: str
    label: str
    enabled: bool
    tools: tuple[str, ...]
    budget: dict[str, int]
    required: bool


async def effective_roles(session: AsyncSession, tenant_id) -> dict[str, EffectiveRole]:
    rows = (
        await session.execute(
            select(AgentTeamRole).where(AgentTeamRole.tenant_id == tenant_id)
        )
    ).scalars().all()
    overrides = {row.role: row for row in rows}
    result: dict[str, EffectiveRole] = {}
    for key in ROLE_KEYS:
        spec = ROLES[key]
        row = overrides.get(key)
        enabled = True if spec.required else (row.enabled if row is not None else True)
        tools = tuple(row.tools) if row is not None and row.tools else spec.tools
        tools = tuple(t for t in tools if t in spec.tools) or spec.tools
        budget = dict(spec.budget)
        if row is not None and isinstance(row.budget, dict):
            for dim, value in row.budget.items():
                if (
                    dim in budget
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 < value <= spec.budget[dim]
                ):
                    budget[dim] = value
        result[key] = EffectiveRole(
            key=key, label=spec.label, enabled=enabled,
            tools=tools, budget=budget, required=spec.required,
        )
    return result


def validate_participants(raw, effective: dict[str, EffectiveRole]) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise AppError("team_roles_invalid", "至少选择一个执行角色", 422)
    requested: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in EXECUTION_ROLES:
            raise AppError("team_roles_invalid", f"未知执行角色：{item}", 422)
        if not effective[item].enabled:
            raise AppError("team_roles_invalid", f"角色 {item} 已被租户关闭", 422)
        requested.append(item)
    if "creator" not in requested:
        raise AppError("team_roles_invalid", "团队必须包含 creator", 422)
    return tuple(role for role in EXECUTION_ROLES if role in requested)


def validate_role_patch(role, *, enabled=None, tools=None, budget=None) -> None:
    if role not in ROLE_KEYS:
        raise AppError("team_roles_invalid", f"未知角色：{role}", 422)
    spec = ROLES[role]
    if enabled is False and spec.required:
        raise AppError("policy_not_loosenable", f"{role} 不可关闭", 422)
    if tools is not None:
        if not isinstance(tools, list) or not tools:
            raise AppError("policy_not_loosenable", "工具列表必须是非空数组", 422)
        for tool in tools:
            if tool not in spec.tools:
                raise AppError("policy_not_loosenable", f"不能新增工具：{tool}", 422)
    if budget is not None:
        if not isinstance(budget, dict):
            raise AppError("policy_not_loosenable", "预算必须是对象", 422)
        for dim, value in budget.items():
            if (
                dim not in spec.budget
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > spec.budget[dim]
            ):
                raise AppError("policy_not_loosenable", f"预算 {dim} 只能调低", 422)


def team_budget_defaults(settings: Settings) -> dict[str, int]:
    return {
        "max_tokens": settings.agent_team_max_tokens,
        "max_tool_calls": settings.agent_team_max_tool_calls,
        "max_turns": settings.agent_team_max_turns,
        "max_seconds": settings.agent_team_max_seconds,
    }


def team_budget_ceilings(settings: Settings) -> dict[str, int]:
    return {
        "max_tokens": settings.agent_team_token_ceiling,
        "max_tool_calls": 60,
        "max_turns": 20,
        "max_seconds": settings.agent_team_seconds_ceiling,
    }


def clamp_team_budget(raw, settings: Settings) -> dict[str, int]:
    cleaned = dict(team_budget_defaults(settings))
    ceilings = team_budget_ceilings(settings)
    if raw is None:
        return cleaned
    if not isinstance(raw, dict):
        raise AppError("budget_invalid", "预算必须是对象", 422)
    for dim, value in raw.items():
        if dim not in TEAM_BUDGET_KEYS:
            raise AppError("budget_invalid", f"未知预算维度：{dim}", 422)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise AppError("budget_invalid", f"预算 {dim} 必须是正整数", 422)
        if value > ceilings[dim]:
            raise AppError("budget_invalid", f"预算 {dim} 超过上限", 422)
        cleaned[dim] = value
    return cleaned