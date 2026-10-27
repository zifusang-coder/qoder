import uuid
from typing import Literal

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.settings import LLMProvider
from app.models.user import User
from app.security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/settings", tags=["settings"])

ProviderKind = Literal["openai_compatible", "anthropic"]


class ProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: ProviderKind = "openai_compatible"
    base_url: str
    api_key: str = Field(min_length=8)
    tiers: dict[str, str] = Field(default_factory=dict)
    is_default: bool = False

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
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
    return f"{key[:3]}****{key[-4:]}" if len(key) > 8 else "****"


def _to_out(provider: LLMProvider) -> ProviderOut:
    try:
        masked = _mask(decrypt_secret(provider.api_key_enc))
    except InvalidToken:
        # MASTER_KEY 轮换后旧密文无法解密，只降级展示，绝不泄露密文或明文
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
    await session.commit()
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
    provider = await session.get(LLMProvider, parsed_id)
    if provider is None or provider.tenant_id != user.tenant_id:
        raise AppError("not_found", "提供商不存在", 404)
    await session.delete(provider)
    await session.commit()
    return Response(status_code=204)
