import uuid
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from app.api import chat as chat_module
from app.api.chat import _summarize
from app.main import app
from app.models.base import DEFAULT_TENANT_ID
from app.models.profile import Profile
from app.models.skill import SkillRun, SkillSetting
from app.services.outputs import adhoc_output_dir, public_output_data
from app.services.router import route_intent
from app.skills.registry import SkillRegistry, get_registry

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
OUT = "outputs/adhoc"


class FakeGateway:
    async def complete(self, messages, **kwargs):
        return "这是运营建议"


def _gateway_must_not_load():
    async def _load(_session, _tenant_id):
        raise AssertionError("gateway must not load")

    return _load


def test_route_keywords():
    evaluated = route_intent("帮我评估一下这个选题", output_dir=OUT)
    assert evaluated.kind == "skill"
    assert evaluated.skill_id == "topic-evaluator"

    note = route_intent("写个小红书笔记：通勤穿搭", output_dir=OUT)
    assert note.kind == "skill"
    assert note.skill_id == "xhs-note"
    assert note.inputs == {"topic": "写个小红书笔记：通勤穿搭"}

    titles = route_intent("生成 8 个标题", output_dir=OUT)
    assert titles.kind == "skill"
    assert titles.skill_id == "title-hook"

    assert route_intent("你好", output_dir=OUT).kind == "chat"

    # 具体意图优先于平台/体裁名词
    title_with_platform = route_intent("公众号标题怎么写", output_dir=OUT)
    assert title_with_platform.skill_id == "title-hook"

    repurpose = route_intent("把公众号文章改写成小红书笔记", output_dir=OUT)
    assert repurpose.skill_id == "content-repurposing"

    cover = route_intent("帮我做个封面，标题叫周报模板", output_dir=OUT)
    assert cover.kind == "skill"
    assert cover.skill_id == "cover-card"
    assert cover.inputs["title"] == "帮我做个封面，标题叫周报模板"
    assert cover.inputs["output_dir"] == OUT

    risk = route_intent("评估这篇内容的违规风险", output_dir=OUT)
    assert risk.skill_id == "risk-scan"

    assert route_intent("公众号怎么涨粉", output_dir=OUT).kind == "chat"

    douyin = route_intent("写个抖音口播文案", output_dir=OUT)
    assert douyin.skill_id == "copywriting"
    assert douyin.inputs["platform"] == "douyin"

    zhihu = route_intent("写个知乎回答", output_dir=OUT)
    assert zhihu.skill_id == "copywriting"
    assert zhihu.inputs["platform"] == "zhihu"

    fallback = route_intent("写个公众号口播", output_dir=OUT)
    assert fallback.skill_id == "copywriting"
    assert fallback.inputs["platform"] == "douyin"


def test_route_avoids_generic_rewrite_collisions():
    assert (
        route_intent("帮我把封面改成浅色", output_dir=OUT).skill_id == "cover-card"
    )
    assert (
        route_intent("把这段标题改成更吸引人", output_dir=OUT).skill_id
        == "title-hook"
    )
    assert (
        route_intent("把这篇笔记改成合规一些", output_dir=OUT).skill_id
        == "risk-scan"
    )
    assert (
        route_intent("写个抖音脚本", output_dir=OUT).skill_id == "copywriting"
    )


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


async def test_chat_plain_injects_profile(client, monkeypatch, db_session):
    class CapturingGateway:
        def __init__(self):
            self.messages = []

        async def complete(self, messages, **kwargs):
            self.messages = messages
            return "收到"

    gateway = CapturingGateway()

    async def _load(_session, _tenant_id):
        return gateway

    monkeypatch.setattr(chat_module, "load_gateway_default", _load)
    profile = Profile(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        name="主账号",
        account_name="效率笔记",
    )
    db_session.add(profile)
    await db_session.commit()

    resp = await client.post(
        "/api/chat", json={"message": "你好", "profile_id": str(profile.id)}
    )
    assert resp.status_code == 200
    assert resp.json()["intent"] == "chat"
    system = gateway.messages[0]["content"]
    assert "<profile_context>" in system
    assert "效率笔记" in system


