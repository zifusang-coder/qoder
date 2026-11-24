import logging
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import metrics

logger = logging.getLogger("newmedia.request")

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    return _request_id.get()


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _request_id.get()
        return True


class RequestContextMiddleware:
    """分配/透传请求 ID，记录请求指标，并输出带关联 ID 的访问日志。

    使用纯 ASGI 中间件而不是 BaseHTTPMiddleware，避免包装 SSE 流式响应。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id")
        request_id = (
            incoming.decode("latin-1").strip()
            if incoming
            else uuid.uuid4().hex
        )
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id.set(request_id)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        method = scope.get("method", "")
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            metrics.observe_request(method, path, status)
            # 只记录路由模板与状态码，不记录查询串，避免凭据进入日志
            logger.info(
                "%s %s -> %d %.1fms", method, path, status,
                (time.perf_counter() - started) * 1000,
            )
            _request_id.reset(token)


def configure_http_logging() -> None:
    # httpx 默认在 INFO 级别记录完整请求 URL，公众号 access_token 会因此进入日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
        )
    for handler in root.handlers:
        if not any(isinstance(item, RequestIdFilter) for item in handler.filters):
            handler.addFilter(RequestIdFilter())
