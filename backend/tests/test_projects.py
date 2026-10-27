from pathlib import Path

import pytest

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
