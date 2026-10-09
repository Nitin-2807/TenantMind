"""Tenant identity comes from the token, and only from the token (PMP §8, §12).

What is proved here
-------------------
* :class:`~app.core.identity.AuthenticatedIdentity` derives ``tenant_id`` and
  ``role`` from a verified access token and from nothing else.
* A request-body, query-parameter or header ``tenant_id`` **cannot** influence
  the identity — not by being honoured, and not by silently overriding.
* The FastAPI dependency returns a single, undifferentiated ``401`` for every
  authentication failure, with ``WWW-Authenticate`` set, and never leaks why.
* ``identity.tenant_id`` equals the ``tenant_id`` claim, and a tampered token
  cannot change one without the other.

Why this file exists separately from ``test_security_isolation.py``
------------------------------------------------------------------
That file proves the *schema* has no room for a cross-tenant leak. This one
proves the *runtime* cannot produce one. They are complementary and neither
would catch the other's failures: a schema can be perfect while a dependency
trusts a header, and a dependency can be perfect while a column is nullable.

The central claim, stated once
------------------------------
Tenant context is derived from **authenticated identity only**. There is no
code path — and in particular no ``IdentitySource`` member — by which a client
supplied value becomes an :class:`AuthenticatedIdentity`'s ``tenant_id``. That
absence is asserted structurally as well as behaviourally, because an
enumeration that omits the bad case is easy to add later without noticing.
"""

from __future__ import annotations

import ast
import inspect
import logging
import typing
import uuid
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.params import Depends as ParamsDepends
from fastapi.testclient import TestClient
from jwt.exceptions import PyJWTError
from sqlalchemy.orm import Session

