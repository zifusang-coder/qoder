"""把轮询型资源详情转成 SSE 快照流。

前端订阅后由服务端按固定间隔读取最新快照，内容变化时推送一帧，
资源进入终态后主动结束，避免前端各页面重复实现轮询。
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.errors import AppError

logger = logging.getLogger(__name__)

LIVE_INTERVAL = 1.0
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _error_event(code: str, message: str) -> dict[str, str]:
    return {
        "data": json.dumps(
            {"error": {"code": code, "message": message}}, ensure_ascii=False
        )
    }


def _data_event(payload: dict[str, Any]) -> dict[str, str]:
    return {"data": json.dumps(payload, ensure_ascii=False)}


async def snapshot_stream(
    load: Callable[[AsyncSession], Awaitable[dict[str, Any]]],
    *,
    status_of: Callable[[dict[str, Any]], str] | None = None,
    interval: float = LIVE_INTERVAL,
) -> AsyncIterator[dict[str, str]]:
    """按周期读取快照，只在内容变化时推送，终态后结束。

    ``load`` 每次调用都会拿到独立的会话，因为流可能长于请求依赖的生命周期。
    ``status_of`` 提取快照里的运行状态，默认读顶层的 ``status``。
    """
    get_status = status_of or (lambda payload: str(payload.get("status", "")))
    last: str | None = None
    while True:
        try:
            async with SessionLocal() as session:
                payload = await load(session)
        except AppError as exc:
            yield _error_event(exc.code, exc.message)
            return
        except Exception:
            logger.exception("实时快照读取失败")
            yield _error_event("live_stream_failed", "实时推送失败")
            return
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if text != last:
            last = text
            yield _data_event(payload)
        if get_status(payload) in TERMINAL_STATUSES:
            yield {"data": "[DONE]"}
            return
        await asyncio.sleep(interval)
