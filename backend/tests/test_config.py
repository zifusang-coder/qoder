import pytest
from pydantic import ValidationError

from app.config import Settings


def test_prod_rejects_default_secrets():
    with pytest.raises(ValidationError):
        Settings(env="prod", _env_file=None)


def test_dev_allows_defaults():
    settings = Settings(env="dev", _env_file=None)
    assert settings.jwt_secret == "dev-only-change-me"


def test_prod_accepts_overridden_secrets():
    settings = Settings(
        env="prod",
        jwt_secret="a-real-secret",
        master_key="a-real-master-key",
        admin_password="a-real-password",
        _env_file=None,
    )
    assert settings.env == "prod"
