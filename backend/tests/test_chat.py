import uuid
from pathlib import Path

from sqlalchemy import select

from app.api import chat as chat_module
from app.main import app
from app.models.base import DEFAULT_TENANT_ID
from app.models.skill import SkillRun, SkillSetting
from app.services.router import route_intent
from app.skills.registry import SkillRegistry, get_registry

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


class FakeGateway:
    async def complete(self, messages, **kwargs):
        return "这是运营建议"


def _gateway_must_not_load():
    async def _load(_session, _tenant_id):
        raise AssertionError("gateway must not load")

    return _load


def test_route_keywords():
    evaluated = route_intent("帮我评估一下这个选题")
    assert evaluated.kind == "skill"
    assert evaluated.skill_id == "topic-evaluator"

    note = route_intent("写个小红书笔记：通勤穿搭")
    assert note.kind == "skill"
    assert note.skill_id == "xhs-note"
    assert note.inputs["platform"] == "xiaohongshu"

    titles = route_intent("生成 8 个标题")
    assert titles.kind == "skill"
    assert titles.skill_id == "title-hook"

    assert route_intent("你好").kind == "chat"


async def test_chat_plain_and_skill(client, monkeypatch, db_session):
    async def _load(_session, _tenant_id):
        return FakeGateway()

    monkeypatch.setattr(chat_module, "load_gateway_default", _load)
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(SKILLS_DIR)
    try:
        plain = await client.post("/api/chat", json={"message": "你好"})
        assert plain.status_code == 200
        body = plain.json()
        assert body["intent"] == "chat"
        assert body["reply"] == "这是运营建议"
        assert body["skill_id"] is None

        routed = await client.post(
            "/api/chat", json={"message": "写个小红书笔记：通勤穿搭"}
        )
        assert routed.status_code == 200
        body = routed.json()
        assert body["intent"] == "skill"
        assert body["skill_id"] == "xhs-note"
        assert body["run_id"]
        assert body["reply"] == "这是运营建议"

        runs = (await db_session.execute(select(SkillRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].skill_id == "xhs-note"
        assert runs[0].status == "succeeded"
    finally:
        app.dependency_overrides.pop(get_registry, None)


async def test_chat_rejects_disabled_skill(client, monkeypatch, db_session):
    db_session.add(
        SkillSetting(
            tenant_id=DEFAULT_TENANT_ID, skill_id="xhs-note", enabled=False
        )
    )
    await db_session.flush()
    monkeypatch.setattr(
        chat_module, "load_gateway_default", _gateway_must_not_load()
    )
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(SKILLS_DIR)
    try:
        resp = await client.post(
            "/api/chat", json={"message": "写个小红书笔记：通勤穿搭"}
        )
        assert resp.status_code == 409
        assert resp.json()["code"] == "skill_disabled"
    finally:
        app.dependency_overrides.pop(get_registry, None)


async def test_chat_missing_inputs(client, monkeypatch):
    monkeypatch.setattr(
        chat_module, "load_gateway_default", _gateway_must_not_load()
    )
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(SKILLS_DIR)
    try:
        resp = await client.post(
            "/api/chat", json={"message": "帮我复盘一下上周数据"}
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "missing_inputs"
    finally:
        app.dependency_overrides.pop(get_registry, None)


async def test_chat_unknown_profile(client, monkeypatch):
    monkeypatch.setattr(
        chat_module, "load_gateway_default", _gateway_must_not_load()
    )
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(SKILLS_DIR)
    try:
        resp = await client.post(
            "/api/chat",
            json={
                "message": "写个小红书笔记：通勤穿搭",
                "profile_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "not_found"
        assert body["message"] == "账号画像不存在"
    finally:
        app.dependency_overrides.pop(get_registry, None)
