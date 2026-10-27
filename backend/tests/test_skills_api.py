from pathlib import Path

import pytest

from app.api.skills import get_llm_gateway
from app.llm.types import Usage
from app.main import app
from app.skills.registry import SkillRegistry, get_registry

FIXTURE = Path(__file__).parent / "fixtures" / "skills"


class FakeGateway:
    def __init__(self):
        self.last_usage = Usage(2, 3)
        self.messages = []

    async def complete(self, messages, **kwargs):
        self.messages = messages
        return "你好，这是结果"

    async def stream(self, messages, **kwargs):
        for char in "你好":
            yield char


@pytest.fixture
def fake_gateway():
    return FakeGateway()


@pytest.fixture
def skills_api(fake_gateway):
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    app.dependency_overrides[get_llm_gateway] = lambda: fake_gateway
    yield fake_gateway
    app.dependency_overrides.pop(get_registry, None)
    app.dependency_overrides.pop(get_llm_gateway, None)


async def test_list_toggle_and_run(client, skills_api):
    listed = await client.get("/api/skills")
    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert item["id"] == "demo-skill" and item["enabled"] is True

    toggled = await client.patch(
        "/api/skills/demo-skill/toggle", json={"enabled": False}
    )
    assert toggled.status_code == 200
    assert toggled.json()["enabled"] is False
    again = await client.get("/api/skills")
    assert again.json()["items"][0]["enabled"] is False

    toggled = await client.patch(
        "/api/skills/demo-skill/toggle", json={"enabled": True}
    )
    assert toggled.json()["enabled"] is True

    ran = await client.post(
        "/api/skills/demo-skill/run", json={"inputs": {"topic": "周报"}}
    )
    assert ran.status_code == 200
    body = ran.json()
    assert body["text"] == "你好，这是结果"
    assert body["prompt_tokens"] == 2
    assert body["completion_tokens"] == 3


async def test_disabled_skill_blocks_run_and_stream(client, skills_api):
    await client.patch("/api/skills/demo-skill/toggle", json={"enabled": False})

    ran = await client.post(
        "/api/skills/demo-skill/run", json={"inputs": {"topic": "周报"}}
    )
    assert ran.status_code == 409
    assert ran.json()["code"] == "skill_disabled"

    streamed = await client.post(
        "/api/skills/demo-skill/stream", json={"inputs": {"topic": "周报"}}
    )
    assert streamed.status_code == 409
    assert streamed.json()["code"] == "skill_disabled"


async def test_unknown_skill_returns_404(client, skills_api):
    assert (await client.get("/api/skills/nope")).status_code == 404
    assert (
        await client.patch("/api/skills/nope/toggle", json={"enabled": True})
    ).status_code == 404
    assert (
        await client.post("/api/skills/nope/run", json={"inputs": {}})
    ).status_code == 404


async def test_invalid_inputs_return_422(client, skills_api):
    ran = await client.post("/api/skills/demo-skill/run", json={"inputs": {}})
    assert ran.status_code == 422
    assert ran.json()["code"] == "skill_inputs_invalid"

    streamed = await client.post(
        "/api/skills/demo-skill/stream", json={"inputs": {}}
    )
    assert streamed.status_code == 422
    assert streamed.json()["code"] == "skill_inputs_invalid"


async def test_profile_context_is_injected(client, skills_api):
    created = await client.post("/api/profiles", json={"name": "效率笔记"})
    assert created.status_code == 201
    profile_id = created.json()["id"]
    memory = await client.post(
        f"/api/profiles/{profile_id}/memories", json={"content": "周报要短"}
    )
    assert memory.status_code == 201

    resp = await client.post(
        "/api/skills/demo-skill/run",
        json={"inputs": {"topic": "周报"}, "profile_id": profile_id},
    )
    assert resp.status_code == 200
    system = skills_api.messages[0]["content"]
    assert "效率笔记" in system
    assert "周报要短" in system


async def test_unknown_profile_returns_404(client, skills_api):
    resp = await client.post(
        "/api/skills/demo-skill/run",
        json={
            "inputs": {"topic": "周报"},
            "profile_id": "00000000-0000-0000-0000-000000000099",
        },
    )
    assert resp.status_code == 404


async def test_stream_returns_sse(client, skills_api):
    async with client.stream(
        "POST",
        "/api/skills/demo-skill/stream",
        json={"inputs": {"topic": "周报"}},
    ) as resp:
        assert resp.status_code == 200
        body = "".join([chunk async for chunk in resp.aiter_text()])
    assert "你" in body and "好" in body and "[DONE]" in body
