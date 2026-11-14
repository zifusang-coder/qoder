import re
from collections.abc import Iterable

import jsonschema

from app.agent.contracts import Risk, ToolSpec

NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
RISKS: set[Risk] = {"read", "low", "high"}


class AgentConfigError(RuntimeError):
    pass


def validate_spec(spec: ToolSpec) -> None:
    if not NAME_RE.match(spec.name):
        raise AgentConfigError(f"工具名不合法: {spec.name}")
    if not spec.description.strip():
        raise AgentConfigError(f"工具缺少描述: {spec.name}")
    if spec.risk not in RISKS:
        raise AgentConfigError(f"未知风险等级 {spec.risk}: {spec.name}")
    schema = spec.input_schema
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise AgentConfigError(f"工具 schema 必须是 object: {spec.name}")
    if schema.get("additionalProperties") is not False:
        raise AgentConfigError(f"工具 schema 必须设置 additionalProperties=false: {spec.name}")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise AgentConfigError(f"工具 schema 非法 {spec.name}: {exc.message}") from exc
    if spec.timeout_seconds <= 0:
        raise AgentConfigError(f"工具超时必须为正数: {spec.name}")


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise AgentConfigError(f"工具名重复: {spec.name}")
        validate_spec(spec)
        self._specs[spec.name] = spec

    def register_all(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def all(self) -> list[ToolSpec]:
        return [self._specs[name] for name in self.names()]

    def schemas(self) -> list[dict]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema,
            }
            for spec in self.all()
        ]


def build_registry() -> ToolRegistry:
    from app.agent.tools import (
        analytics,
        calendar,
        contents,
        media,
        platform,
        profiles,
        projects,
        publish,
        skills,
    )

    registry = ToolRegistry()
    for module in (
        profiles,
        projects,
        contents,
        calendar,
        analytics,
        platform,
        skills,
        media,
        publish,
    ):
        registry.register_all(module.SPECS)
    return registry
