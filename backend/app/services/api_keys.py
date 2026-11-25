import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.models.api_key import ApiKey
from app.models.tenant import Tenant
from app.models.user import User

SECRET_PREFIX = "nmk_"
PREFIX_LENGTH = 12
SCOPES = ("agent:run", "agent:read", "workflow:run", "workflow:read")


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def validate_scopes(scopes: list[str]) -> list[str]:
    cleaned = [item.strip() for item in scopes if isinstance(item, str)]
    if not cleaned:
        raise AppError("invalid_payload", "API Key 至少需要一个范围", 422)
    unknown = [item for item in cleaned if item not in SCOPES]
    if unknown:
        raise AppError(
            "invalid_payload",
            "包含不支持的范围",
            422,
            detail={"unknown": unknown},
        )
    # 保序去重，避免同一范围重复出现在列表与授权判断里。
    return list(dict.fromkeys(cleaned))


async def create_key(
    session: AsyncSession,
    *,
    tenant_id: str,
    created_by: uuid.UUID,
    name: str,
    scopes: list[str],
) -> tuple[ApiKey, str]:
    allowed = validate_scopes(scopes)
    secret = generate_secret()
    row = ApiKey(
        tenant_id=tenant_id,
        name=name,
        prefix=secret[:PREFIX_LENGTH],
        key_hash=hash_secret(secret),
        scopes=allowed,
        created_by=created_by,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, secret


async def list_keys(session: AsyncSession, tenant_id: str) -> list[ApiKey]:
    return list(
        (
            await session.execute(
                select(ApiKey)
                .where(ApiKey.tenant_id == tenant_id)
                .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
            )
        )
        .scalars()
        .all()
    )


async def revoke_key(
    session: AsyncSession, *, tenant_id: str, key_id: str
) -> tuple[ApiKey, bool]:
    try:
        parsed = uuid.UUID(key_id)
    except (ValueError, AttributeError):
        raise AppError("api_key_not_found", "API Key 不存在", 404) from None
    row = (
        await session.execute(
            select(ApiKey).where(
                ApiKey.id == parsed, ApiKey.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise AppError("api_key_not_found", "API Key 不存在", 404)
    if row.revoked_at is not None:
        return row, False
    row.revoked_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(row)
    return row, True


async def authenticate_key(
    session: AsyncSession, secret: str
) -> tuple[ApiKey, User]:
    if not secret or not secret.startswith(SECRET_PREFIX):
        raise AppError("api_key_invalid", "API Key 无效或已吊销", 401)
    row = (
        await session.execute(
            select(ApiKey).where(ApiKey.key_hash == hash_secret(secret))
        )
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        raise AppError("api_key_invalid", "API Key 无效或已吊销", 401)
    creator = await session.get(User, row.created_by)
    if (
        creator is None
        or not creator.is_active
        or creator.tenant_id != row.tenant_id
    ):
        raise AppError("api_key_invalid", "API Key 无效或已吊销", 401)
    tenant = await session.get(Tenant, row.tenant_id)
    if tenant is None or tenant.status != "active":
        raise AppError("api_key_invalid", "API Key 无效或已吊销", 401)
    row.last_used_at = datetime.now(UTC)
    try:
        await session.commit()
    except Exception:  # noqa: BLE001 - 最后使用时间是旁路信息，写失败不能影响调用
        await session.rollback()
    return row, creator