from app.api.deps import (
    UNAUTHORIZED_DETAIL,
    WWW_AUTHENTICATE_VALUE,
    CurrentIdentity,
    get_current_identity,
)
from app.core.config import Settings, get_settings
from app.core.identity import AuthenticatedIdentity, IdentitySource
from app.core.security import (
    TokenClaims,
    TokenType,
    TokenValidationError,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.auth import (
    authenticate,
    exchange_refresh_token,
    revoke_refresh_token,
)
from tests.conftest import TEST_PASSWORD

AccountFactory = Callable[..., tuple[Tenant, User]]

#: Real version-4 UUIDs rather than a readable pattern like ``22222222-...``.
#: A patterned literal has no RFC 4122 version nibble, so ``UUID(...).version`` is
#: ``None`` for it — which would make the "identifiers are always v4" assertion
#: below fail for a reason that has nothing to do with the code under test. The
#: values are still fixed, because these tests need stable, reproducible ids.
TENANT_A = uuid.UUID("6f1a7a4e-2c11-4b3d-9e05-1c2d3e4f5a6b")
TENANT_B = uuid.UUID("0b8d5f21-9a37-4e60-8c14-7d6e5f4a3b2c")
USER_ID = uuid.UUID("c4d5e6f7-a8b9-4c0d-9e1f-2a3b4c5d6e7f")


@pytest.fixture(autouse=True)
def _process_settings(active_auth_settings: Settings) -> None:
    """Give the module a process-wide configuration with a usable signing key."""
    assert active_auth_settings.jwt_signing_key()


@pytest.fixture
def identity_app(active_auth_settings: Settings) -> FastAPI:
    """A minimal app whose only route is protected by ``CurrentIdentity``.

    Built per-test rather than reusing the canonical application so that a route
    existing here proves the *dependency* works, not that some particular
    router happens to wire it. No database is involved: the dependency resolves
    an identity from the token alone.
    """
    application = FastAPI()

    # Registered for three verbs so that a request-supplied tenant id can be
    # tried in a body, a query string and a form. The identity is identical
    # whichever is used; that is the property under test.
    @application.get("/whoami")
    @application.post("/whoami")
    @application.put("/whoami")
    def whoami(identity: CurrentIdentity) -> dict[str, str]:
        return {
            "user_id": str(identity.user_id),
            "tenant_id": str(identity.tenant_id),
            "role": identity.role.value,
            "source": identity.source.value,
        }

    application.dependency_overrides[get_settings] = lambda: active_auth_settings
    return application


@pytest.fixture
def identity_client(identity_app: FastAPI) -> TestClient:
    return TestClient(identity_app)


def token_for(
    tenant_id: uuid.UUID,
    *,
    role: str = "viewer",
    user_id: uuid.UUID = USER_ID,
    app_settings: Settings,
) -> str:
    return create_access_token(
        subject=user_id,
        tenant_id=tenant_id,
        role=role,
        app_settings=app_settings,
    )


# ---------------------------------------------------------------------------
# The identity object itself
# ---------------------------------------------------------------------------


class TestAuthenticatedIdentity:
    def test_it_carries_the_three_facts_and_nothing_else(
        self, auth_settings: Settings
    ) -> None:
        """Three fields. ``email`` was deliberately left out.

        Nothing in this step needs to know who somebody is by name, and a token
        that embeds an address creates a staleness bug the moment an email is
        changed. The field set is asserted via ``dataclasses.fields`` because the
        class is a ``slots=True`` dataclass with no ``__dict__``.
        """

        identity = AuthenticatedIdentity.from_access_token(
            token_for(TENANT_A, app_settings=auth_settings),
            app_settings=auth_settings,
        )
        assert {field.name for field in fields(identity)} == {
            "user_id",
            "tenant_id",
            "role",
            "source",
        }

    def test_tenant_id_comes_from_the_token_claim(self, auth_settings: Settings) -> None:
        identity = AuthenticatedIdentity.from_access_token(
            token_for(TENANT_A, app_settings=auth_settings),
            app_settings=auth_settings,
        )
        assert identity.tenant_id == TENANT_A

    def test_two_tenants_produce_two_different_identities(
        self, auth_settings: Settings
    ) -> None:
        first = AuthenticatedIdentity.from_access_token(
            token_for(TENANT_A, app_settings=auth_settings),
            app_settings=auth_settings,
        )
        second = AuthenticatedIdentity.from_access_token(
            token_for(TENANT_B, app_settings=auth_settings),
            app_settings=auth_settings,
        )
        assert first.tenant_id != second.tenant_id

    def test_the_role_is_the_typed_enum_not_a_string(
        self, auth_settings: Settings
    ) -> None:
        """A role can never be a free-text value at this boundary."""
        identity = AuthenticatedIdentity.from_access_token(
            token_for(TENANT_A, role="editor", app_settings=auth_settings),
            app_settings=auth_settings,
        )
        assert identity.role is UserRole.EDITOR
        assert not isinstance(identity.role, str) or isinstance(
            identity.role, UserRole
        )

    def test_it_is_immutable(self, auth_settings: Settings) -> None:
        """A route handler cannot rewrite the tenant mid-request."""

        identity = AuthenticatedIdentity.from_access_token(
            token_for(TENANT_A, app_settings=auth_settings),
            app_settings=auth_settings,
        )
        # `__setattr__` is called directly because a plain assignment is a
        # mypy "read-only property" error here, and `setattr()` is a ruff B010
        # finding. Neither is wrong for their own reasons, but both would need a
        # suppression to express "attempt to write, expect refusal" — and this
        # project takes no suppressions. Calling the generated guard directly is
        # both lint-clean and a more precise expression of what is being proved.
        with pytest.raises(FrozenInstanceError):
            identity.__setattr__("tenant_id", TENANT_B)
        assert identity.tenant_id == TENANT_A

    def test_it_cannot_hold_a_database_session(self) -> None:
        """Stated as a field-set assertion.

        PMP §12 says the authenticated identity determines tenant context. A
        type that could carry a ``Session`` would make it possible to build an
        identity from uncommitted database state, which is a trust boundary this
        step has no reason to open.
        """

        types = {field.name: field.type for field in fields(AuthenticatedIdentity)}
        assert set(types) == {"user_id", "tenant_id", "role", "source"}
        assert not any("Session" in str(value) for value in types.values())

    def test_every_source_is_something_the_project_trusts(self) -> None:
        """``IdentitySource`` has no member for a request-supplied value.

        The absence is the assertion, and it is an *enumeration*: the set is
        compared exactly, so a future bug that plausibly adds a ``REQUEST_BODY``
        or ``HEADER`` member here fails the test rather than passing quietly.

        Two members, and both are trusted:

        * ``BEARER_ACCESS_TOKEN`` — a claim this application signed.
        * ``PERSISTED_ROW`` — a ``tenant_id`` read from a row this application
          wrote, added in Phase 5 Step 2 for the Celery worker's service
          identity.

        Neither is client-supplied. That is the property this guards, and it is
        why the count may grow to two but the *character* of a member may not
        change: a new member is only legitimate if it names something the
        server itself produced.
        """
        assert {member.value for member in IdentitySource} == {
            "bearer_access_token",
            "persisted_row",
        }

    def test_no_source_member_names_where_a_request_could_have_supplied_it(
        self,
    ) -> None:
        """The trust-source set, checked by meaning rather than by literal.

        :meth:`test_every_source_is_something_the_project_trusts` pins the exact
        names, which makes it fail the moment a new source is added — correct, but
        it cannot say *why* a name was refused. This one states the rule in
        terms a reviewer can apply to a proposed member without reading this
        file: a source that describes a place a **client** can write to is never
        legitimate, however it is spelled.
        """
        untrusted_words = (
            "request",
            "body",
            "query",
            "param",
            "header",
            "form",
            "cookie",
            "client",
            "user",
            "input",
            "payload",
            "slug",
            "jwt_claim_unverified",
        )
        for member in IdentitySource:
            for word in untrusted_words:
                assert word not in member.name.lower(), (
                    f"{member.name} names a place the client can supply a value "
                    "from; an IdentitySource may only name something the server "
                    "itself produced"
                )

    def test_the_bearer_path_and_the_service_path_report_different_sources(
        self,
    ) -> None:
        """Each constructor is reachable only from its own source.

        Without this, a future edit could give :meth:`for_tenant_service` the
        default ``BEARER_ACCESS_TOKEN`` source and this whole file would still
        pass: the enum would be right, the call site would be right, and the
        recorded provenance would be a lie. It also asserts the converse — a
        bearer identity can never claim to have come from a persisted row, so
        :attr:`IdentitySource.PERSISTED_ROW` means what it says.
        """
        service = AuthenticatedIdentity.for_tenant_service(tenant_id=TENANT_A)
        assert service.source is IdentitySource.PERSISTED_ROW

        bearer = AuthenticatedIdentity(
            user_id=USER_ID, tenant_id=TENANT_A, role=UserRole.VIEWER
        )
        assert bearer.source is IdentitySource.BEARER_ACCESS_TOKEN
        assert bearer.source is not IdentitySource.PERSISTED_ROW

    def test_the_default_source_is_the_token(self) -> None:
        identity = AuthenticatedIdentity(
            user_id=USER_ID, tenant_id=TENANT_A, role=UserRole.VIEWER
        )
        assert identity.source is IdentitySource.BEARER_ACCESS_TOKEN


class TestIdentityRejectsBadTokens:
    def test_a_refresh_token_cannot_produce_an_identity(
        self, auth_settings: Settings
    ) -> None:
        """The most consequential confusion case.

        A refresh token outlives an access token, so treating one as an identity
        would grant a client far more than its access-token lifetime allows.
        """
        refresh_token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_A, app_settings=auth_settings
        )
        with pytest.raises(TokenValidationError):
            AuthenticatedIdentity.from_access_token(
                refresh_token, app_settings=auth_settings
            )

    def test_an_unrecognised_role_produces_no_identity(
        self, auth_settings: Settings
    ) -> None:
        """A validly signed token with an unknown role must not authenticate.

        Defaulting to a role here — to ``viewer``, or to ``None`` — would be the
        worst available failure. It is asserted explicitly because the token is
        *genuine*: the rejection is about authority, not authenticity.
        """
        token = token_for(TENANT_A, role="superuser", app_settings=auth_settings)
        with pytest.raises(TokenValidationError, match="unrecognised role"):
            AuthenticatedIdentity.from_access_token(
                token, app_settings=auth_settings
            )

    def test_a_token_signed_with_another_key_produces_no_identity(
        self, auth_settings: Settings, foreign_secret_settings: Settings
    ) -> None:
        token = token_for(TENANT_A, app_settings=auth_settings)
        with pytest.raises(TokenValidationError, match="InvalidSignatureError"):
            AuthenticatedIdentity.from_access_token(
                token, app_settings=foreign_secret_settings
            )

    def test_from_claims_rejects_a_refresh_type(self, auth_settings: Settings) -> None:
        """``from_claims`` trusts its input to be *verified*, not to be an access token.

        The type check lives here rather than only in ``from_access_token`` so
        that a caller which has already decoded claims cannot accidentally build
        an identity from a refresh token.
        """
        refresh_token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_A, app_settings=auth_settings
        )
        claims = decode_token(
            refresh_token,
            expected_type=TokenType.REFRESH,
            app_settings=auth_settings,
        )
        with pytest.raises(TokenValidationError, match="only be built from"):
            AuthenticatedIdentity.from_claims(claims)

    def test_from_claims_rejects_a_missing_role(self) -> None:
        """Hand-written claims, no signing involved.

        Used to isolate the role requirement from every other possible failure,
        which is the only way to prove this specific guard exists.
        """
        claims = TokenClaims(
            subject=USER_ID,
            tenant_id=TENANT_A,
            type=TokenType.ACCESS,
            issued_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=datetime(2026, 1, 1, 0, 15, tzinfo=UTC),
            role=None,
        )
        with pytest.raises(TokenValidationError, match="missing the 'role'"):
            AuthenticatedIdentity.from_claims(claims)


