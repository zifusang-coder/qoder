import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.errors import AppError
from app.llm.types import Usage
from app.models.base import DEFAULT_TENANT_ID
from app.models.project import Content, Project
from app.models.skill import SkillSetting
from app.services import repurpose as repurpose_module
from app.skills.registry import SkillRegistry, get_registry
from app.storage import Storage, get_storage

FIXTURE = Path(__file__).parent / "fixtures" / "skills"
SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"


class FakeGateway:
    last_usage = Usage(4, 6)

    async def complete(self, messages, **kwargs):
        return "# 小红书版\n改写后的正文"


async def test_upload_and_repurpose(client, tmp_path, monkeypatch):
    from app.main import app

    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)

    async def fake_gateway(session, tenant_id):
        return FakeGateway()

    monkeypatch.setattr(repurpose_module, "load_gateway_default", fake_gateway)
    try:
        project = (await client.post("/api/projects", json={"title": "一稿多改"})).json()
        uploaded = await client.post(
            f"/api/projects/{project['id']}/files",
            files={"file": ("notes.md", b"# raw\nline", "text/markdown")},
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["path"].startswith("files/")

        content = (
            await client.post(
                f"/api/projects/{project['id']}/contents",
                json={"platform": "wechat", "title": "主稿", "body": "# 主稿\n正文"},
            )
        ).json()

        result = await client.post(
            f"/api/projects/{project['id']}/repurpose",
            json={
                "source_content_id": content["id"],
                "platform": "xiaohongshu",
                "skill_id": "demo-skill",
            },
        )
        assert result.status_code == 201
        assert result.json()["platform"] == "xiaohongshu"
        assert result.json()["version"] == 1
        assert "小红书版" in result.json()["body"]
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_storage, None)


async def test_upload_rejects_oversized_and_sanitizes_filename(
    client, tmp_path, monkeypatch
):
    from app.api import projects as projects_api
    from app.main import app

    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)
    monkeypatch.setattr(projects_api, "MAX_UPLOAD_BYTES", 4)
    try:
        project = (await client.post("/api/projects", json={"title": "上传"})).json()
        too_large = await client.post(
            f"/api/projects/{project['id']}/files",
            files={"file": ("big.bin", b"12345", "application/octet-stream")},
        )
        assert too_large.status_code == 413
        assert too_large.json()["code"] == "file_too_large"

        monkeypatch.setattr(projects_api, "MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
        uploaded = await client.post(
            f"/api/projects/{project['id']}/files",
            files={"file": ("../../evil.md", b"ok", "text/markdown")},
        )
        assert uploaded.status_code == 201
        assert uploaded.json()["path"] == "files/evil.md"

        listed = await client.get(f"/api/projects/{project['id']}/files")
        assert listed.status_code == 200
        assert listed.json()["items"] == ["files/evil.md"]
    finally:
        app.dependency_overrides.pop(get_storage, None)


async def test_repurpose_validates_source_before_gateway(
    client, tmp_path, monkeypatch, db_session
):
    from app.main import app

    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)

    async def missing_gateway(session, tenant_id):
        raise AppError(
            "llm_not_configured", "尚未配置默认 LLM 提供商，请先在设置页添加", 400
        )

    monkeypatch.setattr(repurpose_module, "load_gateway_default", missing_gateway)
    try:
        project = (await client.post("/api/projects", json={"title": "优先级"})).json()
        project_id = project["id"]

        unknown = await client.post(
            f"/api/projects/{project_id}/repurpose",
            json={
                "source_content_id": str(uuid.uuid4()),
                "platform": "zhihu",
            },
        )
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "not_found"

        malformed = await client.post(
            f"/api/projects/{project_id}/repurpose",
            json={"source_content_id": "not-a-uuid", "platform": "zhihu"},
        )
        assert malformed.status_code == 404

        empty = (
            await client.post(
                f"/api/projects/{project_id}/contents",
                json={"platform": "wechat"},
            )
        ).json()
        empty_resp = await client.post(
            f"/api/projects/{project_id}/repurpose",
            json={"source_content_id": empty["id"], "platform": "zhihu"},
        )
        assert empty_resp.status_code == 409
        assert empty_resp.json()["code"] == "empty_content"

        # 其他租户的内容即使 ID 已知也必须 404，而不是泄露存在性
        foreign_project_id = uuid.uuid4()
        foreign_content_id = uuid.uuid4()
        db_session.add(
            Project(
                id=foreign_project_id,
                tenant_id="other-tenant",
                title="其他租户项目",
            )
        )
        await db_session.flush()
        db_session.add(
            Content(
                id=foreign_content_id,
                tenant_id="other-tenant",
                project_id=foreign_project_id,
                platform="wechat",
                title="外来稿件",
            )
        )
        await db_session.flush()
        foreign = await client.post(
            f"/api/projects/{project_id}/repurpose",
            json={
                "source_content_id": str(foreign_content_id),
                "platform": "zhihu",
            },
        )
        assert foreign.status_code == 404

        # 正例：源内容通过校验后才会解析网关，暴露缺失配置的 400
        content = (
            await client.post(
                f"/api/projects/{project_id}/contents",
                json={"platform": "wechat", "title": "主稿", "body": "正文"},
            )
        ).json()
        no_llm = await client.post(
            f"/api/projects/{project_id}/repurpose",
            json={
                "source_content_id": content["id"],
                "platform": "zhihu",
                "skill_id": "demo-skill",
            },
        )
        assert no_llm.status_code == 400
        assert no_llm.json()["code"] == "llm_not_configured"
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_storage, None)


