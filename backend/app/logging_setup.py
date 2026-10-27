import logging


def configure_http_logging() -> None:
    # httpx 默认在 INFO 级别记录完整请求 URL，公众号 access_token 会因此进入日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
