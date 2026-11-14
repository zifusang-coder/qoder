import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.llm.gateway import LLMGateway
from app.skills.registry import SkillRegistry
from app.storage import StorageBackend

Risk = Literal["read", "low", "high"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    risk: Risk
    handler: Callable[["ToolContext", dict], Awaitable["ToolResult"]]
    timeout_seconds: float = 60.0


@dataclass
class ToolContext:
    session: AsyncSession
    tenant_id: str
    user_id: uuid.UUID
    run_id: str
    step_index: int
    gateway: LLMGateway
    storage: StorageBackend
    registry: SkillRegistry
    queue: ArqRedis | None
    settings: Settings


@dataclass
class ToolResult:
    summary: str
    data: dict | list = field(default_factory=dict)
    artifacts: list = field(default_factory=list)
