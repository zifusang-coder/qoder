from dataclasses import dataclass
from typing import Literal

ProviderKind = Literal["openai_compatible", "anthropic"]


@dataclass(frozen=True)
class LLMConfig:
    provider_id: str
    kind: ProviderKind
    base_url: str
    api_key: str
    tiers: dict[str, str]

    def __post_init__(self) -> None:
        if self.kind not in ("openai_compatible", "anthropic"):
            raise ValueError(f"不支持的 LLM 提供商类型: {self.kind}")


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens
