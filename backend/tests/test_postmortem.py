import json
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.skills import get_llm_gateway
from app.config import DEFAULT_TENANT_ID
from app.llm.types import Usage
from app.main import app
from app.models.analytics import AnalyticsSnapshot
from app.models.profile import ProfileMemory
from app.models.project import Content, Project
from app.models.skill import SkillRun
from app.skills.registry import SkillRegistry, get_registry

FIXTURE = Path(__file__).parent / "fixtures" / "skills"
OTHER_TENANT = "00000000-0000-0000-0000-000000000002"
HEADER = (
    "content_id,platform,captured_at,views,likes,comments,shares,saves,"
    "followers_delta"
)
DEFAULT_REPORT = {
    "verdict": "值得复用",
    "what_worked": ["标题清晰"],
    "what_failed": [],
    "next_actions": ["保持"],
    "memories": [
        {"kind": "insight", "content": "具体标题更受欢迎", "weight": 0.8}
    ],
}


class FakeGateway:
    def __init__(self):
        self.last_usage = Usage(10, 20)
        self.messages: list[dict[str, str]] = []
        self.reply = json.dumps(DEFAULT_REPORT, ensure_ascii=False)
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.messages = messages
        return self.reply


@pytest.fixture
def postmortem_env():
    gateway = FakeGateway()
    registry = SkillRegistry(FIXTURE)
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_llm_gateway] = lambda: gateway
    try:
        yield SimpleNamespace(gateway=gateway, registry=registry)
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_llm_gateway, None)


async def _setup(
    client,
    *,
    with_profile: bool = True,
    body: str = "第一版正文",
    title: str = "周报模板",
):
    profile_id = None
    if with_profile:
        profile = await client.post("/api/profiles", json={"name": "效率笔记"})
        assert profile.status_code == 201
        profile_id = profile.json()["id"]

    project = await client.post(
        "/api/projects", json={"title": "复盘项目", "profile_id": profile_id}
    )
    assert project.status_code == 201
    project_id = project.json()["id"]

    content = await client.post(
        f"/api/projects/{project_id}/contents",
        json={"platform": "wechat", "title": title, "body": body},
    )
    assert content.status_code == 201
    return profile_id, project_id, content.json()["id"]


async def _import_metrics(
    client,
    content_id,
    *,
    captured_at: str = "2026-10-01",
    views: int = 1000,
    likes: int = 100,
    comments: int = 10,
    shares: int = 5,
    saves: int = 5,
    followers_delta: int = 3,
):
    row = (
        f"{content_id},wechat,{captured_at},{views},{likes},{comments},"
        f"{shares},{saves},{followers_delta}"
    )
    response = await client.post(
        "/api/analytics/import",
        files={
            "file": (
                "analytics.csv",
                (HEADER + "\n" + row + "\n").encode("utf-8"),
                "text/csv",
            )
        },
    )
    assert response.status_code == 200
    assert response.json() == {"upserted": 1, "errors": []}


async def _postmortem(client, content_id, payload: dict | None = None):
    return await client.post(
        f"/api/analytics/contents/{content_id}/postmortem",
        json=payload if payload is not None else {"skill_id": "postmortem-fake"},
    )


async def _content(db_session, content_id) -> Content:
    return (
        await db_session.execute(
            select(Content).where(Content.id == uuid.UUID(str(content_id)))
        )
    ).scalar_one()


async def _count(db_session, model, *where) -> int:
    statement = select(func.count()).select_from(model)
    if where:
        statement = statement.where(*where)
    return (await db_session.execute(statement)).scalar_one()


async def test_postmortem_writes_report_memory_and_meta(
    client, db_session, postmortem_env
):
    profile_id, _project_id, content_id = await _setup(client)
    manual = await client.post(
        f"/api/profiles/{profile_id}/memories",
        json={"content": "手工经验：开头要有结论"},
    )
    assert manual.status_code == 201
    await _import_metrics(client, content_id)

    response = await _postmortem(client, content_id)
    assert response.status_code == 200
    body = response.json()
    assert body["report"]["verdict"] == "值得复用"
    assert len(body["memory_ids"]) == 1
    assert uuid.UUID(body["run_id"])

    memories = (
        await client.get(f"/api/profiles/{profile_id}/memories")
    ).json()
    generated = [m for m in memories if m["source"] == "postmortem"]
    assert len(generated) == 1
    assert generated[0]["id"] == body["memory_ids"][0]
    assert generated[0]["content"] == "具体标题更受欢迎"
    assert generated[0]["kind"] == "insight"
    assert generated[0]["weight"] == 0.8

    content = await _content(db_session, content_id)
    assert content.meta["postmortem"]["verdict"] == "值得复用"
    assert content.meta["postmortem_run_id"] == body["run_id"]
    assert content.meta["postmortem_at"]

    system = postmortem_env.gateway.messages[0]["content"]
    assert "<profile_context>" in system
    assert "效率笔记" in system
    assert "手工经验：开头要有结论" in system

    inputs = json.loads(postmortem_env.gateway.messages[-1]["content"])
    assert inputs["title"] == "周报模板"
    assert inputs["platform"] == "wechat"
    assert inputs["metrics"] == {
        "views": 1000,
        "likes": 100,
        "comments": 10,
        "shares": 5,
        "saves": 5,
        "followers_delta": 3,
    }
    assert inputs["content_excerpt"] == "第一版正文"
    assert inputs["publish_time"] == "2026-10-01"

    run = (await db_session.execute(select(SkillRun))).scalar_one()
    assert str(run.id) == body["run_id"]
    assert run.status == "succeeded"
    assert run.skill_id == "postmortem-fake"
    assert run.created_by is not None


async def test_postmortem_without_metrics_returns_409(
    client, db_session, postmortem_env
):
    profile_id, _project_id, content_id = await _setup(client)

    response = await _postmortem(client, content_id)
    assert response.status_code == 409
    assert response.json()["code"] == "no_metrics"
    assert postmortem_env.gateway.calls == 0
    assert await _count(db_session, SkillRun) == 0
    assert await _count(
        db_session, ProfileMemory, ProfileMemory.profile_id == uuid.UUID(profile_id)
    ) == 0

    content = await _content(db_session, content_id)
    assert "postmortem" not in content.meta


async def test_postmortem_content_not_found_and_tenant_isolation(
    client, db_session, postmortem_env
):
    missing = await _postmortem(client, uuid.uuid4())
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"

    other_project_id = uuid.uuid4()
    other_content_id = uuid.uuid4()
    db_session.add(
        Project(
            id=other_project_id,
            tenant_id=OTHER_TENANT,
            title="其他租户项目",
        )
    )
    await db_session.flush()
    db_session.add(
        Content(
            id=other_content_id,
            tenant_id=OTHER_TENANT,
            project_id=other_project_id,
            platform="wechat",
            title="其他租户内容",
        )
    )
    await db_session.commit()

    other = await _postmortem(client, other_content_id)
    assert other.status_code == 404
    assert other.json()["code"] == "not_found"

    malformed = await client.post(
        "/api/analytics/contents/not-a-uuid/postmortem", json={}
    )
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "validation_error"

    assert postmortem_env.gateway.calls == 0
    assert await _count(db_session, SkillRun) == 0