async def test_repurpose_reuses_target_content(client, tmp_path, monkeypatch):
    from app.main import app

    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)

    async def fake_gateway(session, tenant_id):
        return FakeGateway()

    monkeypatch.setattr(repurpose_module, "load_gateway_default", fake_gateway)
    try:
        project = (await client.post("/api/projects", json={"title": "复用目标"})).json()
        content = (
            await client.post(
                f"/api/projects/{project['id']}/contents",
                json={"platform": "wechat", "title": "主稿", "body": "正文"},
            )
        ).json()

        payload = {
            "source_content_id": content["id"],
            "platform": "xiaohongshu",
            "skill_id": "demo-skill",
        }
        first = await client.post(
            f"/api/projects/{project['id']}/repurpose", json=payload
        )
        assert first.status_code == 201
        assert first.json()["version"] == 1

        second = await client.post(
            f"/api/projects/{project['id']}/repurpose", json=payload
        )
        assert second.status_code == 201
        assert second.json()["version"] == 2

        contents = (
            await client.get(f"/api/projects/{project['id']}/contents")
        ).json()
        targets = [c for c in contents if c["platform"] == "xiaohongshu"]
        assert len(targets) == 1
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_storage, None)


async def test_repurpose_rejects_title_only_source(client, tmp_path, monkeypatch):
    from app.main import app

    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)

    async def must_not_load(session, tenant_id):
        raise AssertionError("gateway must not load")

    monkeypatch.setattr(repurpose_module, "load_gateway_default", must_not_load)
    try:
        project = (await client.post("/api/projects", json={"title": "空稿"})).json()
        content = (
            await client.post(
                f"/api/projects/{project['id']}/contents",
                json={"platform": "wechat", "title": "只有标题"},
            )
        ).json()

        result = await client.post(
            f"/api/projects/{project['id']}/repurpose",
            json={
                "source_content_id": content["id"],
                "platform": "xiaohongshu",
                "skill_id": "demo-skill",
            },
        )
        assert result.status_code == 409
        assert result.json()["code"] == "empty_content"
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_storage, None)


async def test_repurpose_rejects_disabled_and_unknown_skills(
    client, tmp_path, monkeypatch, db_session
):
    from app.main import app

    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)

    async def must_not_load(session, tenant_id):
        raise AssertionError("gateway must not load")

    monkeypatch.setattr(repurpose_module, "load_gateway_default", must_not_load)
    try:
        project = (await client.post("/api/projects", json={"title": "技能校验"})).json()
        content = (
            await client.post(
                f"/api/projects/{project['id']}/contents",
                json={"platform": "wechat", "title": "主稿", "body": "正文"},
            )
        ).json()

        db_session.add(
            SkillSetting(
                tenant_id=DEFAULT_TENANT_ID, skill_id="demo-skill", enabled=False
            )
        )
        await db_session.flush()

        disabled = await client.post(
            f"/api/projects/{project['id']}/repurpose",
            json={
                "source_content_id": content["id"],
                "platform": "xiaohongshu",
                "skill_id": "demo-skill",
            },
        )
        assert disabled.status_code == 409
        assert disabled.json()["code"] == "skill_disabled"

        unknown = await client.post(
            f"/api/projects/{project['id']}/repurpose",
            json={
                "source_content_id": content["id"],
                "platform": "xiaohongshu",
                "skill_id": "nope",
            },
        )
        assert unknown.status_code == 404
        assert unknown.json()["code"] == "not_found"
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_storage, None)


