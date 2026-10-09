"""Redis client factory and FastAPI dependency.

This module provides a single, cached :class:`redis.Redis` client built from
:class:`~app.core.config.Settings`. Both the query cache and the rate limiter
import this rather than constructing their own clients, which guarantees:

* One connection pool per process (module-level singleton, like the database
  engine in :mod:`app.db.engine`).
* Identical timeouts and decoding behaviour.
* A single place to override in tests via ``dependency_overrides``.
"""

from __future__ import annotations

from typing import Annotated

import redis
from fastapi import Depends

from app.core.config import Settings, get_settings

#: Default socket timeout for Redis operations (seconds).
#:
#: Short enough to fail fast and trigger the fail-open paths in the cache
#: and rate limiter; long enough for a healthy local Redis.
_REDIS_SOCKET_TIMEOUT = 2.0

#: Default socket connect timeout for Redis (seconds).
_REDIS_CONNECT_TIMEOUT = 2.0


def _build_client(settings: Settings) -> redis.Redis:
    """Create a new Redis client from settings.

    ``decode_responses=True`` returns ``str`` instead of ``bytes`` for
    convenience; the Celery broker uses its own connection with its own
    decoding, so there is no cross-talk.
    """
    return redis.Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_timeout=_REDIS_SOCKET_TIMEOUT,
        socket_connect_timeout=_REDIS_CONNECT_TIMEOUT,
        retry_on_timeout=False,
    )


_client: redis.Redis | None = None


def get_redis_client(app_settings: Annotated[Settings, Depends(get_settings)]) -> redis.Redis:
    """FastAPI dependency yielding the cached Redis client.

    The client is created lazily on first use and cached for the life of the
    process, mirroring :func:`app.db.engine.get_engine`. No connection is
    opened at import time; the first socket opens on the first call.

    A test can override this dependency with a fake or a client pointed at a
    test Redis instance via ``app.dependency_overrides[get_redis_client]``.
    """
    global _client

    if _client is None:
        _client = _build_client(app_settings)

    return _client


def reset_redis_client() -> None:
    """Drop the cached client so the next call rebuilds it.

    Needed by tests that change configuration, matching
    :func:`app.db.engine.reset_engine`.
    """
    global _client

    if _client is not None:
        try:
            _client.close()
        finally:
            _client = None


#: Annotated dependency form for route parameters.
RedisClient = Annotated[redis.Redis, Depends(get_redis_client)]