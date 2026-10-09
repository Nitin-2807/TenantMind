"""Health and readiness endpoints.

Both probes are intentionally cheap, dependency-free, and mounted at the
application root rather than under ``api_v1_prefix``: infrastructure health
checks should keep working across API version changes.

In Phase 1 there is no PostgreSQL, Redis, Qdrant, Celery worker or LLM client
to inspect. Reporting ``"database": "healthy"`` would therefore be a
fabrication, so readiness asserts only what is actually true:

    Configuration validated and the application is serving requests.

Later phases extend readiness with real dependency checks, at the point those
dependencies exist.

Neither probe performs I/O of any kind — no network, no filesystem, no
credential access — so both stay safe to expose and cheap to poll.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.schemas.health import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
)
async def health() -> HealthResponse:
    """Report that the process is alive and able to serve requests."""
    return HealthResponse(status="ok")


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
)
async def readiness() -> ReadinessResponse:
    """Report that configuration loaded and the app is ready to serve."""
    return ReadinessResponse(status="ready")
