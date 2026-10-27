from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"

BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", extra="ignore")

    app_name: str = "新媒体运营系统"
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
    cors_origins: str = "http://localhost:5173"


@lru_cache
def get_settings() -> Settings:
    return Settings()
