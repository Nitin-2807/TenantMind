"""Usage analytics service (Phase 7).

Provides aggregated query statistics for the admin usage endpoint.
All queries are tenant-scoped via the authenticated identity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.identity import AuthenticatedIdentity
from app.models.query_log import QueryLog


def get_usage_summary(
    session: Session,
    identity: AuthenticatedIdentity,
    *,
    days: int = 30,
) -> dict:
    """Return aggregated usage statistics for the caller's tenant.

    Args:
        session: the request's database session.
        identity: the authenticated caller (must be admin; enforced at route level).
        days: lookback window for time-series data (default 30).

    Returns:
        A dict with the following keys:
        - total_queries: total query_logs rows for this tenant.
        - total_answered: count where status='success'.
        - total_failed: count where status='error'.
        - avg_latency_ms: average latency_ms for completed queries (excludes None).
        - queries_last_24h: count in the last 24 hours.
        - queries_by_day: list of {date, count} for the last ``days`` days.
        - queries_by_user: list of {user_id, count} for top users.
        - cache_hit_rate: cache_hit / total_queries (0 if no queries).
        - feedback_up: count of feedback='up'.
        - feedback_down: count of feedback='down'.
    """
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_24h = now - timedelta(hours=24)

    base_where = (QueryLog.tenant_id == identity.tenant_id)

    # Total queries
    total = session.scalar(
        select(func.count()).select_from(QueryLog).where(base_where)
    ) or 0

    # Success/error breakdown
    answered = session.scalar(
        select(func.count())
        .select_from(QueryLog)
        .where(base_where, QueryLog.status == "success")
    ) or 0

    failed = session.scalar(
        select(func.count())
        .select_from(QueryLog)
        .where(base_where, QueryLog.status == "error")
    ) or 0

    # Average latency (only where latency_ms is not null)
    avg_latency = session.scalar(
        select(func.avg(QueryLog.latency_ms))
        .select_from(QueryLog)
        .where(base_where, QueryLog.latency_ms.is_not(None))
    )

    # Last 24h
    recent_24h = session.scalar(
        select(func.count())
        .select_from(QueryLog)
        .where(base_where, QueryLog.created_at >= since_24h)
    ) or 0

    # Per-day series (last N days)
    day_series = session.execute(
        select(
            func.date(QueryLog.created_at).label("day"),
            func.count().label("count"),
        )
        .where(base_where, QueryLog.created_at >= since)
        .group_by(func.date(QueryLog.created_at))
        .order_by(func.date(QueryLog.created_at))
    ).all()

    queries_by_day = [
        {"date": str(row.day), "count": row.count} for row in day_series
    ]

    # Top users by query count
    user_series = session.execute(
        select(QueryLog.user_id, func.count().label("count"))
        .where(base_where)
        .group_by(QueryLog.user_id)
        .order_by(func.count().desc())
        .limit(20)
    ).all()

    queries_by_user = [
        {"user_id": str(row.user_id) if row.user_id else None, "count": row.count}
        for row in user_series
    ]

    # Cache hit rate
    cache_hits = session.scalar(
        select(func.count())
        .select_from(QueryLog)
        .where(base_where, QueryLog.cache_hit.is_(True))
    ) or 0
    cache_hit_rate = cache_hits / total if total > 0 else 0.0

    # Feedback counts
    feedback_up = session.scalar(
        select(func.count())
        .select_from(QueryLog)
        .where(base_where, QueryLog.feedback == "up")
    ) or 0

    feedback_down = session.scalar(
        select(func.count())
        .select_from(QueryLog)
        .where(base_where, QueryLog.feedback == "down")
    ) or 0

    return {
        "total_queries": total,
        "total_answered": answered,
        "total_failed": failed,
        "avg_latency_ms": float(avg_latency) if avg_latency is not None else None,
        "queries_last_24h": recent_24h,
        "queries_by_day": queries_by_day,
        "queries_by_user": queries_by_user,
        "cache_hit_rate": cache_hit_rate,
        "feedback_up": feedback_up,
        "feedback_down": feedback_down,
    }