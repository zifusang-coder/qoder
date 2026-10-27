import json
from collections.abc import AsyncIterator

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.llm.types import LLMConfig
from app.models.settings import LLMProvider
from app.security import decrypt_secret

Message = dict[str, str]


def _parse_sse(line: str) -> str | None:
    """Return the payload of an SSE ``data:`` line, ignoring everything else."""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    return payload or None


class LLMGateway:
    def __init__(self, config: LLMConfig, timeout: float = 120.0):
        self.config = config
        self.timeout = timeout

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if self.config.kind == "anthropic":
            return f"{base}/messages"
        return f"{base}/chat/completions"

    def _model(self, tier: str) -> str:
        model = self.config.tiers.get(tier) or self.config.tiers.get("standard")
        if not model:
            raise ValueError(
                f"provider {self.config.provider_id} has no model for tier {tier}"
            )
        return model

    def _headers(self) -> dict[str, str]:
        if self.config.kind == "anthropic":
            return {
                "x-api-key": self.config.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        messages: list[Message],
        tier: str,
        temperature: float,
        stream: bool,
    ) -> dict:
        model = self._model(tier)
        if self.config.kind == "anthropic":
            system = "\n".join(
                m["content"] for m in messages if m["role"] == "system"
            )
            rest = [m for m in messages if m["role"] != "system"]
            body = {
                "model": model,
                "max_tokens": 4096,
                "temperature": temperature,
                "messages": rest,
                "stream": stream,
            }
            if system:
                body["system"] = system
            return body
        return {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }

    async def complete(
        self,
        messages: list[Message],
        *,
        tier: str = "standard",
        temperature: float = 0.7,
    ) -> str:
        payload = self._payload(messages, tier, temperature, stream=False)
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.endpoint, headers=self._headers(), json=payload
            )
            response.raise_for_status()
            data = response.json()
        if self.config.kind == "anthropic":
            return "".join(
                block.get("text", "") for block in data.get("content", [])
            )
        return data["choices"][0]["message"].get("content") or ""

    async def stream(
        self,
        messages: list[Message],
        *,
        tier: str = "standard",
        temperature: float = 0.7,
    ) -> AsyncIterator[str]:
        payload = self._payload(messages, tier, temperature, stream=True)
        async with (
            httpx.AsyncClient(timeout=self.timeout) as client,
            client.stream(
                "POST", self.endpoint, headers=self._headers(), json=payload
            ) as response,
        ):
            response.raise_for_status()
            async for line in response.aiter_lines():
                raw = _parse_sse(line)
                if raw is None:
                    continue
                if raw == "[DONE]":
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                text = self._extract_delta(event)
                if text:
                    yield text

    def _extract_delta(self, event: dict) -> str | None:
        if self.config.kind == "anthropic":
            if event.get("type") != "content_block_delta":
                return None
            return (event.get("delta") or {}).get("text")
        choices = event.get("choices") or []
        if not choices:
            return None
        return (choices[0].get("delta") or {}).get("content")


async def load_gateway_default(session: AsyncSession, tenant_id: str) -> LLMGateway:
    """Load the tenant's default provider, or fail with a project envelope error."""
    provider = (
        await session.execute(
            select(LLMProvider)
            .where(
                LLMProvider.tenant_id == tenant_id,
                LLMProvider.is_default.is_(True),
            )
            .order_by(LLMProvider.created_at, LLMProvider.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if provider is None:
        raise AppError(
            "llm_not_configured",
            "尚未配置默认 LLM 提供商，请先在设置页添加",
            400,
        )
    config = LLMConfig(
        provider_id=str(provider.id),
        kind=provider.kind,
        base_url=provider.base_url.rstrip("/"),
        api_key=decrypt_secret(provider.api_key_enc),
        tiers=dict(provider.tiers or {}),
    )
    return LLMGateway(config)
