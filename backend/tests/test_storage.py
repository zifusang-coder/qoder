import io

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.storage import (
    LocalStorage,
    S3Storage,
    Storage,
    StorageError,
    StorageNotFound,
)


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.objects[Key] = bytes(Body)

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "", **_: object) -> dict:
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        return {"Contents": [{"Key": key} for key in keys], "IsTruncated": False}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


def test_local_storage_is_a_storage_backend(tmp_path):
    storage = LocalStorage(tmp_path)
    assert isinstance(storage, Storage)
    assert isinstance(storage, LocalStorage)


def test_local_storage_reads_bytes(tmp_path):
    storage = LocalStorage(tmp_path)
    storage.save_bytes("p1", "files/a.bin", b"\x00\x01")
    assert storage.read_bytes("p1", "files/a.bin") == b"\x00\x01"
    with pytest.raises(StorageNotFound):
        storage.read_bytes("p1", "files/missing.bin")


def test_local_storage_treats_directory_as_not_found(tmp_path):
    storage = LocalStorage(tmp_path)
    storage.save_bytes("p1", "files/a.bin", b"x")
    with pytest.raises(StorageNotFound):
        storage.read_bytes("p1", "files")


def test_s3_storage_roundtrip_with_prefix():
    client = FakeS3()
    storage = S3Storage(
        bucket="media", prefix="tenant-a", client=client
    )
    stored = storage.save_bytes("p1", "files/a.bin", b"hello")
    assert stored == "files/a.bin"
    assert client.objects["tenant-a/p1/files/a.bin"] == b"hello"
    assert storage.read_bytes("p1", "files/a.bin") == b"hello"
    assert storage.list_files("p1") == ["files/a.bin"]


def test_s3_storage_lists_only_project_prefix():
    client = FakeS3()
    storage = S3Storage(bucket="media", prefix="", client=client)
    storage.save_text("p1", "notes.md", "one")
    storage.save_text("p2", "notes.md", "two")
    assert storage.list_files("p1") == ["notes.md"]


def test_s3_storage_rejects_traversal_before_calling_client():
    client = FakeS3()
    storage = S3Storage(bucket="media", client=client)
    for relpath in ("../secret", "a/../../secret", "/etc/passwd", "a\\..\\b"):
        with pytest.raises(StorageError):
            storage.save_bytes("p1", relpath, b"x")
    assert client.objects == {}


def test_s3_storage_missing_object_raises_not_found():
    storage = S3Storage(bucket="media", client=FakeS3())
    with pytest.raises(StorageNotFound):
        storage.read_bytes("p1", "files/missing.bin")


def test_s3_backend_requires_bucket():
    with pytest.raises(ValidationError):
        Settings(
            storage_backend="s3",
            storage_s3_bucket="",
            _env_file=None,
        )


def test_default_storage_backend_is_local(tmp_path):
    settings = Settings(outputs_dir=tmp_path, _env_file=None)
    from app.storage import build_storage

    storage = build_storage(settings)
    assert isinstance(storage, LocalStorage)


async def test_file_download_endpoint(client, tmp_path):
    from app.main import app
    from app.storage import get_storage

    storage = LocalStorage(tmp_path)
    app.dependency_overrides[get_storage] = lambda: storage
    try:
        project = await client.post("/api/projects", json={"title": "下载测试"})
        project_id = project.json()["id"]
        upload = await client.post(
            f"/api/projects/{project_id}/files",
            files={"file": ("稿件.md", b"# body", "text/markdown")},
        )
        assert upload.status_code == 201
        path = upload.json()["path"]

        response = await client.get(
            f"/api/projects/{project_id}/files/download", params={"path": path}
        )
        assert response.status_code == 200
        assert response.content == b"# body"
        assert "attachment" in response.headers["content-disposition"]
    finally:
        app.dependency_overrides.pop(get_storage, None)
