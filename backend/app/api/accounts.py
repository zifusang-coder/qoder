import json
import uuid
from typing import Annotated

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models.publish import PlatformAccount, PublishJob
from app.models.user import User
from app.permissions import admin_access, editor_access
from app.publish.base import Platform
from app.publish.registry import (
    PLATFORM_ORDER,
    headline_capability,
    spec_for,
)
from app.security import decrypt_secret, encrypt_secret
from app.services.audit import record

router = APIRouter(prefix="/accounts", tags=["accounts"])

MAX_AUTHOR = 64
MAX_THUMB_MEDIA_ID = 200


def _clean_meta(platform: str, meta: dict) -> dict:
    spec = spec_for(platform)
    allowed = set(spec.meta_fields)
    cleaned: dict = {}
    for key, value in meta.items():
        if key not in allowed:
            raise ValueError(f"{spec.label}不支持 meta 字段：{key}")
        if key == "author":
            if not isinstance(value, str):
                raise ValueError("meta 字段 author 必须是字符串")
            author = value.strip()
            if len(author) > MAX_AUTHOR:
                raise ValueError(
                    f"meta 字段 author 长度不能超过 {MAX_AUTHOR}"
                )
            cleaned[key] = author
        elif key == "thumb_media_id":
            if not isinstance(value, str):
                raise ValueError("meta 字段 thumb_media_id 必须是字符串")
            thumb = value.strip()
            if not thumb:
                raise ValueError("公众号封面图 thumb_media_id 不能为空")
            if len(thumb) > MAX_THUMB_MEDIA_ID:
                raise ValueError(
                    "meta 字段 thumb_media_id 长度不能超过 "
                    f"{MAX_THUMB_MEDIA_ID}"
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
    spec = spec_for(platform)
    declared = {field.key for field in spec.credential_fields}
    unknown = set(credential) - declared
    if unknown:
        raise ValueError(
            f"{spec.label}不支持的凭据字段：{', '.join(sorted(unknown))}"
        )
    cleaned: dict[str, str] = {}
    for field in spec.credential_fields:
        value = credential.get(field.key)
        if value is None:
            if field.required:
                raise ValueError(
                    f"{spec.label}凭据不完整，请填写 {field.label}"
                )
            continue
        if not isinstance(value, str):
            raise ValueError(  # noqa: TRY004
                f"{spec.label}凭据字段 {field.label} 必须是字符串"
            )
        value = value.strip()
        if field.required and not value:
            raise ValueError(f"{spec.label}凭据不完整，请填写 {field.label}")
        if value:
            cleaned[field.key] = value
    return cleaned


class AccountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Platform
    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    ]
    credential: dict = Field(default_factory=dict)
    meta: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_credentials_and_meta(self) -> "AccountIn":
        self.credential = _clean_credential(self.platform, self.credential)
        self.meta = _clean_meta(self.platform, self.meta)
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
        return self


class AccountOut(BaseModel):
    id: str
    platform: str
    name: str
    status: str
    credential_masked: str
    meta: dict
    capability: str
    capability_note: str


class CredentialFieldOut(BaseModel):
    key: str
    label: str
    secret: bool
    required: bool
    placeholder: str


