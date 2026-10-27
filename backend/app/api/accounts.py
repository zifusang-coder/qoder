import json
import uuid
from typing import Annotated

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.publish import PlatformAccount
from app.models.user import User
from app.publish.base import Platform
from app.security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/accounts", tags=["accounts"])

ALLOWED_META = {
    "author",
    "thumb_media_id",
    "need_open_comment",
    "only_fans_can_comment",
}
SCALAR_TYPES = (str, int, float, bool)


class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Platform
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    credential: dict
    meta: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_credentials_and_meta(self) -> "AccountIn":
        if self.platform == "wechat":
            app_id = self.credential.get("app_id")
            app_secret = self.credential.get("app_secret")
            if (
                not isinstance(app_id, str)
                or not app_id.strip()
                or not isinstance(app_secret, str)
                or not app_secret.strip()
            ):
                raise ValueError("公众号凭据不完整，请重新授权")
            self.credential["app_id"] = app_id.strip()
            self.credential["app_secret"] = app_secret.strip()
        for key, value in self.meta.items():
            if key not in ALLOWED_META:
                raise ValueError(f"不支持的 meta 字段：{key}")
            if not isinstance(value, SCALAR_TYPES):
                # pydantic 需要 ValueError 才能转换为 422
                raise ValueError(f"meta 字段 {key} 只支持标量值")  # noqa: TRY004
        return self


class AccountOut(BaseModel):
    id: str
    platform: str
    name: str
    status: str
    credential_masked: str


def _mask_value(value: object) -> str:
    if isinstance(value, SCALAR_TYPES):
        return str(value)[:3] + "****"
    return "****"


def _masked_credential(account: PlatformAccount) -> str:
    try:
        creds = json.loads(decrypt_secret(account.credential_enc))
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        return "{}"
    if not isinstance(creds, dict):
        return "{}"
    masked = {str(key): _mask_value(value) for key, value in creds.items()}
    return json.dumps(masked, ensure_ascii=False)


def _out(account: PlatformAccount) -> AccountOut:
    return AccountOut(
        id=str(account.id),
        platform=account.platform,
        name=account.name,
        status=account.status,
        credential_masked=_masked_credential(account),
    )


def _account_id(account_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(account_id)
    except (ValueError, TypeError):
        raise AppError("not_found", "账号不存在", 404) from None


async def _owned_account(
    session: AsyncSession, account_id: str, user: User
) -> PlatformAccount:
    account = (
        await session.execute(
            select(PlatformAccount).where(
                PlatformAccount.id == _account_id(account_id),
                PlatformAccount.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if account is None:
        raise AppError("not_found", "账号不存在", 404)
    return account


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[AccountOut]:
    rows = (
        await session.execute(
            select(PlatformAccount)
            .where(PlatformAccount.tenant_id == user.tenant_id)
            .order_by(
                PlatformAccount.created_at.desc(), PlatformAccount.id.desc()
            )
        )
    ).scalars().all()
    return [_out(account) for account in rows]


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(
    payload: AccountIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AccountOut:
    account = PlatformAccount(
        tenant_id=user.tenant_id,
        platform=payload.platform,
        name=payload.name,
        credential_enc=encrypt_secret(
            json.dumps(payload.credential, ensure_ascii=False)
        ),
        status="active",
        meta=payload.meta,
    )
    session.add(account)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("account_exists", "该平台下已存在同名账号", 409) from None
    await session.refresh(account)
    return _out(account)


@router.delete("/{account_id}", status_code=204)
async def revoke_account(
    account_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    account = await _owned_account(session, account_id, user)
    if account.status != "revoked":
        account.status = "revoked"
        await session.commit()
    return Response(status_code=204)
