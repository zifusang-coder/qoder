import secrets
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import (
    BaseModel,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models.user import User
from app.permissions import Role, admin_access, owner_access
from app.security import hash_password
from app.services.audit import record
from app.services.members import ensure_not_last_owner

router = APIRouter(prefix="/members", tags=["members"])

Email = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=255)
]
DisplayName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class MemberOut(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime


class MemberCreatedOut(MemberOut):
    initial_password: str


class MemberCreateIn(BaseModel):
    email: Email
    display_name: DisplayName
    role: Role = "editor"

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.lower()


class MemberUpdateIn(BaseModel):
    display_name: DisplayName | None = None
    role: Role | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "MemberUpdateIn":
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个要更新的字段")
        return self


def _out(member: User) -> MemberOut:
    return MemberOut(
        id=str(member.id),
        email=member.email,
        display_name=member.display_name,
        role=member.role,
        is_active=member.is_active,
        created_at=member.created_at,
    )


async def _owned_member(
    session: AsyncSession, member_id: str, actor: User
) -> User:
    try:
        parsed = uuid.UUID(member_id)
    except (ValueError, TypeError):
        raise AppError("not_found", "成员不存在", 404) from None
    member = (
        await session.execute(
            select(User).where(
                User.id == parsed,
                User.tenant_id == actor.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise AppError("not_found", "成员不存在", 404)
    return member


@router.get("", response_model=list[MemberOut])
async def list_members(
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[MemberOut]:
    rows = (
        await session.execute(
            select(User)
            .where(User.tenant_id == user.tenant_id)
            .order_by(User.created_at, User.id)
        )
    ).scalars().all()
    return [_out(member) for member in rows]


@router.post("", response_model=MemberCreatedOut, status_code=201)
async def create_member(
    payload: MemberCreateIn,
    request: Request,
    user: User = Depends(owner_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MemberCreatedOut:
    existing = (
        await session.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise AppError("email_exists", "该邮箱已被使用", 409)
    initial_password = secrets.token_urlsafe(12)
    member = User(
        tenant_id=user.tenant_id,
        email=payload.email,
        password_hash=hash_password(initial_password),
        display_name=payload.display_name,
        role=payload.role,
        is_active=True,
        must_change_password=True,
        is_platform_admin=False,
        token_version=0,
    )
    session.add(member)
    try:
        await session.flush()
        await record(
            session,
            tenant_id=user.tenant_id,
            action="member.create",
            actor=user,
            resource_type="user",
            resource_id=str(member.id),
            detail={"role": member.role},
            request=request,
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise AppError("email_exists", "该邮箱已被使用", 409) from None
    await session.refresh(member)
    return MemberCreatedOut(
        **_out(member).model_dump(), initial_password=initial_password
    )


@router.patch("/{member_id}", response_model=MemberOut)
async def update_member(
    member_id: str,
    payload: MemberUpdateIn,
    request: Request,
    user: User = Depends(owner_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> MemberOut:
    member = await _owned_member(session, member_id, user)
    fields = payload.model_dump(exclude_unset=True)
    if "role" in fields and member.id == user.id:
        raise AppError("cannot_modify_self", "不能修改自己的角色", 409)
    if fields.get("is_active") is False and member.id == user.id:
        raise AppError("cannot_modify_self", "不能停用自己", 409)

    role_changed = "role" in fields and fields["role"] != member.role
    active_changed = (
        "is_active" in fields and fields["is_active"] != member.is_active
    )
    if role_changed or (active_changed and member.is_active):
        await ensure_not_last_owner(session, user.tenant_id, member)

    for key, value in fields.items():
        setattr(member, key, value)
    if role_changed or active_changed:
        member.token_version += 1
    await record(
        session,
        tenant_id=user.tenant_id,
        action="member.update",
        actor=user,
        resource_type="user",
        resource_id=str(member.id),
        detail={"role": member.role, "is_active": member.is_active},
        request=request,
    )
    await session.commit()
    await session.refresh(member)
    return _out(member)


@router.delete("/{member_id}", status_code=204)
async def disable_member(
    member_id: str,
    request: Request,
    user: User = Depends(owner_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    member = await _owned_member(session, member_id, user)
    if member.id == user.id:
        raise AppError("cannot_modify_self", "不能停用自己", 409)
    await ensure_not_last_owner(session, user.tenant_id, member)
    if member.is_active:
        member.is_active = False
        member.token_version += 1
    await record(
        session,
        tenant_id=user.tenant_id,
        action="member.disable",
        actor=user,
        resource_type="user",
        resource_id=str(member.id),
        detail={"is_active": False},
        request=request,
    )
    await session.commit()
    return Response(status_code=204)
