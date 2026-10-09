"""Audit logging service (Phase 7).

Records security-relevant events to the audit_logs table. All writes are
best-effort (fail-open with warning log) so that audit failures never
break the primary request path.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.core.identity import AuthenticatedIdentity
from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)


def record_audit(
    session: Session,
    identity: AuthenticatedIdentity,
    *,
    action: str,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    outcome: str = "success",
    details: dict | None = None,
) -> None:
    """Record an audit event.

    Fail-open: any exception is logged and swallowed. The primary request
    path must not be affected by audit logging failures.

    Args:
        session: the request's database session.
        identity: the authenticated caller (supplies tenant_id and user_id).
        action: the action in ``resource.verb`` format (e.g. "query.requested").
        resource_type: optional resource type (e.g. "query", "document").
        resource_id: optional resource UUID.
        outcome: one of ``success``, ``denied``, ``error``.
        details: optional additional details. Must not contain sensitive
            data (passwords, JWTs, API keys, document contents, retrieved
            chunks, prompts, or sensitive LLM responses).
    """
    try:
        audit = AuditLog(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            details=details,
        )
        # A savepoint keeps a failed audit write from poisoning the request's
        # transaction (e.g. when it follows a failed query-log write).
        with session.begin_nested():
            session.add(audit)
            session.flush()
    except Exception as exc:  # noqa: BLE001 - audit must never break the request path
        logger.warning("audit log write failed: %s", exc)