class PlatformOut(BaseModel):
    key: str
    label: str
    capability: str
    content_capabilities: dict[str, str]
    credential_fields: list[CredentialFieldOut]
    meta_fields: list[str]
    note: str


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
    spec = spec_for(account.platform)
    stored_meta = account.meta if isinstance(account.meta, dict) else {}
    return AccountOut(
        id=str(account.id),
        platform=account.platform,
        name=account.name,
        status=account.status,
        credential_masked=_masked_credential(account),
        meta={
            key: value
            for key, value in stored_meta.items()
            if key in spec.meta_fields
        },
        capability=headline_capability(spec),
        capability_note=spec.note,
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


@router.get("/platforms", response_model=list[PlatformOut])
async def list_platforms(
    user: User = Depends(editor_access),  # noqa: B008
) -> list[PlatformOut]:
    return [
        PlatformOut(
            key=spec.key,
            label=spec.label,
            capability=headline_capability(spec),
            content_capabilities=dict(spec.content_capabilities),
            credential_fields=[
                CredentialFieldOut(
                    key=field.key,
                    label=field.label,
                    secret=field.secret,
                    required=field.required,
                    placeholder=field.placeholder,
                )
                for field in spec.credential_fields
            ],
            meta_fields=list(spec.meta_fields),
            note=spec.note,
        )
        for spec in (spec_for(key) for key in PLATFORM_ORDER)
    ]


@router.get("", response_model=list[AccountOut])
async def list_accounts(
    platform: str | None = None,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[AccountOut]:
    stmt = select(PlatformAccount).where(
        PlatformAccount.tenant_id == user.tenant_id,
        PlatformAccount.platform.in_(PLATFORM_ORDER),
    )
    if platform is not None:
        if platform not in PLATFORM_ORDER:
            raise AppError("not_found", "平台不存在", 404)
        stmt = stmt.where(PlatformAccount.platform == platform)
    rows = (
        await session.execute(
            stmt.order_by(
                PlatformAccount.created_at.desc(),
                PlatformAccount.id.desc(),
            )
        )
    ).scalars().all()
    order = {key: index for index, key in enumerate(PLATFORM_ORDER)}
    # 先按创建时间倒序，再按平台顺序稳定排序，前端可直接分组
    rows = sorted(rows, key=lambda account: order.get(account.platform, 99))
    return [_out(account) for account in rows]


@router.post("", response_model=AccountOut, status_code=201)
async def create_account(
    payload: AccountIn,
    request: Request,
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
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("account_exists", "该平台下已存在同名账号", 409) from None
    await record(
        session,
        tenant_id=user.tenant_id,
        action="account.create",
        actor=user,
        resource_type="account",
        resource_id=str(account.id),
        detail={"platform": account.platform},
        request=request,
    )
    await session.commit()
    await session.refresh(account)
    return _out(account)


@router.patch("/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: str,
    payload: AccountUpdate,
    request: Request,
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
        try:
            cleaned_meta = _clean_meta(account.platform, payload.meta)
        except ValueError as exc:
            raise AppError("validation_error", str(exc), 422) from None
        # PATCH 只合并本次提交的键，未提交的 meta 必须保留
        stored = account.meta if isinstance(account.meta, dict) else {}
        meta = {
            **{
                key: value
                for key, value in stored.items()
                if key in spec_for(account.platform).meta_fields
            },
            **cleaned_meta,
        }
    if name is not None:
        account.name = name
    if credential_enc is not None:
        account.credential_enc = credential_enc
    if meta is not None:
        account.meta = meta
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise AppError("account_exists", "该平台下已存在同名账号", 409) from None
    await record(
        session,
        tenant_id=user.tenant_id,
        action="account.update",
        actor=user,
        resource_type="account",
        resource_id=str(account.id),
        request=request,
    )
    await session.commit()
    await session.refresh(account)
    return _out(account)


@router.delete("/{account_id}", status_code=204)
async def revoke_account(
    account_id: str,
    request: Request,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    account = await _owned_account(session, account_id, user)
    if account.status != "revoked":
        account.status = "revoked"
        # 停用即销毁密钥与 meta，撤销后的账号不再保留任何可用凭据
        account.credential_enc = encrypt_secret("{}")
        account.meta = {}
        await record(
            session,
            tenant_id=user.tenant_id,
            action="account.revoke",
            actor=user,
            resource_type="account",
            resource_id=str(account.id),
            request=request,
        )
        await session.commit()
    return Response(status_code=204)


@router.delete("/{account_id}/permanent", status_code=204)
async def purge_account(
    account_id: str,
    request: Request,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    account = await _owned_account(session, account_id, user)
    if account.status != "revoked":
        raise AppError("account_not_revoked", "请先停用账号再删除", 409)
    job_count = (
        await session.execute(
            select(func.count())
            .select_from(PublishJob)
            .where(PublishJob.account_id == account.id)
        )
    ).scalar_one()
    if job_count:
        raise AppError(
            "account_has_publish_history",
            "该账号存在发布记录，无法删除",
            409,
        )
    await record(
        session,
        tenant_id=user.tenant_id,
        action="account.delete",
        actor=user,
        resource_type="account",
        resource_id=str(account.id),
        detail={"platform": account.platform, "name": account.name},
        request=request,
    )
    await session.delete(account)
    await session.commit()
    return Response(status_code=204)
