from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models.tenant import Tenant
from app.models.user import User
from app.security import decode_token


async def get_current_user(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError("unauthorized", "缺少登录凭证", 401)
    try:
        payload = decode_token(authorization.removeprefix("Bearer ").strip())
    except Exception as exc:  # token 过期、签名错误统一按未授权处理
        raise AppError("unauthorized", "登录已失效，请重新登录", 401) from exc
    subject = payload.get("sub")
    tenant_id = payload.get("tid")
    if not subject or not tenant_id:
        raise AppError("unauthorized", "登录已失效，请重新登录", 401)
    row = (
        await session.execute(
            select(User, Tenant)
            .outerjoin(Tenant, Tenant.id == User.tenant_id)
            .where(User.email == subject, User.tenant_id == tenant_id)
        )
    ).first()
    if row is None:
        raise AppError("unauthorized", "用户不存在", 401)
    user, tenant = row
    if not user.is_active:
        raise AppError("account_disabled", "账号已停用，请联系租户管理员", 403)
    if tenant is None or tenant.status != "active":
        raise AppError("tenant_suspended", "租户已停用，请联系平台管理员", 403)
    if payload["ver"] != user.token_version:
        raise AppError("token_revoked", "登录状态已失效，请重新登录", 401)
    return user


async def get_current_member(
    user: Annotated[User, Depends(get_current_user)],
) -> User:
    """业务接口默认依赖；首次登录未改密时只允许访问 /auth/me 与改密接口。"""
    if user.must_change_password:
        raise AppError("password_change_required", "请先修改初始密码", 403)
    return user
