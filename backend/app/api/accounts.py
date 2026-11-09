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
from app.errors import AppError
from app.models.publish import PlatformAccount
from app.models.user import User
from app.permissions import admin_access, editor_access
from app.publish.base import Platform
from app.security import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/accounts", tags=["accounts"])

ALLOWED_META = {
    "author",
    "thumb_media_id",
    "need_open_comment",
    "only_fans_can_comment",
}
MAX_AUTHOR = 64
MAX_THUMB_MEDIA_ID = 200


def _clean_meta(meta: dict) -> dict:
    cleaned: dict = {}
    for key, value in meta.items():
        if key not in ALLOWED_META:
            raise ValueError(f"不支持的 meta 字段：{key}")
        if key == "author":
            if not isinstance(value, str):
                raise ValueError("meta 字段 author 必须是字符串")
            author = value.strip()
            if len(author) > MAX_AUTHOR:
                raise ValueError(f"meta 字段 author 长度不能超过 {MAX_AUTHOR}")
            cleaned[key] = author
        elif key == "thumb_media_id":
            if not isinstance(value, str):
                raise ValueError("meta 字段 thumb_media_id 必须是字符串")
            thumb = value.strip()
            if not thumb:
                raise ValueError("公众号封面图 thumb_media_id 不能为空")
            if len(thumb) > MAX_THUMB_MEDIA_ID:
                raise ValueError(
                    f"meta 字段 thumb_media_id 长度不能超过 {MAX_THUMB_MEDIA_ID}"
                )
            cleaned[key] = thumb
        else:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value not in (0, 1)
            ):
                raise ValueError(f"meta 字段 {key} 只能为 0 或 1")
            cleaned[key] = value
    return cleaned


def _clean_credential(platform: str, credential: dict) -> dict:
    if platform != "wechat":
        return credential
    app_id = credential.get("app_id")
    app_secret = credential.get("app_secret")
    if (
        not isinstance(app_id, str)
        or not app_id.strip()
        or not isinstance(app_secret, str)
        or not app_secret.strip()
    ):
        raise ValueError("公众号凭据不完整，请重新授权")
    return {"app_id": app_id.strip(), "app_secret": app_secret.strip()}


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
        self.credential = _clean_credential(self.platform, self.credential)
        self.meta = _clean_meta(self.meta)
        return self


class AccountUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        ]
        | None
    ) = None
    credential: dict | None = None
    meta: dict | None = None

    @model_validator(mode="after")
    def _validate_update(self) -> "AccountUpdate":
        if self.name is None and self.credential is None and self.meta is None:
            raise ValueError("至少需要提供一个要更新的字段")
        if self.meta is not None:
            self.meta = _clean_meta(self.meta)
        return self


class AccountOut(BaseModel):
    id: str
    platform: str
    name: str
    status: str
    credential_masked: str


def _masked_credential(account: PlatformAccount) -> str:
    try:
        creds = json.loads(decrypt_secret(account.credential_enc))
    except (InvalidToken, ValueError, TypeError, UnicodeError):
        return "{}"
    if not isinstance(creds, dict):
        return "{}"
    masked = {str(key): "****" for key in creds}
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
    user: User = Depends(editor_access),  # noqa: B008
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
    user: User = Depends(admin_access),  # noqa: B008
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


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: str,
    payload: AccountUpdate,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AccountOut:
    account = await _owned_account(session, account_id, user)
    if account.status != "active":
        raise AppError("account_revoked", "账号已停用，无法修改，请重新创建", 409)
    # 先完成全部校验再修改 ORM 对象，避免后续校验失败时留下脏对象
    name = payload.name
    credential_enc = None
    if payload.credential is not None:
        try:
            credential = _clean_credential(account.platform, payload.credential)
        except ValueError as exc:
            raise AppError("validation_error", str(exc), 422) from None
        credential_enc = encrypt_secret(
            json.dumps(credential, ensure_ascii=False)
        )
    meta = None
    if payload.meta is not None:
        # PATCH 只合并本次提交的键，未提交的 meta（如 thumb_media_id）必须保留
        stored = account.meta if isinstance(account.meta, dict) else {}
        meta = {**{k: v for k, v in stored.items() if k in ALLOWED_META}, **payload.meta}
    if name is not None:
        account.name = name
    if credential_enc is not None:
        account.credential_enc = credential_enc
    if meta is not None:
        account.meta = meta
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
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    account = await _owned_account(session, account_id, user)
    if account.status != "revoked":
        account.status = "revoked"
        # 停用即销毁密钥与 meta，撤销后的账号不再保留任何可用凭据
        account.credential_enc = encrypt_secret("{}")
        account.meta = {}
        await session.commit()
    return Response(status_code=204)
