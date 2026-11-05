import uuid
from pathlib import Path

from app.config import get_settings
from app.errors import AppError
from app.llm.types import Usage
from app.queue import get_queue
from app.services import media as media_module
from app.services.media import run_media_job
from app.skills.registry import SkillRegistry
from app.storage import Storage

FIXTURE = Path(__file__).parent / "fixtures" / "media-skills"


class FakeGateway:
    last_usage = Usage(4, 6)

    async def complete(self, messages, **kwargs):
        return (
            '{"title": "测试短视频", "narration": "测试口播", '
            '"hashtags": [], "scenes": [{"text": "分镜", "seconds": 4}]}'
        )


class RateLimitedGateway:
    last_usage = Usage()

    async def complete(self, messages, **kwargs):
        raise AppError("llm_rate_limited", "LLM 提供商限流，请稍后重试", 502)


class UnavailableGateway:
    last_usage = Usage()

    async def complete(self, messages, **kwargs):
        raise AppError("llm_unavailable", "LLM 服务不可用", 502)


class FakeQueue:
    def __init__(self):
        self.jobs: list[tuple[str, str]] = []

    async def enqueue_job(self, name: str, job_id: str) -> None:
        self.jobs.append((name, job_id))


class DownQueue:
    async def enqueue_job(self, name: str, job_id: str) -> None:
        raise RuntimeError("redis down")


_UNSET = object()


def _setup(tmp_path, monkeypatch, gateway_factory=FakeGateway, queue=_UNSET):
    from app.main import app
    from app.skills.registry import get_registry
    from app.storage import get_storage

    monkeypatch.setenv("OUTPUTS_DIR", str(tmp_path / "outputs"))
    get_settings.cache_clear()
    registry = SkillRegistry(FIXTURE)
    storage = Storage(tmp_path / "storage")
    if queue is _UNSET:
        queue = FakeQueue()
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_queue] = lambda: queue

    async def fake_gateway(session, tenant_id):
        return gateway_factory()

    monkeypatch.setattr(media_module, "load_gateway_default", fake_gateway)
    return registry, storage, queue


def _teardown(monkeypatch):
    from app.main import app
    from app.skills.registry import get_registry
    from app.storage import get_storage

    app.dependency_overrides.pop(get_registry, None)
    app.dependency_overrides.pop(get_storage, None)
    app.dependency_overrides.pop(get_queue, None)
    monkeypatch.delenv("OUTPUTS_DIR", raising=False)
    get_settings.cache_clear()


async def _create_project(client, title: str) -> str:
    project = (await client.post("/api/projects", json={"title": title})).json()
    return project["id"]


async def test_media_generation_queues_then_imports_files_and_previews(
    client, db_session, tmp_path, monkeypatch
):
    registry, storage, queue = _setup(tmp_path, monkeypatch)
    try:
        project_id = await _create_project(client, "媒体")
        response = await client.post(
            f"/api/projects/{project_id}/media/short-video",
            json={"topic": "AI 周报", "duration_seconds": 8},
        )
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["status"] == "queued"
        assert queue.jobs == [("process_media_job", job["id"])]

        queued = await client.get(
            f"/api/projects/{project_id}/media/jobs/{job['id']}"
        )
        assert queued.status_code == 200
        assert queued.json()["status"] == "queued"

        processed = await run_media_job(
            session=db_session,
            job_id=job["id"],
            registry=registry,
            storage=storage,
        )
        assert processed.status == "succeeded"

        finished = await client.get(
            f"/api/projects/{project_id}/media/jobs/{job['id']}"
        )
        assert finished.status_code == 200
        finished_job = finished.json()
        assert finished_job["status"] == "succeeded"
        assert finished_job["content_id"]
        assert finished_job["error_code"] == ""

        listed = await client.get(f"/api/projects/{project_id}/media")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        media = items[0]
        assert media["content_id"] == finished_job["content_id"]
        assert media["kind"] == "video"
        assert media["title"] == "测试短视频"
        assert media["duration_seconds"] == 8
        assert {item["name"] for item in media["files"]} == {
            "video.mp4",
            "cover.png",
            "subtitles.srt",
            "script.txt",
        }
        preview = media["preview"]
        assert preview == f"media/{media['run_id']}/video.mp4"
        stored = (
            tmp_path / "storage" / project_id / f"media/{media['run_id']}/video.mp4"
        )
        assert stored.read_bytes() == b"fake-mp4"

        video = await client.get(
            f"/api/projects/{project_id}/files/preview", params={"path": preview}
        )
        assert video.status_code == 200
        assert video.headers["content-type"] == "video/mp4"
        assert video.headers["content-disposition"].startswith("inline;")
        assert video.headers["x-content-type-options"] == "nosniff"
        assert video.content == b"fake-mp4"

        caption = await client.get(
            f"/api/projects/{project_id}/files/preview",
            params={"path": media["subtitles"]},
        )
        assert caption.status_code == 200
        assert caption.headers["content-type"].startswith("text/plain")
        assert "hello" in caption.text
    finally:
        _teardown(monkeypatch)


async def test_media_tts_creates_audio_content(
    client, db_session, tmp_path, monkeypatch
):
    registry, storage, _ = _setup(tmp_path, monkeypatch)
    try:
        project_id = await _create_project(client, "配音")
        response = await client.post(
            f"/api/projects/{project_id}/media/tts-voiceover",
            json={"text": "这是一段口播稿", "title": "配音"},
        )
        assert response.status_code == 202, response.text
        job = response.json()
        await run_media_job(
            session=db_session,
            job_id=job["id"],
            registry=registry,
            storage=storage,
        )

        listed = await client.get(f"/api/projects/{project_id}/media")
        media = listed.json()["items"][0]
        assert media["kind"] == "audio"
        assert media["preview"].endswith("/voiceover.mp3")

        audio = await client.get(
            f"/api/projects/{project_id}/files/preview",
            params={"path": media["preview"]},
        )
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/mpeg"
        assert audio.content == b"fake-mp3"
    finally:
        _teardown(monkeypatch)


