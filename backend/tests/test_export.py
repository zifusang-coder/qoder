import asyncio
import io
import json
import threading
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from app.models.project import Content, ContentVersion, Project
from app.models.publish import PublishEvent, PublishJob
from app.publish.base import PLATFORMS
from app.publish.export import GUIDES, MANUAL_EXPORT_PLATFORMS, ExportAdapter
from app.services.publish import EXPORT_IN_FLIGHT_MESSAGE
from app.storage import Storage, StorageError, get_storage

OTHER_TENANT = "00000000-0000-0000-0000-000000000002"
BODY = "这是一段用于导出测试的中文正文，包含配图说明与话题标签。"


@pytest.fixture
def storage(tmp_path):
    from app.main import app

    instance = Storage(tmp_path / "outputs")
    app.dependency_overrides[get_storage] = lambda: instance
    try:
        yield instance
    finally:
        app.dependency_overrides.pop(get_storage, None)


class BlockingStorage(Storage):
    def __init__(self, root):
        super().__init__(root)
        self.started = threading.Event()
        self.release = threading.Event()

    def save_bytes(self, project_id, relpath, data):
        self.started.set()
        assert self.release.wait(10)
        return super().save_bytes(project_id, relpath, data)


async def _create_job(
    client, *, platform="xiaohongshu", title="测试标题", body=BODY
):
    project = await client.post(
        "/api/projects", json={"title": f"导出测试-{uuid.uuid4().hex[:8]}"}
    )
    assert project.status_code == 201
    project_body = project.json()
    kind = "video" if platform in {"douyin", "toutiao", "bilibili"} else "article"
    content = await client.post(
        f"/api/projects/{project_body['id']}/contents",
        json={
            "platform": platform,
            "kind": kind,
            "title": title,
            "body": body,
        },
    )
    assert content.status_code == 201
    content_body = content.json()
    payload = {
        "content_id": content_body["id"],
        "platform": platform,
    }
    if platform in {"wechat", "douyin", "toutiao"}:
        account = await client.post(
            "/api/accounts",
            json={
                "platform": platform,
                "name": f"导出账号-{uuid.uuid4().hex[:6]}",
                "credential": _credentials(platform),
            },
        )
        assert account.status_code == 201, account.text
        payload["account_id"] = account.json()["id"]
    job = await client.post("/api/publish/jobs", json=payload)
    assert job.status_code == 201, job.text
    return project_body, content_body, job.json()


def _credentials(platform: str) -> dict:
    if platform == "wechat":
        return {"app_id": "wx-id", "app_secret": "wx-secret"}
    return {
        "client_key": "ck-id",
        "client_secret": "cs-secret",
        "open_id": "open-id",
        "refresh_token": "rt-secret",
    }


async def _export(client, job_id):
    return await client.post(f"/api/publish/jobs/{job_id}/export")


async def _precheck(client, job_id):
    response = await client.post(f"/api/publish/jobs/{job_id}/precheck")
    assert response.status_code == 200
    assert response.json()["job"]["status"] == "awaiting_review"
    return response.json()["job"]


async def _act(client, job_id, action):
    return await client.post(
        f"/api/publish/jobs/{job_id}/actions", json={"action": action}
    )


async def _stored_job(db_session, job_id):
    db_session.expire_all()
    return (
        await db_session.execute(
            select(PublishJob).where(PublishJob.id == uuid.UUID(str(job_id)))
        )
    ).scalar_one()


async def _events(db_session, job_id):
    return (
        (
            await db_session.execute(
                select(PublishEvent).where(
                    PublishEvent.job_id == uuid.UUID(str(job_id))
                )
            )
        )
        .scalars()
        .all()
    )


def _zip(path):
    return zipfile.ZipFile(io.BytesIO(path.read_bytes()))