async def test_chat_skill_inputs_continuation(client, monkeypatch, db_session):
    class PostmortemGateway:
        async def complete(self, messages, **kwargs):
            return '{"summary": "ok", "findings": []}'

    async def _load(_session, _tenant_id):
        return PostmortemGateway()

    monkeypatch.setattr(chat_module, "load_gateway_default", _load)
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(SKILLS_DIR)
    try:
        resp = await client.post(
            "/api/chat",
            json={
                "message": "帮我复盘一下上周数据",
                "inputs": {
                    "title": "周报",
                    "metrics": {"views": 100},
                    "evil": "x",
                },
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["skill_id"] == "postmortem"
        assert body["run_id"]
        assert body["reply"] == "ok"

        runs = (await db_session.execute(select(SkillRun))).scalars().all()
        assert len(runs) == 1
        persisted = runs[0].inputs
        assert persisted["title"] == "周报"
        assert persisted["metrics"] == {"views": 100}
        assert persisted["platform"] == "wechat"
        assert "evil" not in persisted
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
        assert "内容标题" in body["reply"]
        assert body["run_id"] is None
        runs = (await db_session.execute(select(SkillRun))).scalars().all()
        assert runs == []

        blank = await client.post(
            "/api/chat",
            json={
                "message": "帮我复盘一下上周数据",
                "inputs": {"title": "   ", "metrics": {"views": 1}},
            },
        )
        assert blank.status_code == 200
        assert blank.json()["missing"] == ["title"]
    finally:
        app.dependency_overrides.pop(get_registry, None)


async def test_chat_forces_server_output_dir(client, monkeypatch):
    captured = {}

    class StubExecutor:
        def __init__(self, **_kwargs):
            pass

        async def run(self, skill_id, inputs):
            captured["skill_id"] = skill_id
            captured["inputs"] = inputs
            return SimpleNamespace(run_id="run-1", text="ok", data=None)

    async def _load(_session, _tenant_id):
        return FakeGateway()

    monkeypatch.setattr(chat_module, "SkillExecutor", StubExecutor)
    monkeypatch.setattr(chat_module, "load_gateway_default", _load)
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(SKILLS_DIR)
    try:
        resp = await client.post(
            "/api/chat",
            json={
                "message": "帮我做个封面",
                "inputs": {"output_dir": "/tmp/escape", "evil": "x"},
            },
        )
        assert resp.status_code == 200
        assert captured["skill_id"] == "cover-card"
        out_dir = Path(captured["inputs"]["output_dir"])
        assert out_dir.parent.name == DEFAULT_TENANT_ID
        assert out_dir.parent.parent.name == "adhoc"
        assert "/tmp/escape" not in captured["inputs"]["output_dir"]
        assert "evil" not in captured["inputs"]
        assert captured["inputs"]["title"]
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

        clarifying = await client.post(
            "/api/chat",
            json={
                "message": "帮我复盘一下上周数据",
                "profile_id": str(uuid.uuid4()),
            },
        )
        assert clarifying.status_code == 404
        assert clarifying.json()["message"] == "账号画像不存在"
    finally:
        app.dependency_overrides.pop(get_registry, None)


def test_public_data_and_summarize(tmp_path):
    outputs_root = tmp_path / "outputs"
    inside = outputs_root / "adhoc" / "x" / "cover.png"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"png")

    assert public_output_data({"path": str(inside)}, outputs_root) == {
        "path": "adhoc/x/cover.png"
    }
    outside = tmp_path / "elsewhere" / "cover.png"
    assert public_output_data({"path": str(outside)}, outputs_root) == {
        "path": str(outside)
    }
    assert public_output_data(None, outputs_root) is None

    assert _summarize({"titles": [{"text": "A"}, {"text": "B"}]}) == "1. A\n2. B"
    assert (
        _summarize({"summary": "high", "scan": {"risk_level": "high", "total": 3}})
        == "风险等级：高；命中 3 处"
    )
    assert _summarize({"verdict": "advance"}) == "建议推进"
    assert (
        _summarize({"verdict": "advance", "score": 82, "reasons": ["时效性强"]})
        == "建议推进（82 分）；理由：时效性强"
    )
    assert _summarize({"verdict": {"x": 1}}) == "已完成，请查看结构化结果。"
    assert (
        _summarize({"verdict": "超预期", "next_actions": ["多做对比图"]})
        == "超预期；建议：多做对比图"
    )
    assert _summarize({"score": 82.0, "fixes": []}) == "评分 82"
    assert _summarize({"score": 82, "fixes": ["改标题"]}) == "评分 82；建议：改标题"
    assert _summarize({"score": 80, "fixes": 3}) == "评分 80"
    assert _summarize({}) == "已完成，请查看结构化结果。"
    assert (
        _summarize({"titles": [{"text": "A"}, {"style": "x"}, None, "B"]})
        == "1. A\n2. B"
    )
    assert (
        _summarize({"path": "adhoc/x/cover.png"})
        == "已生成文件：adhoc/x/cover.png"
    )


def test_adhoc_output_dir_is_tenant_scoped(tmp_path, monkeypatch):
    import app.services.outputs as outputs_module

    monkeypatch.setattr(
        outputs_module,
        "get_settings",
        lambda: SimpleNamespace(outputs_dir=tmp_path),
    )
    first = adhoc_output_dir(DEFAULT_TENANT_ID)
    second = adhoc_output_dir(DEFAULT_TENANT_ID)
    assert first != second
    assert first.parent == tmp_path / "adhoc" / DEFAULT_TENANT_ID
    assert second.parent == tmp_path / "adhoc" / DEFAULT_TENANT_ID
    assert len(first.name) == 32
