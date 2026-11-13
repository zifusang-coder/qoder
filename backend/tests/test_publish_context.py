import json
import uuid

import pytest
from sqlalchemy import select

from app.config import DEFAULT_TENANT_ID
from app.models.project import ContentVersion
from app.models.publish import PlatformAccount
from app.publish.base import PublishConfigError, PublishContent, PublishResult
from app.publish.wechat import WeChatMPAdapter
from app.security import decrypt_secret
from app.services.publish import apply_action, execute_job
from app.storage import StorageBackend

DOUYIN_CREDENTIAL = {
    "client_key": "ck-1",
    "client_secret": "cs-1",
    "open_id": "open-1",
    "refresh_token": "rt-1",
}


class FakeStorage(StorageBackend):
    def save_text(self, project_id, relpath, text):
        raise NotImplementedError

    def save_bytes(self, project_id, relpath, data):
        raise NotImplementedError

    def read_text(self, project_id, relpath):
        raise NotImplementedError

    def read_bytes(self, project_id, relpath):
        return b"video-bytes"

    def list_files(self, project_id):
        raise NotImplementedError


class CapturingAdapter:
    platform = "douyin"
    capability = "auto"

    def __init__(self):
        self.calls: list[PublishContent] = []

    async def publish(self, *, account, content, storage):
        assert isinstance(storage, FakeStorage)
        self.calls.append(content)
        return PublishResult(
            external_id="video-1",
            credential_patch={"access_token": "fresh-token"},
        )


async def _create_douyin_job(client, db_session):
    project = await client.post(
        "/api/projects", json={"title": f"上下文-{uuid.uuid4().hex[:8]}"}
    )
    assert project.status_code == 201
    content = await client.post(
        f"/api/projects/{project.json()['id']}/contents",
        json={
            "platform": "douyin",
            "kind": "video",
            "title": "视频标题",
            "body": "视频正文",
        },
    )
    assert content.status_code == 201, content.text
    account = await client.post(
        "/api/accounts",
        json={
            "platform": "douyin",
            "name": f"抖音号-{uuid.uuid4().hex[:6]}",
            "credential": DOUYIN_CREDENTIAL,
            "meta": {},
        },
    )
    assert account.status_code == 201, account.text
    job = await client.post(
        "/api/publish/jobs",
        json={
            "content_id": content.json()["id"],
            "platform": "douyin",
            "account_id": account.json()["id"],
        },
    )
    assert job.status_code == 201, job.text

    version = (
        await db_session.execute(
            select(ContentVersion).where(
                ContentVersion.content_id == uuid.UUID(content.json()["id"])
            )
        )
    ).scalar_one()
    version.meta = {
        "files": [
            {
                "name": "video.mp4",
                "path": "media/run/video.mp4",
                "kind": "video",
                "size": 10,
            }
        ],
        "cover": "media/run/cover.png",
    }
    await db_session.commit()

    job_id = uuid.UUID(job.json()["id"])
    for action in ("submit", "pass", "approve"):
        await apply_action(
            db_session,
            job_id,
            action,
            tenant_id=DEFAULT_TENANT_ID,
            actor="tester",
        )
    return job_id, account.json()["id"]


async def test_execute_job_passes_content_context(client, db_session):
    job_id, account_id = await _create_douyin_job(client, db_session)
    adapter = CapturingAdapter()

    job = await execute_job(
        db_session,
        job_id,
        adapter,
        tenant_id=DEFAULT_TENANT_ID,
        actor="tester",
        storage=FakeStorage(),
    )

    assert job.status == "published"
    assert job.external_id == "video-1"
    content = adapter.calls[0]
    assert content.title == "视频标题"
    assert content.body == "视频正文"
    assert content.kind == "video"
    assert content.project_id
    assert [item.path for item in content.media] == ["media/run/video.mp4"]
    assert content.media[0].kind == "video"
    assert content.cover_path == "media/run/cover.png"

    db_session.expire_all()
    account = (
        await db_session.execute(
            select(PlatformAccount).where(
                PlatformAccount.id == uuid.UUID(account_id)
            )
        )
    ).scalar_one()
    stored = json.loads(decrypt_secret(account.credential_enc))
    assert stored["access_token"] == "fresh-token"
    assert "fresh-token" not in account.credential_enc


async def test_credential_patch_failure_keeps_published(
    client, db_session, monkeypatch
):
    job_id, _account_id = await _create_douyin_job(client, db_session)

    def boom(account, patch):
        raise RuntimeError("write failed")

    monkeypatch.setattr("app.services.publish.merge_credential_patch", boom)

    job = await execute_job(
        db_session,
        job_id,
        CapturingAdapter(),
        tenant_id=DEFAULT_TENANT_ID,
        actor="tester",
        storage=FakeStorage(),
    )

    assert job.status == "published"
    assert job.error == ""


async def test_wechat_rejects_video_content_without_http():
    account = PlatformAccount(
        tenant_id=DEFAULT_TENANT_ID,
        platform="wechat",
        name="公众号",
        credential_enc="unused",
        status="active",
        meta={"thumb_media_id": "thumb-1"},
    )
    content = PublishContent(
        project_id="p",
        title="标题",
        body="正文",
        kind="video",
    )

    with pytest.raises(PublishConfigError, match="公众号仅支持图文内容"):
        await WeChatMPAdapter().publish(
            account=account, content=content, storage=FakeStorage()
        )
