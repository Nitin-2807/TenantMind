"""Unit tests for the Redis-backed rate limiter (Phase 7).

The key is deterministic and tenant/user-scoped
(``rate_limit:{tenant_id}:{user_id}:{window}``), the limit maps to an HTTP 429
at the route layer, and Redis failures fail open (the request is allowed).
"""

from __future__ import annotations

import time
from typing import cast

import pytest
import redis

from app.rag.rate_limit import RateLimiter, RateLimitExceeded, _rate_key
from tests.redis import FakeRedis


def _limiter(
    client: FakeRedis | None = None,
    *,
    limit: int,
    window_seconds: int = 60,
) -> RateLimiter:
    """A limiter over the fake; the cast reconciles fake-with-stub typing."""
    return RateLimiter(
        client=cast(redis.Redis, client or FakeRedis()),
        limit=limit,
        window_seconds=window_seconds,
    )


class TestRateKey:
    def test_key_is_tenant_and_user_scoped(self) -> None:
        key = _rate_key(tenant_id="tenant-a", user_id="user-1", window=123)

        assert key == "rate_limit:tenant-a:user-1:123"

    def test_key_differs_between_users_within_a_tenant(self) -> None:
        key_one = _rate_key(tenant_id="t", user_id="u1", window=1)
        key_two = _rate_key(tenant_id="t", user_id="u2", window=1)

        assert key_one != key_two

    def test_key_differs_between_tenants(self) -> None:
        key_one = _rate_key(tenant_id="t1", user_id="u", window=1)
        key_two = _rate_key(tenant_id="t2", user_id="u", window=1)

        assert key_one != key_two

    def test_key_differs_between_windows(self) -> None:
        key_one = _rate_key(tenant_id="t", user_id="u", window=1)
        key_two = _rate_key(tenant_id="t", user_id="u", window=2)

        assert key_one != key_two


class TestCheck:
    def test_requests_under_the_limit_are_allowed(self) -> None:
        limiter = _limiter(limit=3, window_seconds=60)

        for _ in range(3):
            limiter.check(tenant_id="t", user_id="u")  # must not raise

    def test_the_next_request_over_the_limit_is_refused(self) -> None:
        limiter = _limiter(limit=3, window_seconds=60)

        for _ in range(3):
            limiter.check(tenant_id="t", user_id="u")

        with pytest.raises(RateLimitExceeded) as excinfo:
            limiter.check(tenant_id="t", user_id="u")

        assert excinfo.value.retry_after >= 1

    def test_a_limit_of_zero_disables_the_limiter(self) -> None:
        limiter = _limiter(limit=0, window_seconds=60)

        for _ in range(100):
            limiter.check(tenant_id="t", user_id="u")  # never raises

    def test_counts_are_per_user_within_a_tenant(self) -> None:
        """One user exhausting the limit must not affect another user."""
        client = FakeRedis()
        limiter = _limiter(client=client, limit=1, window_seconds=60)

        limiter.check(tenant_id="t", user_id="u1")
        with pytest.raises(RateLimitExceeded):
            limiter.check(tenant_id="t", user_id="u1")

        # A different user in the same tenant still has their own quota.
        limiter.check(tenant_id="t", user_id="u2")
        with pytest.raises(RateLimitExceeded):
            limiter.check(tenant_id="t", user_id="u2")

    def test_counts_are_per_tenant(self) -> None:
        client = FakeRedis()
        limiter = _limiter(client=client, limit=1, window_seconds=60)

        limiter.check(tenant_id="t1", user_id="u")
        with pytest.raises(RateLimitExceeded):
            limiter.check(tenant_id="t1", user_id="u")

        limiter.check(tenant_id="t2", user_id="u")  # unaffected


class TestFailOpen:
    def test_redis_failure_allows_the_request(self) -> None:
        class BrokenRedis(FakeRedis):
            def incr(self, key: str) -> int:  # type: ignore[override]
                raise redis.RedisError("redis is down")

        limiter = _limiter(client=BrokenRedis(), limit=1, window_seconds=60)

        # Must not raise: the failure is logged and the request proceeds.
        limiter.check(tenant_id="t", user_id="u")


class TestWindow:
    def test_a_new_window_resets_the_counter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = FakeRedis()
        limiter = _limiter(client=client, limit=1, window_seconds=60)
        window = [100]
        monkeypatch.setattr(limiter, "_current_window", lambda: window[0])

        limiter.check(tenant_id="t", user_id="u")  # window 100
        with pytest.raises(RateLimitExceeded):
            limiter.check(tenant_id="t", user_id="u")

        # The fixed window advances: the counter key changes, so the old count
        # no longer applies and the user is allowed again.
        window[0] = 101
        limiter.check(tenant_id="t", user_id="u")
        with pytest.raises(RateLimitExceeded):
            limiter.check(tenant_id="t", user_id="u")

    def test_first_request_sets_the_key_expiry(self) -> None:
        client = FakeRedis()
        _limiter(client=client, limit=1, window_seconds=60).check(
            tenant_id="t", user_id="u"
        )

        key = _rate_key(tenant_id="t", user_id="u", window=int(time.time()) // 60)
        # The fake records the TTL the limiter asked for; 60 is the window size.
        assert client.expiries.get(key) == 60