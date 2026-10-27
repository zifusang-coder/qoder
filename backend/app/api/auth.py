from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models.user import User
from app.security import create_token, hash_password, verify_password

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


class MeOut(BaseModel):
    email: str
    display_name: str


@router.post("/login", response_model=TokenOut)
async def login(
    payload: LoginIn, session: AsyncSession = Depends(get_session)  # noqa: B008
) -> TokenOut:
    user = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if user is None:
        verify_password(payload.password, _DUMMY_HASH)
        raise AppError("invalid_credentials", "邮箱或密码错误", 401)
    if not verify_password(payload.password, user.password_hash):
        raise AppError("invalid_credentials", "邮箱或密码错误", 401)
    return TokenOut(
        access_token=create_token(user.email, user.tenant_id),
        display_name=user.display_name,
    )


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> MeOut:  # noqa: B008
    return MeOut(email=user.email, display_name=user.display_name)
