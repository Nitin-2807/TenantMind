"""Service-level tests for the audit log (Phase 7).

The audit record is metadata-only — never passwords, JWTs, API keys, document
contents, retrieved chunks, prompts, or sensitive LLM responses — and writes
fail open: an audit failure must never break the request that triggered it.
"""

from __future__ import annotations

import uuid
from typing import Literal, Self

from app.core.identity import AuthenticatedIdentity
from app.models.audit_log import AuditLog
from app.models.user import UserRole
from app.services.audit import record_audit


def _identity(tenant_id: uuid.UUID, user_id: uuid.UUID) -> AuthenticatedIdentity:
    return AuthenticatedIdentity(
        user_id=user_id,
        tenant_id=tenant_id,
        role=UserRole.ADMIN,
    )


class TestRecordAudit:
    def test_writes_a_metadata_only_row(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")

        record_audit(
            model_session,
            _identity(tenant.id, user.id),
            action="query.requested",
            resource_type="query",
            resource_id=uuid.uuid4(),
            outcome="success",
            details={"cache_hit": False, "latency_ms": 12},
        )

        row = model_session.query(AuditLog).one()
        assert row.tenant_id == tenant.id
        assert row.user_id == user.id
        assert row.action == "query.requested"
        assert row.resource_type == "query"
        assert row.outcome == "success"
        # ``details`` holds the metadata payload; nothing sensitive belongs here.
        assert row.details == {"cache_hit": False, "latency_ms": 12}

    def test_defaults(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")

        record_audit(model_session, _identity(tenant.id, user.id), action="query.rate_limited")

        row = model_session.query(AuditLog).one()
        assert row.outcome == "success"
        assert row.resource_type is None
        assert row.resource_id is None
        assert row.details is None

    def test_tenant_id_comes_from_the_identity_only(
        self, model_session, make_committed_account
    ) -> None:
        tenant_a, user_a = make_committed_account(slug="acme", email="a@example.com")
        tenant_b, user_b = make_committed_account(slug="globex", email="b@example.com")

        record_audit(model_session, _identity(tenant_a.id, user_a.id), action="query.requested")
        record_audit(model_session, _identity(tenant_b.id, user_b.id), action="admin.usage_viewed")

        rows = model_session.query(AuditLog).all()
        assert {row.tenant_id for row in rows} == {tenant_a.id, tenant_b.id}

    def test_a_failed_write_is_fail_open(self) -> None:
        class BrokenSession:
            def begin_nested(self) -> BrokenSession:
                return self

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *exc: object) -> Literal[False]:
                return False

            def add(self, log: object) -> None:  # pragma: no cover - stub
                raise RuntimeError("disk full")

            def flush(self) -> None:  # pragma: no cover - stub
                raise RuntimeError("disk full")

        # Must not raise: audit failures never break the primary request path.
        record_audit(
            BrokenSession(),  # type: ignore[arg-type]
            _identity(uuid.uuid4(), uuid.uuid4()),
            action="query.requested",
        )