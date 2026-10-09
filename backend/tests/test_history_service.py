"""Service-level tests for query history: recording, listing, feedback (Phase 7).

The two properties that matter for the audit/security story:

* **tenant scoping** — ``list_history`` only ever returns the caller's tenant's
  rows, and ``add_feedback`` refuses to touch a row that belongs to another
  tenant (it reports "not found", never "this is someone else's query");
* **tenant_id never accepted from the caller** — the service reads it from the
  identity, which is the only tenant-shaped input any function here takes.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Self

import pytest

from app.core.identity import AuthenticatedIdentity
from app.models.query_log import QueryLog
from app.models.user import UserRole
from app.rag.retrieval import Citation
from app.services.history import (
    InvalidFeedbackError,
    QueryNotFoundError,
    add_feedback,
    list_history,
    record_query,
)
from app.services.query import QueryAnswer


def _answer(
    question: str = "What is the limit?",
    answer: str = "42",
    grounded: bool = True,
    citations: tuple[Citation, ...] = (),
) -> QueryAnswer:
    return QueryAnswer(
        question=question,
        answer=answer,
        citations=citations,
        grounded=grounded,
        context_truncated=False,
    )


def _identity(tenant_id: uuid.UUID, user_id: uuid.UUID) -> AuthenticatedIdentity:
    return AuthenticatedIdentity(
        user_id=user_id,
        tenant_id=tenant_id,
        role=UserRole.VIEWER,
    )


class TestRecordQuery:
    def test_records_the_full_answer(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")
        identity = _identity(tenant.id, user.id)
        citation = Citation(
            document_id=uuid.uuid4(),
            filename="limits.pdf",
            chunk_index=3,
            page=2,
        )

        log = record_query(
            model_session,
            identity,
            _answer(citations=(citation,)),
            latency_ms=123,
        )

        assert log is not None
        assert log.tenant_id == tenant.id
        assert log.user_id == user.id
        assert log.question == "What is the limit?"
        assert log.answer == "42"
        assert log.grounded is True
        assert log.status == "success"
        assert log.error_kind is None
        assert log.latency_ms == 123
        assert log.cache_hit is False
        # Citations are reduced to metadata only — never the chunk text.
        assert log.citations == [
            {
                "document_id": str(citation.document_id),
                "filename": "limits.pdf",
                "chunk_index": 3,
                "page": 2,
            }
        ]

    def test_no_citations_stores_none(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")

        log = record_query(
            model_session, _identity(tenant.id, user.id), _answer(grounded=False)
        )

        assert log is not None
        assert log.grounded is False
        assert log.citations is None

    def test_error_path_records_kind(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")

        log = record_query(
            model_session,
            _identity(tenant.id, user.id),
            _answer(),
            status="error",
            error_kind="rate_limited",
        )

        assert log is not None
        assert log.status == "error"
        assert log.error_kind == "rate_limited"

    def test_cache_hit_is_recorded(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")

        log = record_query(
            model_session, _identity(tenant.id, user.id), _answer(), cache_hit=True
        )

        assert log is not None
        assert log.cache_hit is True

    def test_tenant_id_is_never_accepted_from_the_caller(
        self, model_session, make_committed_account
    ) -> None:
        """The service signature has no tenant parameter; the identity is the source."""
        import inspect

        params = inspect.signature(record_query).parameters

        assert "tenant_id" not in params
        assert "identity" in params

    def test_a_failed_write_is_fail_open(self) -> None:
        """A database failure recording the query must not break the answer path."""

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

        log = record_query(
            BrokenSession(),  # type: ignore[arg-type]
            _identity(uuid.uuid4(), uuid.uuid4()),
            _answer(),
        )

        assert log is None


class TestListHistory:
    def test_returns_only_the_callers_tenant(
        self, model_session, make_committed_account
    ) -> None:
        tenant_a, user_a = make_committed_account(slug="acme", email="a@example.com")
        tenant_b, user_b = make_committed_account(slug="globex", email="b@example.com")

        record_query(model_session, _identity(tenant_a.id, user_a.id), _answer("A-one"))
        record_query(model_session, _identity(tenant_b.id, user_b.id), _answer("B-one"))
        record_query(model_session, _identity(tenant_a.id, user_a.id), _answer("A-two"))

        history = list_history(model_session, _identity(tenant_a.id, user_a.id))

        assert [row.question for row in history] == ["A-two", "A-one"]
        assert all(row.tenant_id == tenant_a.id for row in history)

    def test_most_recent_first(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")
        now = datetime.now(timezone.utc)
        older = record_query(model_session, _identity(tenant.id, user.id), _answer("old"))
        newer = record_query(model_session, _identity(tenant.id, user.id), _answer("new"))
        assert older is not None and newer is not None
        older.created_at = now - timedelta(hours=1)
        newer.created_at = now
        model_session.flush()

        history = list_history(model_session, _identity(tenant.id, user.id))

        assert [row.question for row in history] == ["new", "old"]

    def test_limit_and_offset(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")
        for i in range(5):
            record_query(model_session, _identity(tenant.id, user.id), _answer(f"q{i}"))

        page_one = list_history(model_session, _identity(tenant.id, user.id), limit=2)
        page_two = list_history(
            model_session, _identity(tenant.id, user.id), limit=2, offset=2
        )

        assert len(page_one) == 2
        assert len(page_two) == 2
        assert {row.question for row in page_one} != {row.question for row in page_two}


class TestAddFeedback:
    def test_valid_feedback_updates_the_row(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")
        log = record_query(model_session, _identity(tenant.id, user.id), _answer())
        assert log is not None

        updated = add_feedback(model_session, _identity(tenant.id, user.id), log.id, "up")

        assert updated.feedback == "up"
        assert updated.feedback_at is not None

    def test_feedback_can_be_changed(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")
        log = record_query(model_session, _identity(tenant.id, user.id), _answer())
        assert log is not None

        add_feedback(model_session, _identity(tenant.id, user.id), log.id, "up")
        changed = add_feedback(model_session, _identity(tenant.id, user.id), log.id, "down")

        assert changed.feedback == "down"

    def test_invalid_value_is_rejected(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")
        log = record_query(model_session, _identity(tenant.id, user.id), _answer())
        assert log is not None

        with pytest.raises(InvalidFeedbackError):
            add_feedback(
                model_session, _identity(tenant.id, user.id), log.id, "sideways"
            )

    def test_an_unknown_id_is_not_found(self, model_session, make_committed_account) -> None:
        tenant, user = make_committed_account(slug="acme", email="u@example.com")

        with pytest.raises(QueryNotFoundError):
            add_feedback(model_session, _identity(tenant.id, user.id), uuid.uuid4(), "up")

    def test_another_tenants_query_is_not_found(
        self, model_session, make_committed_account
    ) -> None:
        """Feedback on a rival's query reports 'not found', never 'it exists'."""
        tenant_a, user_a = make_committed_account(slug="acme", email="a@example.com")
        tenant_b, user_b = make_committed_account(slug="globex", email="b@example.com")
        log = record_query(model_session, _identity(tenant_a.id, user_a.id), _answer())
        assert log is not None

        with pytest.raises(QueryNotFoundError):
            add_feedback(model_session, _identity(tenant_b.id, user_b.id), log.id, "up")

        # And the row was not touched.
        untouched = model_session.get(QueryLog, log.id)
        assert untouched is not None
        assert untouched.feedback is None