async def test_media_tts_falls_back_to_raw_text_when_llm_is_rate_limited(
    client, db_session, tmp_path, monkeypatch
):
    registry, storage, _ = _setup(
        tmp_path, monkeypatch, gateway_factory=RateLimitedGateway
    )
    try:
        project_id = await _create_project(client, "配音降级")
        response = await client.post(
            f"/api/projects/{project_id}/media/tts-voiceover",
            json={"text": "这是一段原始口播稿", "title": "降级"},
        )
        assert response.status_code == 202, response.text
        job = response.json()

        processed = await run_media_job(
            session=db_session,
            job_id=job["id"],
            registry=registry,
            storage=storage,
        )
        assert processed.status == "succeeded"

        media = (await client.get(f"/api/projects/{project_id}/media")).json()[
            "items"
        ][0]
        assert media["body"] == "这是一段原始口播稿"
        assert media["audio"]["llm_fallback"] == "llm_rate_limited"
    finally:
        _teardown(monkeypatch)


async def test_failed_media_job_can_be_retried(
    client, db_session, tmp_path, monkeypatch
):
    registry, storage, queue = _setup(
        tmp_path, monkeypatch, gateway_factory=UnavailableGateway
    )
    try:
        project_id = await _create_project(client, "重试")
        response = await client.post(
            f"/api/projects/{project_id}/media/short-video",
            json={"topic": "AI 周报", "duration_seconds": 8},
        )
        assert response.status_code == 202, response.text
        job = response.json()
        await run_media_job(
            session=db_session,
            job_id=job["id"],
            registry=registry,
            storage=storage,
        )

        failed = await client.get(
            f"/api/projects/{project_id}/media/jobs/{job['id']}"
        )
        assert failed.json()["status"] == "failed"
        assert failed.json()["error_code"] == "llm_unavailable"

        retried = await client.post(
            f"/api/projects/{project_id}/media/jobs/{job['id']}/retry"
        )
        assert retried.status_code == 202, retried.text
        assert retried.json()["status"] == "queued"
        assert len(queue.jobs) == 2
        assert queue.jobs[1] == ("process_media_job", job["id"])
    finally:
        _teardown(monkeypatch)


async def test_media_queue_failure_marks_job_failed(
    client, db_session, tmp_path, monkeypatch
):
    from sqlalchemy import select

    from app.models.media import MediaJob

    _setup(tmp_path, monkeypatch, queue=DownQueue())
    try:
        project_id = await _create_project(client, "队列不可用")
        response = await client.post(
            f"/api/projects/{project_id}/media/tts-voiceover",
            json={"text": "这是一段口播稿"},
        )
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "queue_unavailable"

        job = (
            await db_session.execute(
                select(MediaJob).where(
                    MediaJob.project_id == uuid.UUID(project_id)
                )
            )
        ).scalar_one()
        assert job.status == "failed"
        assert job.error_code == "queue_unavailable"
    finally:
        _teardown(monkeypatch)


async def test_media_queue_creation_failure_marks_job_failed(
    client, db_session, tmp_path, monkeypatch
):
    from sqlalchemy import select

    from app.models.media import MediaJob

    _setup(tmp_path, monkeypatch, queue=None)
    try:
        project_id = await _create_project(client, "队列依赖不可用")
        response = await client.post(
            f"/api/projects/{project_id}/media/tts-voiceover",
            json={"text": "这是一段口播稿"},
        )
        assert response.status_code == 503, response.text
        assert response.json()["code"] == "queue_unavailable"

        job = (
            await db_session.execute(
                select(MediaJob).where(
                    MediaJob.project_id == uuid.UUID(project_id)
                )
            )
        ).scalar_one()
        assert job.status == "failed"
        assert job.error_code == "queue_unavailable"
    finally:
        _teardown(monkeypatch)


async def test_media_rejects_unknown_skill_and_missing_topic(
    client, tmp_path, monkeypatch
):
    _setup(tmp_path, monkeypatch)
    try:
        project_id = await _create_project(client, "校验")
        unknown = await client.post(
            f"/api/projects/{project_id}/media/copywriting",
            json={"topic": "x"},
        )
        assert unknown.status_code == 404

        missing = await client.post(
            f"/api/projects/{project_id}/media/short-video",
            json={"topic": "  "},
        )
        assert missing.status_code == 422
        assert missing.json()["code"] == "skill_inputs_invalid"
    finally:
        _teardown(monkeypatch)


async def test_preview_rejects_unsupported_type_and_escape(
    client, tmp_path, monkeypatch
):
    _setup(tmp_path, monkeypatch)
    try:
        project_id = await _create_project(client, "预览")
        uploaded = await client.post(
            f"/api/projects/{project_id}/files",
            files={"file": ("page.html", b"<script>alert(1)</script>", "text/html")},
        )
        assert uploaded.status_code == 201

        unsupported = await client.get(
            f"/api/projects/{project_id}/files/preview",
            params={"path": uploaded.json()["path"]},
        )
        assert unsupported.status_code == 415
        assert unsupported.json()["code"] == "unsupported_media_type"

        escape = await client.get(
            f"/api/projects/{project_id}/files/preview",
            params={"path": "../../etc/passwd.mp4"},
        )
        assert escape.status_code == 422
        assert escape.json()["code"] == "invalid_path"
    finally:
        _teardown(monkeypatch)
