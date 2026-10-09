"""Service-level tests for usage analytics (Phase 7).

Analytics is metadata aggregation over the caller's own tenant's query logs —
never another tenant's, and never anything but aggregates. The admin route
enforces the role; this service only has to aggregate *correctly* and *scoped*.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.core.identity import AuthenticatedIdentity
from app.models.query_log import QueryLog
from app.models.user import UserRole
from app.services.analytics import get_usage_summary


def _identity(tenant_id: uuid.UUID, user_id: uuid.UUID) -> AuthenticatedIdentity:
    return AuthenticatedIdentity(
        user_id=user_id,
        tenant_id=tenant_id,
        role=UserRole.ADMIN,
    )


def _seed(
    session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    status: str = "success",
    cache_hit: bool = False,
    feedback: str | None = None,
    latency_ms: int | None = 100,
    created_at: datetime | None = None,
) -> QueryLog:
    row = QueryLog(
        tenant_id=tenant_id,
        user_id=user_id,
        question="q",
        answer="a",
        grounded=True,
        context_truncated=False,
        status=status,
        cache_hit=cache_hit,
        feedback=feedback,
        latency_ms=latency_ms,
        created_at=created_at or datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    return row


class TestUsageSummary:
    def test_empty_tenant_returns_zeros(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")

        summary = get_usage_summary(model_session, _identity(tenant.id, user.id))

        assert summary["total_queries"] == 0
        assert summary["total_answered"] == 0
        assert summary["total_failed"] == 0
        assert summary["avg_latency_ms"] is None
        assert summary["queries_last_24h"] == 0
        assert summary["queries_by_day"] == []
        assert summary["queries_by_user"] == []
        assert summary["cache_hit_rate"] == 0.0
        assert summary["feedback_up"] == 0
        assert summary["feedback_down"] == 0

    def test_aggregates_only_the_callers_tenant(
        self, model_session, make_committed_account
    ) -> None:
        tenant_a, user_a = make_committed_account(slug="acme", email="a@example.com")
        tenant_b, user_b = make_committed_account(slug="globex", email="b@example.com")
        _seed(model_session, tenant_id=tenant_a.id, user_id=user_a.id, latency_ms=100)
        _seed(model_session, tenant_id=tenant_a.id, user_id=user_a.id, latency_ms=200)
        # Rival tenant's traffic must not leak into the summary.
        _seed(model_session, tenant_id=tenant_b.id, user_id=user_b.id, latency_ms=9999)

        summary = get_usage_summary(model_session, _identity(tenant_a.id, user_a.id))

        assert summary["total_queries"] == 2
        assert summary["total_answered"] == 2
        assert summary["total_failed"] == 0
        assert summary["avg_latency_ms"] == 150.0
        assert summary["queries_by_user"] == [
            {"user_id": str(user_a.id), "count": 2}
        ]

    def test_success_error_cache_and_feedback_counts(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")
        _seed(model_session, tenant_id=tenant.id, user_id=user.id, status="success")
        _seed(
            model_session,
            tenant_id=tenant.id,
            user_id=user.id,
            status="success",
            cache_hit=True,
        )
        _seed(model_session, tenant_id=tenant.id, user_id=user.id, status="error")
        _seed(
            model_session,
            tenant_id=tenant.id,
            user_id=user.id,
            status="error",
            feedback="up",
        )
        _seed(
            model_session,
            tenant_id=tenant.id,
            user_id=user.id,
            status="success",
            feedback="down",
        )

        summary = get_usage_summary(model_session, _identity(tenant.id, user.id))

        assert summary["total_queries"] == 5
        assert summary["total_answered"] == 3
        assert summary["total_failed"] == 2
        assert summary["cache_hit_rate"] == 1 / 5
        assert summary["feedback_up"] == 1
        assert summary["feedback_down"] == 1

    def test_last_24h_and_day_series_respect_the_window(
        self, model_session, make_committed_account
    ) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")
        now = datetime.now(timezone.utc)
        _seed(model_session, tenant_id=tenant.id, user_id=user.id, created_at=now)
        _seed(
            model_session,
            tenant_id=tenant.id,
            user_id=user.id,
            created_at=now - timedelta(hours=2),
        )
        _seed(
            model_session,
            tenant_id=tenant.id,
            user_id=user.id,
            created_at=now - timedelta(days=5),
        )
        _seed(
            model_session,
            tenant_id=tenant.id,
            user_id=user.id,
            created_at=now - timedelta(days=6),
        )
        _seed(
            model_session,
            tenant_id=tenant.id,
            user_id=user.id,
            created_at=now - timedelta(days=40),  # outside the 30-day lookback
        )

        summary = get_usage_summary(model_session, _identity(tenant.id, user.id), days=30)

        assert summary["queries_last_24h"] == 2
        # Totals count everything; only the day series honours the lookback.
        assert summary["total_queries"] == 5
        assert len(summary["queries_by_day"]) == 3
        # The busiest day (today, with the two recent rows) sorts last: the
        # series is ordered by date ascending.
        assert summary["queries_by_day"][-1]["count"] == 2

    def test_latency_of_null_rows_is_excluded(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")
        _seed(model_session, tenant_id=tenant.id, user_id=user.id, latency_ms=300)
        _seed(model_session, tenant_id=tenant.id, user_id=user.id, latency_ms=None)

        summary = get_usage_summary(model_session, _identity(tenant.id, user.id))

        assert summary["total_queries"] == 2
        assert summary["avg_latency_ms"] == 300.0