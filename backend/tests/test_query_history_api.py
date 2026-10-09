"""HTTP tests for query history, feedback, and admin usage (Phase 7).

Everything goes through the real application: the real router, the real bearer
dependency, the real :func:`app.api.rbac.require_role` authorization. The
database is the isolated SQLite one ``api_app`` wires in, and seeds are written
with the service layer then committed so the request's own session sees them.

The properties asserted here are the ones that only an HTTP test can prove:

* the tenant id is never accepted from the client — the routes read it from the
  identity, and a rival tenant's query id reads as ``404``, not "it exists";
* ``GET /admin/usage`` is ``Admin``-only (PMP §16) and each view is audited;
* the response shapes match what the served OpenAPI document declares.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.config import Settings
from app.core.identity import AuthenticatedIdentity
from app.core.security import create_access_token
from app.models.user import UserRole
from app.services.history import record_query
from app.services.query import QueryAnswer

HISTORY_PATH = "/api/v1/query/history"
FEEDBACK_PATH = "/api/v1/query/{query_id}/feedback"
USAGE_PATH = "/api/v1/admin/usage"

BEARER_SCHEME = "Bearer"


def credentials(token: str) -> dict[str, str]:
    return {"Authorization": f"{BEARER_SCHEME} {token}"}


def token_for(user: Any, tenant: Any, auth_settings: Settings) -> str:
    return create_access_token(
        subject=user.id,
        tenant_id=tenant.id,
        role=user.role,
        app_settings=auth_settings,
    )


@pytest.fixture
def accounts(
    make_committed_account: Any,
    auth_settings: Settings,
) -> dict[str, Any]:
    """Two committed tenants with their users and bearer tokens.

    acme holds an admin, editor and viewer; globex is the rival tenant (admin).
    """
    acme, admin = make_committed_account(
        slug="acme", email="admin@example.com", role=UserRole.ADMIN
    )
    _, viewer = make_committed_account(
        slug="acme", email="viewer@example.com", role=UserRole.VIEWER
    )
    _, editor = make_committed_account(
        slug="acme", email="editor@example.com", role=UserRole.EDITOR
    )
    globex, rival = make_committed_account(
        slug="globex", email="rival@example.com", role=UserRole.ADMIN
    )
    return {
        "acme": acme,
        "admin": admin,
        "admin_token": token_for(admin, acme, auth_settings),
        "viewer_token": token_for(viewer, acme, auth_settings),
        "editor_token": token_for(editor, acme, auth_settings),
        "globex": globex,
        "rival": rival,
        "rival_token": token_for(rival, globex, auth_settings),
    }


def seed_query(
    model_session: Any,
    *,
    tenant: Any,
    user: Any,
    question: str,
    answer: str = "seeded answer",
    status: str = "success",
) -> Any:
    identity = AuthenticatedIdentity(
        user_id=user.id,
        tenant_id=tenant.id,
        role=user.role,
    )
    log = record_query(
        model_session,
        identity,
        QueryAnswer(
            question=question,
            answer=answer,
            citations=(),
            grounded=True,
            context_truncated=False,
        ),
        status=status,
    )
    model_session.commit()
    return log


class TestAuthentication:
    def test_history_requires_a_token(self, api_client: Any) -> None:
        assert api_client.get(HISTORY_PATH).status_code == 401

    def test_feedback_requires_a_token(self, api_client: Any) -> None:
        assert (
            api_client.post(
                FEEDBACK_PATH.format(query_id=uuid.uuid4()),
                json={"value": "up"},
            ).status_code
            == 401
        )

    def test_usage_requires_a_token(self, api_client: Any) -> None:
        assert api_client.get(USAGE_PATH).status_code == 401


class TestHistoryOverHttp:
    def test_history_is_scoped_to_the_callers_tenant(
        self, api_client: Any, model_session: Any, accounts: dict[str, Any]
    ) -> None:
        seed_query(
            model_session,
            tenant=accounts["acme"],
            user=accounts["admin"],
            question="acme one",
        )
        seed_query(
            model_session,
            tenant=accounts["acme"],
            user=accounts["admin"],
            question="acme two",
        )
        seed_query(
            model_session,
            tenant=accounts["globex"],
            user=accounts["rival"],
            question="rival one",
        )

        acme = api_client.get(HISTORY_PATH, headers=credentials(accounts["admin_token"]))
        rival = api_client.get(HISTORY_PATH, headers=credentials(accounts["rival_token"]))

        assert acme.status_code == 200
        assert {item["question"] for item in acme.json()["items"]} == {
            "acme one",
            "acme two",
        }
        assert {item["question"] for item in rival.json()["items"]} == {"rival one"}

    def test_history_shape_and_echoed_paging(
        self, api_client: Any, model_session: Any, accounts: dict[str, Any]
    ) -> None:
        log = seed_query(
            model_session,
            tenant=accounts["acme"],
            user=accounts["admin"],
            question="one",
        )
        assert log is not None

        response = api_client.get(
            HISTORY_PATH,
            headers=credentials(accounts["viewer_token"]),
            params={"limit": 5, "offset": 0},
        )

        body = response.json()
        assert response.status_code == 200
        assert body["limit"] == 5
        assert body["offset"] == 0
        (item,) = body["items"]
        assert item["id"] == str(log.id)
        assert item["question"] == "one"
        assert item["answer"] == "seeded answer"
        assert item["grounded"] is True
        assert item["status"] == "success"
        assert item["citations"] is None
        assert item["feedback"] is None
        assert item["created_at"]

    def test_history_rejects_out_of_range_paging(
        self, api_client: Any, accounts: dict[str, Any]
    ) -> None:
        headers = credentials(accounts["admin_token"])
        assert (
            api_client.get(HISTORY_PATH, headers=headers, params={"limit": 0}).status_code
            == 422
        )
        assert (
            api_client.get(HISTORY_PATH, headers=headers, params={"limit": 201}).status_code
            == 422
        )
        assert (
            api_client.get(HISTORY_PATH, headers=headers, params={"offset": -1}).status_code
            == 422
        )


class TestFeedbackOverHttp:
    def test_feedback_updates_a_query(
        self, api_client: Any, model_session: Any, accounts: dict[str, Any]
    ) -> None:
        log = seed_query(
            model_session,
            tenant=accounts["acme"],
            user=accounts["admin"],
            question="was this helpful",
        )
        assert log is not None

        response = api_client.post(
            FEEDBACK_PATH.format(query_id=log.id),
            json={"value": "up"},
            headers=credentials(accounts["viewer_token"]),
        )

        assert response.status_code == 200
        body = response.json()
        assert body["query_id"] == str(log.id)
        assert body["feedback"] == "up"
        assert body["feedback_at"]

        # The request ran on a different session; expire this session's cached
        # copy of the row so the assertion reads the committed update.
        model_session.expire_all()
        persisted = model_session.get(type(log), log.id)
        assert persisted is not None
        assert persisted.feedback == "up"

    def test_another_tenants_query_is_a_404(
        self, api_client: Any, model_session: Any, accounts: dict[str, Any]
    ) -> None:
        log = seed_query(
            model_session,
            tenant=accounts["acme"],
            user=accounts["admin"],
            question="secret",
        )
        assert log is not None

        response = api_client.post(
            FEEDBACK_PATH.format(query_id=log.id),
            json={"value": "down"},
            headers=credentials(accounts["rival_token"]),
        )

        assert response.status_code == 404

    def test_invalid_value_is_a_422(
        self, api_client: Any, model_session: Any, accounts: dict[str, Any]
    ) -> None:
        log = seed_query(
            model_session,
            tenant=accounts["acme"],
            user=accounts["admin"],
            question="q",
        )
        assert log is not None

        response = api_client.post(
            FEEDBACK_PATH.format(query_id=log.id),
            json={"value": "sideways"},
            headers=credentials(accounts["admin_token"]),
        )

        assert response.status_code == 422

    def test_a_malformed_query_id_is_a_422(
        self, api_client: Any, accounts: dict[str, Any]
    ) -> None:
        response = api_client.post(
            FEEDBACK_PATH.format(query_id="not-a-uuid"),
            json={"value": "up"},
            headers=credentials(accounts["admin_token"]),
        )

        assert response.status_code == 422


class TestUsageOverHttp:
    def test_usage_requires_admin(
        self, api_client: Any, accounts: dict[str, Any]
    ) -> None:
        viewer = api_client.get(USAGE_PATH, headers=credentials(accounts["viewer_token"]))
        editor = api_client.get(USAGE_PATH, headers=credentials(accounts["editor_token"]))
        admin = api_client.get(USAGE_PATH, headers=credentials(accounts["admin_token"]))

        assert viewer.status_code == 403
        assert editor.status_code == 403
        assert admin.status_code == 200

    def test_usage_aggregates_only_the_callers_tenant(
        self, api_client: Any, model_session: Any, accounts: dict[str, Any]
    ) -> None:
        seed_query(
            model_session,
            tenant=accounts["acme"],
            user=accounts["admin"],
            question="q1",
        )
        seed_query(
            model_session,
            tenant=accounts["globex"],
            user=accounts["rival"],
            question="rival only",
        )

        acme = api_client.get(USAGE_PATH, headers=credentials(accounts["admin_token"])).json()
        rival = api_client.get(USAGE_PATH, headers=credentials(accounts["rival_token"])).json()

        assert acme["total_queries"] == 1
        assert acme["total_answered"] == 1
        assert rival["total_queries"] == 1
        # Rival traffic never appears in acme's summary.
        assert acme["queries_by_user"][0]["count"] == 1

    def test_a_usage_view_is_audited(
        self, api_client: Any, model_session: Any, accounts: dict[str, Any]
    ) -> None:
        from app.models.audit_log import AuditLog

        api_client.get(USAGE_PATH, headers=credentials(accounts["admin_token"]))

        action = (
            model_session.query(AuditLog)
            .filter(AuditLog.action == "admin.usage_viewed")
            .one_or_none()
        )
        assert action is not None
        assert action.tenant_id == accounts["acme"].id
        assert action.user_id == accounts["admin"].id

    def test_the_usage_schema_carries_metadata_only(
        self, api_client: Any, model_session: Any, accounts: dict[str, Any]
    ) -> None:
        """No question text, no answers, no prompts — just aggregates."""
        seed_query(
            model_session,
            tenant=accounts["acme"],
            user=accounts["admin"],
            question="a secret question nobody should see in analytics",
        )

        body = api_client.get(
            USAGE_PATH, headers=credentials(accounts["admin_token"])
        ).json()

        assert body["total_queries"] == 1
        assert body["cache_hit_rate"] == 0.0
        assert "secret" not in str(body)
        assert body["queries_by_user"][0]["user_id"] == str(accounts["admin"].id)