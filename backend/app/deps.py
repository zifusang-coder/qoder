from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
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
    user = (
        await session.execute(
            select(User).where(User.email == subject, User.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if user is None:
        raise AppError("unauthorized", "用户不存在", 401)
    return user
