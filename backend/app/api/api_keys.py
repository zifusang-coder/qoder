from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models.api_key import ApiKey
from app.models.user import User
from app.permissions import admin_access
from app.services import api_keys as api_keys_service
from app.services.audit import record_best_effort

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    scopes: list[str] = Field(default_factory=list)


class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    scopes: list[str]
    created_by: str
    created_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreatedOut(ApiKeyOut):
    secret: str


def _key_out(row: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=str(row.id),
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes or []),
        created_by=str(row.created_by),
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


@router.get("", response_model=list[ApiKeyOut])
async def list_api_keys(
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[ApiKeyOut]:
    rows = await api_keys_service.list_keys(session, user.tenant_id)
    return [_key_out(row) for row in rows]


@router.post("", response_model=ApiKeyCreatedOut, status_code=201)
async def create_api_key(
    payload: ApiKeyIn,
    request: Request,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ApiKeyCreatedOut:
    name = payload.name.strip()
    if not name:
        raise AppError("invalid_payload", "名称不能为空", 422)
    row, secret = await api_keys_service.create_key(
        session,
        tenant_id=user.tenant_id,
        created_by=user.id,
        name=name,
        scopes=payload.scopes,
    )
    await record_best_effort(
        session,
        tenant_id=user.tenant_id,
        action="api_key.create",
        actor=user,
        resource_type="api_key",
        resource_id=str(row.id),
        detail={"name": name, "prefix": row.prefix, "scopes": list(row.scopes or [])},
        request=request,
    )
    return ApiKeyCreatedOut(**_key_out(row).model_dump(), secret=secret)


@router.delete("/{key_id}", response_model=ApiKeyOut)
async def revoke_api_key(
    key_id: str,
    request: Request,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ApiKeyOut:
    row, changed = await api_keys_service.revoke_key(
        session, tenant_id=user.tenant_id, key_id=key_id
    )
    if changed:
        await record_best_effort(
            session,
            tenant_id=user.tenant_id,
            action="api_key.revoke",
            actor=user,
            resource_type="api_key",
            resource_id=str(row.id),
            detail={"name": row.name, "prefix": row.prefix},
            request=request,
        )
    return _key_out(row)