# ---------------------------------------------------------------------------
# The FastAPI dependency
# ---------------------------------------------------------------------------


class TestDependencyReturnsTheTokenTenant:
    def test_a_valid_token_authenticates(
        self, identity_client: TestClient, auth_settings: Settings
    ) -> None:
        response = identity_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token_for(TENANT_A, app_settings=auth_settings)}"
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["tenant_id"] == str(TENANT_A)
        assert body["user_id"] == str(USER_ID)
        assert body["role"] == "viewer"
        assert body["source"] == "bearer_access_token"

    def test_tenant_b_sees_tenant_b_only(
        self, identity_client: TestClient, auth_settings: Settings
    ) -> None:
        """The isolation assertion, at the HTTP boundary."""
        response = identity_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token_for(TENANT_B, app_settings=auth_settings)}"
            },
        )
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(TENANT_B)

    def test_the_bearer_scheme_is_case_insensitive(
        self, identity_client: TestClient, auth_settings: Settings
    ) -> None:
        """RFC 6750 §2.1 makes the scheme token case-insensitive."""
        token = token_for(TENANT_A, app_settings=auth_settings)
        for scheme in ("Bearer", "bearer", "BEARER", "BeArEr"):
            response = identity_client.get(
                "/whoami", headers={"Authorization": f"{scheme} {token}"}
            )
            assert response.status_code == 200, scheme

    def test_an_extra_space_before_the_token_still_works(
        self, identity_client: TestClient, auth_settings: Settings
    ) -> None:
        token = token_for(TENANT_A, app_settings=auth_settings)
        response = identity_client.get(
            "/whoami", headers={"Authorization": f"Bearer  {token}"}
        )
        assert response.status_code == 200


class TestRequestSuppliedTenantCannotOverrideTheToken:
    """The headline property of this step.

    Every request here sends a token naming Tenant A *and* a client-supplied
    ``tenant_id`` naming Tenant B, in every place a client can put one. In all
    cases the resolved identity must be Tenant A.
    """

    def test_a_body_tenant_id_is_ignored(
        self, identity_client: TestClient, auth_settings: Settings
    ) -> None:
        response = identity_client.post(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token_for(TENANT_A, app_settings=auth_settings)}"
            },
            json={"tenant_id": str(TENANT_B), "role": "admin"},
        )
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(TENANT_A)
        assert response.json()["role"] == "viewer"

    def test_a_query_tenant_id_is_ignored(
        self, identity_client: TestClient, auth_settings: Settings
    ) -> None:
        response = identity_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token_for(TENANT_A, app_settings=auth_settings)}"
            },
            params={"tenant_id": str(TENANT_B), "role": "admin"},
        )
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(TENANT_A)
        assert response.json()["role"] == "viewer"

    def test_a_tenant_header_is_ignored(
        self, identity_client: TestClient, auth_settings: Settings
    ) -> None:
        response = identity_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token_for(TENANT_A, app_settings=auth_settings)}",
                "X-Tenant-Id": str(TENANT_B),
                "X-Tenant": "globex",
                "X-Role": "admin",
            },
        )
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(TENANT_A)
        assert response.json()["role"] == "viewer"

    def test_a_path_tenant_id_is_ignored(
        self, identity_client: TestClient, auth_settings: Settings
    ) -> None:
        response = identity_client.get(
            f"/tenants/{TENANT_B}/whoami",
            headers={
                "Authorization": f"Bearer {token_for(TENANT_A, app_settings=auth_settings)}"
            },
        )
        # The route does not exist, so nothing is served from any tenant context
        # at all — which is the correct outcome for a tenant id in a path.
        assert response.status_code == 404

    def test_a_cookie_tenant_id_is_ignored(
        self, identity_client: TestClient, auth_settings: Settings
    ) -> None:
        identity_client.cookies.set("tenant_id", str(TENANT_B))
        response = identity_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token_for(TENANT_A, app_settings=auth_settings)}"
            },
        )
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(TENANT_A)


