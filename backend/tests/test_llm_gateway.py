import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.errors import AppError
from app.llm.gateway import LLMGateway, load_gateway_default
from app.llm.types import LLMConfig
from app.models.settings import LLMProvider
from app.security import encrypt_secret

OPENAI_URL = "https://llm.test/v1/chat/completions"
ANTHROPIC_URL = "https://llm.test/messages"

TENANT_A = "aaaaaaaa-1111-1111-1111-111111111111"
TENANT_B = "bbbbbbbb-2222-2222-2222-222222222222"


def _config(**overrides) -> LLMConfig:
    values = {
        "provider_id": "p1",
        "kind": "openai_compatible",
        "base_url": "https://llm.test/v1",
        "api_key": "sk-test",
        "tiers": {"fast": "small", "standard": "medium", "premium": "large"},
    }
    values.update(overrides)
    return LLMConfig(**values)


def _anthropic_config(**overrides) -> LLMConfig:
    values = {
        "provider_id": "p2",
        "kind": "anthropic",
        "base_url": "https://llm.test",
        "api_key": "sk-ant-test",
        "tiers": {"fast": "claude-small", "standard": "claude-medium"},
    }
    values.update(overrides)
    return LLMConfig(**values)


def _sse(*chunks: str) -> bytes:
    return "".join(f"{chunk}\n\n" for chunk in chunks).encode()


def _provider(**overrides) -> LLMProvider:
    values = {
        "tenant_id": TENANT_A,
        "name": "提供商",
        "kind": "openai_compatible",
        "base_url": "https://llm.test/v1",
        "api_key_enc": encrypt_secret("sk-tenant-a"),
        "tiers": {"fast": "small", "standard": "medium"},
        "is_default": True,
    }
    values.update(overrides)
    return LLMProvider(**values)


# --- non-streaming completion -------------------------------------------------


@respx.mock
async def test_complete_uses_tier_model():
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "你好"}}]})
    )
    gateway = LLMGateway(_config())
    text = await gateway.complete([{"role": "user", "content": "hi"}], tier="fast")
    assert text == "你好"
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "small"
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"


@respx.mock
async def test_complete_falls_back_to_standard_tier():
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    gateway = LLMGateway(_config())
    assert await gateway.complete([{"role": "user", "content": "hi"}], tier="unknown") == "ok"
    body = route.calls[0].request.content.decode()
    assert '"model": "medium"' in body or '"model":"medium"' in body


@respx.mock
async def test_empty_tiers_raises_provider_invalid_before_http_call():
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
    )
    gateway = LLMGateway(_config(tiers={}))
    with pytest.raises(AppError) as excinfo:
        await gateway.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "llm_provider_invalid"
    assert excinfo.value.status == 400
    assert route.called is False


@respx.mock
async def test_http_error_raises_app_error_with_upstream_detail():
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        await gateway.complete([{"role": "user", "content": "hi"}])
    err = excinfo.value
    assert err.code == "llm_request_failed"
    assert err.status == 502
    assert "boom" in err.detail["upstream"]


@respx.mock
async def test_http_401_maps_to_auth_failed():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"})
    )
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        await gateway.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "llm_auth_failed"
    assert excinfo.value.status == 502


@respx.mock
async def test_http_429_maps_to_rate_limited():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(429, json={"error": "slow down"})
    )
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        await gateway.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "llm_rate_limited"
    assert excinfo.value.status == 502


@respx.mock
async def test_upstream_error_redacts_api_key():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            500, json={"error": "invalid key sk-test"}
        )
    )
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        await gateway.complete([{"role": "user", "content": "hi"}])
    upstream = excinfo.value.detail["upstream"]
    assert "sk-test" not in upstream
    assert "***" in upstream


@respx.mock
async def test_read_timeout_maps_to_llm_timeout():
    respx.post(OPENAI_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        await gateway.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "llm_timeout"
    assert excinfo.value.status == 504


@respx.mock
async def test_connect_error_maps_to_llm_unavailable():
    respx.post(OPENAI_URL).mock(side_effect=httpx.ConnectError("refused"))
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        await gateway.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "llm_unavailable"
    assert excinfo.value.status == 502


@respx.mock
async def test_invalid_completion_shape_raises_invalid_response():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, json={"choices": []})
    )
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        await gateway.complete([{"role": "user", "content": "hi"}])
    assert excinfo.value.code == "llm_invalid_response"
    assert excinfo.value.status == 502


