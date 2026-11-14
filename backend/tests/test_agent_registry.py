import pytest

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.registry import AgentConfigError, ToolRegistry


def _spec(name="demo.echo", *, risk="read", schema=None, description="示例工具"):
    async def handler(_ctx: ToolContext, _args: dict) -> ToolResult:
        return ToolResult(summary="ok")

    return ToolSpec(
        name=name,
        description=description,
        input_schema=schema
        or {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        },
        risk=risk,
        handler=handler,
    )


def test_register_and_list_specs():
    registry = ToolRegistry()
    registry.register(_spec())
    assert registry.get("demo.echo").name == "demo.echo"
    assert registry.names() == ["demo.echo"]
    assert registry.schemas() == [
        {
            "name": "demo.echo",
            "description": "示例工具",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "additionalProperties": False,
            },
        }
    ]


def test_duplicate_name_rejected():
    registry = ToolRegistry()
    registry.register(_spec())
    with pytest.raises(AgentConfigError):
        registry.register(_spec())


def test_schema_must_forbid_extra_properties():
    with pytest.raises(AgentConfigError):
        ToolRegistry().register(
            _spec(
                schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                }
            )
        )


def test_invalid_schema_rejected():
    with pytest.raises(AgentConfigError):
        ToolRegistry().register(
            _spec(schema={"type": "object", "properties": {"x": {"type": "nope"}}, "additionalProperties": False})
        )


def test_blank_description_rejected():
    with pytest.raises(AgentConfigError):
        ToolRegistry().register(_spec(description="  "))


def test_unknown_risk_rejected():
    with pytest.raises(AgentConfigError):
        ToolRegistry().register(_spec(risk="dangerous"))


def test_get_unknown_returns_none():
    assert ToolRegistry().get("missing") is None
