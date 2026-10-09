"""Redis-backed query response cache (Phase 7).

Caches the serialized :class:`~app.schemas.query.QueryResponse` keyed by
tenant, role, and question. The cache is strictly tenant-scoped and
role-aware so that a cached answer generated for one role is never served
to a different role within the same tenant.

All operations are **fail-open**: any Redis error is logged and the call
returns ``None`` (on ``get``) or silently succeeds (on ``set``), allowing
the query pipeline to continue without caching.
"""

from __future__ import annotations

import hashlib
import logging
from typing import cast

import redis

from app.schemas.query import QueryResponse

logger = logging.getLogger(__name__)

#: Key prefix for all query cache entries.
_CACHE_PREFIX = "tenant"

#: Field separator in the key.
_KEY_SEP = ":"


def _cache_key(*, tenant_id: str, role: str, question: str) -> str:
    """Build the Redis cache key.

    Format: ``tenant:{tenant_id}:query:{hash}`` where ``hash`` is a
    deterministic SHA-256 of ``role:question`` truncated to 32 hex chars.
    Including the role in the hash ensures that answers generated under one
    role are not served to another role within the same tenant.

    Args:
        tenant_id: the tenant UUID (string form).
        role: the caller's role (e.g. "admin", "editor", "viewer").
        question: the trimmed user question.

    Returns:
        The cache key string.
    """
    # Role is part of the hash input so that different roles get different keys.
    hash_input = f"{role}:{question}"
    digest = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:32]
    return _KEY_SEP.join([_CACHE_PREFIX, tenant_id, "query", digest])


class QueryCache:
    """Redis-backed cache for query responses.

    Attributes:
        client: the Redis client. Injected for testability; defaults to the
            process-wide client from :func:`app.core.redis.get_redis_client`.
        ttl_seconds: time-to-live for cache entries. ``0`` disables caching.
    """

    def __init__(
        self,
        client: redis.Redis,
        ttl_seconds: int,
    ) -> None:
        self._client = client
        self._ttl = ttl_seconds

    def get(self, *, tenant_id: str, role: str, question: str) -> QueryResponse | None:
        """Retrieve a cached response, or ``None`` on miss or error.

        Fail-open: any Redis exception is logged and ``None`` is returned.
        """
        if self._ttl <= 0:
            return None

        key = _cache_key(tenant_id=tenant_id, role=role, question=question)
        try:
            # redis-py's stubs type commands as possibly-awaitable (for the
            # cluster variant); the sync client returns the value directly.
            data = cast(str | bytes | None, self._client.get(key))
        except redis.RedisError as exc:  # pragma: no cover - fail-open path
            logger.warning("query cache get failed: %s", exc)
            return None

        if not data:
            return None

        try:
            return QueryResponse.model_validate_json(data)
        except (ValueError, TypeError) as exc:  # pragma: no cover - corrupted cache entry
            logger.warning("query cache entry invalid, discarding: %s", exc)
            # Best-effort delete to avoid repeated parse failures.
            try:
                self._client.delete(key)
            except redis.RedisError:
                pass
            return None

    def set(
        self,
        *,
        tenant_id: str,
        role: str,
        question: str,
        response: QueryResponse,
    ) -> None:
        """Store a response in the cache.

        Fail-open: any Redis exception is logged and the call returns
        silently; the query pipeline continues unaffected.
        """
        if self._ttl <= 0:
            return

        key = _cache_key(tenant_id=tenant_id, role=role, question=question)
        try:
            # Use JSON serialization via pydantic for stability.
            self._client.setex(key, self._ttl, response.model_dump_json())
        except redis.RedisError as exc:  # pragma: no cover - fail-open path
            logger.warning("query cache set failed: %s", exc)

    def invalidate_tenant(self, tenant_id: str) -> None:
        """Delete all cache keys for a tenant.

        Used when documents are created or deleted so that stale answers
        are not served. Best-effort: logs and swallows any Redis error.
        """
        pattern = _KEY_SEP.join([_CACHE_PREFIX, tenant_id, "query", "*"])
        try:
            # ``scan_iter`` is safe for production; ``KEYS`` would block.
            for key in self._client.scan_iter(match=pattern, count=100):
                self._client.delete(key)
        except redis.RedisError as exc:  # pragma: no cover - fail-open path
            logger.warning("query cache invalidate failed: %s", exc)