from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.db import get_session
from app.models.user import User
from app.permissions import viewer_access
from app.services.live import snapshot_stream
from app.services.notifications import collect_notifications

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: str
    kind: str
    source: str
    severity: str
    title: str
    detail: str
    href: str
    created_at: str | None = None


class NotificationsOut(BaseModel):
    items: list[NotificationOut]
    counts: dict[str, int]


@router.get("", response_model=NotificationsOut)
async def list_notifications(
    user: User = Depends(viewer_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> NotificationsOut:
    payload = await collect_notifications(session, user.tenant_id)
    return NotificationsOut(**payload)


@router.get("/stream")
async def stream_notifications(
    frames: int | None = None,
    user: User = Depends(viewer_access),  # noqa: B008
) -> EventSourceResponse:
    tenant_id = user.tenant_id

    async def load(read: AsyncSession) -> dict[str, Any]:
        return await collect_notifications(read, tenant_id)

    # 通知中心常驻订阅：没有终态，客户端断开即结束；
    # frames 用于一次性快照（集成测试或 curl 冒烟）。
    return EventSourceResponse(
        snapshot_stream(
            load, status_of=lambda _payload: "live", max_frames=frames
        )
    )
