from dataclasses import dataclass

from app.llm.types import Usage


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class ToolTurn:
    content: str
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: Usage
