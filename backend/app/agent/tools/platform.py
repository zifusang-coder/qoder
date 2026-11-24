from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import schema
from app.publish.registry import SPECS as PLATFORM_SPECS


async def capabilities(_ctx: ToolContext, _args: dict) -> ToolResult:
    items = [
        {
            "platform": spec.key,
            "label": spec.label,
            "capabilities": dict(spec.content_capabilities),
            "note": spec.note,
        }
        for spec in PLATFORM_SPECS.values()
    ]
    return ToolResult(summary=f"{len(items)} 个平台的发布能力", data={"items": items})


SPECS = [
    ToolSpec(
        name="platform.capabilities",
        description="读取各平台与内容类型的发布能力矩阵，避免越界承诺。",
        input_schema=schema(),
        risk="read",
        handler=capabilities,
    )
]