async def test_repurpose_uses_production_default_skill(client, tmp_path, monkeypatch):
    from app.main import app

    app.dependency_overrides[get_registry] = lambda: SkillRegistry(SKILLS_DIR)
    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)

    async def fake_gateway(session, tenant_id):
        return FakeGateway()

    monkeypatch.setattr(repurpose_module, "load_gateway_default", fake_gateway)
    try:
        project = (await client.post("/api/projects", json={"title": "默认技能"})).json()
        content = (
            await client.post(
                f"/api/projects/{project['id']}/contents",
                json={"platform": "wechat", "title": "主稿", "body": "正文"},
            )
        ).json()

        result = await client.post(
            f"/api/projects/{project['id']}/repurpose",
            json={
                "source_content_id": content["id"],
                "platform": "xiaohongshu",
            },
        )
        assert result.status_code == 201
        assert result.json()["platform"] == "xiaohongshu"
        assert "改写后的正文" in result.json()["body"]
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_storage, None)


async def test_repurpose_target_ordering_and_title_backfill(
    client, tmp_path, monkeypatch, db_session
):
    from app.main import app

    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)

    async def fake_gateway(session, tenant_id):
        return FakeGateway()

    monkeypatch.setattr(repurpose_module, "load_gateway_default", fake_gateway)
    try:
        project = (
            await client.post("/api/projects", json={"title": "目标顺序"})
        ).json()
        source = (
            await client.post(
                f"/api/projects/{project['id']}/contents",
                json={"platform": "wechat", "title": "主稿", "body": "正文"},
            )
        ).json()

        older_id = uuid.uuid4()
        newer_id = uuid.uuid4()
        db_session.add_all(
            [
                Content(
                    id=older_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    project_id=uuid.UUID(project["id"]),
                    platform="zhihu",
                    title="",
                    created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),  # noqa: UP017
                ),
                Content(
                    id=newer_id,
                    tenant_id=DEFAULT_TENANT_ID,
                    project_id=uuid.UUID(project["id"]),
                    platform="zhihu",
                    title="较新目标",
                    created_at=datetime(2024, 6, 1, tzinfo=timezone.utc),  # noqa: UP017
                ),
            ]
        )
        await db_session.flush()

        result = await client.post(
            f"/api/projects/{project['id']}/repurpose",
            json={
                "source_content_id": source["id"],
                "platform": "zhihu",
                "skill_id": "demo-skill",
            },
        )
        assert result.status_code == 201
        assert result.json()["content_id"] == str(older_id)

        contents = (
            await client.get(f"/api/projects/{project['id']}/contents")
        ).json()
        zhihu = [c for c in contents if c["platform"] == "zhihu"]
        assert len(zhihu) == 2
        older = next(c for c in zhihu if c["id"] == str(older_id))
        assert older["title"] == "主稿"
    finally:
        app.dependency_overrides.pop(get_registry, None)
        app.dependency_overrides.pop(get_storage, None)


async def test_upload_rejects_control_char_filename(client, tmp_path):
    from app.main import app

    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path)
    try:
        project = (
            await client.post("/api/projects", json={"title": "非法文件名"})
        ).json()

        # httpx 会把 0x01 转义成 %01，这里手工构造 multipart 以发送真正的控制字符
        boundary = "control-char-boundary"
        control_body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="\x01bad.md"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
            "ok\r\n"
            f"--{boundary}--\r\n"
        ).encode()
        control = await client.post(
            f"/api/projects/{project['id']}/files",
            content=control_body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert control.status_code == 422
        assert control.json()["code"] == "invalid_filename"

        long_name = "文" * 70 + ".md"
        too_long = await client.post(
            f"/api/projects/{project['id']}/files",
            files={"file": (long_name, b"ok", "text/markdown")},
        )
        assert too_long.status_code == 422
        assert too_long.json()["code"] == "invalid_filename"
    finally:
        app.dependency_overrides.pop(get_storage, None)
