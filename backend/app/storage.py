from functools import lru_cache
from pathlib import Path

from app.config import get_settings


class StorageError(ValueError):
    pass


class Storage:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        if not project_id or "/" in project_id or ".." in project_id:
            raise StorageError("非法项目 ID")
        path = (self.root / project_id).resolve()
        if not path.is_relative_to(self.root):
            raise StorageError("非法项目路径")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def resolve(self, project_id: str, relpath: str) -> Path:
        base = self.project_dir(project_id)
        path = (base / relpath).resolve()
        if not path.is_relative_to(base):
            raise StorageError("非法文件路径")
        return path

    def save_text(self, project_id: str, relpath: str, text: str) -> str:
        path = self.resolve(project_id, relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return str(path.relative_to(self.project_dir(project_id)))

    def save_bytes(self, project_id: str, relpath: str, data: bytes) -> str:
        path = self.resolve(project_id, relpath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path.relative_to(self.project_dir(project_id)))

    def read_text(self, project_id: str, relpath: str) -> str:
        return self.resolve(project_id, relpath).read_text(encoding="utf-8")

    def list_files(self, project_id: str) -> list[str]:
        base = self.project_dir(project_id)
        return sorted(
            str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()
        )


@lru_cache
def get_storage() -> Storage:
    return Storage(get_settings().outputs_dir)
