import logging
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.user import User

logger = logging.getLogger(__name__)

BLOCKED_KEY_PARTS = {
    "password",
    "token",
    "secret",
    "apikey",
    "credential",
    "authorization",
    "cookie",
    "privatekey",
    "passwd",
    "pwd",
    "sessionid",
}
_UNSUPPORTED = object()


def _normalize_key(key: str) -> str:
    return "".join(char for char in key.lower() if char.isalnum())


def _is_blocked_key(key: str) -> bool:
    normalized = _normalize_key(key)
    return any(part in normalized for part in BLOCKED_KEY_PARTS)


def _clean_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:200]
    return _UNSUPPORTED


def sanitize_detail(detail: dict | None) -> dict:
    if not isinstance(detail, dict):
        return {}

    cleaned: dict[str, Any] = {}
    for key, value in detail.items():
        if not isinstance(key, str) or _is_blocked_key(key):
            continue
        if isinstance(value, dict):
            nested: dict[str, Any] = {}
            for nested_key, nested_value in value.items():
                if not isinstance(nested_key, str) or _is_blocked_key(nested_key):
                    continue
                scalar = _clean_scalar(nested_value)
                if scalar is not _UNSUPPORTED:
                    nested[nested_key] = scalar
            cleaned[key] = nested
        elif isinstance(value, list):
            items = []
            for item in value[:50]:
                scalar = _clean_scalar(item)
                if scalar is not _UNSUPPORTED:
                    items.append(scalar)
            cleaned[key] = items
        else:
            scalar = _clean_scalar(value)
            if scalar is not _UNSUPPORTED:
                cleaned[key] = scalar
    return cleaned


async def record(
    session: AsyncSession,
    *,
    tenant_id: str,
    action: str,
    actor: User | str,
    resource_type: str = "",
    resource_id: str = "",
    status: str = "success",
    detail: dict | None = None,
    request: Request | None = None,
) -> None:
    if isinstance(actor, str):
        actor_id = None
        actor_email = actor
    else:
        actor_id = str(actor.id)
        actor_email = actor.email

    ip = ""
    user_agent = ""
    if request is not None:
        ip = (request.client.host if request.client is not None else "")[:64]
        user_agent = (request.headers.get("user-agent") or "")[:300]

    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_id=actor_id,
            actor_email=actor_email,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            ip=ip,
            user_agent=user_agent,
            detail=sanitize_detail(detail),
        )
    )
    await session.flush()


async def record_best_effort(
    session: AsyncSession,
    *,
    tenant_id: str,
    action: str,
    actor: User | str,
    resource_type: str = "",
    resource_id: str = "",
    status: str = "success",
    detail: dict | None = None,
    request: Request | None = None,
) -> None:
    """用于已经提交或独立事件的审计；失败只记日志，不影响主流程。

    审计写入放在 SAVEPOINT 中执行，失败时只回滚审计行，不会 rollback 外层
    事务，因此调用方已加载的对象与未提交的业务写入都保持可用。仅在
    SAVEPOINT 成功后提交外层事务，所以本函数只适用于独立事件（如登录）；
    事务中途的业务写入请使用 ``record`` 加入调用方事务。
    """
    try:
        async with session.begin_nested():
            await record(
                session,
                tenant_id=tenant_id,
                action=action,
                actor=actor,
                resource_type=resource_type,
                resource_id=resource_id,
                status=status,
                detail=detail,
                request=request,
            )
    except Exception:  # 审计失败不能改变业务响应
        logger.exception("审计写入失败 action=%s", action)
        return

    try:
        await session.commit()
    except Exception:  # 提交失败时清理事务，但清理本身不能再抛出
        logger.exception("审计提交失败 action=%s", action)
        try:
            await session.rollback()
        except Exception:
            logger.exception("审计回滚失败 action=%s", action)
