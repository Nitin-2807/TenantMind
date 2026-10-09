"""Query history service: recording, listing, and feedback (Phase 7).

All functions are tenant-scoped via the passed :class:`~app.core.identity.AuthenticatedIdentity`.
No function accepts a tenant_id from the caller.

Two of these functions are part of the query read path: :func:`record_query`
writes a history row after every answer, and the write is **fail-open** — a
database failure logging the query must never break the answer that was just
delivered. The user-facing reads and edits (:func:`list_history`,
:func:`add_feedback`) are the opposite: they are the entire request, so their
failures surface to the caller.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.identity import AuthenticatedIdentity
from app.models.query_log import QueryLog

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.query import QueryAnswer


class QueryNotFoundError(LookupError):
    """Raised when a query log is not found for the given tenant."""


class InvalidFeedbackError(ValueError):
    """Raised when feedback value is not 'up' or 'down'."""


def _citations_to_json(citations: tuple) -> list[dict] | None:
    """Convert internal citations to JSON-serializable metadata only."""
    if not citations:
        return None
    return [
        {
            "document_id": str(c.document_id),
            "filename": c.filename,
            "chunk_index": c.chunk_index,
            "page": c.page,
        }
        for c in citations
    ]


def record_query(
    session: Session,
    identity: AuthenticatedIdentity,
    answer: QueryAnswer,
    *,
    cache_hit: bool = False,
    latency_ms: int | None = None,
    status: str = "success",
    error_kind: str | None = None,
) -> QueryLog | None:
    """Persist a query log entry.

    Args:
        session: the request's database session.
        identity: the authenticated caller (supplies tenant_id and user_id).
        answer: the answer returned by the query pipeline.
        cache_hit: whether the answer was served from cache.
        latency_ms: end-to-end latency in milliseconds.
        status: ``success`` or ``error``.
        error_kind: if ``status == "error"``, one of ``rejected``, ``retrieval``, ``generation``, ``rate_limited``.

    Returns:
        The created :class:`QueryLog` row (with generated ``id`` and ``created_at``),
        or ``None`` if the write failed (fail-open: the answer path never breaks
        because history could not be persisted).
    """
    log = QueryLog(
        tenant_id=identity.tenant_id,
        user_id=identity.user_id,
        question=answer.question,
        answer=answer.answer,
        grounded=answer.grounded,
        context_truncated=answer.context_truncated,
        cache_hit=cache_hit,
        status=status,
        error_kind=error_kind,
        citations=_citations_to_json(answer.citations),
        latency_ms=latency_ms,
    )
    try:
        # A savepoint keeps a failed log write from poisoning the request's
        # transaction: the write is rolled back alone and the session remains
        # usable (e.g. for the audit write that follows).
        with session.begin_nested():
            session.add(log)
            session.flush()
        return log
    except Exception as exc:  # noqa: BLE001 - fail-open boundary
        logger.warning("query log write failed: %s", exc)
        return None


def list_history(
    session: Session,
    identity: AuthenticatedIdentity,
    *,
    limit: int = 50,
    offset: int = 0,
) -> list[QueryLog]:
    """Return the caller's query history, most recent first.

    Scoped to the caller's tenant via ``identity.tenant_id``.
    """
    stmt = (
        select(QueryLog)
        .where(QueryLog.tenant_id == identity.tenant_id)
        .order_by(QueryLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(session.scalars(stmt).all())


def add_feedback(
    session: Session,
    identity: AuthenticatedIdentity,
    query_id: uuid.UUID,
    value: str,
) -> QueryLog:
    """Add or update feedback on a query.

    Args:
        session: the request's database session.
        identity: the authenticated caller. The query must belong to
            ``identity.tenant_id``; otherwise ``QueryNotFoundError``.
        query_id: the query log ID.
        value: ``"up"`` or ``"down"``.

    Returns:
        The updated :class:`QueryLog`.

    Raises:
        QueryNotFoundError: if no query with ``query_id`` exists in the caller's tenant.
        InvalidFeedbackError: if ``value`` is not ``"up"`` or ``"down"``.
    """
    if value not in ("up", "down"):
        raise InvalidFeedbackError("feedback must be 'up' or 'down'")

    log = session.get(QueryLog, query_id)
    if log is None or log.tenant_id != identity.tenant_id:
        raise QueryNotFoundError("query not found")

    log.feedback = value
    log.feedback_at = datetime.now(timezone.utc)
    session.flush()
    return log