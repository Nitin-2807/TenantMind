"""Query history and feedback routes — Phase 7.

``GET /query/history`` and ``POST /query/{id}/feedback``. Both are
tenant-scoped through :data:`app.api.documents.ViewingIdentity`: the tenant
id comes from the authenticated identity and is never accepted from the
client, and a query log id is only ever resolved within the caller's own
tenant (a foreign id reads as "not found", not "forbidden").
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.documents import ViewingIdentity
from app.db.session import DbSession
from app.services.history import (
    InvalidFeedbackError,
    QueryNotFoundError,
    add_feedback,
    list_history,
)

router = APIRouter(prefix="/query", tags=["query"])


class FeedbackRequest(BaseModel):
    """Request body for ``POST /query/{id}/feedback``.

    A single field; the value is constrained to exactly ``"up"`` or
    ``"down"`` at validation time so the service layer never sees anything
    else, and the database CHECK constraint is belt-and-braces.
    """

    value: str = Field(pattern="^(up|down)$")


class FeedbackResponse(BaseModel):
    """The recorded feedback state after ``POST /query/{id}/feedback``."""

    query_id: str
    feedback: str
    feedback_at: str


class HistoryItem(BaseModel):
    """One item in ``GET /query/history``.

    The question and answer are the record of what the caller was shown —
    that is the point of a history feature. Citation metadata only is
    stored (document id, filename, chunk index, page); retrieved chunk text
    is never persisted.
    """

    id: str
    question: str
    answer: str
    grounded: bool
    context_truncated: bool
    cache_hit: bool
    status: str
    error_kind: str | None
    citations: list[dict] | None
    latency_ms: int | None
    feedback: str | None
    feedback_at: str | None
    created_at: str


class HistoryResponse(BaseModel):
    """Response for ``GET /query/history``."""

    items: list[HistoryItem]
    limit: int
    offset: int


def _log_to_item(log) -> HistoryItem:
    return HistoryItem(
        id=str(log.id),
        question=log.question,
        answer=log.answer,
        grounded=log.grounded,
        context_truncated=log.context_truncated,
        cache_hit=log.cache_hit,
        status=log.status,
        error_kind=log.error_kind,
        citations=log.citations,
        latency_ms=log.latency_ms,
        feedback=log.feedback,
        feedback_at=log.feedback_at.isoformat() if log.feedback_at else None,
        created_at=log.created_at.isoformat(),
    )


@router.get(
    "/history",
    response_model=HistoryResponse,
    summary="Get the caller's query history",
)
def get_query_history(
    identity: ViewingIdentity,
    session: Session = DbSession,
    limit: int = 50,
    offset: int = 0,
) -> HistoryResponse:
    """Return the caller's tenant query history, most recent first.

    ``limit`` is capped at 200 and ``offset`` must be non-negative; the
    service derives the tenant from the authenticated identity.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="limit must be between 1 and 200",
        )
    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="offset must be non-negative",
        )

    logs = list_history(
        session=session,
        identity=identity,
        limit=limit,
        offset=offset,
    )
    return HistoryResponse(
        items=[_log_to_item(log) for log in logs],
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{query_id}/feedback",
    response_model=FeedbackResponse,
    summary="Submit feedback on a query",
)
def submit_feedback(
    query_id: str,
    request: FeedbackRequest,
    identity: ViewingIdentity,
    session: Session = DbSession,
) -> FeedbackResponse:
    """Record ``up`` or ``down`` feedback on a past query.

    The ``query_id`` must name a query log within the caller's tenant;
    anything else is a ``404``. Updating existing feedback is allowed and
    simply replaces the value and timestamp.
    """
    try:
        qid = uuid.UUID(query_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="invalid query id",
        ) from None

    try:
        log = add_feedback(
            session=session,
            identity=identity,
            query_id=qid,
            value=request.value,
        )
    except QueryNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="query not found",
        ) from exc
    except InvalidFeedbackError as exc:
        # Unreachable while the schema's pattern check stands; kept so the
        # route stays correct if the schema is ever widened.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    # ``add_feedback`` guarantees a value on success; narrow the type for mypy.
    assert log.feedback is not None
    return FeedbackResponse(
        query_id=str(log.id),
        feedback=log.feedback,
        feedback_at=log.feedback_at.isoformat() if log.feedback_at else "",
    )