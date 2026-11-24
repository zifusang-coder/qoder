import pytest

from app.config import get_settings
from app.main import app
from app.services.ratelimit import (
    InMemoryRateLimitStore,
    LoginRateLimiter,
    get_login_limiter,
)


def _limiter(store=None, *, email_limit=5, ip_limit=20, window_seconds=300, clock=None):
    kwargs = {
        "email_limit": email_limit,
        "ip_limit": ip_limit,
        "window_seconds": window_seconds,
    }
    if clock is not None:
        kwargs["clock"] = clock
    return LoginRateLimiter(store or InMemoryRateLimitStore(), **kwargs)


async def test_email_failures_block_until_window_rolls_over():
    now = [1200.0]
    limiter = _limiter(clock=lambda: now[0], email_limit=2, window_seconds=60)

    assert (await limiter.check("a@local", "1.1.1.1")).allowed
    await limiter.record_failure("a@local", "1.1.1.1")
    await limiter.record_failure("a@local", "1.1.1.1")

    blocked = await limiter.check("a@local", "1.1.1.1")
    assert blocked.allowed is False
    assert blocked.retry_after == 60

    now[0] += 60
    assert (await limiter.check("a@local", "1.1.1.1")).allowed


async def test_failures_are_shared_across_emails_from_one_ip():
    limiter = _limiter(email_limit=10, ip_limit=2)
    await limiter.record_failure("a@local", "1.1.1.1")
    await limiter.record_failure("b@local", "1.1.1.1")

    assert (await limiter.check("c@local", "1.1.1.1")).allowed is False
    assert (await limiter.check("c@local", "2.2.2.2")).allowed is True


async def test_success_clears_email_counter_but_not_ip_counter():
    limiter = _limiter(email_limit=2, ip_limit=3)
    await limiter.record_failure("a@local", "1.1.1.1")
    await limiter.record_success("a@local")
    await limiter.record_failure("a@local", "1.1.1.1")

    assert (await limiter.check("a@local", "1.1.1.1")).allowed is True


async def test_missing_ip_skips_ip_counter():
    limiter = _limiter(email_limit=10, ip_limit=1)
    await limiter.record_failure("a@local", "")
    await limiter.record_failure("b@local", "")

    assert (await limiter.check("c@local", "")).allowed is True


async def test_login_is_rate_limited_per_email(auth_client):
    settings = get_settings()
    for _ in range(5):
        resp = await auth_client.post(
            "/api/auth/login",
            json={"email": settings.admin_email, "password": "wrong"},
        )
        assert resp.status_code == 401

    blocked = await auth_client.post(
        "/api/auth/login",
        json={
            "email": settings.admin_email,
            "password": settings.admin_password,
        },
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_attempts"
    assert int(blocked.headers["retry-after"]) >= 1


async def test_successful_login_clears_email_failures(auth_client):
    settings = get_settings()
    for _ in range(4):
        await auth_client.post(
            "/api/auth/login",
            json={"email": settings.admin_email, "password": "wrong"},
        )

    ok = await auth_client.post(
        "/api/auth/login",
        json={
            "email": settings.admin_email,
            "password": settings.admin_password,
        },
    )
    assert ok.status_code == 200

    for _ in range(4):
        await auth_client.post(
            "/api/auth/login",
            json={"email": settings.admin_email, "password": "wrong"},
        )
    again = await auth_client.post(
        "/api/auth/login",
        json={
            "email": settings.admin_email,
            "password": settings.admin_password,
        },
    )
    assert again.status_code == 200


async def test_unknown_email_attempts_count_toward_ip_limit(auth_client):
    limiter = _limiter(email_limit=100, ip_limit=2)
    app.dependency_overrides[get_login_limiter] = lambda: limiter

    first = await auth_client.post(
        "/api/auth/login",
        json={"email": "nobody-1@local", "password": "wrong"},
    )
    assert first.status_code == 401
    second = await auth_client.post(
        "/api/auth/login",
        json={"email": "nobody-2@local", "password": "wrong"},
    )
    assert second.status_code == 401
    blocked = await auth_client.post(
        "/api/auth/login",
        json={"email": "nobody-3@local", "password": "wrong"},
    )
    assert blocked.status_code == 429


async def test_rate_limit_store_failure_fails_open(auth_client):
    class BrokenStore:
        async def get(self, key: str) -> int:
            raise RuntimeError("redis down")

        async def incr(self, key: str, window_seconds: int) -> int:
            raise RuntimeError("redis down")

        async def reset(self, key: str) -> None:
            raise RuntimeError("redis down")

    app.dependency_overrides[get_login_limiter] = lambda: _limiter(BrokenStore())

    settings = get_settings()
    resp = await auth_client.post(
        "/api/auth/login",
        json={
            "email": settings.admin_email,
            "password": settings.admin_password,
        },
    )
    assert resp.status_code == 200


@pytest.mark.parametrize("limit", [0, -1])
def test_limiter_rejects_non_positive_limits(limit):
    with pytest.raises(ValueError):
        _limiter(email_limit=limit)