@respx.mock
async def test_max_tokens_default_and_override():
    openai_route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    await LLMGateway(_config()).complete([{"role": "user", "content": "hi"}])
    assert json.loads(openai_route.calls[0].request.content)["max_tokens"] == 4096

    anthropic_route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(200, json={"content": [{"text": "ok"}]})
    )
    await LLMGateway(_anthropic_config()).complete(
        [{"role": "user", "content": "hi"}], max_tokens=256
    )
    assert json.loads(anthropic_route.calls[0].request.content)["max_tokens"] == 256


@respx.mock
async def test_anthropic_system_only_rejected_without_http_call():
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(200, json={"content": [{"text": "ok"}]})
    )
    gateway = LLMGateway(_anthropic_config())
    with pytest.raises(AppError) as excinfo:
        await gateway.complete([{"role": "system", "content": "只允许 system"}])
    assert excinfo.value.code == "llm_invalid_messages"
    assert excinfo.value.status == 400
    assert route.call_count == 0


# --- anthropic completion -----------------------------------------------------


@respx.mock
async def test_anthropic_complete_hoists_system_and_joins_blocks():
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "你好，"},
                    {"type": "text", "text": "世界"},
                ]
            },
        )
    )
    gateway = LLMGateway(_anthropic_config())
    messages = [
        {"role": "system", "content": "你是助手"},
        {"role": "system", "content": "保持简洁"},
        {"role": "user", "content": "hi"},
    ]
    text = await gateway.complete(messages, tier="fast")

    assert text == "你好，世界"
    request = route.calls[0].request
    assert request.headers["x-api-key"] == "sk-ant-test"
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["system"] == "你是助手\n保持简洁"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["model"] == "claude-small"
    assert body["stream"] is False


# --- streaming ----------------------------------------------------------------


@respx.mock
async def test_openai_stream_yields_deltas_and_skips_non_data_lines():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                'data: {"choices": [{"delta": {"content": "你"}}]}',
                "",
                ": keep-alive comment",
                "event: message",
                'data: {"choices": [{"delta": {"content": "好"}}]}',
                'data: {"choices": [{"delta": {}}]}',
                "data: [DONE]",
            ),
        )
    )
    gateway = LLMGateway(_config())
    chunks = [
        chunk
        async for chunk in gateway.stream([{"role": "user", "content": "hi"}], tier="fast")
    ]
    assert chunks == ["你", "好"]


@respx.mock
async def test_anthropic_stream_yields_text_deltas_only():
    respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                "event: message_start",
                'data: {"type": "message_start", "message": {}}',
                "event: content_block_delta",
                'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你"}}',
                "event: content_block_delta",
                'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "好"}}',
                "event: message_delta",
                'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}',
                "event: message_stop",
                'data: {"type": "message_stop"}',
            ),
        )
    )
    gateway = LLMGateway(_anthropic_config())
    chunks = [
        chunk
        async for chunk in gateway.stream([{"role": "user", "content": "hi"}], tier="fast")
    ]
    assert chunks == ["你", "好"]


@respx.mock
async def test_stream_http_error_raises_request_failed():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        async for _chunk in gateway.stream([{"role": "user", "content": "hi"}]):
            pass
    assert excinfo.value.code == "llm_request_failed"
    assert excinfo.value.status == 502
    assert "boom" in excinfo.value.detail["upstream"]


@respx.mock
async def test_stream_done_only_raises_empty_stream():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse("data: [DONE]"),
        )
    )
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        async for _chunk in gateway.stream([{"role": "user", "content": "hi"}]):
            pass
    assert excinfo.value.code == "llm_empty_stream"
    assert excinfo.value.status == 502


@respx.mock
async def test_stream_accepts_compact_data_prefix_and_crlf():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                'data:{"choices":[{"delta":{"content":"你"}}]}\r\n\r\n'
                "data: [DONE]\r\n\r\n"
            ).encode(),
        )
    )
    gateway = LLMGateway(_config())
    chunks = [
        chunk
        async for chunk in gateway.stream([{"role": "user", "content": "hi"}])
    ]
    assert chunks == ["你"]


@respx.mock
async def test_stream_error_event_raises_request_failed():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                'data: {"error": {"message": "bad key sk-test"}}',
            ),
        )
    )
    gateway = LLMGateway(_config())
    with pytest.raises(AppError) as excinfo:
        async for _chunk in gateway.stream([{"role": "user", "content": "hi"}]):
            pass
    assert excinfo.value.code == "llm_request_failed"
    upstream = excinfo.value.detail["upstream"]
    assert "sk-test" not in upstream
    assert "***" in upstream


# --- config validation --------------------------------------------------------


def test_unknown_kind_rejected():
    with pytest.raises(ValueError):
        _config(kind="azure")


def test_repr_redacts_api_key():
    config = _config()
    gateway = LLMGateway(config)
    assert "sk-test" not in repr(config)
    assert "sk-test" not in repr(gateway)


