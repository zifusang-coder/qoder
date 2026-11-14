import json

from app.agent.sanitize import (
    COMPRESSED_PREFIX,
    MAX_DATA_BYTES,
    MAX_MESSAGES_BYTES,
    OBSERVATION_PREFIX,
    REDACTED,
    compact_messages,
    fit_bytes,
    redact_secrets,
    scrub,
    wrap_observation,
)


def _tool_call(call_id: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "x", "arguments": "{}"},
    }


def _orphan_tool_replies(items: list[dict]) -> list[dict]:
    live_ids = {
        call.get("id")
        for item in items
        if item.get("role") == "assistant"
        for call in item.get("tool_calls") or []
        if isinstance(call, dict)
    }
    return [
        item
        for item in items
        if item.get("role") == "tool" and item.get("tool_call_id") not in live_ids
    ]


def test_scrub_drops_sensitive_keys_recursively():
    data = {
        "title": "标题",
        "app_secret": "s3cr3t",
        "nested": {"access_token": "t", "count": 2},
        "items": [{"password": "p", "name": "n"}],
    }
    cleaned = scrub(data)
    assert cleaned == {
        "title": "标题",
        "nested": {"count": 2},
        "items": [{"name": "n"}],
    }


def test_scrub_caps_long_strings():
    cleaned = scrub({"body": "x" * 5000})
    assert len(cleaned["body"]) == 2000


def test_redact_secrets_scrubs_api_key_inside_prose():
    text = redact_secrets("Incorrect API key provided: sk-abc123")
    assert "sk-abc123" not in text
    assert REDACTED in text
    assert text.startswith("Incorrect API key provided:")


def test_redact_secrets_scrubs_bearer_token():
    text = redact_secrets("Authorization: Bearer abc")
    assert "abc" not in text
    assert "Bearer" in text
    assert REDACTED in text


def test_redact_secrets_keeps_normal_chinese_prose():
    text = "运行发生内部错误，请稍后重试"
    assert redact_secrets(text) == text


def test_scrub_redacts_secret_values_too():
    cleaned = scrub({"error": "Incorrect API key provided: sk-abc123"})
    assert "sk-abc123" not in cleaned["error"]
    assert REDACTED in cleaned["error"]


def test_fit_bytes_truncates_list_and_reports_flag():
    data = [{"text": "x" * 1000} for _ in range(100)]
    trimmed, truncated = fit_bytes(data)
    assert truncated is True
    assert len(json.dumps(trimmed, ensure_ascii=False).encode()) <= MAX_DATA_BYTES
    assert len(trimmed) < len(data)


def test_fit_bytes_keeps_small_payload():
    data = {"a": 1}
    trimmed, truncated = fit_bytes(data)
    assert trimmed == data
    assert truncated is False


def test_wrap_observation_contains_prefix_and_tool_name():
    text = wrap_observation("profile.list", "找到 1 个画像", {"items": []})
    assert text.startswith("以下为系统数据")
    assert "profile.list" in text
    assert "找到 1 个画像" in text


def test_compact_messages_keeps_recent_observations_full():
    items = [{"role": "system", "content": "sys"}]
    for index in range(12):
        items.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            }
        )
        items.append(
            {"role": "tool", "tool_call_id": f"c{index}", "content": f"obs-{index}-" + "y" * 300}
        )
    packed = compact_messages(items)
    assert packed["truncated"] is True
    stored = packed["items"]
    tool_items = [item for item in stored if item["role"] == "tool"]
    assert len(tool_items) == 12
    assert tool_items[-1]["content"].startswith("obs-11-")
    assert tool_items[0]["content"].startswith("[已压缩]")
    assert len(tool_items[0]["content"]) <= 220
    assert stored[0] == {"role": "system", "content": "sys"}


def test_compact_messages_is_idempotent_across_repeated_persists():
    items = [{"role": "system", "content": "sys"}]
    for index in range(12):
        items.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{index}",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            }
        )
        items.append(
            {"role": "tool", "tool_call_id": f"c{index}", "content": f"obs-{index}-" + "y" * 300}
        )

    once = compact_messages(items)["items"]
    for _ in range(4):
        again = compact_messages(once)["items"]
        assert again == once

    contents = [
        item["content"] for item in once if item.get("role") == "tool"
    ]
    compressed = [content for content in contents if content.startswith(COMPRESSED_PREFIX)]
    assert compressed
    assert all(COMPRESSED_PREFIX * 2 not in content for content in contents)
    assert all(len(content) <= len(COMPRESSED_PREFIX) + 200 for content in compressed)


def test_compact_messages_enforces_size_limit():
    items = [{"role": "system", "content": "sys"}]
    for index in range(40):
        items.append({"role": "user", "content": f"u{index}-" + "z" * 8000})
    packed = compact_messages(items)
    size = len(json.dumps(packed, ensure_ascii=False).encode())
    assert size <= MAX_MESSAGES_BYTES
    assert packed["truncated"] is True


def test_compact_messages_drops_tool_replies_orphaned_by_dropped_assistant():
    items = [
        {
            "role": "assistant",
            "content": "a" * 120_000,
            "tool_calls": [_tool_call("c0"), _tool_call("c1")],
        },
        {"role": "tool", "tool_call_id": "c0", "content": "b" * 45_000},
        {"role": "system", "content": "sys"},
        {"role": "tool", "tool_call_id": "c1", "content": "d" * 45_000},
    ]
    assert len(json.dumps(items, ensure_ascii=False).encode()) > MAX_MESSAGES_BYTES

    packed = compact_messages(items)

    stored = packed["items"]
    assert packed["truncated"] is True
    assert stored, "compaction must never return an empty message list"
    assert _orphan_tool_replies(stored) == []
    assert len(json.dumps(stored, ensure_ascii=False).encode()) <= MAX_MESSAGES_BYTES


