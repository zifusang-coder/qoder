import json

from app.agent.sanitize import (
    MAX_DATA_BYTES,
    MAX_MESSAGES_BYTES,
    compact_messages,
    fit_bytes,
    scrub,
    wrap_observation,
)


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


def test_compact_messages_enforces_size_limit():
    items = [{"role": "system", "content": "sys"}]
    for index in range(40):
        items.append({"role": "user", "content": f"u{index}-" + "z" * 8000})
    packed = compact_messages(items)
    size = len(json.dumps(packed, ensure_ascii=False).encode())
    assert size <= MAX_MESSAGES_BYTES
    assert packed["truncated"] is True
