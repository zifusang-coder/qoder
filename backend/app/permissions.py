from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

from fastapi import Depends

from app.deps import get_current_user
from app.errors import AppError
from app.models.user import User

Role = Literal["viewer", "editor", "admin", "owner"]

ROLE_RANK: dict[str, int] = {
    "viewer": 0,
    "editor": 1,
    "admin": 2,
    "owner": 3,
}

UserDep = Annotated[User, Depends(get_current_user)]


def has_role(user: User, min_role: Role) -> bool:
    return user.role in ROLE_RANK and ROLE_RANK[user.role] >= ROLE_RANK[min_role]


def require_role(min_role: Role) -> Callable[..., Awaitable[User]]:
    async def dependency(user: UserDep) -> User:
        if not has_role(user, min_role):
            raise AppError("forbidden", "权限不足", 403)
        return user

    return dependency


async def require_platform_admin(user: UserDep) -> User:
    if not user.is_platform_admin:
        raise AppError("platform_admin_required", "需要平台管理员权限", 403)
    return user


viewer_access = require_role("viewer")
editor_access = require_role("editor")
admin_access = require_role("admin")
owner_access = require_role("owner")