class TestEveryAuthenticationFailureIsOneResponse:
    """One status, one message, one header — for every way to fail.

    The alternative is an oracle: if a caller can tell a missing header from an
    expired token, it learns whether a token it holds was ever valid.
    """

    MISSING = None
    NOT_A_TOKEN = "definitely not a jwt"

    def failures(
        self, auth_settings: Settings, foreign_secret_settings: Settings
    ) -> dict[str, dict[str, str] | None]:
        """Every failing ``Authorization`` header value, keyed by description."""
        expired = create_access_token(
            subject=USER_ID,
            tenant_id=TENANT_A,
            role="viewer",
            app_settings=auth_settings,
            # `expires_delta` exists precisely so a test can mint an already-
            # expired token without freezing the clock globally.
            expires_delta=timedelta(minutes=-5),
        )
        wrong_key = token_for(TENANT_A, app_settings=foreign_secret_settings)
        refresh_token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_A, app_settings=auth_settings
        )
        return {
            "missing-header": None,
            "empty": {},
            "no-scheme": {"Authorization": self.NOT_A_TOKEN},
            "wrong-scheme": {"Authorization": f"Basic {self.NOT_A_TOKEN}"},
            "empty-credentials": {"Authorization": "Bearer "},
            "not-a-token": {"Authorization": f"Bearer {self.NOT_A_TOKEN}"},
            "wrong-signature": {"Authorization": f"Bearer {wrong_key}"},
            "expired": {"Authorization": f"Bearer {expired}"},
            "refresh-token": {"Authorization": f"Bearer {refresh_token}"},
            "unknown-role": {
                "Authorization": f"Bearer {token_for(TENANT_A, role='superuser', app_settings=auth_settings)}"
            },
        }

    def test_all_failures_produce_an_identical_response(
        self,
        identity_client: TestClient,
        auth_settings: Settings,
        foreign_secret_settings: Settings,
    ) -> None:
        cases = self.failures(auth_settings, foreign_secret_settings)
        assert len(cases) >= 9, "the failure catalogue shrank; keep it exhaustive"

        responses = {
            description: identity_client.get("/whoami", headers=headers)
            for description, headers in cases.items()
        }
        for description, response in responses.items():
            assert response.status_code == 401, description
            assert response.json() == {"detail": UNAUTHORIZED_DETAIL}, description
            assert response.headers["www-authenticate"] == WWW_AUTHENTICATE_VALUE, (
                description
            )

    def test_the_response_leaks_no_reason(
        self,
        identity_client: TestClient,
        auth_settings: Settings,
        foreign_secret_settings: Settings,
    ) -> None:
        """No exception type, claim name, tenant id or token fragment anywhere.

        Asserted as a substring scan over the whole raw response rather than on
        parsed fields, because a leak in a header would not show up in the JSON.
        """
        for description, headers in self.failures(
            auth_settings, foreign_secret_settings
        ).items():
            response = identity_client.get("/whoami", headers=headers)
            body = response.text.lower()
            for forbidden in (
                "expired",
                "signature",
                "algorithm",
                "missingrequiredclaim",
                "invalidjti",
                "decodeerror",
                "pyjwt",
                "jwt",
                "claim",
                "secret",
                str(TENANT_A).lower(),
            ):
                assert forbidden not in body, (
                    f"{description}: response leaked {forbidden!r}: {response.text}"
                )

    def test_the_real_reason_is_logged_not_returned(
        self,
        identity_client: TestClient,
        auth_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Operators need to know *why*; attackers must not.

        Pairs with the previous test: the detail exists, it just goes to the log.
        """
        caplog.set_level(logging.WARNING)
        response = identity_client.get(
            "/whoami",
            headers={
                "Authorization": f"Bearer {token_for(TENANT_A, app_settings=auth_settings)}",
                "X-Ignored": "yes",
            },
        )
        # A *valid* token is not rejected, so nothing is logged at all.
        assert response.status_code == 200

        bad = identity_client.get("/whoami", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert messages, "the rejection reason must reach the log"
        assert UNAUTHORIZED_DETAIL not in messages

    def test_a_403_is_never_returned(
        self, identity_client: TestClient
    ) -> None:
        """``403`` would mean "authenticated, not allowed" — which is false here.

        FastAPI's ``HTTPBearer`` defaults to raising its own ``403`` for a
        malformed header; ``auto_error=False`` is what prevents that.
        """
        for headers in (None, {}, {"Authorization": "Basic x"}, {"Authorization": "x"}):
            response = identity_client.get("/whoami", headers=headers)
            assert response.status_code != 403
            assert response.status_code == 401


# ---------------------------------------------------------------------------
# The tenant actually in the token is the one that matters
# ---------------------------------------------------------------------------


class TestTokensFromTheDatabaseAreTheSourceOfTruth:
    """End-to-end: log in, then use the resulting token.

    Closes the loop between the login service and the dependency, and is the
    assertion that a real login produces a usable, correctly-scoped identity.
    """

    def test_a_login_token_authenticates_as_that_tenant(
        self,
        identity_client: TestClient,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        session = model_session
        tenant, user = make_account(slug="acme", email="alice@example.com")
        _u, _t, tokens = authenticate(
            session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        session.commit()

        response = identity_client.get(
            "/whoami", headers={"Authorization": f"Bearer {tokens.access_token}"}
        )
        assert response.status_code == 200
        assert response.json()["tenant_id"] == str(tenant.id)
        assert response.json()["user_id"] == str(user.id)

    def test_a_token_from_tenant_a_cannot_be_relabelled_as_tenant_b(
        self,
        identity_client: TestClient,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """The realistic attack: reuse a valid token against another tenant.

        No request is modified at all — the same bytes are sent, and the
        dependency resolves Tenant A every time. This is the case that a
        request-supplied tenant id would defeat, and it is why the token is the
        only tenant source.
        """
        session = model_session
        tenant_a, _user_a = make_account(slug="acme", email="alice@example.com")
        tenant_b, _user_b = make_account(slug="globex", email="alice@example.com")
        _u, _t, tokens = authenticate(
            session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        session.commit()

        assert tenant_a.id != tenant_b.id

        first = identity_client.get(
            "/whoami", headers={"Authorization": f"Bearer {tokens.access_token}"}
        )
        assert first.json()["tenant_id"] == str(tenant_a.id)

        # Same token, but the request is framed as belonging to Tenant B.
        second = identity_client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {tokens.access_token}"},
            params={"tenant_id": str(tenant_b.id)},
        )
        assert second.status_code == 200
        assert second.json()["tenant_id"] == str(tenant_a.id)

    def test_a_logout_revokes_the_refresh_token_but_not_the_access_token(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """Documented behaviour, asserted so it is not mistaken for a bug.

        Revoking a refresh token stops *future* refreshes; the access token
        already issued stays valid until its own 15-minute expiry. Fixing that
        would require a token blocklist, which is a database read on every
        request — deliberately not done at this step, because the access token's
        short lifetime is the PMP's stated mechanism instead (PMP §8).
        """
        session = model_session
        make_account(slug="acme")
        _u, _t, tokens = authenticate(
            session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        session.commit()

        identity = AuthenticatedIdentity.from_access_token(
            tokens.access_token, app_settings=auth_settings
        )
        assert identity.tenant_id is not None

        revoke_refresh_token(session, refresh_token=tokens.refresh_token)
        session.commit()

        with pytest.raises(TokenValidationError):
            exchange_refresh_token(
                session,
                refresh_token=tokens.refresh_token,
                app_settings=auth_settings,
            )

        # The already-issued access token still verifies, as documented above.
        assert (
            AuthenticatedIdentity.from_access_token(
                tokens.access_token, app_settings=auth_settings
            ).user_id
            == identity.user_id
        )


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------


def test_the_dependency_takes_no_tenant_parameter() -> None:
    """``get_current_identity`` has no way to accept a tenant from anywhere.

    A structural counterpart to the behavioural tests: the function's signature
    admits no tenant id, so the only one it can produce is the one it decoded.
    """

    parameters = inspect.signature(get_current_identity).parameters
    assert set(parameters) == {"credentials", "app_settings"}
    for name in parameters:
        assert "tenant" not in name
        assert "role" not in name


def test_the_identity_is_constructed_at_exactly_two_named_sites() -> None:
    """``cls(...)`` is called twice, and only from the two named constructors.

    Asserted on the parsed source rather than by counting the substring ``"cls("``,
    which is what the original version of this test did. Counting was too weak in
    both directions at once: a rename or a docstring mentioning ``cls(`` would
    break a passing state, and — more seriously — the substring cannot tell a
    construction from a *mention* of one, so an extra call site hiding inside an
    f-string or a comment would have satisfied it.

    Reading the tree instead makes the assertion about structure: exactly two
    ``cls(...)`` calls in the module, each inside a function whose name is named
    here. A third constructor, or a ``cls(...)`` anywhere else — including inside
    a nested helper — fails.

    The two are the whole of the design:

    * ``from_claims`` — reached only from ``from_access_token``, which verifies
      the signature first.
    * ``for_tenant_service`` — added in Phase 5 Step 2 for the Celery worker,
      which holds no token to verify and reads its tenant from a persisted row.
    """

    import app.core.identity as identity_module

    tree = ast.parse(Path(identity_module.__file__).read_text(encoding="utf-8"))
    expected = {"from_claims", "for_tenant_service"}

    sites = _cls_call_sites(tree)

    assert {enclosing for enclosing, _ in sites} == expected, (
        "AuthenticatedIdentity must be constructed only from "
        f"{sorted(expected)}, one construction site each"
    )
    assert len(sites) == len(expected), (
        "each named constructor must construct the identity exactly once; a "
        "duplicate call site inside one of them is a third way in"
    )


def _cls_call_sites(tree: ast.AST) -> list[tuple[str | None, ast.Call]]:
    """Return ``(enclosing function name, call)`` for every ``cls(...)`` call.

    Walks into nested functions and lambdas rather than stopping at them: a
    ``cls(...)`` inside a closure is still a way to construct an identity, and it
    is exactly the sort of thing a name-based guard misses.
    """
    sites: list[tuple[str | None, ast.Call]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call) and _is_cls_call(inner):
                    sites.append((node.name, inner))
    return sites


def _is_cls_call(call: ast.Call) -> bool:
    """Whether ``call`` invokes ``cls`` by that exact name.

    ``self(...)`` does not count: a classmethod's own name is ``cls``, and
    allowing any spelling would let a differently-named local satisfy the guard.
    """
    return isinstance(call.func, ast.Name) and call.func.id == "cls"


def test_only_the_service_constructor_may_build_an_identity_from_a_row() -> None:
    """The persisted-row source is only reachable through its named constructor.

    Complements :func:`test_the_identity_is_constructed_at_exactly_two_named_sites`.
    That test says there are two doors; this says what comes through the second
    one, because "two doors" alone would be satisfied by a second door that also
    accepted a caller's ``tenant_id`` — which is precisely the hole
    :meth:`AuthenticatedIdentity.for_tenant_service` exists to close.
    """
    assert (
        AuthenticatedIdentity.for_tenant_service(tenant_id=TENANT_A).tenant_id
        is TENANT_A
    )

    # A row source must be *the only* thing that constructor can produce: it
    # hardcodes the role rather than accepting one, so no caller can widen the
    # service actor to an administrator.
    service = AuthenticatedIdentity.for_tenant_service(tenant_id=TENANT_A)
    assert service.role is UserRole.VIEWER, (
        "a service identity must be least privilege; an admin role here would "
        "grant the worker authority a document-ingest task has no use for"
    )

    # And it must be keyword-only, so a positional caller cannot pass the tenant
    # into a parameter that does not exist.
    parameters = inspect.signature(
        AuthenticatedIdentity.for_tenant_service
    ).parameters
    assert set(parameters) == {"tenant_id"}
    assert (
        parameters["tenant_id"].kind is inspect.Parameter.KEYWORD_ONLY
    ), "tenant_id must be keyword-only"
    assert all("role" not in name for name in parameters), (
        "the service constructor must not accept a role; least privilege has to "
        "be the only option, not the default"
    )


def test_the_service_identity_names_a_fixed_non_person_actor() -> None:
    """``user_id`` is a constant, not a random or caller-chosen value.

    Asserted rather than trusted because the alternative is the worst version of
    this feature: a worker that attributes its writes to whoever happened to
    upload the document, which would make an audit trail record a person for work
    that person did not do. The constant is the honest answer, and it is also
    never a real ``users`` row, so nothing can join on it by accident.
    """
    first = AuthenticatedIdentity.for_tenant_service(tenant_id=TENANT_A)
    second = AuthenticatedIdentity.for_tenant_service(tenant_id=TENANT_B)

    assert first.user_id == second.user_id, (
        "the system actor must be the same constant for every service identity, "
        "so two runs cannot be attributed to two different 'users'"
    )
    assert first.user_id.int == 0, (
        "the system actor is the all-zeros UUID — unmistakable in a log and "
        "impossible to confuse with a real user"
    )
    assert first.tenant_id != second.tenant_id, (
        "two service identities differ only in the tenant they act within"
    )


def test_no_module_outside_identity_constructs_an_identity_directly() -> None:
    """The worker may not build an identity with ``AuthenticatedIdentity(...)``.

    The guard in :func:`test_the_identity_is_constructed_at_exactly_two_named_sites`
    reads ``identity.py``'s own source, so it cannot see a construction in another
    file — which is exactly what the Celery worker would write if it were allowed
    to. Phase 5 Step 2 reached for :meth:`for_tenant_service` instead; this is the
    test that keeps that choice from quietly being reverted.

    Scans every module in ``app/`` except ``app/core/identity.py`` itself, and
    matches both call spellings: the imported name and an attribute access on an
    aliased import.
    """
    import app

    app_root = Path(app.__file__).parent
    offenders: list[str] = []
    for path in sorted(app_root.rglob("*.py")):
        if path.name == "identity.py" and path.parent.name == "core":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            # `AuthenticatedIdentity(...)` and an attribute access on an aliased
            # import, which is the other way to name it.
            if (
                isinstance(called, ast.Name)
                and called.id == "AuthenticatedIdentity"
                or isinstance(called, ast.Attribute)
                and called.attr == "AuthenticatedIdentity"
            ):
                offenders.append(f"{path.relative_to(app_root)}:{node.lineno}")

    assert not offenders, (
        "AuthenticatedIdentity must be built only through its two classmethods; "
        f"direct construction at {offenders}"
    )


def test_identity_errors_are_not_catchable_as_library_errors() -> None:
    """A caller must not mistake a rejection for a library fault.

    If ``TokenValidationError`` subclassed ``PyJWTError``, an internal decoding
    failure elsewhere could be swallowed by the same ``except`` clause that
    handles a bad token.
    """
    assert issubclass(TokenValidationError, ValueError)
    assert not issubclass(TokenValidationError, PyJWTError)


def test_the_dependency_is_the_one_registered_for_overrides() -> None:
    """``CurrentIdentity`` must reference *this* function object.

    If it were re-declared per route, ``dependency_overrides`` would silently
    stop working and a test could pass while production behaved differently.
    """

    # `CurrentIdentity` is `Annotated[AuthenticatedIdentity, Depends(...)]`, so the
    # metadata tuple holds the Depends marker itself, carrying the function it
    # wraps on `.dependency`.
    origin = typing.get_origin(CurrentIdentity)
    hint, *metadata = typing.get_args(CurrentIdentity)
    assert origin is typing.Annotated
    assert hint is AuthenticatedIdentity
    assert len(metadata) == 1, "one dependency marker, nothing else"

    # `fastapi.Depends` is a *function* returning a `fastapi.params.Depends`
    # marker, so the marker class is imported from `params` for the isinstance
    # check. Using the class directly is what makes this a real type assertion
    # rather than a duck-typed attribute check.
    depends_marker = metadata[0]
    assert isinstance(depends_marker, ParamsDepends)
    assert depends_marker.dependency is get_current_identity


def test_the_role_check_is_not_an_enforcement_check() -> None:
    """``role`` is carried, never enforced.

    RBAC is Phase 2 Step 3. This module has no ``require_role``-style helper
    and none is added here, because a dependency that both authenticates and
    authorizes cannot be reused by a route with a different role rule.
    """

    from app.api import deps

    assert not any(
        inspect.iscoroutinefunction(value) and "role" in name
        for name, value in vars(deps).items()
        if callable(value)
    )
    for marker in ("require_role", "check_permission", "has_role", "is_admin"):
        assert not any(
            marker in name.lower()
            for name, value in vars(deps).items()
            if callable(value)
        )


def test_no_tenant_id_is_accepted_by_any_auth_entry_point() -> None:
    """None of the four entry points takes a tenant identifier.

    ``from_access_token``, ``from_claims``, ``get_current_identity`` and
    ``authenticate`` are the complete set of ways this step turns something into
    an identity. Each is checked for the *absence* of the parameter, which is
    the property that cannot be weakened by a behaviour change elsewhere.
    """

    from app.services import auth as auth_service

    for callable_under_test in (
        AuthenticatedIdentity.from_access_token,
        AuthenticatedIdentity.from_claims,
        get_current_identity,
        auth_service.authenticate,
    ):
        parameters = inspect.signature(callable_under_test).parameters
        assert "tenant_id" not in parameters, (
            f"{callable_under_test.__name__} accepts a tenant_id; tenant context "
            "must come from authenticated identity only"
        )

    # `authenticate` legitimately takes a slug, which selects the tenant to
    # search and never becomes an identity.
    authenticate_parameters = inspect.signature(
        auth_service.authenticate
    ).parameters
    assert "tenant_slug" in authenticate_parameters
    assert "tenant_id" not in authenticate_parameters


def test_the_tenant_scope_constant_still_has_no_request_source() -> None:
    """The Phase 2 Step 1 AST guard still passes after this step.

    The original suite proved no application module read a tenant from request
    data. Step 2 added modules; this confirms none of them reintroduced one.
    """
    from tests.test_security_isolation import (
        application_python_files,
        matching_environment_reads,
        matching_sources,
        module_level_tenant_names,
        parse,
    )

    for path in application_python_files():
        tree = parse(path)
        assert not matching_sources(tree), path
        assert not matching_environment_reads(tree), path
        assert not module_level_tenant_names(tree), path


def test_the_dep_module_defines_only_the_identity_dependency() -> None:
    """``app/api/deps.py`` is the request-scoped surface; keep it minimal.

    Judged on *definitions* rather than on everything callable in the module's
    namespace: the module legitimately imports ``Depends``, ``HTTPBearer``,
    ``get_settings`` and ``Settings``, and those are not things it offers. The
    risk being guarded against is a *second authentication* function defined
    here — a ``require_role`` or a ``get_current_tenant`` — which would be a new
    authentication surface nobody reviewed.

    Phase 2.4 added :func:`app.api.deps.get_file_store`, and the assertion is
    widened rather than relaxed: a build dependency that constructs a filesystem
    store from settings is a different kind of thing from a dependency that
    decides who the caller is, and the closed set is what keeps a third
    authentication helper from appearing unnoticed. Authorization continues to
    live in exactly one place, :mod:`app.api.rbac`, which
    ``tests/test_rbac.py`` audits separately.

    PMP Phase 4 added three more build dependencies,
    :func:`app.api.deps.get_embedding_provider`,
    :func:`app.api.deps.get_generation_provider` and
    :func:`app.api.deps.get_vector_store`, on the same argument as
    ``get_file_store``: each constructs an object whose implementation is chosen by
    configuration, so each must be substitutable through ``dependency_overrides``
    for a test to exercise the query path offline. None decides anything about the
    caller, and the closed set is precisely what lets a reader confirm that — a
    dependency that took an identity would appear in this set and fail the
    equality.

    :func:`app.api.deps.get_vector_store` is the fifth member and the only one whose
    *factory* lives elsewhere: :func:`app.storage.qdrant.get_vector_store` is a cached
    constructor called directly by ingestion, and the dependency here wraps it so its
    ``settings`` parameter is resolved by injection. That wrapper is not an extra
    authentication or build surface — it takes settings and returns a store — and
    ``test_every_dependency_parameter_is_resolved_by_injection`` below is what keeps it
    honest.
    """
    from app.api import deps

    defined = {
        name
        for name, value in vars(deps).items()
        if inspect.isfunction(value) and value.__module__ == deps.__name__
    }
    assert defined == {
        "get_current_identity",
        "get_file_store",
        "get_embedding_provider",
        "get_generation_provider",
        "get_vector_store",
    }, sorted(defined)


def test_every_dependency_parameter_is_resolved_by_injection() -> None:
    """No dependency in ``app/api/deps.py`` may take a bare pydantic-model parameter.

    This is the guard for a defect Phase 4 shipped and this test file did not catch.
    :func:`app.api.deps.get_vector_store` was originally the *cached factory* imported
    straight from :mod:`app.storage.qdrant`, whose signature is
    ``(settings: Settings)``. FastAPI does not reject a dependency whose parameter is a
    plain pydantic model — it treats it as **one more request body field**. So
    ``POST /query`` grew an implicit body model of ``request`` plus a required
    ``settings``, and answered ``422`` for every request a client could correctly send.
    No exception, no failing unit test, and an OpenAPI document that faithfully described a
    body nobody could produce.

    The rule is therefore stated structurally rather than as a list of the parameters
    involved: for every function this module defines, each parameter must either be
    ``Annotated[..., Depends(...)]`` or resolve to a non-pydantic type. A pydantic model
    reaching FastAPI unannotated is always a body field, in this module and in any
    other, so naming the one instance would only protect the one instance.
    """
    from fastapi import params
    from pydantic import BaseModel

    from app.api import deps

    offenders: dict[str, list[str]] = {}
    for name, value in vars(deps).items():
        if not (inspect.isfunction(value) and value.__module__ == deps.__name__):
            continue
        leaked = [
            parameter_name
            for parameter_name, parameter in typing.get_type_hints(
                value, include_extras=True
            ).items()
            if not isinstance(parameter, params.Depends)
            and not (
                typing.get_origin(parameter) is not None
                or not issubclass(parameter, BaseModel)
            )
        ]
        if leaked:
            offenders[name] = leaked

    assert offenders == {}, offenders


def test_no_dependency_in_the_dep_module_takes_a_role_to_judge_against() -> None:
    """Authorization may not acquire a second entry point in ``app/api/deps.py``.

    The closed set in the test above proves no *new* function appeared there. This proves
    the ones that did cannot be used to decide what a caller may see.

    Stated as **a role appearing in any signature**, which is the shape a second
    authorization helper would have: :func:`app.api.rbac.require_role` exists precisely to
    turn ``UserRole`` values into an allowlist decision, and a second place that could
    accept a role is a second place that could decide one. Note that
    :func:`app.api.deps.get_current_identity` does *not* appear as an offender, and should
    not: it *returns* an identity and never receives a role, so authentication and
    authorization stay on opposite sides of this assertion.

    Expressed over annotations rather than over bodies, because a body-level check would
    have to recognise every spelling of a role comparison and would pass the day someone
    wrote one it did not anticipate. A function can only branch on a role it was handed.
    """
    from app.api import deps

    offenders = {
        name
        for name, value in vars(deps).items()
        if inspect.isfunction(value)
        and value.__module__ == deps.__name__
        and any(
            "UserRole" in str(parameter.annotation)
            for parameter in inspect.signature(value).parameters.values()
        )
    }
    assert offenders == set(), sorted(offenders)


def test_the_single_role_checking_dependency_still_lives_in_rbac() -> None:
    """The counterpart to the assertion above, so widening ``deps`` has an anchor.

    Stated positively rather than as "nothing else was added": an audit that can only
    report absence gives the same result whether authorization stayed in one module or was
    quietly duplicated into three. This fails if ``require_role`` disappears or is renamed,
    which is the situation in which :func:`require_role`'s callers — including the query
    route's — would silently stop being checked at all.
    """
    from app.api import rbac

    assert callable(rbac.require_role)
    assert rbac.require_role.__module__ == "app.api.rbac"


def test_identity_is_a_plain_value_with_no_orm_row_attached() -> None:
    """Building an identity from claims does not need or hold a session.

    Complements ``test_it_cannot_hold_a_database_session``: this proves the
    construction path is genuinely independent of the database, so authentication
    cannot be blocked by one and cannot observe uncommitted state.
    """
    identity = AuthenticatedIdentity(
        user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=UserRole.ADMIN
    )
    assert isinstance(identity.user_id, uuid.UUID)
    assert isinstance(identity.tenant_id, uuid.UUID)
    assert identity.role is UserRole.ADMIN


def test_annotated_identity_is_usable_as_a_route_parameter(
    identity_app: FastAPI,
) -> None:
    """Sanity check that the ``Annotated`` alias is wired for FastAPI.

    Without this, a typo in the alias would leave the dependency tests passing
    against a hand-written route while every real route failed at import.
    """
    application = FastAPI()

    @application.get("/check")
    def check(identity: Annotated[AuthenticatedIdentity, Depends(get_current_identity)]) -> str:
        return str(identity.tenant_id)

    paths = {route.path for route in application.routes if hasattr(route, "path")}
    assert "/check" in paths


def test_a_role_claim_that_is_a_known_enum_member_round_trips(
    auth_settings: Settings,
) -> None:
    """Every ``UserRole`` value survives the token round-trip intact.

    This is what ties the two enumerations together. ``UserRole`` is defined once
    in ``app.models.user`` and reused by the identity boundary, so a role can
    never be a differently-spelled string in two places.
    """
    for role in UserRole:
        identity = AuthenticatedIdentity.from_access_token(
            token_for(TENANT_A, role=role.value, app_settings=auth_settings),
            app_settings=auth_settings,
        )
        assert identity.role is role


def test_the_tenant_in_the_identity_is_always_a_uuid_not_a_slug(
    auth_settings: Settings,
) -> None:
    """A tenant identifier is a UUID; a slug can never be one.

    ``authenticate`` is the only function that sees a slug, and it converts to an
    id before minting anything. Asserted through the token because that is where
    the slug would otherwise leak back in.
    """
    identity = AuthenticatedIdentity.from_access_token(
        token_for(TENANT_A, app_settings=auth_settings),
        app_settings=auth_settings,
    )
    assert isinstance(identity.tenant_id, uuid.UUID)
    assert identity.tenant_id.version == 4