import secrets
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import (
    BaseModel,
    StringConstraints,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.members import MemberCreatedOut
from app.db import get_session
from app.errors import AppError
from app.models.tenant import Tenant
from app.models.user import User
from app.permissions import require_platform_admin
from app.security import hash_password
from app.services.audit import record

router = APIRouter(prefix="/tenants", tags=["tenants"])

Name = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=2,
        max_length=50,
        pattern=r"^[a-z0-9][a-z0-9-]{1,49}$",
    ),
]
Email = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=255)
]


class TenantOut(BaseModel):
    id: str
    name: str
    slug: str
    status: str
    members_count: int
    created_at: datetime


class TenantCreatedOut(TenantOut):
    owner: MemberCreatedOut


class TenantCreateIn(BaseModel):
    name: Name
    slug: Slug
    owner_email: Email
    owner_display_name: Name

    @field_validator("owner_email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.lower()


class TenantUpdateIn(BaseModel):
    name: Name | None = None
    status: Literal["active", "suspended"] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> "TenantUpdateIn":
        if not self.model_fields_set:
            raise ValueError("至少需要提供一个要更新的字段")
        return self

    @model_validator(mode="after")
    def _reject_explicit_nulls(self) -> "TenantUpdateIn":
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} 不能为空")
        return self


def _out(tenant: Tenant, members_count: int) -> TenantOut:
    return TenantOut(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
        members_count=members_count,
        created_at=tenant.created_at,
    )


async def _member_count(session: AsyncSession, tenant_id: str) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(User)
            .where(User.tenant_id == tenant_id)
        )
    ).scalar_one()


@router.get("", response_model=list[TenantOut])
async def list_tenants(
    _: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[TenantOut]:
    counts = (
        select(User.tenant_id, func.count().label("cnt"))
        .group_by(User.tenant_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Tenant, func.coalesce(counts.c.cnt, 0))
            .outerjoin(counts, counts.c.tenant_id == Tenant.id)
            .order_by(Tenant.created_at.desc(), Tenant.id.desc())
        )
    ).all()
    return [_out(tenant, count) for tenant, count in rows]


@router.post("", response_model=TenantCreatedOut, status_code=201)
async def create_tenant(
    payload: TenantCreateIn,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TenantCreatedOut:
    slug_taken = (
        await session.execute(
            select(Tenant).where(Tenant.slug == payload.slug)
        )
    ).scalar_one_or_none()
    if slug_taken is not None:
        raise AppError("slug_exists", "租户标识已存在", 409)
    email_taken = (
        await session.execute(
            select(User).where(User.email == payload.owner_email)
        )
    ).scalar_one_or_none()
    if email_taken is not None:
        raise AppError("email_exists", "该邮箱已被使用", 409)

    initial_password = secrets.token_urlsafe(12)
    tenant = Tenant(
        id=str(uuid.uuid4()),
        name=payload.name,
        slug=payload.slug,
        status="active",
    )
    owner = User(
        tenant_id=tenant.id,
        email=payload.owner_email,
        password_hash=hash_password(initial_password),
        display_name=payload.owner_display_name,
        role="owner",
        is_active=True,
        must_change_password=True,
        is_platform_admin=False,
        token_version=0,
    )
    session.add(tenant)
    session.add(owner)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        if (
            await session.execute(
                select(Tenant.id).where(Tenant.slug == payload.slug)
            )
        ).first() is not None:
            raise AppError("slug_exists", "租户标识已存在", 409) from None
        if (
            await session.execute(
                select(User.id).where(User.email == payload.owner_email)
            )
        ).first() is not None:
            raise AppError("email_exists", "该邮箱已被使用", 409) from None
        raise
    await record(
        session,
        tenant_id=user.tenant_id,
        action="tenant.create",
        actor=user,
        resource_type="tenant",
        resource_id=tenant.id,
        detail={
            "slug": tenant.slug,
            "owner_email": owner.email,
        },
        request=request,
    )
    await session.commit()
    await session.refresh(tenant)
    await session.refresh(owner)
    owner_out = MemberCreatedOut(
        id=str(owner.id),
        email=owner.email,
        display_name=owner.display_name,
        role=owner.role,
        is_active=owner.is_active,
        created_at=owner.created_at,
        initial_password=initial_password,
    )
    return TenantCreatedOut(**_out(tenant, 1).model_dump(), owner=owner_out)


@router.patch("/{tenant_id}", response_model=TenantOut)
async def update_tenant(
    tenant_id: str,
    payload: TenantUpdateIn,
    request: Request,
    user: User = Depends(require_platform_admin),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> TenantOut:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        raise AppError("not_found", "租户不存在", 404)
    fields = payload.model_dump(exclude_unset=True)
    if fields.get("status") == "suspended" and tenant.id == user.tenant_id:
        raise AppError("cannot_suspend_self", "不能停用自己的租户", 409)

    turning_off = (
        fields.get("status") == "suspended" and tenant.status != "suspended"
    )
    for key, value in fields.items():
        setattr(tenant, key, value)
    if turning_off:
        await session.execute(
            update(User)
            .where(User.tenant_id == tenant.id)
            .values(token_version=User.token_version + 1)
        )
    await record(
        session,
        tenant_id=user.tenant_id,
        action="tenant.update",
        actor=user,
        resource_type="tenant",
        resource_id=tenant.id,
        detail={"status": tenant.status},
        request=request,
    )
    await session.commit()
    await session.refresh(tenant)
    return _out(tenant, await _member_count(session, tenant.id))
