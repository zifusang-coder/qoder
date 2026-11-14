import json
import logging
from collections.abc import AsyncIterator

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.llm.tools import ToolCall, ToolTurn
from app.llm.types import LLMConfig, Usage
from app.models.settings import LLMProvider
from app.security import decrypt_secret

Message = dict[str, object]

logger = logging.getLogger(__name__)


def _parse_sse(line: str) -> str | None:
    """Return the payload of an SSE ``data:`` line, ignoring everything else."""
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    return payload or None


def _token_count(value: object) -> int:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(count, 0)


class LLMGateway:
    def __init__(self, config: LLMConfig, timeout: float = 120.0):
        self.config = config
        self.timeout = timeout
        self.last_usage = Usage()

    @property
    def endpoint(self) -> str:
        base = self.config.base_url.rstrip("/")
        if self.config.kind == "anthropic":
            return f"{base}/messages"
        return f"{base}/chat/completions"

    def _model(self, tier: str) -> str:
        model = self.config.tiers.get(tier) or self.config.tiers.get("standard")
        if not model:
            raise AppError(
                "llm_provider_invalid",
                "LLM 提供商缺少可用模型，请检查设置",
                400,
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

    def _sanitize(self, text: str) -> str:
        collapsed = " ".join(str(text).split())
        if self.config.api_key:
            collapsed = collapsed.replace(self.config.api_key, "***")
        return collapsed[:300]

    def _raise_for_upstream_error(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        upstream = self._sanitize(response.text)
        detail = {"upstream": upstream} if upstream else None
        if response.status_code in (401, 403):
            raise AppError("llm_auth_failed", "LLM 提供商鉴权失败", 502, detail=detail)
        if response.status_code == 429:
            raise AppError(
                "llm_rate_limited",
                "LLM 提供商限流，请稍后重试",
                502,
                detail=detail,
            )
        raise AppError(
            "llm_request_failed",
            f"LLM 提供商返回错误（{response.status_code}）",
            502,
            detail=detail,
        )

    def _payload(
        self,
        messages: list[Message],
        tier: str,
        temperature: float,
        stream: bool,
        max_tokens: int,
    ) -> dict:
        model = self._model(tier)
        if self.config.kind == "anthropic":
            system = "\n".join(
                m["content"] for m in messages if m["role"] == "system"
            )
            rest = [m for m in messages if m["role"] != "system"]
            if not rest:
                raise AppError(
                    "llm_invalid_messages",
                    "至少需要一条非 system 消息",
                    400,
                )
            body = {
                "model": model,
                "max_tokens": max_tokens,
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
            "max_tokens": max_tokens,
            "stream": stream,
        }

    def _openai_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool["parameters"],
                },
            }
            for tool in tools
        ]

    def _anthropic_payload(
        self, messages: list[dict]
    ) -> tuple[str, list[dict]]:
        system = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system"
        )
        converted: list[dict] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                continue
            if role == "assistant":
                blocks: list[dict] = []
                text = message.get("content")
                if text:
                    blocks.append({"type": "text", "text": str(text)})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    raw = function.get("arguments") or "{}"
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = {"raw_arguments": raw}
                    if not isinstance(parsed, dict):
                        parsed = {"value": parsed}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": str(call.get("id") or ""),
                            "name": str(function.get("name") or ""),
                            "input": parsed,
                        }
                    )
                converted.append(
                    {"role": "assistant", "content": blocks or [{"type": "text", "text": " "}]}
                )
            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": str(message.get("tool_call_id") or ""),
                    "content": str(message.get("content") or ""),
                }
                previous = converted[-1] if converted else None
                if (
                    previous is not None
                    and previous.get("role") == "user"
                    and previous.get("_tool_results")
                ):
                    previous["content"].append(block)
                else:
                    converted.append(
                        {"role": "user", "content": [block], "_tool_results": True}
                    )
            else:
                converted.append(
                    {"role": "user", "content": str(message.get("content") or "")}
                )
        for message in converted:
            message.pop("_tool_results", None)
        return system, converted

    async def complete_tool_calls(
        self,
        messages: list[Message],
        *,
        tools: list[dict],
        tool_choice: str = "auto",
        tier: str = "standard",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> ToolTurn:
        self.last_usage = Usage()
        model = self._model(tier)
        if self.config.kind == "anthropic":
            system, converted = self._anthropic_payload(messages)
            payload: dict = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": converted,
                "tools": [
                    {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "input_schema": tool["parameters"],
                    }
                    for tool in tools
                ],
                "tool_choice": {"type": "auto"},
            }
            if system:
                payload["system"] = system
        else:
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": self._openai_tools(tools),
                "tool_choice": tool_choice if tool_choice in {"auto", "none", "required"} else "auto",
            }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint, headers=self._headers(), json=payload
                )
        except httpx.TimeoutException as exc:
            raise AppError("llm_timeout", "LLM 请求超时", 504) from exc
        except httpx.RequestError as exc:
            raise AppError("llm_unavailable", "LLM 服务不可用", 502) from exc
        self._raise_for_upstream_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502) from exc
        if not isinstance(data, dict):
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)

        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        if self.config.kind == "anthropic":
            self.last_usage = Usage(
                _token_count(usage.get("input_tokens")),
                _token_count(usage.get("output_tokens")),
            )
            content = data.get("content")
            if not isinstance(content, list):
                raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
            text = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            calls = [
                ToolCall(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    arguments=json.dumps(block.get("input") or {}, ensure_ascii=False),
                )
                for block in content
                if isinstance(block, dict) and block.get("type") == "tool_use"
            ]
            stop = data.get("stop_reason")
            finish_reason = {
                "tool_use": "tool_calls",
                "max_tokens": "length",
                "end_turn": "stop",
            }.get(str(stop), str(stop or "stop"))
            return ToolTurn(text, calls, finish_reason, self.last_usage)

        self.last_usage = Usage(
            _token_count(usage.get("prompt_tokens")),
            _token_count(usage.get("completion_tokens")),
        )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") if isinstance(first.get("message"), dict) else {}
        text = message.get("content")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        calls = []
        raw_calls = message.get("tool_calls")
        if raw_calls is not None and not isinstance(raw_calls, list):
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        for index, item in enumerate(raw_calls or []):
            if not isinstance(item, dict):
                continue
            function = item.get("function") if isinstance(item.get("function"), dict) else {}
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=str(function.get("arguments") or "{}"),
                )
            )
        finish_reason = str(
            first.get("finish_reason") or ("tool_calls" if calls else "stop")
        )
        return ToolTurn(text, calls, finish_reason, self.last_usage)

    async def complete(
        self,
        messages: list[Message],
        *,
        tier: str = "standard",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> str:
        self.last_usage = Usage()
        payload = self._payload(
            messages, tier, temperature, stream=False, max_tokens=max_tokens
        )
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint, headers=self._headers(), json=payload
                )
        except httpx.TimeoutException as exc:
            raise AppError("llm_timeout", "LLM 请求超时", 504) from exc
        except httpx.RequestError as exc:
            raise AppError("llm_unavailable", "LLM 服务不可用", 502) from exc
        self._raise_for_upstream_error(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise AppError(
                "llm_invalid_response", "LLM 返回格式异常", 502
            ) from exc

        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            usage = {}
        if self.config.kind == "anthropic":
            self.last_usage = Usage(
                _token_count(usage.get("input_tokens")),
                _token_count(usage.get("output_tokens")),
            )
        else:
            self.last_usage = Usage(
                _token_count(usage.get("prompt_tokens")),
                _token_count(usage.get("completion_tokens")),
            )

        if self.config.kind == "anthropic":
            content = data.get("content") if isinstance(data, dict) else None
            if not isinstance(content, list):
                raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
            if data.get("stop_reason") == "max_tokens":
                logger.warning(
                    "LLM response truncated provider_id=%s tier=%s",
                    self.config.provider_id,
                    tier,
                )
            return "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
            )

        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        if first.get("finish_reason") == "length":
            logger.warning(
                "LLM response truncated provider_id=%s tier=%s",
                self.config.provider_id,
                tier,
            )
        return content

    async def stream(
        self,
        messages: list[Message],
        *,
        tier: str = "standard",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[str]:
        self.last_usage = Usage()
        payload = self._payload(
            messages, tier, temperature, stream=True, max_tokens=max_tokens
        )
        try:
            async with (
                httpx.AsyncClient(timeout=self.timeout) as client,
                client.stream(
                    "POST", self.endpoint, headers=self._headers(), json=payload
                ) as response,
            ):
                if not response.is_success:
                    await response.aread()
                    self._raise_for_upstream_error(response)
                yielded = False
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
                    if isinstance(event, dict) and "error" in event:
                        raise AppError(
                            "llm_request_failed",
                            "LLM 提供商返回错误",
                            502,
                            detail={
                                "upstream": self._sanitize(
                                    json.dumps(event, ensure_ascii=False)
                                )
                            },
                        )
                    text = self._extract_delta(event)
                    self._maybe_warn_truncation(event, tier)
                    if text:
                        yielded = True
                        yield text
                if not yielded:
                    raise AppError("llm_empty_stream", "LLM 返回了空响应", 502)
        except httpx.TimeoutException as exc:
            raise AppError("llm_timeout", "LLM 请求超时", 504) from exc
        except httpx.RequestError as exc:
            raise AppError("llm_unavailable", "LLM 服务不可用", 502) from exc

    def _maybe_warn_truncation(self, event: object, tier: str) -> None:
        """Log when the provider stopped because the token budget ran out."""
        if not isinstance(event, dict):
            return
        if self.config.kind == "anthropic":
            if event.get("type") != "message_delta":
                return
            delta = event.get("delta")
            if not isinstance(delta, dict):
                return
            truncated = delta.get("stop_reason") == "max_tokens"
        else:
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                return
            first = choices[0]
            if not isinstance(first, dict):
                return
            truncated = first.get("finish_reason") == "length"
        if truncated:
            logger.warning(
                "LLM response truncated provider_id=%s tier=%s",
                self.config.provider_id,
                tier,
            )

    def _extract_delta(self, event: dict) -> str | None:
        if not isinstance(event, dict):
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        if self.config.kind == "anthropic":
            if event.get("type") != "content_block_delta":
                return None
            delta = event.get("delta")
            if not isinstance(delta, dict):
                raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
            text = delta.get("text")
            if text is not None and not isinstance(text, str):
                raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
            return text
        choices = event.get("choices")
        if not isinstance(choices, list):
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        if not choices:
            return None
        first = choices[0]
        if not isinstance(first, dict):
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        delta = first.get("delta")
        if delta is None:
            return None
        if not isinstance(delta, dict):
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        content = delta.get("content")
        if content is not None and not isinstance(content, str):
            raise AppError("llm_invalid_response", "LLM 返回格式异常", 502)
        return content


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
    try:
        api_key = decrypt_secret(provider.api_key_enc)
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise AppError(
            "llm_provider_unreadable",
            "LLM 提供商密钥无法解密，请重新填写",
            400,
        ) from exc
    try:
        config = LLMConfig(
            provider_id=str(provider.id),
            kind=provider.kind,
            base_url=provider.base_url.rstrip("/"),
            api_key=api_key,
            tiers=dict(provider.tiers or {}),
        )
    except ValueError as exc:
        raise AppError(
            "llm_provider_invalid",
            "LLM 提供商配置无效，请检查设置",
            400,
        ) from exc
    return LLMGateway(config)
