import json

from cryptography.fernet import InvalidToken

from app.models.publish import PlatformAccount
from app.publish.base import PublishConfigError
from app.security import decrypt_secret, encrypt_secret


def _decrypt(account: PlatformAccount, *, label: str) -> dict:
    try:
        raw = decrypt_secret(account.credential_enc)
        creds = json.loads(raw)
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        raise PublishConfigError(f"{label}凭据不可用，请重新授权") from None
    if not isinstance(creds, dict):
        raise PublishConfigError(f"{label}凭据不完整，请重新授权")
    return creds


def _require(
    raw: dict,
    required: tuple[str, ...],
    *,
    label: str,
) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key in required:
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise PublishConfigError(f"{label}凭据不完整，请重新授权")
        cleaned[key] = value.strip()
    return cleaned


def load_credential(
    account: PlatformAccount,
    required: tuple[str, ...],
    *,
    label: str,
) -> dict[str, str]:
    return _require(_decrypt(account, label=label), required, label=label)


def credential_with_tokens(
    account: PlatformAccount,
    required: tuple[str, ...],
    *,
    label: str,
) -> dict[str, object]:
    raw = _decrypt(account, label=label)
    creds: dict[str, object] = dict(_require(raw, required, label=label))
    for key in ("access_token", "expires_at"):
        value = raw.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and value:
            creds[key] = value
    return creds


def merge_credential_patch(account: PlatformAccount, patch: dict) -> None:
    creds = _decrypt(account, label="账号")
    for key, value in patch.items():
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            creds[str(key)] = value
    account.credential_enc = encrypt_secret(
        json.dumps(creds, ensure_ascii=False)
    )
