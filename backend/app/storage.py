import os
import re
import uuid
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path, PurePosixPath

from app.config import Settings, get_settings

# save_bytes 的临时文件命名为 "<name>.<uuid4().hex>.tmp"，只过滤这一种
_TMP_FILE = re.compile(r"\.[0-9a-f]{32}\.tmp$")


class StorageError(ValueError):
    pass


class StorageNotFound(StorageError):
    pass


def _safe_project_id(project_id: str) -> str:
    if (
        not project_id
        or project_id in {".", ".."}
        or "/" in project_id
        or "\\" in project_id
        or "\x00" in project_id
    ):
        raise StorageError("非法项目 ID")
    return project_id


def _safe_relpath(relpath: str) -> str:
    if not relpath or not relpath.strip():
        raise StorageError("非法文件路径")
    if relpath.startswith(("/", "\\")) or "\\" in relpath or "\x00" in relpath:
        raise StorageError("非法文件路径")
    if any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in relpath):
        raise StorageError("非法文件路径")
    parts = PurePosixPath(relpath).parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise StorageError("非法文件路径")
    return "/".join(parts)


class StorageBackend(ABC):
    @abstractmethod
    def save_text(self, project_id: str, relpath: str, text: str) -> str: ...

    @abstractmethod
    def save_bytes(self, project_id: str, relpath: str, data: bytes) -> str: ...

    @abstractmethod
    def read_text(self, project_id: str, relpath: str) -> str: ...

    @abstractmethod
    def read_bytes(self, project_id: str, relpath: str) -> bytes: ...

    @abstractmethod
    def list_files(self, project_id: str) -> list[str]: ...


class LocalStorage(StorageBackend):
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _project_path(self, project_id: str, *, create: bool) -> Path:
        _safe_project_id(project_id)
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
        _safe_relpath(relpath)
        base = self._project_path(project_id, create=create)
        try:
            path = (base / relpath).resolve()
        except (ValueError, OSError):
            raise StorageError("非法文件路径") from None
        if path == base or not path.is_relative_to(base):
            raise StorageError("非法文件路径")
        return path

    def save_text(self, project_id: str, relpath: str, text: str) -> str:
        return self.save_bytes(project_id, relpath, text.encode("utf-8"))

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
        return self.read_bytes(project_id, relpath).decode("utf-8")

    def read_bytes(self, project_id: str, relpath: str) -> bytes:
        path = self.resolve(project_id, relpath, create=False)
        try:
            return path.read_bytes()
        except (FileNotFoundError, IsADirectoryError):
            raise StorageNotFound("文件不存在") from None

    def list_files(self, project_id: str) -> list[str]:
        base = self.project_dir(project_id)
        return sorted(
            str(p.relative_to(base))
            for p in base.rglob("*")
            if p.is_file() and _TMP_FILE.search(p.name) is None
        )


# 保留原类名，现有调用方与测试无需改动
Storage = LocalStorage


