from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.audit import AuditLog
from app.models.user import User
from app.permissions import admin_access

router = APIRouter(prefix="/audit-logs", tags=["audit"])


class AuditLogOut(BaseModel):
    id: str
    actor_email: str
    action: str
    resource_type: str
    resource_id: str
    status: str
    ip: str
    user_agent: str
    detail: dict
    created_at: datetime


class AuditLogList(BaseModel):
    items: list[AuditLogOut]
    total: int


def _out(row: AuditLog) -> AuditLogOut:
    return AuditLogOut(
        id=str(row.id),
        actor_email=row.actor_email,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        status=row.status,
        ip=row.ip,
        user_agent=row.user_agent,
        detail=row.detail or {},
        created_at=row.created_at,
    )


@router.get("", response_model=AuditLogList)
async def list_audit_logs(
    action: str | None = None,
    status: Literal["success", "failure"] | None = None,
    actor_email: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    user: User = Depends(admin_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> AuditLogList:
    conditions = [AuditLog.tenant_id == user.tenant_id]
    if action is not None:
        conditions.append(AuditLog.action == action)
    if status is not None:
        conditions.append(AuditLog.status == status)
    if actor_email is not None:
        conditions.append(AuditLog.actor_email == actor_email)
    if from_ is not None:
        conditions.append(AuditLog.created_at >= from_)
    if to is not None:
        conditions.append(AuditLog.created_at <= to)

    total = (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(AuditLog)
            .where(*conditions)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return AuditLogList(items=[_out(row) for row in rows], total=total)
