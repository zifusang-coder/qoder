import uuid
from pathlib import Path

from sqlalchemy import select

from app.api import chat as chat_module
from app.api.chat import _public_data, _summarize
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
    assert note.inputs == {"topic": "写个小红书笔记：通勤穿搭"}

    titles = route_intent("生成 8 个标题")
    assert titles.kind == "skill"
    assert titles.skill_id == "title-hook"

    assert route_intent("你好").kind == "chat"

    # 具体意图优先于平台/体裁名词
    title_with_platform = route_intent("公众号标题怎么写")
    assert title_with_platform.skill_id == "title-hook"

    repurpose = route_intent("把公众号文章改写成小红书笔记")
    assert repurpose.skill_id == "content-repurposing"

    cover = route_intent("帮我做个封面，标题叫周报模板")
    assert cover.kind == "skill"
    assert cover.skill_id == "cover-card"
    assert cover.inputs["title"] == "帮我做个封面，标题叫周报模板"
    assert cover.inputs["output_dir"]

    risk = route_intent("评估这篇内容的违规风险")
    assert risk.skill_id == "risk-scan"

    assert route_intent("公众号怎么涨粉").kind == "chat"

    douyin = route_intent("写个抖音口播文案")
    assert douyin.skill_id == "copywriting"
    assert douyin.inputs["platform"] == "douyin"

    fallback = route_intent("写个公众号口播")
    assert fallback.skill_id == "copywriting"
    assert fallback.inputs["platform"] == "douyin"


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
        assert body["missing"] is None

        routed = await client.post(
            "/api/chat", json={"message": "写个小红书笔记：通勤穿搭"}
        )
        assert routed.status_code == 200
        body = routed.json()
        assert body["intent"] == "skill"
        assert body["skill_id"] == "xhs-note"
        assert body["run_id"]
        assert body["missing"] is None
        assert body["reply"] == "这是运营建议"

        runs = (await db_session.execute(select(SkillRun))).scalars().all()
        assert len(runs) == 1
        assert runs[0].skill_id == "xhs-note"
        assert runs[0].status == "succeeded"
        assert runs[0].inputs == {"topic": "写个小红书笔记：通勤穿搭"}
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


async def test_chat_missing_inputs(client, monkeypatch, db_session):
    monkeypatch.setattr(
        chat_module, "load_gateway_default", _gateway_must_not_load()
    )
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(SKILLS_DIR)
    try:
        resp = await client.post(
            "/api/chat", json={"message": "帮我复盘一下上周数据"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["intent"] == "skill"
        assert body["skill_id"] == "postmortem"
        assert "title" in body["missing"]
        assert "metrics" in body["missing"]
        assert body["run_id"] is None
        runs = (await db_session.execute(select(SkillRun))).scalars().all()
        assert runs == []
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

        plain = await client.post(
            "/api/chat",
            json={"message": "你好", "profile_id": str(uuid.uuid4())},
        )
        assert plain.status_code == 404
        assert plain.json()["message"] == "账号画像不存在"
    finally:
        app.dependency_overrides.pop(get_registry, None)


def test_public_data_and_summarize(tmp_path):
    outputs_root = tmp_path / "outputs"
    inside = outputs_root / "adhoc" / "x" / "cover.png"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"png")

    assert _public_data({"path": str(inside)}, outputs_root) == {
        "path": "adhoc/x/cover.png"
    }
    outside = tmp_path / "elsewhere" / "cover.png"
    assert _public_data({"path": str(outside)}, outputs_root) == {
        "path": str(outside)
    }
    assert _public_data(None, outputs_root) is None

    assert _summarize({"titles": [{"text": "A"}, {"text": "B"}]}) == "1. A\n2. B"
    assert (
        _summarize({"summary": "high", "scan": {"risk_level": "high", "total": 3}})
        == "风险等级：高"
    )
    assert _summarize({"verdict": "advance"}) == "建议推进"
    assert _summarize({"score": 82, "fixes": ["改标题"]}) == "评分 82；建议：改标题"
    assert (
        _summarize({"path": "adhoc/x/cover.png"})
        == "已生成文件：adhoc/x/cover.png"
    )