def test_compact_messages_truncates_single_oversized_message():
    items = [{"role": "user", "content": "z" * (MAX_MESSAGES_BYTES + 10_000)}]
    packed = compact_messages(items)
    stored = packed["items"]
    assert len(stored) == 1
    assert stored[0]["role"] == "user"
    assert len(json.dumps(stored, ensure_ascii=False).encode()) <= MAX_MESSAGES_BYTES
    assert packed["truncated"] is True


def test_fit_bytes_truncates_lone_oversized_dict_value():
    trimmed, truncated = fit_bytes({"only": "x" * 30_000})
    assert isinstance(trimmed, dict)
    assert trimmed, "a non-empty dict must never be trimmed to {}"
    assert list(trimmed) == ["only"]
    assert isinstance(trimmed["only"], str)
    assert len(json.dumps(trimmed, ensure_ascii=False).encode()) <= MAX_DATA_BYTES
    assert truncated is True


def test_wrap_observation_shrinks_summary_into_valid_json():
    text = wrap_observation("profile.list", "s" * 5000, {"items": []})
    assert text.startswith(OBSERVATION_PREFIX)
    payload = json.loads(text[len(OBSERVATION_PREFIX) :])
    assert payload["truncated"] is True
    assert payload["tool"] == "profile.list"
    assert len(text) <= MAX_DATA_BYTES
    assert len(text.encode()) <= MAX_DATA_BYTES


def test_compact_messages_respects_max_bytes_argument():
    items = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c0",
                    "type": "function",
                    "function": {"name": "x", "arguments": json.dumps({"blob": "q" * 3000})},
                }
            ],
        }
    ]
    assert len(json.dumps(items, ensure_ascii=False).encode()) > 2000

    packed = compact_messages(items, max_bytes=2000)

    stored = packed["items"]
    assert stored, "compaction must never return an empty message list"
    assert packed["truncated"] is True
    assert len(json.dumps(stored, ensure_ascii=False).encode()) <= 2000


def test_compact_messages_caps_assistant_with_large_tool_calls():
    calls = [
        {
            "id": f"c{index}",
            "type": "function",
            "function": {"name": "x", "arguments": json.dumps({"blob": "q" * 40_000})},
        }
        for index in range(8)
    ]
    items = [
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "c0", "content": "r" * 40_000},
    ]
    assert len(json.dumps(items, ensure_ascii=False).encode()) > MAX_MESSAGES_BYTES

    packed = compact_messages(items)

    stored = packed["items"]
    assert stored, "compaction must never return an empty message list"
    assert all(isinstance(item, dict) and "role" in item for item in stored)
    assert packed["truncated"] is True
    assert len(json.dumps(stored, ensure_ascii=False).encode()) <= MAX_MESSAGES_BYTES


def test_compact_messages_stubs_history_but_keeps_final_tool_turn():
    calls = [
        {
            "id": f"c{index:05d}" + "i" * 40,
            "type": "function",
            "function": {"name": "x", "arguments": "{}"},
        }
        for index in range(4000)
    ]
    items = [{"role": "assistant", "content": "", "tool_calls": calls}]
    assert len(json.dumps(items, ensure_ascii=False).encode()) > MAX_MESSAGES_BYTES

    packed = compact_messages(items)

    stored = packed["items"]
    assert packed["truncated"] is True
    assert stored[0] == {"role": "user", "content": "[对话历史过长，已截断]"}
    assistant = stored[1]
    assert assistant["role"] == "assistant"
    assert [call["id"] for call in assistant["tool_calls"]] == [
        call["id"] for call in calls[:4]
    ]
    assert len(json.dumps(stored, ensure_ascii=False).encode()) <= MAX_MESSAGES_BYTES


def test_compact_messages_stub_keeps_pending_tool_call_and_reply():
    calls = [
        {
            "id": f"c{index:06d}" + "i" * 280,
            "type": "function",
            "function": {"name": "x", "arguments": "{}"},
        }
        for index in range(600)
    ]
    items = [
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": "", "tool_calls": calls},
        {"role": "tool", "tool_call_id": calls[0]["id"], "content": "ok"},
    ]
    assert len(json.dumps(items, ensure_ascii=False).encode()) > MAX_MESSAGES_BYTES

    packed = compact_messages(items)

    stored = packed["items"]
    assert packed["truncated"] is True
    assert stored, "compaction must never return an empty message list"
    assert _orphan_tool_replies(stored) == []
    assistants = [item for item in stored if item["role"] == "assistant"]
    assert len(assistants) == 1
    kept_ids = {call["id"] for call in assistants[0]["tool_calls"]}
    assert calls[0]["id"] in kept_ids
    replies = [item for item in stored if item["role"] == "tool"]
    assert replies and all(item["tool_call_id"] in kept_ids for item in replies)
    assert len(json.dumps(stored, ensure_ascii=False).encode()) <= MAX_MESSAGES_BYTES


def test_compact_messages_keeps_json_for_non_string_content():
    items = [{"role": "user", "content": ["a" * 3000, "b" * 3000]}]
    packed = compact_messages(items, max_bytes=2000)
    stored = packed["items"]
    assert len(stored) == 1
    assert packed["truncated"] is True
    content = stored[0]["content"]
    assert isinstance(content, str)
    assert content.startswith('["'), "list content must be JSON text, not a repr"
    assert "'" not in content
    assert len(json.dumps(stored, ensure_ascii=False).encode()) <= 2000
