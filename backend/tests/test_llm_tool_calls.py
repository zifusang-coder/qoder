import json

import httpx
import respx

from app.llm.gateway import LLMGateway
from app.llm.tools import ToolCall, ToolTurn
from app.llm.types import LLMConfig

OPENAI_URL = "https://llm.test/v1/chat/completions"
ANTHROPIC_URL = "https://llm.test/messages"


def _config() -> LLMConfig:
    return LLMConfig(
        provider_id="p1",
        kind="openai_compatible",
        base_url="https://llm.test/v1",
        api_key="sk-test",
        tiers={"standard": "medium"},
    )


def _anthropic_config() -> LLMConfig:
    return LLMConfig(
        provider_id="p2",
        kind="anthropic",
        base_url="https://llm.test",
        api_key="sk-ant-test",
        tiers={"standard": "claude-medium"},
    )


TOOLS = [
    {
        "name": "profile.list",
        "description": "列出画像",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "additionalProperties": False,
        },
    }
]


@respx.mock
async def test_openai_complete_tool_calls_parses_tool_calls():
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "profile.list",
                                        "arguments": '{"limit": 3}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )
    )
    gateway = LLMGateway(_config())
    turn = await gateway.complete_tool_calls(
        [{"role": "user", "content": "hi"}], tools=TOOLS
    )
    assert turn == ToolTurn(
        content="",
        tool_calls=[ToolCall(id="call_1", name="profile.list", arguments='{"limit": 3}')],
        finish_reason="tool_calls",
        usage=turn.usage,
    )
    assert turn.usage.prompt_tokens == 11
    assert turn.usage.completion_tokens == 7
    body = json.loads(route.calls[0].request.content)
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "profile.list"
    assert body["tool_choice"] == "auto"


@respx.mock
async def test_anthropic_complete_tool_calls_parses_blocks_and_converts_history():
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "先看画像。"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "profile.list",
                        "input": {"limit": 2},
                    },
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 20, "output_tokens": 9},
            },
        )
    )
    gateway = LLMGateway(_anthropic_config())
    messages = [
        {"role": "system", "content": "系统提示"},
        {"role": "user", "content": "看看画像"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "tu_0",
                    "type": "function",
                    "function": {"name": "profile.list", "arguments": '{"limit": 1}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tu_0", "content": "old observation"},
    ]
    turn = await gateway.complete_tool_calls(messages, tools=TOOLS)
    assert turn.content == "先看画像。"
    assert turn.tool_calls == [
        ToolCall(id="tu_1", name="profile.list", arguments='{"limit": 2}')
    ]
    assert turn.finish_reason == "tool_calls"
    assert turn.usage.prompt_tokens == 20
    body = json.loads(route.calls[0].request.content)
    assert body["system"] == "系统提示"
    assert all(message["role"] != "system" for message in body["messages"])
    assert body["tools"][0]["input_schema"]["additionalProperties"] is False
    assert body["messages"][1]["content"][0] == {
        "type": "tool_use",
        "id": "tu_0",
        "name": "profile.list",
        "input": {"limit": 1},
    }
    assert body["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "tu_0",
        "content": "old observation",
    }


@respx.mock
async def test_openai_invalid_arguments_are_passed_through():
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c",
                                    "type": "function",
                                    "function": {"name": "x", "arguments": "not-json"},
                                }
                            ],
                        }
                    }
                ]
            },
        )
    )
    turn = await LLMGateway(_config()).complete_tool_calls(
        [{"role": "user", "content": "hi"}], tools=TOOLS
    )
    assert turn.tool_calls[0].arguments == "not-json"


@respx.mock
async def test_openai_shape_error_raises_invalid_response():
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={"choices": []}))
    try:
        await LLMGateway(_config()).complete_tool_calls(
            [{"role": "user", "content": "hi"}], tools=TOOLS
        )
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "code", "") == "llm_invalid_response"
    else:  # pragma: no cover
        raise AssertionError("expected llm_invalid_response")
