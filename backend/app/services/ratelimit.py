import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from redis.asyncio import Redis

from app.config import get_settings


class RateLimitStore(Protocol):
    async def get(self, key: str) -> int: ...

    async def incr(self, key: str, window_seconds: int) -> int: ...

    async def reset(self, key: str) -> None: ...


class InMemoryRateLimitStore:
    """测试用固定窗口存储；clock 可注入以便验证窗口滚动。"""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._counters: dict[str, tuple[float, int]] = {}

    async def get(self, key: str) -> int:
        expires_at, count = self._counters.get(key, (0.0, 0))
        if expires_at <= self._clock():
            return 0
        return count

    async def incr(self, key: str, window_seconds: int) -> int:
        now = self._clock()
        expires_at, count = self._counters.get(key, (0.0, 0))
        if expires_at <= now:
            expires_at, count = now + window_seconds, 0
        count += 1
        self._counters[key] = (expires_at, count)
        return count

    async def reset(self, key: str) -> None:
        self._counters.pop(key, None)


class RedisRateLimitStore:
    def __init__(self, client: Redis):
        self._client = client

    async def get(self, key: str) -> int:
        return int(await self._client.get(key) or 0)

    async def incr(self, key: str, window_seconds: int) -> int:
        async with self._client.pipeline(transaction=False) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds)
            count, _ = await pipe.execute()
        return int(count)

    async def reset(self, key: str) -> None:
        await self._client.delete(key)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int = 0


@dataclass(frozen=True)
class LoginRateLimiter:
    store: RateLimitStore = field(repr=False)
    email_limit: int = 5
    ip_limit: int = 20
    window_seconds: int = 300
    clock: Callable[[], float] = field(default=time.time, repr=False)

    def __post_init__(self) -> None:
        if self.email_limit <= 0 or self.ip_limit <= 0:
            raise ValueError("限流阈值必须为正数")
        if self.window_seconds <= 0:
            raise ValueError("限流窗口必须为正数")

    def _bucket(self, now: float) -> int:
        return int(now // self.window_seconds)

    def _email_key(self, email: str, bucket: int) -> str:
        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        return f"rl:login:email:{digest}:{bucket}"

    def _ip_key(self, ip: str, bucket: int) -> str:
        return f"rl:login:ip:{ip}:{bucket}"

    async def check(self, email: str, ip: str) -> RateLimitDecision:
        now = self.clock()
        bucket = self._bucket(now)
        retry_after = max(1, self.window_seconds - int(now % self.window_seconds))
        email_count = await self.store.get(self._email_key(email, bucket))
        if email_count >= self.email_limit:
            return RateLimitDecision(False, retry_after)
        if ip:
            ip_count = await self.store.get(self._ip_key(ip, bucket))
            if ip_count >= self.ip_limit:
                return RateLimitDecision(False, retry_after)
        return RateLimitDecision(True)

    async def record_failure(self, email: str, ip: str) -> None:
        bucket = self._bucket(self.clock())
        await self.store.incr(self._email_key(email, bucket), self.window_seconds)
        if ip:
            await self.store.incr(self._ip_key(ip, bucket), self.window_seconds)

    async def record_success(self, email: str) -> None:
        bucket = self._bucket(self.clock())
        await self.store.reset(self._email_key(email, bucket))


@lru_cache
def get_login_limiter() -> LoginRateLimiter:
    settings = get_settings()
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    return LoginRateLimiter(
        RedisRateLimitStore(client),
        email_limit=settings.login_rate_limit_email,
        ip_limit=settings.login_rate_limit_ip,
        window_seconds=settings.login_rate_limit_window_seconds,
    )
