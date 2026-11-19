"""内置团队角色：提示词、工具白名单与默认预算。代码管理，租户只能收窄。"""

from dataclasses import dataclass

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