async def test_postmortem_unknown_skill_returns_404(
    client, db_session, postmortem_env
):
    _profile_id, _project_id, content_id = await _setup(client)
    await _import_metrics(client, content_id)

    response = await _postmortem(
        client, content_id, {"skill_id": "does-not-exist"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert await _count(db_session, SkillRun) == 0


async def test_postmortem_without_profile_skips_writeback(
    client, db_session, postmortem_env
):
    _profile_id, _project_id, content_id = await _setup(
        client, with_profile=False
    )
    await _import_metrics(client, content_id)

    response = await _postmortem(client, content_id)
    assert response.status_code == 200
    body = response.json()
    assert body["memory_ids"] == []
    assert body["report"]["verdict"] == "值得复用"
    assert await _count(db_session, ProfileMemory) == 0

    content = await _content(db_session, content_id)
    assert content.meta["postmortem"]["verdict"] == "值得复用"
    assert content.meta["postmortem_run_id"] == body["run_id"]

    system = postmortem_env.gateway.messages[0]["content"]
    assert "<profile_context>" not in system


async def test_postmortem_invalid_json_marks_run_failed(
    client, db_session, postmortem_env
):
    _profile_id, _project_id, content_id = await _setup(client)
    await _import_metrics(client, content_id)

    postmortem_env.gateway.reply = "not json"
    first = await _postmortem(client, content_id)
    assert first.status_code == 502
    assert first.json()["code"] == "skill_output_invalid"
    message = first.json()["message"]
    assert message
    assert len(message) <= 200
    assert "Traceback" not in message
    assert "sqlalchemy" not in message.lower()

    postmortem_env.gateway.reply = "[]"
    second = await _postmortem(client, content_id)
    assert second.status_code == 502
    assert second.json()["code"] == "skill_output_invalid"

    runs = (
        await db_session.execute(select(SkillRun).order_by(SkillRun.created_at))
    ).scalars().all()
    assert len(runs) == 2
    assert [run.status for run in runs] == ["failed", "failed"]
    assert all(run.error for run in runs)

    assert await _count(db_session, ProfileMemory) == 0
    content = await _content(db_session, content_id)
    assert "postmortem" not in content.meta


async def test_postmortem_sanitizes_memory_candidates(
    client, db_session, postmortem_env
):
    profile_id, _project_id, content_id = await _setup(client)
    await _import_metrics(client, content_id)

    postmortem_env.gateway.reply = json.dumps(
        {"verdict": "ok", "memories": {"bad": "shape"}}, ensure_ascii=False
    )
    not_list = await _postmortem(client, content_id)
    assert not_list.status_code == 200
    assert not_list.json()["memory_ids"] == []
    assert await _count(db_session, ProfileMemory) == 0

    long_text = "长" * 300
    postmortem_env.gateway.reply = json.dumps(
        {
            "verdict": "ok",
            "memories": [
                {"content": "   ", "weight": 0.9},
                {"content": "布尔权重", "weight": True},
                {"content": long_text, "kind": "  ", "weight": 5},
                {"content": "默认权重"},
                {"content": "负权重", "weight": -1},
            ],
        },
        ensure_ascii=False,
    )
    response = await _postmortem(client, content_id)
    assert response.status_code == 200
    assert len(response.json()["memory_ids"]) == 3

    memories = (
        await client.get(f"/api/profiles/{profile_id}/memories")
    ).json()
    assert len(memories) == 3
    by_content = {memory["content"]: memory for memory in memories}

    truncated = by_content[long_text[:200]]
    assert len(truncated["content"]) == 200
    assert truncated["kind"] == "insight"
    assert truncated["weight"] == 1.0
    assert by_content["默认权重"]["weight"] == 0.5
    assert by_content["默认权重"]["kind"] == "insight"
    assert by_content["负权重"]["weight"] == 0.0
    assert "布尔权重" not in by_content


async def test_postmortem_deduplicates_memories(
    client, db_session, postmortem_env
):
    _profile_id, _project_id, content_id = await _setup(client)
    await _import_metrics(client, content_id)
    postmortem_env.gateway.reply = json.dumps(
        {
            "verdict": "ok",
            "memories": [
                {"content": "重复经验", "weight": 0.7},
                {"content": "  重复经验  ", "weight": 0.2},
                {"content": "另一条", "weight": 0.6},
            ],
        },
        ensure_ascii=False,
    )

    first = await _postmortem(client, content_id)
    assert first.status_code == 200
    assert len(first.json()["memory_ids"]) == 2

    second = await _postmortem(client, content_id)
    assert second.status_code == 200
    assert second.json()["memory_ids"] == []

    rows = (await db_session.execute(select(ProfileMemory))).scalars().all()
    assert len(rows) == 2
    assert {row.content for row in rows} == {"重复经验", "另一条"}
    assert all(row.source == "postmortem" for row in rows)


async def test_postmortem_uses_pinned_version(
    client, db_session, postmortem_env
):
    _profile_id, _project_id, content_id = await _setup(
        client, body="第一版正文"
    )
    version = await client.post(
        f"/api/contents/{content_id}/versions",
        json={"title": "周报模板二稿", "body": "第二版正文内容"},
    )
    assert version.status_code == 201
    await _import_metrics(client, content_id)

    response = await _postmortem(client, content_id)
    assert response.status_code == 200
    inputs = json.loads(postmortem_env.gateway.messages[-1]["content"])
    assert inputs["title"] == "周报模板二稿"
    assert inputs["content_excerpt"] == "第二版正文内容"
    assert "第一版" not in inputs["content_excerpt"]


async def test_postmortem_cross_tenant_snapshot_not_used(
    client, db_session, postmortem_env
):
    project_id = uuid.uuid4()
    content_id = uuid.uuid4()
    db_session.add(
        Project(id=project_id, tenant_id=OTHER_TENANT, title="外部项目")
    )
    await db_session.flush()
    db_session.add(
        Content(
            id=content_id,
            tenant_id=OTHER_TENANT,
            project_id=project_id,
            platform="wechat",
            title="外部内容",
        )
    )
    await db_session.flush()
    db_session.add(
        AnalyticsSnapshot(
            tenant_id=OTHER_TENANT,
            content_id=content_id,
            platform="wechat",
            captured_at=date(2026, 10, 1),
            views=99,
            likes=9,
        )
    )
    await db_session.commit()

    response = await _postmortem(client, content_id)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert postmortem_env.gateway.calls == 0

    snapshot = (
        await db_session.execute(
            select(AnalyticsSnapshot).where(
                AnalyticsSnapshot.tenant_id == OTHER_TENANT,
                AnalyticsSnapshot.content_id == content_id,
            )
        )
    ).scalar_one()
    assert snapshot.views == 99
    assert DEFAULT_TENANT_ID != OTHER_TENANT