# --- load_gateway_default -----------------------------------------------------


async def test_load_gateway_default_scopes_to_tenant_and_decrypts(db_session):
    own = _provider(
        tenant_id=TENANT_A,
        name="租户 A 默认",
        base_url="https://a.example.com/v1/",
        api_key_enc=encrypt_secret("sk-secret-a"),
        tiers={"fast": "a-small", "standard": "a-medium"},
    )
    foreign = _provider(
        tenant_id=TENANT_B,
        name="租户 B 默认",
        base_url="https://b.example.com/v1",
        api_key_enc=encrypt_secret("sk-secret-b"),
        tiers={"fast": "b-small"},
    )
    non_default = _provider(
        tenant_id=TENANT_A,
        name="租户 A 非默认",
        base_url="https://legacy.example.com/v1",
        api_key_enc=encrypt_secret("sk-legacy-a"),
        is_default=False,
    )
    non_default.created_at = datetime(2000, 1, 1, tzinfo=UTC)
    db_session.add_all([own, foreign, non_default])
    await db_session.commit()

    gateway = await load_gateway_default(db_session, TENANT_A)

    assert gateway.config.provider_id == str(own.id)
    assert gateway.config.base_url == "https://a.example.com/v1"
    assert gateway.config.api_key == "sk-secret-a"
    assert gateway.config.tiers == {"fast": "a-small", "standard": "a-medium"}
    assert gateway.config.kind == "openai_compatible"


async def test_load_gateway_default_other_tenant_gets_own_provider(db_session):
    provider_a = _provider(tenant_id=TENANT_A, api_key_enc=encrypt_secret("sk-secret-a"))
    provider_b = _provider(
        tenant_id=TENANT_B, api_key_enc=encrypt_secret("sk-secret-b"), base_url="https://b.test/v1"
    )
    db_session.add_all([provider_a, provider_b])
    await db_session.commit()

    gateway = await load_gateway_default(db_session, TENANT_B)

    assert gateway.config.provider_id == str(provider_b.id)
    assert gateway.config.api_key == "sk-secret-b"


async def test_load_gateway_default_missing_raises_app_error(db_session):
    # 只有非默认提供商也必须报 llm_not_configured，不能回退到任意一行
    db_session.add(
        _provider(
            tenant_id=TENANT_A,
            name="非默认",
            api_key_enc=encrypt_secret("sk-must-not-leak"),
            is_default=False,
        )
    )
    await db_session.commit()

    with pytest.raises(AppError) as excinfo:
        await load_gateway_default(db_session, TENANT_A)

    err = excinfo.value
    assert err.code == "llm_not_configured"
    assert err.status == 400
    assert "默认 LLM 提供商" in err.message
    assert "sk-must-not-leak" not in str(err)
    assert "sk-must-not-leak" not in err.message


async def test_load_gateway_default_ignores_other_tenant_default(db_session):
    db_session.add(
        _provider(
            tenant_id=TENANT_B,
            name="别家默认",
            api_key_enc=encrypt_secret("sk-other-tenant"),
            is_default=True,
        )
    )
    await db_session.commit()

    with pytest.raises(AppError) as excinfo:
        await load_gateway_default(db_session, TENANT_A)

    assert excinfo.value.code == "llm_not_configured"
    assert "sk-other-tenant" not in str(excinfo.value)


async def test_load_gateway_default_provider_id_is_uuid_string(db_session):
    provider = _provider(tenant_id=TENANT_A)
    db_session.add(provider)
    await db_session.commit()

    gateway = await load_gateway_default(db_session, TENANT_A)

    assert gateway.config.provider_id == str(provider.id)
    assert uuid.UUID(gateway.config.provider_id) == provider.id


async def test_load_gateway_default_invalid_stored_kind_raises_app_error(db_session):
    db_session.add(_provider(tenant_id=TENANT_A, kind="azure"))
    await db_session.commit()

    with pytest.raises(AppError) as excinfo:
        await load_gateway_default(db_session, TENANT_A)

    assert excinfo.value.code == "llm_provider_invalid"
    assert excinfo.value.status == 400
    assert "sk-tenant-a" not in str(excinfo.value)


async def test_load_gateway_default_corrupt_ciphertext_raises_app_error(db_session):
    db_session.add(_provider(tenant_id=TENANT_A, api_key_enc="not-a-fernet-token"))
    await db_session.commit()

    with pytest.raises(AppError) as excinfo:
        await load_gateway_default(db_session, TENANT_A)

    assert excinfo.value.code == "llm_provider_unreadable"
    assert excinfo.value.status == 400
    assert "not-a-fernet-token" not in str(excinfo.value)
