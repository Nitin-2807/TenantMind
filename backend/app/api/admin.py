"""Administration routes (Phase 7).

``GET /admin/usage`` — usage analytics for the caller's tenant. PMP §15 lists
this under "Administration"; PMP §16 reserves viewing usage analytics for the
``Admin`` role, so the route depends on :data:`app.api.documents.AdminIdentity`.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.documents import AdminIdentity
from app.db.session import DbSession
from app.services.analytics import get_usage_summary
from app.services.audit import record_audit

router = APIRouter(prefix="/admin", tags=["admin"])


class UsageSummaryResponse(BaseModel):
    """Response for ``GET /admin/usage``.

    Metadata only: counts, averages and ratios derived from ``query_logs``.
    No question text, retrieved content, prompts, or LLM outputs are included.
    """

    model_config = ConfigDict(from_attributes=True)

    total_queries: int
    total_answered: int
    total_failed: int
    avg_latency_ms: float | None
    queries_last_24h: int
    queries_by_day: list[dict]
    queries_by_user: list[dict]
    cache_hit_rate: float
    feedback_up: int
    feedback_down: int


@router.get(
    "/usage",
    response_model=UsageSummaryResponse,
    summary="Get usage analytics for this tenant",
)
def get_usage(
    identity: AdminIdentity,
    session: Session = DbSession,
) -> UsageSummaryResponse:
    """Return aggregated usage statistics for the admin's tenant.

    Requires the ``Admin`` role (route-level RBAC via
    :data:`app.api.documents.AdminIdentity`). The tenant is derived from the
    authenticated identity; no tenant parameter is accepted.

    Each view is itself recorded in the audit log (metadata only).
    """
    summary = get_usage_summary(session=session, identity=identity)

    record_audit(
        session=session,
        identity=identity,
        action="admin.usage_viewed",
        resource_type="usage",
        outcome="success",
        details={"days": 30},
    )

    return UsageSummaryResponse(**summary)