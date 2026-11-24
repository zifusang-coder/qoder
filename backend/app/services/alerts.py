"""失败告警：计数始终累加，Webhook 仅在配置后尽力投递。

告警体只包含资源标识与错误码，不发送提示词、凭据或运行消息正文，
与「原始凭据永不出现在日志/响应」的约束保持一致。
"""

import logging

import httpx

from app import metrics
from app.config import get_settings

logger = logging.getLogger(__name__)

ALERT_TIMEOUT_SECONDS = 3.0


async def report_failure(
    *,
    kind: str,
    resource_id: str,
    tenant_id: str | None,
    error_code: str,
) -> None:
    metrics.observe_failure(kind, error_code)
    settings = get_settings()
    url = settings.alert_webhook_url.strip()
    if not url:
        return
    payload = {
        "app": settings.app_name,
        "event": "run_failed",
        "kind": kind,
        "resource_id": resource_id,
        "tenant_id": tenant_id or "",
        "error_code": error_code,
    }
    try:
        async with httpx.AsyncClient(timeout=ALERT_TIMEOUT_SECONDS) as client:
            await client.post(url, json=payload)
    except Exception:  # noqa: BLE001 - 告警失败不能影响失败落库
        logger.warning("失败告警投递失败 kind=%s resource=%s", kind, resource_id)
