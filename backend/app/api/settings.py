import logging
import uuid
from typing import Annotated, Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.settings import LLMProvider
from app.models.user import User
from app.security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/settings", tags=["settings"])

logger = logging.getLogger(__name__)

ProviderKind = Literal["openai_compatible", "anthropic"]


class ProviderIn(BaseModel):
    name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    kind: ProviderKind = "openai_compatible"
    base_url: str = Field(max_length=500)
    api_key: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=8, max_length=512)
    ]
    tiers: dict[str, str] = Field(default_factory=dict)
    is_default: bool = False

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return value

    @field_validator("api_key")
    @classmethod
    def _ensure_ascii_api_key(cls, value: str) -> str:
        # Fernet 密文长度随 UTF-8 字节数膨胀，非 ASCII 密钥会超出 api_key_enc 容量
        if not value.isascii():
            raise ValueError("api_key 只能包含 ASCII 字符")
        return value


class ProviderOut(BaseModel):
    id: str
    name: str
    kind: str
    base_url: str
    api_key_masked: str
    tiers: dict[str, str]
    is_default: bool


def _mask(key: str) -> str:
    # 不返回任何前缀，避免短密钥在掩码中泄露可用信息
    return f"****{key[-4:]}" if len(key) > 8 else "****"


def _to_out(provider: LLMProvider) -> ProviderOut:
    try:
        masked = _mask(decrypt_secret(provider.api_key_enc))
    except (InvalidToken, UnicodeDecodeError):
        # MASTER_KEY 轮换后旧密文无法解密，只降级展示，绝不泄露密文或明文
        logger.warning("api_key_decrypt_failed provider_id=%s", provider.id)
        masked = "****"
    return ProviderOut(
        id=str(provider.id),
        name=provider.name,
        kind=provider.kind,
        base_url=provider.base_url,
        api_key_masked=masked,
        tiers=dict(provider.tiers or {}),
        is_default=provider.is_default,
    )


@router.get("/providers", response_model=list[ProviderOut])
async def list_providers(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[ProviderOut]:
    rows = (
        await session.execute(
            select(LLMProvider)
            .where(LLMProvider.tenant_id == user.tenant_id)
            .order_by(LLMProvider.created_at, LLMProvider.id)
        )
    ).scalars()
    return [_to_out(row) for row in rows]


@router.post("/providers", response_model=ProviderOut, status_code=201)
async def create_provider(
    payload: ProviderIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ProviderOut:
    if payload.is_default:
        await session.execute(
            update(LLMProvider)
            .where(
                LLMProvider.tenant_id == user.tenant_id,
                LLMProvider.is_default.is_(True),
            )
            .values(is_default=False)
        )
    provider = LLMProvider(
        tenant_id=user.tenant_id,
        name=payload.name,
        kind=payload.kind,
        base_url=payload.base_url,
        api_key_enc=encrypt_secret(payload.api_key),
        tiers=payload.tiers,
        is_default=payload.is_default,
    )
    session.add(provider)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("default_conflict", "默认提供商冲突，请重试", 409) from None
    await session.refresh(provider)
    return _to_out(provider)


@router.delete("/providers/{provider_id}", status_code=204)
async def delete_provider(
    provider_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    try:
        parsed_id = uuid.UUID(provider_id)
    except ValueError:
        raise AppError("not_found", "提供商不存在", 404) from None
    provider = (
        await session.execute(
            select(LLMProvider).where(
                LLMProvider.id == parsed_id,
                LLMProvider.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if provider is None:
        raise AppError("not_found", "提供商不存在", 404)
    await session.delete(provider)
    await session.commit()
    return Response(status_code=204)
