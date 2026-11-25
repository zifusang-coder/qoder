import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api import projects as projects_api
from app.db import get_session
from app.deps import get_current_user
from app.main import app
from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID
from app.models.project import Content, ContentVersion, Project
from app.models.user import User
from app.storage import Storage, StorageError


async def test_content_versions_increment(client):
    project = await client.post("/api/projects", json={"title": "AI 周报", "topic": "自动化"})
    assert project.status_code == 201
    project_id = project.json()["id"]

    content = await client.post(
        f"/api/projects/{project_id}/contents",
        json={"platform": "wechat", "kind": "article", "title": "初稿", "body": "# 初稿\n正文"},
    )
    assert content.status_code == 201
    content_id = content.json()["id"]
    assert content.json()["current_version"] == 1

    version = await client.post(
        f"/api/contents/{content_id}/versions",
        json={"title": "二稿", "body": "# 二稿\n修改后正文"},
    )
    assert version.status_code == 201
    assert version.json()["version"] == 2

    versions = await client.get(f"/api/contents/{content_id}/versions")
    assert [v["version"] for v in versions.json()["items"]] == [2, 1]


def test_storage_rejects_traversal(tmp_path: Path):
    storage = Storage(tmp_path)
    with pytest.raises(StorageError):
        storage.resolve("p1", "../../etc/passwd")


async def test_ids_are_tenant_scoped(client, db_session):
    project_id = uuid.uuid4()
    content_id = uuid.uuid4()
    db_session.add(
        Project(id=project_id, tenant_id="other-tenant", title="其他租户项目")
    )
    await db_session.flush()
    db_session.add(
        Content(
            id=content_id,
            tenant_id="other-tenant",
            project_id=project_id,
            platform="wechat",
        )
    )
    await db_session.flush()

    responses = [
        await client.get(f"/api/projects/{project_id}"),
        await client.get(f"/api/projects/{project_id}/contents"),
        await client.post(
            f"/api/projects/{project_id}/contents", json={"platform": "wechat"}
        ),
        await client.get(f"/api/contents/{content_id}/versions"),
        await client.post(f"/api/contents/{content_id}/versions", json={"body": "x"}),
    ]
    assert [r.status_code for r in responses] == [404] * 5
    assert all(r.json()["code"] == "not_found" for r in responses)


async def test_invalid_ids_return_404(client):
    project = await client.get("/api/projects/not-a-uuid")
    content = await client.get("/api/contents/not-a-uuid/versions")

    assert project.status_code == 404
    assert project.json()["code"] == "not_found"
    assert content.status_code == 404
    assert content.json()["code"] == "not_found"


async def test_input_lengths_are_validated(client):
    project = await client.post("/api/projects", json={"title": "边界测试"})
    assert project.status_code == 201
    project_id = project.json()["id"]

    too_long_platform = await client.post(
        f"/api/projects/{project_id}/contents", json={"platform": "a" * 33}
    )
    assert too_long_platform.status_code == 422

    content = await client.post(
        f"/api/projects/{project_id}/contents",
        json={"platform": "wechat", "title": "初稿", "body": "正文"},
    )
    assert content.status_code == 201
    too_long_title = await client.post(
        f"/api/contents/{content.json()['id']}/versions",
        json={"title": "a" * 301},
    )
    assert too_long_title.status_code == 422

    extra_field = await client.post("/api/projects", json={"title": "x", "oops": 1})
    assert extra_field.status_code == 422


async def test_profile_id_blank_and_unknown(client):
    blank = await client.post("/api/projects", json={"title": "无画像", "profile_id": ""})
    assert blank.status_code == 201
    assert blank.json()["profile_id"] is None

    unknown = await client.post(
        "/api/projects", json={"title": "未知画像", "profile_id": str(uuid.uuid4())}
    )
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "not_found"


