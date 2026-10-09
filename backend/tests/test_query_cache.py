"""Unit tests for the Redis query cache (Phase 7).

Covers the two properties PMP §19 and the phase plan single out:

* the cache key **must** contain tenant context (``tenant:{tenant_id}:query:...``)
  and must capture the role, because authorization differs by role — an answer
  generated for one role must never be served to another;
* every operation is **fail-open**: a Redis error (or a corrupted entry) makes
  ``get`` return ``None`` and ``set`` return silently, so the query pipeline
  continues without caching.

The client is :class:`tests.redis.FakeRedis` — deterministic, no server.
"""

from __future__ import annotations

import uuid
from typing import cast

import redis

from app.rag.cache import QueryCache, _cache_key
from app.schemas.query import CitationResponse, QueryResponse
from tests.redis import FakeRedis


def _response(question: str = "What is the limit?", answer: str = "42") -> QueryResponse:
    return QueryResponse(
        question=question,
        answer=answer,
        citations=[
            CitationResponse(
                document_id=uuid.uuid4(),
                filename="limits.pdf",
                chunk_index=0,
                page=1,
            )
        ],
        grounded=True,
        context_truncated=False,
    )


def _cache(client: FakeRedis | None = None, ttl: int = 300) -> QueryCache:
    # The fake stands in for a real client at the type level too.
    return QueryCache(
        client=cast(redis.Redis, client or FakeRedis()),
        ttl_seconds=ttl,
    )


class TestCacheKey:
    def test_key_is_tenant_prefixed_and_role_hashed(self) -> None:
        key = _cache_key(
            tenant_id="11111111-1111-1111-1111-111111111111",
            role="admin",
            question="What is the limit?",
        )

        assert key.startswith("tenant:11111111-1111-1111-1111-111111111111:query:")
        # The hash portion is exactly 32 hex chars (a truncated SHA-256).
        digest = key.rsplit(":", 1)[1]
        assert len(digest) == 32
        assert all(c in "0123456789abcdef" for c in digest)

    def test_key_captures_the_role(self) -> None:
        """Two roles asking the same question must key different cache slots."""
        key_admin = _cache_key(tenant_id="t", role="admin", question="same question")
        key_viewer = _cache_key(tenant_id="t", role="viewer", question="same question")

        assert key_admin != key_viewer

    def test_key_isolation_between_tenants(self) -> None:
        """Same role and question, different tenants → different keys."""
        key_a = _cache_key(tenant_id="tenant-a", role="viewer", question="same")
        key_b = _cache_key(tenant_id="tenant-b", role="viewer", question="same")

        assert key_a != key_b

    def test_key_is_deterministic(self) -> None:
        key_one = _cache_key(tenant_id="t", role="viewer", question="q")
        key_two = _cache_key(tenant_id="t", role="viewer", question="q")

        assert key_one == key_two

    def test_key_differs_by_question(self) -> None:
        key_one = _cache_key(tenant_id="t", role="viewer", question="one")
        key_two = _cache_key(tenant_id="t", role="viewer", question="two")

        assert key_one != key_two


class TestGetSet:
    def test_round_trip(self) -> None:
        cache = _cache()
        response = _response()

        cache.set(tenant_id="t", role="viewer", question="q", response=response)
        got = cache.get(tenant_id="t", role="viewer", question="q")

        assert got is not None
        assert got.question == response.question
        assert got.answer == response.answer
        assert got.grounded is True
        assert got.citations[0].filename == "limits.pdf"

    def test_miss_returns_none(self) -> None:
        cache = _cache()

        assert cache.get(tenant_id="t", role="viewer", question="nothing cached") is None

    def test_role_never_shares_a_slot(self) -> None:
        """An answer cached for one role must be a miss for every other role."""
        cache = _cache()
        cache.set(tenant_id="t", role="admin", question="q", response=_response())

        assert cache.get(tenant_id="t", role="viewer", question="q") is None
        assert cache.get(tenant_id="t", role="editor", question="q") is None
        assert cache.get(tenant_id="t", role="admin", question="q") is not None

    def test_tenant_never_shares_a_slot(self) -> None:
        cache = _cache()
        cache.set(tenant_id="tenant-a", role="viewer", question="q", response=_response())

        assert cache.get(tenant_id="tenant-b", role="viewer", question="q") is None

    def test_ttl_of_zero_disables_caching(self) -> None:
        cache = _cache(ttl=0)

        cache.set(tenant_id="t", role="viewer", question="q", response=_response())

        assert cache.get(tenant_id="t", role="viewer", question="q") is None


class TestFailOpen:
    def test_get_failure_returns_none(self) -> None:
        class BrokenRedis(FakeRedis):
            def get(self, key: str) -> str | None:  # type: ignore[override]
                raise redis.RedisError("redis is down")

        cache = _cache(client=BrokenRedis())
        # Must not raise; the pipeline falls through to retrieval.
        assert cache.get(tenant_id="t", role="viewer", question="q") is None

    def test_set_failure_is_swallowed(self) -> None:
        class BrokenRedis(FakeRedis):
            def setex(self, key: str, time: int, value: str) -> None:  # type: ignore[override]
                raise redis.RedisError("redis is down")

        cache = _cache(client=BrokenRedis())
        # Must not raise.
        cache.set(tenant_id="t", role="viewer", question="q", response=_response())

    def test_corrupted_entry_is_discarded(self) -> None:
        client = FakeRedis()
        cache = _cache(client=client)
        key = _cache_key(tenant_id="t", role="viewer", question="q")
        client.store[key] = "not json at all"

        assert cache.get(tenant_id="t", role="viewer", question="q") is None
        # The bad entry was deleted so it cannot keep failing.
        assert key not in client.store

    def test_invalidate_tenant_removes_only_that_tenant(self) -> None:
        client = FakeRedis()
        cache = _cache(client=client)
        cache.set(tenant_id="tenant-a", role="viewer", question="q", response=_response())
        cache.set(tenant_id="tenant-b", role="viewer", question="q", response=_response())

        cache.invalidate_tenant("tenant-a")

        assert cache.get(tenant_id="tenant-a", role="viewer", question="q") is None
        assert cache.get(tenant_id="tenant-b", role="viewer", question="q") is not None