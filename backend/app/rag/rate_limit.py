"""Redis-backed rate limiter (Phase 7).

Fixed-window counter with ``INCR`` + ``EXPIRE``. Key design is deterministic
and tenant/user-scoped: ``rate_limit:{tenant_id}:{user_id}:{window}`` where
``window`` is the current minute epoch (``int(time.time()) // 60``).

All operations are **fail-open**: any Redis error allows the request and
logs a warning.
"""

from __future__ import annotations

import logging
import time
from typing import cast

import redis

logger = logging.getLogger(__name__)

#: Key prefix for all rate limit entries.
_RATE_PREFIX = "rate_limit"

#: Field separator in the key.
_KEY_SEP = ":"


def _rate_key(*, tenant_id: str, user_id: str, window: int) -> str:
    """Build the Redis rate limit key.

    Format: ``rate_limit:{tenant_id}:{user_id}:{window}``

    Args:
        tenant_id: the tenant UUID (string form).
        user_id: the user UUID (string form).
        window: the current minute epoch (``int(time.time()) // 60``).

    Returns:
        The rate limit key string.
    """
    return _KEY_SEP.join([_RATE_PREFIX, tenant_id, user_id, str(window)])


class RateLimitExceeded(RuntimeError):
    """Raised when the rate limit is exceeded.

    The API layer maps this to ``HTTP 429``.
    """

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limit exceeded, retry after {retry_after}s")
        self.retry_after = retry_after


class RateLimiter:
    """Redis-backed fixed-window rate limiter.

    Attributes:
        client: the Redis client. Injected for testability.
        limit: maximum requests allowed per window.
        window_seconds: window size in seconds (default 60).
    """

    def __init__(
        self,
        client: redis.Redis,
        limit: int,
        window_seconds: int = 60,
    ) -> None:
        self._client = client
        self._limit = limit
        self._window = window_seconds

    def _current_window(self) -> int:
        return int(time.time()) // self._window

    def check(self, *, tenant_id: str, user_id: str) -> None:
        """Check and increment the counter for the current window.

        Raises:
            RateLimitExceeded: if the limit is exceeded. Contains
                ``retry_after`` seconds until the window resets.
        """
        if self._limit <= 0:
            return  # disabled

        window = self._current_window()
        key = _rate_key(tenant_id=tenant_id, user_id=user_id, window=window)

        try:
            # INCR returns the new value; if 1, set the TTL. (redis-py's stubs
            # type commands as possibly-awaitable for the cluster variant; the
            # sync client returns the value directly.)
            count = cast(int, self._client.incr(key))
            if count == 1:
                # First request in this window — set expiry to window size.
                self._client.expire(key, self._window)
        except redis.RedisError as exc:  # pragma: no cover - fail-open path
            logger.warning("rate limit check failed, allowing request: %s", exc)
            return

        if count > self._limit:
            # Calculate seconds until the window resets.
            window_end = (window + 1) * self._window
            retry_after = max(1, window_end - int(time.time()))
            raise RateLimitExceeded(retry_after=retry_after)