async def test_version_conflict_returns_409(client, monkeypatch):
    project = await client.post("/api/projects", json={"title": "冲突测试"})
    content = await client.post(
        f"/api/projects/{project.json()['id']}/contents",
        json={"platform": "wechat", "title": "初稿", "body": "正文"},
    )
    assert content.status_code == 201

    async def fake_next_version(session, content_id):
        return 1

    monkeypatch.setattr(projects_api, "_next_version", fake_next_version)
    conflict = await client.post(
        f"/api/contents/{content.json()['id']}/versions",
        json={"title": "重复版本", "body": "正文"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "version_conflict"


def test_storage_roundtrip_and_edge_cases(tmp_path: Path):
    storage = Storage(tmp_path)
    stored = storage.save_text("p1", "drafts/a.md", "正文")
    assert stored == "drafts/a.md"
    assert storage.read_text("p1", "drafts/a.md") == "正文"
    assert storage.list_files("p1") == ["drafts/a.md"]

    with pytest.raises(StorageError):
        storage.project_dir(".")
    with pytest.raises(StorageError):
        storage.project_dir("a/b")
    with pytest.raises(StorageError):
        storage.resolve("p1", "")


@asynccontextmanager
async def _role_client(db_session, role: str):
    fake = User(
        id=uuid.uuid4(),
        tenant_id=DEFAULT_TENANT_ID,
        email=f"{role}-projects@local",
        password_hash="x",
        display_name=role,
        role=role,
        is_active=True,
        must_change_password=False,
        is_platform_admin=False,
        token_version=0,
    )
    app.dependency_overrides[get_current_user] = lambda: fake
    app.dependency_overrides[get_session] = lambda: db_session
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_session, None)


async def test_delete_content_cascades_versions_and_audits(client, db_session):
    project = await client.post("/api/projects", json={"title": "待删项目"})
    project_id = project.json()["id"]
    content = await client.post(
        f"/api/projects/{project_id}/contents",
        json={"platform": "wechat", "title": "待删内容", "body": "正文"},
    )
    content_id = content.json()["id"]
    assert content.status_code == 201

    deleted = await client.delete(f"/api/contents/{content_id}")
    assert deleted.status_code == 204

    remaining = await client.get(f"/api/projects/{project_id}/contents")
    assert remaining.json() == []
    versions = (
        await db_session.execute(
            select(ContentVersion).where(
                ContentVersion.content_id == uuid.UUID(content_id)
            )
        )
    ).scalars().all()
    assert versions == []
    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "content.delete")
        )
    ).scalars().all()
    assert [row.resource_id for row in audits] == [content_id]
    assert audits[0].detail["title"] == "待删内容"

    again = await client.delete(f"/api/contents/{content_id}")
    assert again.status_code == 404


async def test_delete_content_is_tenant_scoped_and_role_gated(client, db_session):
    foreign_project = uuid.uuid4()
    foreign_content = uuid.uuid4()
    db_session.add(
        Project(id=foreign_project, tenant_id="other-tenant", title="外部项目")
    )
    await db_session.flush()
    db_session.add(
        Content(
            id=foreign_content,
            tenant_id="other-tenant",
            project_id=foreign_project,
            platform="wechat",
        )
    )
    await db_session.commit()

    cross = await client.delete(f"/api/contents/{foreign_content}")
    assert cross.status_code == 404
    invalid = await client.delete("/api/contents/not-a-uuid")
    assert invalid.status_code == 404

    async with _role_client(db_session, "viewer") as viewer:
        denied = await viewer.delete(f"/api/contents/{foreign_content}")
        assert denied.status_code == 403
        assert denied.json()["code"] == "forbidden"


async def test_delete_project_cascades_contents_and_audits(client, db_session):
    project = await client.post("/api/projects", json={"title": "待删项目二"})
    project_id = project.json()["id"]
    content = await client.post(
        f"/api/projects/{project_id}/contents",
        json={"platform": "wechat", "title": "子内容", "body": "正文"},
    )
    content_id = content.json()["id"]

    deleted = await client.delete(f"/api/projects/{project_id}")
    assert deleted.status_code == 204

    assert (await client.get(f"/api/projects/{project_id}")).status_code == 404
    left = (
        await db_session.execute(
            select(Content).where(Content.id == uuid.UUID(content_id))
        )
    ).scalar_one_or_none()
    assert left is None
    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "project.delete")
        )
    ).scalars().all()
    assert [row.resource_id for row in audits] == [project_id]
    assert audits[0].detail["title"] == "待删项目二"

    again = await client.delete(f"/api/projects/{project_id}")
    assert again.status_code == 404


async def test_delete_project_is_tenant_scoped_and_role_gated(client, db_session):
    foreign_project = uuid.uuid4()
    db_session.add(
        Project(id=foreign_project, tenant_id="other-tenant", title="外部项目二")
    )
    await db_session.commit()

    cross = await client.delete(f"/api/projects/{foreign_project}")
    assert cross.status_code == 404
    invalid = await client.delete("/api/projects/not-a-uuid")
    assert invalid.status_code == 404

    async with _role_client(db_session, "viewer") as viewer:
        denied = await viewer.delete(f"/api/projects/{foreign_project}")
        assert denied.status_code == 403
        assert denied.json()["code"] == "forbidden"
