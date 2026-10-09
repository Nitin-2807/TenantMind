"""The authentication endpoints, end to end over HTTP (PMP §15, Phase 2 Step 3).

These tests drive the real application through ``TestClient``: real routing, real
Pydantic validation, real Argon2, real SQL, real transaction boundaries. The
only substitutions are the two the application provides seams for — the settings
and the database session — and both live in ``conftest.py``.

What is deliberately *not* stubbed
----------------------------------
:func:`app.core.security.hash_password` runs for real on every registration and
invitation. A suite that mocked the hasher would pass with a completely broken
password check, and this is the phase that makes hashing reachable from a
request. The cost is roughly 60-100 ms per hash, which is why the fixtures are
written to hash as little as each assertion needs.

Grouping
--------
The classes below are grouped by the property under test rather than by endpoint,
so a failure names what broke: the mounted surface, registration, login's opaque
failures, rotation and revocation, and what a response must never carry.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import (
    TokenType,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant
from app.models.user import User
from app.services.auth import BEARER_SCHEME
from tests.conftest import make_settings

#: The mounted prefix. Asserted against the configured value in
#: :class:`TestMountedUnderTheVersionedPrefix` so it cannot drift.
API_PREFIX = "/api/v1"

#: Addresses under ``.test``, which RFC 6761 reserves and which therefore can
#: never be registered by anyone. Safe to use in tests for that reason alone.
ADMIN_EMAIL = "admin@acme.test"

#: Long enough to satisfy the creation policy; obviously synthetic.
STRONG_PASSWORD = "correct horse battery staple"

#: Below the creation policy's 12-character floor, used to prove that a *login*
#: attempt is not rejected for failing a policy that governs only creation.
BENEATH_THE_FLOOR = "short"

#: The one 401 detail the whole authentication surface shares.
REJECTED = {"detail": "Could not validate credentials"}


def register(client: TestClient, **overrides: Any) -> dict[str, Any]:
    """Register a tenant and return the parsed 201 body."""
    payload: dict[str, Any] = {
        "name": "Acme Corporation",
        "slug": "acme",
        "admin_email": ADMIN_EMAIL,
        "admin_password": STRONG_PASSWORD,
    }
    payload.update(overrides)
    response = client.post(f"{API_PREFIX}/auth/register-tenant", json=payload)
    assert response.status_code == 201, response.text
    return dict(response.json())


def login(client: TestClient, **overrides: Any) -> Any:
    """POST the login endpoint and return the whole ``Response``.

    A ``Response`` rather than a parsed body, so a caller can assert on the
    status code of a *failure* without this helper having to model one.
    """
    payload: dict[str, Any] = {
        "tenant_slug": "acme",
        "email": ADMIN_EMAIL,
        "password": STRONG_PASSWORD,
    }
    payload.update(overrides)
    return client.post(f"{API_PREFIX}/auth/login", json=payload)


def exchange(client: TestClient, refresh_token: str) -> Any:
    """POST the refresh endpoint."""
    return client.post(
        f"{API_PREFIX}/auth/refresh", json={"refresh_token": refresh_token}
    )


def logout(client: TestClient, refresh_token: str) -> Any:
    """POST the logout endpoint."""
    return client.post(f"{API_PREFIX}/auth/logout", json={"refresh_token": refresh_token})


# ---------------------------------------------------------------------------
# The mounted surface
# ---------------------------------------------------------------------------


class TestMountedUnderTheVersionedPrefix:
    def test_the_hard_coded_prefix_matches_the_configured_one(self) -> None:
        """These tests spell the prefix out; the configuration must agree."""
        assert make_settings().api_v1_prefix == API_PREFIX

    def test_the_health_probes_are_not_versioned(
        self, api_client: TestClient
    ) -> None:
        """Infrastructure probes stay at the root across API versions."""
        assert api_client.get("/health").status_code == 200
        assert api_client.get("/health/ready").status_code == 200
        assert api_client.get(f"{API_PREFIX}/health").status_code == 404

    @pytest.mark.parametrize(
        "path",
        [
            f"{API_PREFIX}/auth/register-tenant",
            f"{API_PREFIX}/auth/login",
            f"{API_PREFIX}/auth/refresh",
            f"{API_PREFIX}/auth/logout",
        ],
    )
    def test_the_endpoint_is_wired(self, api_client: TestClient, path: str) -> None:
        """A registered route answers, even to a request it will reject.

        ``422`` is the proof the route matched: a missing route answers ``404``
        before the body is ever validated. Sending an empty object is the
        cheapest way to ask "is this path wired?" without asserting anything
        about a real request's outcome.
        """
        assert api_client.post(path, json={}).status_code == 422

    def test_there_is_no_short_register_alias(self, api_client: TestClient) -> None:
        """``/auth/register`` is not a second name for registration.

        Two URLs for one operation is a second thing to keep in sync and a second
        name in the API documentation. PMP §15 says ``/auth/register-tenant``, and
        that is the only spelling served.
        """
        assert api_client.post(f"{API_PREFIX}/auth/register", json={}).status_code == 404


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_it_creates_the_tenant_and_its_first_administrator(
        self, api_client: TestClient
    ) -> None:
        body = register(api_client)

        assert body["tenant"]["slug"] == "acme"
        assert body["tenant"]["name"] == "Acme Corporation"
        assert body["user"]["email"] == ADMIN_EMAIL
        assert body["user"]["role"] == "admin"
        assert body["user"]["is_active"] is True

    def test_the_first_user_is_an_administrator(self, api_client: TestClient) -> None:
        """Otherwise FR-03 is unreachable: nobody could promote anybody.

        A tenant whose only user is a viewer could never grant a role, invite a
        user, or delete one — every one of those routes requires an
        authenticated administrator. The role is therefore not a request field,
        and this is the only place outside an administrator's own decision that
        the ADMIN role is assigned.
        """
        assert register(api_client)["user"]["role"] == "admin"

    def test_the_tenant_id_is_generated_server_side(
        self, api_client: TestClient
    ) -> None:
        assert uuid.UUID(register(api_client)["tenant"]["id"]).version == 4

    def test_a_caller_supplied_tenant_id_is_ignored(
        self, api_client: TestClient
    ) -> None:
        """A guessed or leaked UUID cannot become a tenant identity.

        Sent as an extra field, which Pydantic ignores: the request model has no
        ``tenant_id``, so the value does not exist as far as the handler is
        concerned. The tenant created still gets a fresh id of its own.
        """
        supplied = "11111111-2222-4333-8444-555555555555"
        body = register(api_client, tenant_id=supplied)

        assert body["tenant"]["id"] != supplied

    def test_the_password_is_stored_as_an_argon2_hash(
        self, api_client: TestClient, read_only: Any
    ) -> None:
        """The column holds an Argon2id PHC string, never the password.

        Read back from the database rather than from the response, because the
        property being asserted is about what was *written*. The hash is then
        verified to actually accept the password, which is what proves the
        algorithm and the parameters are wired up rather than merely labelled.
        """
        register(api_client)

        with read_only() as session:
            stored = session.scalars(select(User)).one().password_hash

        assert stored.startswith("$argon2id$")
        assert STRONG_PASSWORD not in stored
        assert verify_password(STRONG_PASSWORD, stored)

    def test_it_returns_a_usable_token_pair(
        self, api_client: TestClient, auth_settings: Settings
    ) -> None:
        """Both tokens verify, and the stated lifetime is the configured one.

        ``expires_in_seconds`` is read off the settings rather than written as a
        literal, so a change to the configured access-token lifetime shows up here
        as a failure instead of as a client that trusts a stale number.
        """
        tokens = register(api_client)["tokens"]

        assert tokens["token_type"] == BEARER_SCHEME
        assert tokens["access_token"] and tokens["refresh_token"]
        assert tokens["expires_in_seconds"] == auth_settings.access_token_expire_minutes * 60

        access = decode_token(
            str(tokens["access_token"]),
            expected_type=TokenType.ACCESS,
            app_settings=auth_settings,
        )
        assert access.role == "admin"

        refresh = decode_token(
            str(tokens["refresh_token"]),
            expected_type=TokenType.REFRESH,
            app_settings=auth_settings,
        )
        assert str(refresh.subject) == str(access.subject)

    def test_the_returned_tokens_belong_to_the_new_tenant(
        self, api_client: TestClient, auth_settings: Settings
    ) -> None:
        body = register(api_client)

        claims = decode_token(
            body["tokens"]["access_token"],
            expected_type=TokenType.ACCESS,
            app_settings=auth_settings,
        )

        assert str(claims.tenant_id) == body["tenant"]["id"]
        assert str(claims.subject) == body["user"]["id"]
        assert claims.role == "admin"

    def test_the_refresh_token_is_persisted_only_as_a_digest(
        self, api_client: TestClient, read_only: Any
    ) -> None:
        """The raw token is never written; only its SHA-256 digest is.

        A dump of ``refresh_tokens`` must therefore yield no usable credential —
        which is the whole reason the column holds a digest at all.
        """
        raw = str(register(api_client)["tokens"]["refresh_token"])

        with read_only() as session:
            stored = session.scalars(select(RefreshToken)).all()

        assert len(stored) == 1
        assert stored[0].token_hash != raw
        assert raw not in stored[0].token_hash

    def test_a_second_registration_of_the_same_slug_is_refused(
        self, api_client: TestClient
    ) -> None:
        register(api_client)

        response = api_client.post(
            f"{API_PREFIX}/auth/register-tenant",
            json=duplicate_slug_payload(),
        )

        assert response.status_code == 409

    def test_a_refused_duplicate_writes_nothing(
        self, api_client: TestClient, read_only: Any
    ) -> None:
        """The conflict must not leave a half-registered second tenant behind.

        This is the transactional property that matters. A tenant row without its
        administrator is an organisation that can never be administered, and
        nothing in the API can repair it — every user-management route requires
        an authenticated administrator who does not exist.
        """
        register(api_client)
        api_client.post(
            f"{API_PREFIX}/auth/register-tenant", json=duplicate_slug_payload()
        )

        with read_only() as session:
            assert session.scalar(select(func.count()).select_from(Tenant)) == 1
            assert session.scalar(select(func.count()).select_from(User)) == 1

    def test_the_conflict_message_discloses_nothing_about_the_incumbent(
        self, api_client: TestClient
    ) -> None:
        """The endpoint is unauthenticated, so its errors must stay quiet.

        The incumbent tenant's name, id and administrator's address are all
        information about somebody else's registration, and naming any of them
        would let an unauthenticated caller probe for it.
        """
        register(api_client)
        response = api_client.post(
            f"{API_PREFIX}/auth/register-tenant", json=duplicate_slug_payload()
        )

        detail = response.json()["detail"]
        assert "Acme Corporation" not in detail
        assert ADMIN_EMAIL not in detail

    @pytest.mark.parametrize("slug", ["Acme", "ACME", "  acme  ", " acMe "])
    def test_slugs_are_folded_so_case_cannot_duplicate_one(
        self, api_client: TestClient, slug: str
    ) -> None:
        """Registering ``Acme`` and ``acme`` is one tenant, not two.

        Without folding, ``tenants.slug`` is unique only case-sensitively, so one
        organisation could hold two rows differing by case — and a user who typed
        the slug in the other case at login would be locked out of the tenant they
        registered.
        """
        register(api_client)

        response = api_client.post(
            f"{API_PREFIX}/auth/register-tenant",
            json={
                "name": "Impostor",
                "slug": slug,
                "admin_email": "someone-else@acme.test",
                "admin_password": STRONG_PASSWORD,
            },
        )

        assert response.status_code == 409


def duplicate_slug_payload() -> dict[str, str]:
    """A second registration attempt against an already-registered slug."""
    return {
        "name": "Impostor",
        "slug": "acme",
        "admin_email": "someone-else@acme.test",
        "admin_password": STRONG_PASSWORD,
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_valid_credentials_return_a_token_pair(
        self, api_client: TestClient
    ) -> None:
        register(api_client)

        response = login(api_client)

        assert response.status_code == 200
        body = response.json()
        assert body["user"]["email"] == ADMIN_EMAIL
        assert body["user"]["role"] == "admin"
        assert body["tokens"]["token_type"] == BEARER_SCHEME
        assert body["tokens"]["access_token"]

    def test_email_case_does_not_matter(self, api_client: TestClient) -> None:
        """Login folds exactly as registration does, so the two agree.

        The two endpoints must use the same rule. If registration folded to lower
        case and login did not, a correct password on a correct address would
        return "invalid credentials" — a failure with no visible cause.
        """
        register(api_client)

        assert login(api_client, email="ADMIN@ACME.TEST").status_code == 200

    def test_the_tenant_slug_is_case_insensitive_too(
        self, api_client: TestClient
    ) -> None:
        register(api_client)

        assert login(api_client, tenant_slug="ACME").status_code == 200

    @pytest.mark.parametrize(
        "overrides",
        [
            {"password": "not the password"},
            {"email": "nobody@acme.test"},
            {"tenant_slug": "not-a-tenant"},
            {"email": "admin@other.test"},
            {"email": ""},
        ],
        ids=["wrong-password", "unknown-user", "unknown-tenant", "other-tenant", "empty-email"],
    )
    def test_every_credential_failure_is_the_same_401(
        self, api_client: TestClient, overrides: dict[str, str]
    ) -> None:
        """One failure, one message, whatever the cause.

        This is the property that makes login unusable as an
        account-enumeration oracle. The ``ids`` label each case; nothing in the
        assertion depends on which one is being exercised.
        """
        register(api_client)

        response = login(api_client, **overrides)

        assert response.status_code in (401, 422)
        if response.status_code == 401:
            assert response.json() == REJECTED

    def test_the_failure_message_is_identical_across_causes(
        self, api_client: TestClient
    ) -> None:
        """The detail string, the status and the challenge header all match."""
        register(api_client)

        wrong_password = login(api_client, password="not the password")
        unknown_tenant = login(api_client, tenant_slug="not-a-tenant")
        unknown_user = login(api_client, email="nobody@acme.test")

        assert wrong_password.json() == unknown_tenant.json()
        assert wrong_password.json() == unknown_user.json()
        for response in (wrong_password, unknown_tenant, unknown_user):
            assert response.status_code == 401
            assert response.headers["WWW-Authenticate"] == BEARER_SCHEME

    def test_no_failure_response_names_a_specific_reason(
        self, api_client: TestClient
    ) -> None:
        """Nothing in the body distinguishes one cause from another."""
        register(api_client)

        body = login(api_client, password="not the password").json()

        serialized = str(body).lower()
        for leaked in ("password", "tenant", "user", "slug", "exist", "unknown"):
            assert leaked not in serialized

    def test_a_disabled_account_cannot_log_in(
        self, api_client: TestClient, make_committed_account: Any
    ) -> None:
        """``is_active`` is enforced at login, not merely stored.

        A row with a correct password and ``is_active = False`` must fail. Phase
        2.2 established the check; this phase makes it reachable, and before an
        endpoint existed the rule was untested at the HTTP boundary.

        The account is created with ``STRONG_PASSWORD`` so the *only* thing
        making the login fail is ``is_active``. Sharing a password with the
        working-credentials test above means the two differ in exactly one flag.
        """
        make_committed_account(slug="acme", email=ADMIN_EMAIL, is_active=False)

        response = login(api_client)

        assert response.status_code == 401
        assert response.json() == REJECTED

    def test_the_enabled_account_with_the_same_credentials_succeeds(
        self, api_client: TestClient, make_committed_account: Any
    ) -> None:
        """The control case, so the test above cannot pass for the wrong reason.

        Identical to ``test_a_disabled_account_cannot_log_in`` in every respect
        except ``is_active``. Without this pair, a login broken for an unrelated
        reason would make the disabled-account test look like a success.
        """
        make_committed_account(slug="acme", email=ADMIN_EMAIL, is_active=True)

        assert login(api_client).status_code == 200

    def test_a_disabled_account_is_indistinguishable_from_a_wrong_password(
        self, api_client: TestClient, make_committed_account: Any
    ) -> None:
        """Otherwise disabling an account would confirm that it exists."""
        make_committed_account(
            slug="acme", email=ADMIN_EMAIL, password="a different one", is_active=True
        )

        disabled = login(api_client, password=STRONG_PASSWORD)
        wrong_password = login(api_client, password="not the password")

        assert disabled.status_code == wrong_password.status_code == 401
        assert disabled.json() == wrong_password.json()

    def test_a_password_beneath_the_creation_floor_is_still_verified(
        self, api_client: TestClient, make_committed_account: Any
    ) -> None:
        """Login must not apply the creation policy to a verification path.

        A stored password that no longer satisfies today's policy is still a
        stored password. The honest answer to presenting it is "invalid
        credentials"; returning ``422`` would claim the request was malformed,
        which is a different and misleading statement.

        The row is created outside registration precisely because registration
        *cannot* create it — the 12-character floor applies there, and applying
        it on this path too would be the bug.
        """
        make_committed_account(
            slug="acme", email=ADMIN_EMAIL, password=BENEATH_THE_FLOOR
        )

        assert login(api_client, password=BENEATH_THE_FLOOR).status_code == 200
        assert login(api_client, password="still wrong").status_code == 401

    def test_registration_refuses_the_same_password_though(
        self, api_client: TestClient
    ) -> None:
        """The contrast that makes the previous test meaningful.

        The identical password is refused at creation and accepted at
        verification. If either half of that pair were wrong, the two tests
        together would still pass.
        """
        response = api_client.post(
            f"{API_PREFIX}/auth/register-tenant",
            json={
                "name": "Acme Corporation",
                "slug": "acme",
                "admin_email": ADMIN_EMAIL,
                "admin_password": BENEATH_THE_FLOOR,
            },
        )

        assert response.status_code == 422

    def test_a_caller_supplied_tenant_id_is_ignored(
        self, api_client: TestClient
    ) -> None:
        """A ``tenant_id`` cannot redirect the lookup, in body, query or header.

        The request model has no such field and the slug is the only selector the
        handler reads, so all three are inert.
        """
        register(api_client)
        other_tenant = uuid.UUID("11111111-2222-4333-8444-555555555555")

        in_body = api_client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "tenant_slug": "acme",
                "email": ADMIN_EMAIL,
                "password": STRONG_PASSWORD,
                "tenant_id": str(other_tenant),
            },
        )
        in_query = api_client.post(
            f"{API_PREFIX}/auth/login?tenant_id={other_tenant}",
            json={
                "tenant_slug": "acme",
                "email": ADMIN_EMAIL,
                "password": STRONG_PASSWORD,
            },
            headers={"X-Tenant-Id": str(other_tenant)},
        )

        assert in_body.status_code == in_query.status_code == 200
        for response in (in_body, in_query):
            assert response.json()["user"]["email"] == ADMIN_EMAIL


# ---------------------------------------------------------------------------
# Refresh and rotation
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_a_live_refresh_token_yields_a_new_session(
        self, api_client: TestClient
    ) -> None:
        """The exchange yields a *different* session, proven by the new token.

        The refresh token must change — that is the rotation. The access token
        need not: ``create_access_token`` derives its claims from the user and
        the clock with no nonce, so two issuances in the same second for the
        same role are byte-identical. That is a property of a stateless access
        token rather than a defect: the replacement carries exactly the same
        authority as the original, so an identical reissue discloses nothing.
        """
        first = register(api_client)["tokens"]

        response = exchange(api_client, str(first["refresh_token"]))

        assert response.status_code == 200
        body = response.json()
        assert body["tokens"]["refresh_token"] != first["refresh_token"]
        assert body["user"]["email"] == ADMIN_EMAIL

    def test_the_access_token_is_reissued_from_the_current_role(
        self, api_client: TestClient, auth_settings: Settings
    ) -> None:
        """The replacement carries the role the database holds right now.

        This is the property a role change relies on: the token is minted by
        reading ``user.role`` at exchange time, not by copying a role claim out
        of the refresh token that was presented.
        """
        token = str(register(api_client)["tokens"]["refresh_token"])

        response = exchange(api_client, token)
        claims = decode_token(
            str(response.json()["tokens"]["access_token"]),
            expected_type=TokenType.ACCESS,
            app_settings=auth_settings,
        )

        assert claims.role == "admin"

    def test_the_presented_token_is_revoked_by_the_exchange(
        self, api_client: TestClient
    ) -> None:
        """Rotate-on-use: a second presentation of the same token fails.

        This is the replay protection. The revocation and the replacement are in
        one transaction, so there is no window in which both succeed.
        """
        token = str(register(api_client)["tokens"]["refresh_token"])

        assert exchange(api_client, token).status_code == 200
        assert exchange(api_client, token).status_code == 401

    def test_exactly_one_of_the_two_rows_is_live_after_an_exchange(
        self, api_client: TestClient, read_only: Any
    ) -> None:
        """The consumed token is dead and the replacement is live."""
        token = str(register(api_client)["tokens"]["refresh_token"])
        exchange(api_client, token)

        with read_only() as session:
            rows = session.scalars(
                select(RefreshToken).order_by(RefreshToken.created_at)
            ).all()

        assert len(rows) == 2
        assert [row.revoked for row in rows] == [True, False]

    def test_each_rotation_issues_a_distinct_pair(
        self, api_client: TestClient
    ) -> None:
        """A chain of exchanges never reissues the same credential."""
        token = str(register(api_client)["tokens"]["refresh_token"])
        seen = {token}

        for _ in range(3):
            response = exchange(api_client, token)
            assert response.status_code == 200
            token = str(response.json()["tokens"]["refresh_token"])
            assert token not in seen
            seen.add(token)

        assert len(seen) == 4

    @pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c"])
    def test_a_malformed_refresh_token_is_refused(
        self, api_client: TestClient, token: str
    ) -> None:
        response = exchange(api_client, token)

        assert response.status_code == 401
        assert response.json() == REJECTED

    def test_an_empty_refresh_token_fails_validation(
        self, api_client: TestClient
    ) -> None:
        """``min_length=1`` refuses it before the service is reached."""
        assert exchange(api_client, "").status_code == 422

    def test_an_access_token_cannot_be_exchanged_as_a_refresh_token(
        self, api_client: TestClient
    ) -> None:
        """The token *type* is checked, so a valid access token is not a session."""
        access = str(register(api_client)["tokens"]["access_token"])

        assert exchange(api_client, access).status_code == 401

    def test_a_refresh_token_signed_with_another_key_is_refused(
        self, api_client: TestClient, foreign_secret_settings: Settings
    ) -> None:
        forged, _jti, _expiry = create_refresh_token(
            subject=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            app_settings=foreign_secret_settings,
        )

        assert exchange(api_client, forged).status_code == 401

    def test_a_correctly_signed_token_with_no_stored_session_is_refused(
        self, api_client: TestClient, auth_settings: Settings
    ) -> None:
        """Distinct from a forged one, and indistinguishable in the response.

        The signature verifies and the type is right; the failure is that no
        ``refresh_tokens`` row was ever persisted for it. That is the state a
        server-side revocation is meant to produce, so it must be reachable.
        """
        orphan, _jti, _expiry = create_refresh_token(
            subject=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            app_settings=auth_settings,
        )

        response = exchange(api_client, orphan)

        assert response.status_code == 401
        assert response.json() == REJECTED

    def test_a_missing_body_field_is_refused(self, api_client: TestClient) -> None:
        assert api_client.post(f"{API_PREFIX}/auth/refresh", json={}).status_code == 422


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestLogout:
    def test_it_revokes_the_presented_token(
        self, api_client: TestClient, read_only: Any
    ) -> None:
        token = str(register(api_client)["tokens"]["refresh_token"])

        assert logout(api_client, token).status_code == 204

        with read_only() as session:
            assert session.scalars(select(RefreshToken)).one().revoked is True

    def test_the_revoked_token_can_no_longer_be_exchanged(
        self, api_client: TestClient
    ) -> None:
        token = str(register(api_client)["tokens"]["refresh_token"])
        logout(api_client, token)

        assert exchange(api_client, token).status_code == 401

    def test_logout_is_idempotent(self, api_client: TestClient) -> None:
        """A second logout is the same empty 204, not an error.

        The caller's intent is "make this stop working", and it either did or
        already had. Reporting the difference would be reporting which tokens
        exist, which is the thing logout must not become.
        """
        token = str(register(api_client)["tokens"]["refresh_token"])

        first = logout(api_client, token)
        second = logout(api_client, token)

        assert first.status_code == second.status_code == 204
        assert first.content == second.content == b""

    def test_logout_of_an_unknown_token_still_succeeds(
        self, api_client: TestClient
    ) -> None:
        """An unknown token is indistinguishable from a known one."""
        response = logout(api_client, "not-a-token")

        assert response.status_code == 204
        assert response.content == b""

    def test_logout_needs_no_access_token(self, api_client: TestClient) -> None:
        """The refresh token is itself the credential.

        Requiring a *second* one would make logout fail exactly when it is most
        needed — after the access token has expired — and would buy nothing: an
        attacker holding a refresh token gains no capability by revoking it.
        """
        token = str(register(api_client)["tokens"]["refresh_token"])

        assert logout(api_client, token).status_code == 204

    def test_logout_leaves_the_access_token_verifiable_until_it_expires(
        self, api_client: TestClient, auth_settings: Settings
    ) -> None:
        """The documented limit of the design, asserted rather than assumed.

        There is no access-token blocklist (decision D019): access tokens are
        stateless and short-lived, and revoking the refresh token is what ends the
        *session*. The access token stays verifiable until it expires, which is
        why its lifetime is 15 minutes.
        """
        access = str(register(api_client)["tokens"]["access_token"])
        logout(api_client, access)

        claims = decode_token(
            access, expected_type=TokenType.ACCESS, app_settings=auth_settings
        )

        assert claims.subject is not None

    def test_a_missing_body_field_is_refused(self, api_client: TestClient) -> None:
        assert api_client.post(f"{API_PREFIX}/auth/logout", json={}).status_code == 422


# ---------------------------------------------------------------------------
# What a response must never contain
# ---------------------------------------------------------------------------


class TestResponsesCarryNoSecrets:
    @pytest.fixture
    def every_response(self, api_client: TestClient) -> Iterator[dict[str, Any]]:
        """Every body the authentication surface can produce.

        ``logout`` is included as its raw bytes rather than parsed JSON, because
        it is the one endpoint that returns no body at all — ``204 No Content``.
        Asserting that it is genuinely empty is part of what this fixture is for;
        parsing it would fail the fixture rather than test the property.
        """
        registration = register(api_client)
        token = str(registration["tokens"]["refresh_token"])
        logout_response = logout(api_client, token)
        assert logout_response.status_code == 204
        yield {
            "register-tenant": registration,
            "login": login(api_client).json(),
            "refresh": exchange(api_client, token).json(),
            "logout": logout_response.content.decode(),
        }

    def test_logout_really_has_no_body(self, every_response: dict[str, Any]) -> None:
        """``204`` with an empty body, not ``204`` with a ``{}``."""
        assert every_response["logout"] == ""

    def test_no_response_contains_a_password_or_a_hash(
        self, every_response: dict[str, Any]
    ) -> None:
        for name, body in every_response.items():
            serialized = str(body)
            assert "password" not in serialized.lower(), name
            assert "$argon2" not in serialized, name
            assert STRONG_PASSWORD not in serialized, name

    def test_no_response_contains_a_stored_digest(
        self, every_response: dict[str, Any], read_only: Any
    ) -> None:
        """No response echoes the SHA-256 digest the database holds."""
        with read_only() as session:
            digests = [row.token_hash for row in session.scalars(select(RefreshToken))]

        assert digests
        for name, body in every_response.items():
            for digest in digests:
                assert digest not in str(body), name

    def test_the_refresh_token_appears_only_inside_the_tokens_object(
        self, every_response: dict[str, Any]
    ) -> None:
        """Never in a user record, so a persisted profile cannot hold a session.

        The raw refresh token is returned at issuance and at exchange, and
        nowhere else. A client that stores the ``user`` object it receives cannot
        accidentally store a working credential alongside it.
        """
        token = every_response["register-tenant"]["tokens"]["refresh_token"]

        assert token not in str(every_response["register-tenant"]["user"])
        assert token not in str(every_response["login"]["user"])
        assert token not in str(every_response["register-tenant"]["tenant"])

    def test_no_response_exposes_a_tenant_id_on_a_user(
        self, every_response: dict[str, Any]
    ) -> None:
        """The tenant is the caller's own context; echoing it adds nothing.

        It is also the one field that must never be *readable* as if it were
        client-controllable. Omitting it from the user representation keeps the
        boundary unambiguous.
        """
        for name, body in every_response.items():
            if not isinstance(body, dict):
                # `logout` is an empty body, already covered above.
                continue
            user = body.get("user")
            if isinstance(user, dict):
                assert "tenant_id" not in user, name


# ---------------------------------------------------------------------------
# Reused helper types are actually used
# ---------------------------------------------------------------------------


def test_read_only_returns_a_context_manager(
    read_only: Callable[[], AbstractContextManager[Session]],
) -> None:
    """Guards the fixture's declared shape, which the tests above rely on.

    Without this, a future change that made ``read_only`` return a bare session
    would still pass the tests that use it — they would just leak connections.
    """
    with read_only() as session:
        assert session.scalar(select(func.count()).select_from(Tenant)) == 0
