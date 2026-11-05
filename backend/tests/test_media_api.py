from pathlib import Path

from app.config import get_settings
from app.llm.types import Usage
from app.services import media as media_module
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


def _setup(tmp_path, monkeypatch):
    from app.main import app
    from app.skills.registry import get_registry
    from app.storage import get_storage

    monkeypatch.setenv("OUTPUTS_DIR", str(tmp_path / "outputs"))
    get_settings.cache_clear()
    app.dependency_overrides[get_registry] = lambda: SkillRegistry(FIXTURE)
    app.dependency_overrides[get_storage] = lambda: Storage(tmp_path / "storage")

    async def fake_gateway(session, tenant_id):
        return FakeGateway()

    monkeypatch.setattr(media_module, "load_gateway_default", fake_gateway)


def _teardown():
    from app.main import app
    from app.skills.registry import get_registry
    from app.storage import get_storage

    app.dependency_overrides.pop(get_registry, None)
    app.dependency_overrides.pop(get_storage, None)


async def test_media_generation_imports_files_and_previews(client, tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        project = (await client.post("/api/projects", json={"title": "媒体"})).json()
        project_id = project["id"]
        response = await client.post(
            f"/api/projects/{project_id}/media/short-video",
            json={"topic": "AI 周报", "duration_seconds": 8},
        )
        assert response.status_code == 201, response.text
        media = response.json()
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

        listed = await client.get(f"/api/projects/{project_id}/media")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["content_id"] == media["content_id"]
        assert items[0]["preview"] == preview

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
        _teardown()
        monkeypatch.delenv("OUTPUTS_DIR", raising=False)
        get_settings.cache_clear()


async def test_media_tts_creates_audio_content(client, tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        project = (await client.post("/api/projects", json={"title": "配音"})).json()
        response = await client.post(
            f"/api/projects/{project['id']}/media/tts-voiceover",
            json={"text": "这是一段口播稿", "title": "配音"},
        )
        assert response.status_code == 201, response.text
        media = response.json()
        assert media["kind"] == "audio"
        assert media["preview"].endswith("/voiceover.mp3")

        audio = await client.get(
            f"/api/projects/{project['id']}/files/preview",
            params={"path": media["preview"]},
        )
        assert audio.status_code == 200
        assert audio.headers["content-type"] == "audio/mpeg"
        assert audio.content == b"fake-mp3"
    finally:
        _teardown()
        monkeypatch.delenv("OUTPUTS_DIR", raising=False)
        get_settings.cache_clear()


async def test_media_rejects_unknown_skill_and_missing_topic(client, tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        project = (await client.post("/api/projects", json={"title": "校验"})).json()
        unknown = await client.post(
            f"/api/projects/{project['id']}/media/copywriting",
            json={"topic": "x"},
        )
        assert unknown.status_code == 404

        missing = await client.post(
            f"/api/projects/{project['id']}/media/short-video",
            json={"topic": "  "},
        )
        assert missing.status_code == 422
        assert missing.json()["code"] == "skill_inputs_invalid"
    finally:
        _teardown()
        monkeypatch.delenv("OUTPUTS_DIR", raising=False)
        get_settings.cache_clear()


async def test_preview_rejects_unsupported_type_and_escape(client, tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    try:
        project = (await client.post("/api/projects", json={"title": "预览"})).json()
        uploaded = await client.post(
            f"/api/projects/{project['id']}/files",
            files={"file": ("page.html", b"<script>alert(1)</script>", "text/html")},
        )
        assert uploaded.status_code == 201

        unsupported = await client.get(
            f"/api/projects/{project['id']}/files/preview",
            params={"path": uploaded.json()["path"]},
        )
        assert unsupported.status_code == 415
        assert unsupported.json()["code"] == "unsupported_media_type"

        escape = await client.get(
            f"/api/projects/{project['id']}/files/preview",
            params={"path": "../../etc/passwd.mp4"},
        )
        assert escape.status_code == 422
        assert escape.json()["code"] == "invalid_path"
    finally:
        _teardown()
        monkeypatch.delenv("OUTPUTS_DIR", raising=False)
        get_settings.cache_clear()
