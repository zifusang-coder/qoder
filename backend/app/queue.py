import logging
from collections.abc import AsyncIterator

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import get_settings

logger = logging.getLogger(__name__)


async def get_queue() -> AsyncIterator[ArqRedis | None]:
    try:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    except Exception:
        # Redis 不可用时由调用方落库并返回可重试错误
        logger.exception("Redis 队列不可用")
        yield None
        return
    try:
        yield pool
    finally:
        await pool.aclose()
