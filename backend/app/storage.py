import os
import re
import uuid
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

# save_bytes 的临时文件命名为 "<name>.<uuid4().hex>.tmp"，只过滤这一种
_TMP_FILE = re.compile(r"\.[0-9a-f]{32}\.tmp$")


class StorageError(ValueError):
    pass


class Storage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _project_path(self, project_id: str, *, create: bool) -> Path:
        if (
            not project_id
            or project_id in {".", ".."}
            or "/" in project_id
            or "\\" in project_id
            or "\x00" in project_id
        ):
            raise StorageError("非法项目 ID")
        try:
            path = (self.root / project_id).resolve()
        except (ValueError, OSError):
            raise StorageError("非法项目路径") from None
        if not path.is_relative_to(self.root):
            raise StorageError("非法项目路径")
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def project_dir(self, project_id: str) -> Path:
        return self._project_path(project_id, create=True)

    def resolve(
        self, project_id: str, relpath: str, *, create: bool = True
    ) -> Path:
        if not relpath or not relpath.strip():
            raise StorageError("非法文件路径")
        if Path(relpath).is_absolute():
            raise StorageError("非法文件路径")
        base = self._project_path(project_id, create=create)
        try:
            path = (base / relpath).resolve()
        except (ValueError, OSError):
            raise StorageError("非法文件路径") from None
        if path == base or not path.is_relative_to(base):
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
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return str(path.relative_to(self.project_dir(project_id)))

    def read_text(self, project_id: str, relpath: str) -> str:
        return self.resolve(project_id, relpath).read_text(encoding="utf-8")

    def list_files(self, project_id: str) -> list[str]:
        base = self.project_dir(project_id)
        return sorted(
            str(p.relative_to(base))
            for p in base.rglob("*")
            if p.is_file() and _TMP_FILE.search(p.name) is None
        )


@lru_cache
def _storage_for(root: Path) -> Storage:
    return Storage(root)


def get_storage() -> Storage:
    return _storage_for(get_settings().outputs_dir)
