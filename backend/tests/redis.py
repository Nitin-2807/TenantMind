"""In-memory fake Redis client for unit tests (Phase 7).

Implements exactly the surface the phase uses — the subset needed by
:class:`~app.rag.cache.QueryCache` and :class:`~app.rag.rate_limit.RateLimiter`:

* ``get`` / ``setex`` / ``delete`` / ``scan_iter`` for the query cache.
* ``incr`` / ``expire`` for the fixed-window rate limiter.

Deterministic and synchronous, so tests can seed a cache hit (``seed``) or
raise the rate-limit counter without a live Redis. Nothing here touches the
network, which is the point: the unit suites must run with no Redis server.
"""

from __future__ import annotations

import re
from collections.abc import Iterator


class FakeRedis:
    """A dict-backed stand-in for the small Redis API the phase uses.

    Attributes:
        store: the live entries. Tests may read or seed it directly.
        expiries: ``key -> ttl`` set by ``setex``. Purely advisory; a test that
            wants to exercise expiry can mutate this between calls.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self._counters: dict[str, int] = {}

    # -- string commands used by the cache ------------------------------------

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def setex(self, key: str, time: int, value: str) -> None:
        self.store[key] = value
        self.expiries[key] = time

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
            self.expiries.pop(key, None)
            self._counters.pop(key, None)
        return removed

    def scan_iter(self, match: str | None = None, count: int | None = None) -> Iterator[str]:
        snapshot = list(self.store)
        if match is None:
            yield from snapshot
            return
        regex = re.compile(match.replace("*", ".*"))
        for key in snapshot:
            if regex.fullmatch(key):
                yield key

    # -- counters used by the rate limiter ------------------------------------

    def incr(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def expire(self, key: str, time: int) -> bool:
        self.expiries[key] = time
        return True