from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_DEV_DEFAULTS = {
    "jwt_secret": {"dev-only-change-me", "change-me-to-32-plus-random-chars"},
    "master_key": {"dev-only-master-key-change-me", "change-me-to-a-long-random-passphrase"},
    "admin_password": {"admin12345"},
}

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    app_name: str = "云AI新媒体运营系统"
    env: str = "dev"
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/newmedia"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "dev-only-change-me"
    jwt_expire_minutes: int = 720
    master_key: str = "dev-only-master-key-change-me"
    admin_email: str = "admin@local"
    admin_password: str = "admin12345"
    skills_dir: Path = ROOT_DIR / "skills"
    outputs_dir: Path = ROOT_DIR / "outputs"
    storage_backend: Literal["local", "s3"] = "local"
    storage_s3_endpoint: str = ""
    storage_s3_bucket: str = ""
    storage_s3_region: str = ""
    storage_s3_access_key: str = ""
    storage_s3_secret_key: str = ""
    storage_s3_prefix: str = ""
    storage_s3_use_ssl: bool = True
    storage_s3_path_style: bool = True
    embedding_model: str = ""
    cors_origins: str = "http://localhost:5173"
    login_rate_limit_email: int = 5
    login_rate_limit_ip: int = 20
    login_rate_limit_window_seconds: int = 300
    audit_retention_days: int = 180

    @model_validator(mode="after")
    def _reject_dev_secrets_outside_dev(self) -> "Settings":
        if self.env == "dev":
            return self
        insecure = [
            name for name, defaults in _DEV_DEFAULTS.items() if getattr(self, name) in defaults
        ]
        if insecure:
            raise ValueError(f"非 dev 环境必须覆盖默认密钥: {', '.join(insecure)}")
        return self

    @model_validator(mode="after")
    def _require_s3_bucket(self) -> "Settings":
        if self.storage_backend == "s3" and not self.storage_s3_bucket.strip():
            raise ValueError("storage_backend=s3 时必须配置 STORAGE_S3_BUCKET")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