async def test_export_creates_zip(client, db_session, storage):
    project, content, job = await _create_job(client)

    response = await _export(client, job["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "exported"
    assert body["export_path"].startswith("exports/xiaohongshu-")
    assert body["export_path"].endswith("-v1.zip")
    assert "tenant_id" not in body

    path = storage.root / project["id"] / body["export_path"]
    assert path.is_file()
    with _zip(path) as archive:
        assert sorted(archive.namelist()) == [
            "content.md",
            "manifest.json",
            "publish_guide.md",
        ]
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        content_md = archive.read("content.md").decode("utf-8")
        guide_md = archive.read("publish_guide.md").decode("utf-8")

    assert manifest["platform"] == "xiaohongshu"
    assert manifest["title"] == "测试标题"
    assert manifest["content_id"] == content["id"]
    assert manifest["job_id"] == job["id"]
    assert manifest["content_version"] == 1
    assert manifest["checklist"] == GUIDES["xiaohongshu"]
    assert manifest["exported_at"].endswith("Z")
    db_session.expire_all()
    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "exported"
    assert stored.export_path == body["export_path"]
    assert BODY in content_md
    assert "1. " in guide_md
    assert GUIDES["xiaohongshu"][0] in guide_md


async def test_export_pins_content_version(client, db_session, storage):
    project, content, job = await _create_job(client)
    second = await client.post(
        f"/api/contents/{content['id']}/versions",
        json={"title": "第二版标题", "body": "第二版正文"},
    )
    assert second.status_code == 201
    assert second.json()["version"] == 2

    response = await _export(client, job["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "exported"
    assert body["content_version"] == 1

    path = storage.root / project["id"] / body["export_path"]
    assert path.is_file()
    with _zip(path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        content_md = archive.read("content.md").decode("utf-8")
    assert manifest["content_version"] == 1
    assert BODY in content_md
    assert "测试标题" in content_md
    assert "第二版正文" not in content_md
    assert "第二版标题" not in content_md


async def test_export_exported_is_idempotent(client, db_session, storage):
    _, _, job = await _create_job(client)
    first = await _export(client, job["id"])
    assert first.status_code == 200

    second = await _export(client, job["id"])
    assert second.status_code == 200
    assert second.json()["status"] == "exported"
    assert second.json()["export_path"] == first.json()["export_path"]
    assert len(list(storage.root.rglob("*.zip"))) == 1


async def test_export_from_awaiting_review(client, db_session, storage):
    _, _, job = await _create_job(client)
    prechecked = await _precheck(client, job["id"])
    assert prechecked["status"] == "awaiting_review"

    response = await _export(client, job["id"])
    assert response.status_code == 200
    assert response.json()["status"] == "exported"

    actions = [event.action for event in await _events(db_session, job["id"])]
    assert actions[-2:] == ["export", "export_done"]


async def test_export_from_approved(client, db_session, storage):
    _, _, job = await _create_job(client)
    await _precheck(client, job["id"])
    approved = await _act(client, job["id"], "approve")
    assert approved.status_code == 200

    response = await _export(client, job["id"])
    assert response.status_code == 200
    assert response.json()["status"] == "exported"


async def test_export_unsupported_platforms(client, db_session, storage):
    expected = set(PLATFORMS) - set(MANUAL_EXPORT_PLATFORMS)
    assert expected
    for platform in sorted(expected):
        _, _, job = await _create_job(client, platform=platform)
        response = await _export(client, job["id"])
        assert response.status_code == 409
        assert response.json()["code"] == "export_unsupported"
        stored = await _stored_job(db_session, job["id"])
        assert stored.status == "draft"
        assert stored.export_path == ""


class FailingStorage(Storage):
    def save_bytes(self, project_id: str, relpath: str, data: bytes) -> str:
        raise StorageError("disk full at /tmp/secret")


async def test_export_failed_then_retry_resumes(client, db_session, storage):
    from app.main import app

    project, _, job = await _create_job(client)
    failing = FailingStorage(storage.root)
    app.dependency_overrides[get_storage] = lambda: failing

    response = await _export(client, job["id"])
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "export_failed"
    assert body["message"] == "导出失败，请稍后重试"
    assert "disk full" not in response.text
    assert "secret" not in response.text

    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "export_failed"
    assert stored.error == "导出失败，请稍后重试"

    retried = await _act(client, job["id"], "retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "exporting"

    app.dependency_overrides[get_storage] = lambda: storage
    response = await _export(client, job["id"])
    assert response.status_code == 200
    exported = response.json()
    assert exported["status"] == "exported"
    assert (storage.root / project["id"] / exported["export_path"]).is_file()

    actions = [event.action for event in await _events(db_session, job["id"])]
    assert actions == ["create", "export", "fail", "retry", "export_done"]
    stored = await _stored_job(db_session, job["id"])
    assert stored.error == ""


async def test_export_failure_race_returns_exported_job(
    client, db_session, storage, monkeypatch
):
    import app.services.publish as publish_service
    from app.main import app
    from app.publish.state import InvalidTransition

    original = publish_service.apply_action

    async def racy(session, job_id, action, **kwargs):
        if action == "fail":
            await session.execute(
                update(PublishJob)
                .where(PublishJob.id == job_id)
                .values(status="exported", export_path="exports/raced.zip")
            )
            await session.commit()
            raise InvalidTransition("另一路径已完成导出")
        return await original(session, job_id, action, **kwargs)

    monkeypatch.setattr(publish_service, "apply_action", racy)

    _, _, job = await _create_job(client)
    failing = FailingStorage(storage.root)
    app.dependency_overrides[get_storage] = lambda: failing

    response = await _export(client, job["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "exported"
    assert body["export_path"] == "exports/raced.zip"


async def test_export_in_progress_returns_409(client, db_session, storage):
    _, _, job = await _create_job(client)
    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == uuid.UUID(job["id"]))
        .values(
            status="exporting",
            error=EXPORT_IN_FLIGHT_MESSAGE,
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    response = await _export(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "export_in_progress"
    assert list(storage.root.rglob("*.zip")) == []


async def test_export_resumes_stale_export(client, db_session, storage):
    project, _, job = await _create_job(client)
    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == uuid.UUID(job["id"]))
        .values(
            status="exporting",
            error=EXPORT_IN_FLIGHT_MESSAGE,
            updated_at=datetime.now(UTC) - timedelta(seconds=120),
        )
    )
    await db_session.commit()

    response = await _export(client, job["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "exported"
    assert (storage.root / project["id"] / body["export_path"]).is_file()


async def test_export_second_call_while_in_flight_returns_409(
    client, db_session, storage
):
    from app.main import app

    _, _, job = await _create_job(client)
    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == uuid.UUID(job["id"]))
        .values(
            status="exporting",
            error=EXPORT_IN_FLIGHT_MESSAGE,
            updated_at=datetime.now(UTC) - timedelta(seconds=120),
        )
    )
    await db_session.commit()

    blocking = BlockingStorage(storage.root)
    app.dependency_overrides[get_storage] = lambda: blocking

    first = asyncio.create_task(_export(client, job["id"]))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while not blocking.started.is_set():
        assert loop.time() < deadline, "导出未在超时前开始"
        await asyncio.sleep(0.01)
    # 续租（含行锁）必须在进入 I/O 前持久提交，否则第二个请求的 409 只是同会话可见
    assert not db_session.in_transaction()
    try:
        second = await asyncio.wait_for(_export(client, job["id"]), timeout=5)
    finally:
        blocking.release.set()
    assert second.status_code == 409
    assert second.json()["code"] == "export_in_progress"

    done = await first
    assert done.status_code == 200
    assert done.json()["status"] == "exported"


async def test_export_tolerates_failure_after_commit(
    client, db_session, storage, monkeypatch
):
    import app.services.publish as publish_service

    original = publish_service.apply_action

    async def after_commit_failure(session, job_id, action, **kwargs):
        result = await original(session, job_id, action, **kwargs)
        if action == "export_done":
            raise RuntimeError("refresh failed after commit")
        return result

    monkeypatch.setattr(publish_service, "apply_action", after_commit_failure)

    project, _, job = await _create_job(client)
    response = await _export(client, job["id"])
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "exported"

    zips = list(storage.root.rglob("*.zip"))
    assert len(zips) == 1
    assert list(storage.root.rglob("*.tmp")) == []

    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "exported"
    assert stored.export_path
    referenced = storage.root / project["id"] / stored.export_path
    assert referenced.is_file()
    assert referenced == zips[0]


async def test_export_finish_failure_leaves_reusable_bundle(
    client, db_session, storage, monkeypatch
):
    import app.services.publish as publish_service

    original = publish_service.apply_action

    async def flaky(session, job_id, action, **kwargs):
        if action == "export_done":
            raise RuntimeError("commit failed")
        return await original(session, job_id, action, **kwargs)

    monkeypatch.setattr(publish_service, "apply_action", flaky)

    project, _, job = await _create_job(client)
    job_hex = job["id"].replace("-", "")
    expected_relpath = f"exports/xiaohongshu-{job_hex}-v1.zip"
    expected_path = storage.root / project["id"] / expected_relpath

    response = await _export(client, job["id"])
    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "export_failed"
    assert body["message"] == "导出失败，请稍后重试"

    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "export_failed"
    assert stored.error == "导出失败，请稍后重试"

    # 确定性路径上的产物保留下来供下一次重试复用
    assert expected_path.is_file()
    assert list(storage.root.rglob("*.tmp")) == []
    assert [p.name for p in storage.root.rglob("*.zip")] == [expected_path.name]

    monkeypatch.undo()

    retried = await _act(client, job["id"], "retry")
    assert retried.status_code == 200
    assert retried.json()["status"] == "exporting"

    response = await _export(client, job["id"])
    assert response.status_code == 200
    exported = response.json()
    assert exported["status"] == "exported"
    assert exported["export_path"] == expected_relpath

    zips = list(storage.root.rglob("*.zip"))
    assert zips == [expected_path]
    with _zip(expected_path) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    assert manifest["job_id"] == job["id"]
    assert manifest["content_version"] == 1


async def test_export_missing_version_returns_409(client, db_session, storage):
    _, content, job = await _create_job(client)
    await db_session.execute(
        delete(ContentVersion).where(
            ContentVersion.content_id == uuid.UUID(content["id"])
        )
    )
    await db_session.commit()

    response = await _export(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "empty_content"

    stored = await _stored_job(db_session, job["id"])
    assert stored.status == "draft"
    assert stored.export_path == ""


async def test_export_foreign_or_malformed_job_returns_404(
    client, db_session, storage
):
    project = Project(tenant_id=OTHER_TENANT, title="外部项目")
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=OTHER_TENANT,
        project_id=project.id,
        platform="xiaohongshu",
        title="外部内容",
        current_version=1,
    )
    db_session.add(content)
    await db_session.flush()
    db_session.add(
        ContentVersion(
            tenant_id=OTHER_TENANT,
            content_id=content.id,
            version=1,
            title="外部内容",
            body="外部正文",
        )
    )
    job = PublishJob(
        tenant_id=OTHER_TENANT,
        content_id=content.id,
        content_version=1,
        platform="xiaohongshu",
        status="draft",
        idempotency_key=f"foreign-{uuid.uuid4().hex}",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    response = await _export(client, job.id)
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"

    response = await _export(client, "not-a-uuid")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_export_rejects_terminal_and_failed_states(
    client, db_session, storage
):
    _, _, job = await _create_job(client)
    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == uuid.UUID(job["id"]))
        .values(status="published")
    )
    await db_session.commit()

    response = await _export(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_state"

    await db_session.execute(
        update(PublishJob)
        .where(PublishJob.id == uuid.UUID(job["id"]))
        .values(status="export_failed")
    )
    await db_session.commit()

    response = await _export(client, job["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_state"
    assert list(storage.root.rglob("*.zip")) == []


async def test_export_paths_are_unique(client, db_session, storage):
    first_project, _, first = await _create_job(client)
    second_project, _, second = await _create_job(client)

    first_response = await _export(client, first["id"])
    second_response = await _export(client, second["id"])
    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_path = first_response.json()["export_path"]
    second_path = second_response.json()["export_path"]
    assert first_path != second_path

    for project, relpath in (
        (first_project, first_path),
        (second_project, second_path),
    ):
        assert (storage.root / project["id"] / relpath).is_file()


async def test_list_files_ignores_only_export_temp_files(storage):
    project_id = str(uuid.uuid4())
    storage.save_text(project_id, "notes.tmp", "keep")

    exports = storage.project_dir(project_id) / "exports"
    exports.mkdir(parents=True)
    (exports / f"bundle.zip.{uuid.uuid4().hex}.tmp").write_text("junk")

    assert storage.list_files(project_id) == ["notes.tmp"]


def test_adapter_guides_per_platform(tmp_path):
    for platform, steps in GUIDES.items():
        project_id = str(uuid.uuid4())
        job_id = str(uuid.uuid4())
        adapter = ExportAdapter(Storage(tmp_path))
        relpath = adapter.export(
            project_id=project_id,
            platform=platform,
            job_id=job_id,
            content_id=str(uuid.uuid4()),
            content_version=3,
            title="导出标题",
            body=BODY,
        )
        path = (tmp_path / project_id / relpath).resolve()
        assert path.is_file()

        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            guide = archive.read("publish_guide.md").decode("utf-8")
        assert manifest["checklist"] == steps
        assert manifest["content_version"] == 3
        assert manifest["exported_at"].endswith("Z")
        for index, step in enumerate(steps, start=1):
            assert f"{index}. {step}" in guide
