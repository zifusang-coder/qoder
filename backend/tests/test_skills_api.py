import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.api import skills as skills_module
from app.llm.types import Usage
from app.main import app
from app.models.base import DEFAULT_TENANT_ID
from app.models.skill import SkillRun, SkillSetting
from app.services.skill_settings import enabled_map as _enabled_map
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
def skills_api(fake_gateway, monkeypatch):
    async def _load_gateway(_session, _tenant_id):
        return fake_gateway

    monkeypatch.setattr(skills_module, "load_gateway_default", _load_gateway)
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    yield fake_gateway
    app.dependency_overrides.pop(get_registry, None)


async def test_list_toggle_and_run(client, skills_api, db_session):
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

    runs = (await db_session.execute(select(SkillRun))).scalars().all()
    assert len(runs) == 1
    assert runs[0].skill_id == "demo-skill"
    assert runs[0].status == "succeeded"


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


async def test_stream_returns_sse(client, skills_api, db_session):
    async with client.stream(
        "POST",
        "/api/skills/demo-skill/stream",
        json={"inputs": {"topic": "周报"}},
    ) as resp:
        assert resp.status_code == 200
        body = "".join([chunk async for chunk in resp.aiter_text()])

    data_lines = [
        line[len("data: "):].strip()
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert data_lines[-1] == "[DONE]"
    deltas = [json.loads(item)["delta"] for item in data_lines[:-1]]
    assert "".join(deltas) == "你好"

    count = (await db_session.execute(select(func.count(SkillRun.id)))).scalar_one()
    assert count == 0  # streaming must not persist an audit row


async def test_enabled_map_is_tenant_scoped(db_session):
    other = "00000000-0000-0000-0000-000000000002"
    db_session.add(SkillSetting(tenant_id=other, skill_id="demo-skill", enabled=False))
    await db_session.commit()

    assert await _enabled_map(db_session, DEFAULT_TENANT_ID) == {}
    assert (await _enabled_map(db_session, other))["demo-skill"] is False


@pytest.fixture
def skills_registry():
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    yield
    app.dependency_overrides.pop(get_registry, None)


async def test_error_precedence_beats_missing_llm_config(
    client, skills_registry, db_session
):
    assert (
        await client.post("/api/skills/nope/run", json={"inputs": {}})
    ).status_code == 404
    assert (
        await client.post("/api/skills/nope/stream", json={"inputs": {}})
    ).status_code == 404

    await client.patch("/api/skills/demo-skill/toggle", json={"enabled": False})
    disabled = await client.post(
        "/api/skills/demo-skill/run", json={"inputs": {"topic": "周报"}}
    )
    assert disabled.status_code == 409
    assert disabled.json()["code"] == "skill_disabled"

    disabled_stream = await client.post(
        "/api/skills/demo-skill/stream", json={"inputs": {"topic": "周报"}}
    )
    assert disabled_stream.status_code == 409
    assert disabled_stream.json()["code"] == "skill_disabled"

    await client.patch("/api/skills/demo-skill/toggle", json={"enabled": True})
    invalid = await client.post("/api/skills/demo-skill/run", json={"inputs": {}})
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "skill_inputs_invalid"

    invalid_stream = await client.post(
        "/api/skills/demo-skill/stream", json={"inputs": {}}
    )
    assert invalid_stream.status_code == 422
    assert invalid_stream.json()["code"] == "skill_inputs_invalid"

    # Positive control: a valid request gets past precedence checks and only
    # then fails on gateway resolution (proving precedence was not masked).
    missing = await client.post(
        "/api/skills/demo-skill/run", json={"inputs": {"topic": "周报"}}
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "llm_not_configured"

    count = (await db_session.execute(select(func.count(SkillRun.id)))).scalar_one()
    assert count == 0


async def test_toggle_recovers_from_concurrent_insert(
    client, skills_registry, db_session, monkeypatch, caplog
):
    db_session.add(
        SkillSetting(
            tenant_id=DEFAULT_TENANT_ID, skill_id="demo-skill", enabled=True
        )
    )
    await db_session.commit()

    original_execute = db_session.execute
    skipped = {"done": False}

    async def flaky_execute(statement, *args, **kwargs):
        descriptions = getattr(statement, "column_descriptions", [])
        is_skill_setting = (
            bool(descriptions) and descriptions[0].get("entity") is SkillSetting
        )
        if is_skill_setting and not skipped["done"]:
            skipped["done"] = True

            class _NoRow:
                def scalar_one_or_none(self):
                    return None

            return _NoRow()
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "execute", flaky_execute)

    with caplog.at_level(logging.INFO, logger="app.api.skills"):
        response = await client.patch(
            "/api/skills/demo-skill/toggle", json={"enabled": False}
        )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert skipped["done"] is True
    assert "技能启停并发写入" in caplog.text

    row = (
        await original_execute(
            select(SkillSetting).where(
                SkillSetting.tenant_id == DEFAULT_TENANT_ID,
                SkillSetting.skill_id == "demo-skill",
            )
        )
    ).scalar_one()
    assert row.enabled is False


async def test_toggle_reraises_integrity_error_without_winner(db_session, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    from app.api.skills import ToggleIn, toggle_skill
    from app.models.user import User

    user = User(
        tenant_id=DEFAULT_TENANT_ID,
        email="tester@local",
        password_hash="x",
        display_name="Tester",
    )

    class _NoRow:
        def scalar_one_or_none(self):
            return None

    calls = {"execute": 0}

    async def empty_execute(*_args, **_kwargs):
        calls["execute"] += 1
        return _NoRow()

    async def failing_commit():
        raise IntegrityError("insert", {}, Exception("unique"))

    monkeypatch.setattr(db_session, "execute", empty_execute)
    monkeypatch.setattr(db_session, "commit", failing_commit)

    with pytest.raises(IntegrityError):
        await toggle_skill(
            "demo-skill",
            ToggleIn(enabled=False),
            user=user,
            session=db_session,
            registry=SkillRegistry(FIXTURE),
        )
    assert calls["execute"] == 2  # first read + retry read both returned no row
