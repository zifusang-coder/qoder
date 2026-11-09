from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, StringConstraints, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.tenant import Tenant
from app.models.user import User
from app.security import create_token, hash_password, verify_password
from app.services.audit import record, record_best_effort

router = APIRouter(prefix="/auth", tags=["auth"])

_DUMMY_HASH = hash_password("timing-only-dummy-password")


class LoginIn(BaseModel):
    # 不用 EmailStr：默认管理员 admin@local 是单标签域名，会被 email-validator 拒绝而变 422
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    display_name: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: Annotated[
        str, StringConstraints(min_length=8, max_length=128)
    ]

    @model_validator(mode="after")
    def _different(self) -> "ChangePasswordIn":
        if self.new_password == self.current_password:
            raise ValueError("新密码不能与当前密码相同")
        return self


class MeOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    tenant_id: str
    tenant_name: str
    is_platform_admin: bool
    must_change_password: bool


@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginIn,
    request: Request,
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TokenOut:
    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user is None:
        verify_password(payload.password, _DUMMY_HASH)
        raise AppError("invalid_credentials", "邮箱或密码错误", 401)
    if not verify_password(payload.password, user.password_hash):
        await record_best_effort(
            session,
            tenant_id=user.tenant_id,
            action="auth.login_failed",
            actor=user,
            status="failure",
            detail={"reason": "bad_password"},
            request=request,
        )
        raise AppError("invalid_credentials", "邮箱或密码错误", 401)
    if not user.is_active:
        raise AppError("account_disabled", "账号已停用，请联系租户管理员", 403)
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or tenant.status != "active":
        raise AppError("tenant_suspended", "租户已停用，请联系平台管理员", 403)
    token = create_token(user.email, user.tenant_id, user.token_version)
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="auth.login",
        actor=user,
        request=request,
    )
    return TokenOut(access_token=token, display_name=user.display_name)


@router.post("/change-password", response_model=TokenOut)
async def change_password(
    payload: ChangePasswordIn,
    request: Request,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TokenOut:
    if not verify_password(payload.current_password, user.password_hash):
        raise AppError("invalid_password", "当前密码不正确", 400)
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    user.token_version += 1
    await record(
        session,
        tenant_id=user.tenant_id,
        action="auth.change_password",
        actor=user,
        resource_type="user",
        resource_id=str(user.id),
        request=request,
    )
    await session.commit()
    token = create_token(user.email, user.tenant_id, user.token_version)
    return TokenOut(access_token=token, display_name=user.display_name)


@router.get("/me", response_model=MeOut)
async def me(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MeOut:
    tenant = await session.get(Tenant, user.tenant_id)
    if tenant is None or tenant.status != "active":
        raise AppError("tenant_suspended", "租户已停用，请联系平台管理员", 403)
    return MeOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        tenant_id=user.tenant_id,
        tenant_name=tenant.name,
        is_platform_admin=user.is_platform_admin,
        must_change_password=user.must_change_password,
    )
