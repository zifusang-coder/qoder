"""内置工作流模板。

模板只包含图，不含租户数据；实例化时深拷贝，调用方负责落库。
所有模板都必须能通过 `validate_publish`：单一输入、所有节点可达输出、无环。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Template:
    key: str
    name: str
    description: str
    parameters: list[dict]
    graph: dict


WEEKLY_TOPIC = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "position": {"x": 0, "y": 140},
            "data": {
                "name": "本周主题",
                "fields": [
                    {
                        "key": "topic",
                        "type": "string",
                        "required": True,
                        "maxLength": 200,
                    }
                ],
            },
        },
        {
            "id": "write",
            "type": "agent",
            "position": {"x": 260, "y": 140},
            "data": {
                "name": "选题与创作",
                "goal": (
                    "围绕「{{inputs.topic}}」完成本周选题，并写出一篇可直接"
                    "发布的完整稿件，包含标题与正文。"
                ),
                "tools": ["profile.*", "content.list"],
                "budget": {"max_steps": 12, "max_tool_calls": 6},
                "retry": 1,
            },
        },
        {
            "id": "review",
            "type": "review",
            "position": {"x": 540, "y": 140},
            "data": {"name": "内容审核", "title": "审核本周稿件"},
        },
        {
            "id": "draft",
            "type": "tool",
            "position": {"x": 820, "y": 140},
            "data": {
                "name": "建立项目草稿",
                "tool": "project.create",
                "args": {"title": "{{inputs.topic}}"},
                "retry": 0,
            },
        },
        {
            "id": "out",
            "type": "output",
            "position": {"x": 1100, "y": 140},
            "data": {},
        },
    ],
    "edges": [
        {"source": "in", "target": "write"},
        {"source": "write", "target": "review"},
        {"source": "review", "target": "draft"},
        {"source": "draft", "target": "out"},
    ],
}

MULTI_PLATFORM_REWRITE = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "position": {"x": 0, "y": 140},
            "data": {
                "name": "原文与目标平台",
                "fields": [
                    {
                        "key": "source",
                        "type": "string",
                        "required": True,
                        "maxLength": 100000,
                    },
                    {
                        "key": "platforms",
                        "type": "string",
                        "required": False,
                        "maxLength": 200,
                    },
                ],
            },
        },
        {
            "id": "rewrite",
            "type": "agent",
            "position": {"x": 300, "y": 140},
            "data": {
                "name": "分平台改写",
                "goal": (
                    "把下面的原文改写成适合 {{inputs.platforms}} 的版本，"
                    "保留全部事实，分别给出标题与正文：\n\n{{inputs.source}}"
                ),
                "tools": ["profile.*", "content.list"],
                "budget": {"max_steps": 10, "max_tool_calls": 4},
                "retry": 1,
            },
        },
        {
            "id": "out",
            "type": "output",
            "position": {"x": 600, "y": 140},
            "data": {},
        },
    ],
    "edges": [
        {"source": "in", "target": "rewrite"},
        {"source": "rewrite", "target": "out"},
    ],
}

POST_PUBLISH_REVIEW = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "position": {"x": 0, "y": 140},
            "data": {
                "name": "复盘窗口",
                "fields": [
                    {"key": "days", "type": "number", "required": True}
                ],
            },
        },
        {
            "id": "summary",
            "type": "tool",
            "position": {"x": 260, "y": 140},
            "data": {
                "name": "拉取数据",
                "tool": "analytics.summary",
                "args": {"days": "{{inputs.days}}"},
                "retry": 1,
            },
        },
        {
            "id": "retro",
            "type": "agent",
            "position": {"x": 540, "y": 140},
            "data": {
                "name": "撰写复盘",
                "goal": (
                    "根据最近 {{inputs.days}} 天的发布数据写一份复盘："
                    "数据亮点、问题、下一步动作。"
                ),
                "tools": ["analytics.summary", "calendar.list", "publish.job.list"],
                "budget": {"max_steps": 10, "max_tool_calls": 4},
                "retry": 1,
            },
        },
        {
            "id": "out",
            "type": "output",
            "position": {"x": 820, "y": 140},
            "data": {},
        },
    ],
    "edges": [
        {"source": "in", "target": "summary"},
        {"source": "summary", "target": "retro"},
        {"source": "retro", "target": "out"},
    ],
}

CONTENT_TEAM_GRAPH = {
    "nodes": [
        {
            "id": "in",
            "type": "input",
            "position": {"x": 0, "y": 140},
            "data": {
                "name": "本周主题",
                "fields": [
                    {"key": "topic", "type": "string", "required": True, "maxLength": 200}
                ],
            },
        },
        {
            "id": "team",
            "type": "team",
            "position": {"x": 280, "y": 140},
            "data": {
                "name": "内容团队",
                "roles": ["scout", "creator", "critic", "publisher"],
                "goal": (
                    "围绕「{{inputs.topic}}」完成调研、创作、审稿与交付："
                    "产出一篇可发布的内容版本，并创建 draft 发布任务（不排期）。"
                ),
                "budget": {"max_tokens": 120000, "max_turns": 12},
                "retry": 0,
            },
        },
        {
            "id": "out",
            "type": "output",
            "position": {"x": 560, "y": 140},
            "data": {"name": "交付结果"},
        },
    ],
    "edges": [
        {"id": "in-team", "source": "in", "target": "team"},
        {"id": "team-out", "source": "team", "target": "out"},
    ],
}

TEMPLATES: tuple[Template, ...] = (
    Template(
        key="weekly_topic",
        name="每周选题 → 创作 → 审核 → 建草稿",
        description=(
            "按主题让 Agent 选题并写稿，人工审核通过后建立项目草稿。"
            "适合每周固定产出。"
        ),
        parameters=[
            {
                "key": "topic",
                "type": "string",
                "required": True,
                "description": "本周主题或方向",
            }
        ],
        graph=WEEKLY_TOPIC,
    ),
    Template(
        key="multi_platform_rewrite",
        name="批量平台改写",
        description=(
            "把一篇原文改写成多个平台的版本，保留事实并适配语气。"
        ),
        parameters=[
            {
                "key": "source",
                "type": "string",
                "required": True,
                "description": "原文正文",
            },
            {
                "key": "platforms",
                "type": "string",
                "required": False,
                "description": "目标平台，逗号分隔",
            },
        ],
        graph=MULTI_PLATFORM_REWRITE,
    ),
    Template(
        key="post_publish_review",
        name="发布后复盘",
        description=(
            "拉取最近若干天的发布数据，让 Agent 产出复盘与下一步动作。"
        ),
        parameters=[
            {
                "key": "days",
                "type": "number",
                "required": True,
                "description": "回看天数（1–365）",
            }
        ],
        graph=POST_PUBLISH_REVIEW,
    ),
    Template(
        key="content_team",
        name="内容团队",
        description="五角色团队围绕一个主题完成调研、创作、审稿与草稿交付。",
        parameters=[
            {"key": "topic", "label": "主题", "type": "string", "required": True}
        ],
        graph=CONTENT_TEAM_GRAPH,
    ),
)


def get_template(key: str) -> Template | None:
    for template in TEMPLATES:
        if template.key == key:
            return template
    return None