class S3Storage(StorageBackend):
    """S3 兼容对象存储（AWS S3 / MinIO / 腾讯云 COS 的 S3 网关）。"""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        use_ssl: bool = True,
        path_style: bool = True,
        client=None,
    ):
        if not bucket.strip():
            raise StorageError("S3 存储必须配置 bucket")
        self.bucket = bucket
        self.prefix = _safe_relpath(prefix) if prefix else ""
        self.client = client or _create_s3_client(
            endpoint_url=endpoint_url,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            use_ssl=use_ssl,
            path_style=path_style,
        )

    def _key(self, project_id: str, relpath: str) -> str:
        parts = [_safe_project_id(project_id), _safe_relpath(relpath)]
        if self.prefix:
            parts.insert(0, self.prefix)
        return "/".join(parts)

    def _prefix(self, project_id: str) -> str:
        base = f"{_safe_project_id(project_id)}/"
        return f"{self.prefix}/{base}" if self.prefix else base

    def save_text(self, project_id: str, relpath: str, text: str) -> str:
        return self.save_bytes(project_id, relpath, text.encode("utf-8"))

    def save_bytes(self, project_id: str, relpath: str, data: bytes) -> str:
        key = self._key(project_id, relpath)
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        except Exception:  # noqa: BLE001 - 客户端异常统一归一化，避免泄露凭据与内部细节
            raise StorageError("对象存储写入失败") from None
        return _safe_relpath(relpath)

    def read_text(self, project_id: str, relpath: str) -> str:
        return self.read_bytes(project_id, relpath).decode("utf-8")

    def read_bytes(self, project_id: str, relpath: str) -> bytes:
        key = self._key(project_id, relpath)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except Exception as exc:  # noqa: BLE001 - 先区分不存在，其余统一归一化
            if _is_not_found(exc):
                raise StorageNotFound("文件不存在") from None
            raise StorageError("对象存储读取失败") from None

    def list_files(self, project_id: str) -> list[str]:
        prefix = self._prefix(project_id)
        keys: list[str] = []
        token: str | None = None
        try:
            while True:
                kwargs = {"Bucket": self.bucket, "Prefix": prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                response = self.client.list_objects_v2(**kwargs)
                for item in response.get("Contents", []):
                    key = item.get("Key", "")
                    if key.startswith(prefix) and not key.endswith("/"):
                        keys.append(key[len(prefix) :])
                if not response.get("IsTruncated"):
                    break
                token = response.get("NextContinuationToken")
                if not token:
                    break
        except Exception:  # noqa: BLE001 - 客户端异常统一归一化
            raise StorageError("对象存储读取失败") from None
        return sorted(keys)


def _is_not_found(exc: Exception) -> bool:
    if isinstance(exc, (KeyError, FileNotFoundError)):
        return True
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"NoSuchKey", "NotFound", "404"} or status == 404


def _create_s3_client(
    *,
    endpoint_url: str | None,
    region: str | None,
    access_key: str | None,
    secret_key: str | None,
    use_ssl: bool,
    path_style: bool,
):
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        raise StorageError(
            "S3 存储需要安装 boto3（uv sync --extra s3）"
        ) from None
    config = BotoConfig(
        s3={"addressing_style": "path" if path_style else "auto"}
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        region_name=region or None,
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        use_ssl=use_ssl,
        config=config,
    )


def _s3_storage(settings: Settings) -> S3Storage:
    return S3Storage(
        bucket=settings.storage_s3_bucket,
        prefix=settings.storage_s3_prefix,
        endpoint_url=settings.storage_s3_endpoint,
        region=settings.storage_s3_region,
        access_key=settings.storage_s3_access_key,
        secret_key=settings.storage_s3_secret_key,
        use_ssl=settings.storage_s3_use_ssl,
        path_style=settings.storage_s3_path_style,
    )


def build_storage(settings: Settings) -> StorageBackend:
    if settings.storage_backend == "s3":
        return _s3_storage(settings)
    return _local_storage(settings.outputs_dir)


@lru_cache
def _local_storage(root: Path) -> LocalStorage:
    return LocalStorage(root)


@lru_cache
def _cached_s3_storage(
    bucket: str,
    prefix: str,
    endpoint: str,
    region: str,
    access_key: str,
    secret_key: str,
    use_ssl: bool,
    path_style: bool,
) -> S3Storage:
    return S3Storage(
        bucket=bucket,
        prefix=prefix,
        endpoint_url=endpoint,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        use_ssl=use_ssl,
        path_style=path_style,
    )


def get_storage() -> StorageBackend:
    settings = get_settings()
    if settings.storage_backend == "s3":
        return _cached_s3_storage(
            settings.storage_s3_bucket,
            settings.storage_s3_prefix,
            settings.storage_s3_endpoint,
            settings.storage_s3_region,
            settings.storage_s3_access_key,
            settings.storage_s3_secret_key,
            settings.storage_s3_use_ssl,
            settings.storage_s3_path_style,
        )
    return _local_storage(settings.outputs_dir)
