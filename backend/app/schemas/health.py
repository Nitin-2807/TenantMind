"""Response models for the health endpoints (PMP §22, Phase 1).

The health surface is deliberately tiny and frozen: a liveness probe and a
readiness probe, each returning exactly one field. Nothing about the host,
configuration, or infrastructure is modelled here — see :mod:`app.api.health`
for why that matters.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness: the process is running and able to answer requests."""

    status: Literal["ok"] = Field(
        description="Always 'ok' while the process is serving requests."
    )


class ReadinessResponse(BaseModel):
    """Readiness: configuration loaded and the app is able to serve requests.

    Deliberately carries no dependency status. PostgreSQL, Redis, Qdrant,
    Celery and the LLM provider are not implemented in Phase 1, so a
    ``"database": "healthy"`` field here would be fabricated rather than
    measured. Later phases widen this model at the point those subsystems
    actually exist and can be genuinely probed.
    """

    status: Literal["ready"] = Field(
        description="Always 'ready' once configuration has loaded."
    )
