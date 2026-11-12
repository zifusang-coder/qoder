import json

from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.base import DEFAULT_TENANT_ID
from app.models.project import Content, ContentVersion, Project


async def _audits(session, action):
    return (
        await session.execute(
            select(AuditLog).where(AuditLog.action == action)
        )
    ).scalars().all()


async def test_account_create_and_revoke_are_audited(client, db_session):
    created = await client.post(
        "/api/accounts",
        json={
            "platform": "wechat",
            "name": "审计账号",
            "credential": {"app_id": "app-id", "app_secret": "raw-secret"},
        },
    )
    assert created.status_code == 201
    account_id = created.json()["id"]

    rows = await _audits(db_session, "account.create")
    assert len(rows) == 1
    assert rows[0].detail == {"platform": "wechat"}
    assert "raw-secret" not in json.dumps(rows[0].detail, ensure_ascii=False)

    revoked = await client.delete(f"/api/accounts/{account_id}")
    assert revoked.status_code == 204
    rows = await _audits(db_session, "account.revoke")
    assert len(rows) == 1
    assert rows[0].resource_id == account_id


async def test_provider_create_and_delete_are_audited(client, db_session):
    created = await client.post(
        "/api/settings/providers",
        json={
            "name": "审计提供商",
            "kind": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-audit-key-123456",
        },
    )
    assert created.status_code == 201, created.text
    provider_id = created.json()["id"]

    rows = await _audits(db_session, "provider.create")
    assert len(rows) == 1
    assert rows[0].resource_id == provider_id
    assert rows[0].detail == {"name": "审计提供商"}
    assert "sk-audit" not in json.dumps(rows[0].detail, ensure_ascii=False)

    deleted = await client.delete(f"/api/settings/providers/{provider_id}")
    assert deleted.status_code == 204
    rows = await _audits(db_session, "provider.delete")
    assert len(rows) == 1
    assert rows[0].resource_id == provider_id


async def test_skill_toggle_is_audited(client, db_session):
    resp = await client.patch(
        "/api/skills/article-outline/toggle", json={"enabled": False}
    )
    assert resp.status_code == 200
    rows = await _audits(db_session, "skill.toggle")
    assert len(rows) == 1
    assert rows[0].detail == {"enabled": False}


async def test_publish_create_is_audited(client, db_session):
    project = Project(tenant_id=DEFAULT_TENANT_ID, title="审计项目")
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=DEFAULT_TENANT_ID,
        project_id=project.id,
        platform="wechat",
        title="审计内容",
        current_version=1,
    )
    db_session.add(content)
    await db_session.flush()
    db_session.add(
        ContentVersion(
            tenant_id=DEFAULT_TENANT_ID,
            content_id=content.id,
            version=1,
            title="审计内容",
            body="正文内容足够长，用于通过发布任务创建。" * 10,
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/publish/jobs",
        json={"content_id": str(content.id), "platform": "wechat"},
    )
    assert resp.status_code == 201
    rows = await _audits(db_session, "publish.create")
    assert len(rows) == 1
    assert rows[0].detail == {"platform": "wechat"}


async def test_media_create_is_audited(client, db_session, tmp_path, monkeypatch):
    from tests.test_media_api import _setup, _teardown

    _setup(tmp_path, monkeypatch)
    try:
        project = (
            await client.post("/api/projects", json={"title": "媒体审计"})
        ).json()
        resp = await client.post(
            f"/api/projects/{project['id']}/media/short-video",
            json={"topic": "AI 周报", "duration_seconds": 8},
        )
        assert resp.status_code == 202, resp.text
        rows = await _audits(db_session, "media.create")
        assert len(rows) == 1
        assert rows[0].resource_id == resp.json()["id"]
        assert rows[0].detail == {"skill_id": "short-video"}
    finally:
        _teardown(monkeypatch)
