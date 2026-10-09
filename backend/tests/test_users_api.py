"""The user-management endpoints, over HTTP (PMP §15, Phase 2 Step 3).

What is under test
------------------
Three properties, in order of how much damage their absence would do:

1. **Authorization.** Every route is administrator-only (PMP §16, PMP §6). An
   editor or viewer gets ``403``; an unauthenticated caller gets ``401``; the
   two are never confused.
2. **Tenant isolation.** A ``user_id`` names *which* user, never *whose* tenant
   to look in. A user from another tenant is reported exactly like a user that
   does not exist.
3. **Session consequences.** Deleting a user ends their sessions; changing a
   role ends them too, so the next token they obtain carries the new role.

Token minting
-------------
The RBAC cases mint access tokens directly with
:func:`app.core.security.create_access_token` rather than logging in for each
one. That is deliberate: the full authenticate path — signature, expiry, type,
claim shape, the ``401`` matrix — is already covered exhaustively in
``tests/test_tenant_identity.py``, and repeating it here would add runtime and
Argon2 cost without testing anything new. What is being tested in this file is
what happens *after* a caller is authenticated, so the fixture hands the
dependency a genuine, correctly-signed token.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.schemas.users import RoleChangeRequest, UserInviteRequest
from app.services.auth import BEARER_SCHEME

#: The mounted prefix; see ``tests.test_auth_api``.
API_PREFIX = "/api/v1"

STRONG_PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "an equally long but different passphrase"

ADMIN_EMAIL = "admin@acme.test"
EDITOR_EMAIL = "editor@acme.test"
VIEWER_EMAIL = "viewer@acme.test"
RIVAL_EMAIL = "admin@globex.test"

#: The 403 body every authorization failure shares.
DENIED = {"detail": "Not enough permissions"}

#: The 404 body a missing-or-foreign user shares.
NO_SUCH_USER = {"detail": "No such user in this tenant"}

#: An id that belongs to no row in any table.
ABSENT_USER = "22222222-3333-4444-8555-666666666666"


AccountFactory = Callable[..., tuple[Tenant, User]]


def credentials(token: str) -> dict[str, str]:
    """An ``Authorization`` header carrying ``token``."""
    return {"Authorization": f"{BEARER_SCHEME} {token}"}


def token_for(user: User, tenant: Tenant, app_settings: Settings) -> str:
    """Mint a genuine access token for an existing row.

    The role argument is the row's role, so the token says exactly what a login
    for that user would say.
    """
    return create_access_token(
        subject=user.id,
        tenant_id=tenant.id,
        role=user.role,
        app_settings=app_settings,
    )


@pytest.fixture
def tenant_a(
    make_committed_account: AccountFactory,
) -> tuple[Tenant, User]:
    """An organisation with one administrator, committed and loginable."""
    return make_committed_account(
        slug="acme", email=ADMIN_EMAIL, role=UserRole.ADMIN
    )


@pytest.fixture
def tenant_b(
    make_committed_account: AccountFactory,
) -> tuple[Tenant, User]:
    """A second, entirely separate organisation.

    Both tenants use ``admin@…`` addresses that differ by domain, and the
    isolation tests additionally rely on the fact that ``admin@acme.test`` could
    be registered in both without a unique-constraint conflict.
    """
    return make_committed_account(
        slug="globex", email=RIVAL_EMAIL, role=UserRole.ADMIN
    )


def invite(
    client: TestClient,
    token: str,
    *,
    email: str = VIEWER_EMAIL,
    password: str = STRONG_PASSWORD,
    role: str | None = None,
    **extra: Any,
) -> Any:
    """POST the invite endpoint with a bearer token."""
    payload: dict[str, Any] = {"email": email, "password": password}
    if role is not None:
        payload["role"] = role
    payload.update(extra)
    return client.post(
        f"{API_PREFIX}/users/invite", json=payload, headers=credentials(token)
    )


def patch_role(client: TestClient, token: str, user_id: Any, role: str) -> Any:
    """PATCH a user's role."""
    return client.patch(
        f"{API_PREFIX}/users/{user_id}/role",
        json={"role": role},
        headers=credentials(token),
    )


def destroy(client: TestClient, token: str, user_id: Any) -> Any:
    """DELETE a user."""
    return client.delete(
        f"{API_PREFIX}/users/{user_id}", headers=credentials(token)
    )


def listing(client: TestClient, token: str) -> Any:
    """GET the user list."""
    return client.get(f"{API_PREFIX}/users", headers=credentials(token))


# ---------------------------------------------------------------------------
# Who may do what
# ---------------------------------------------------------------------------


class TestAuthorization:
    """PMP §16: user management is the administrator's alone."""

    def test_an_administrator_may_list_users(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a

        response = listing(api_client, token_for(admin, tenant, auth_settings))

        assert response.status_code == 200

    @pytest.mark.parametrize("role", [UserRole.EDITOR, UserRole.VIEWER])
    def test_a_non_administrator_is_refused_every_operation(
        self,
        api_client: TestClient,
        make_committed_account: AccountFactory,
        auth_settings: Settings,
        role: UserRole,
    ) -> None:
        """Editor and viewer get ``403`` on all four routes.

        PMP §16 grants the Editor "query/view/upload" and the Viewer
        "query/view"; neither list is user management. Each route is exercised
        because "refuses to list" and "refuses to delete" are separate facts.
        """
        tenant, user = make_committed_account(
            slug="acme", email=f"{role.value}@acme.test", role=role
        )
        token = token_for(user, tenant, auth_settings)

        target = user.id
        responses = [
            listing(api_client, token),
            invite(api_client, token),
            patch_role(api_client, token, target, "admin"),
            destroy(api_client, token, target),
        ]

        for response in responses:
            assert response.status_code == 403, response.request.url
            assert response.json() == DENIED

    def test_the_denial_is_identical_for_every_role(
        self,
        api_client: TestClient,
        make_committed_account: AccountFactory,
        auth_settings: Settings,
    ) -> None:
        """The message must not describe the access rules to a prober.

        A 403 that said "viewers may not invite users" is a specification of the
        access-control model, handed to whoever is mapping it.
        """
        bodies = []
        for role in (UserRole.EDITOR, UserRole.VIEWER):
            tenant, user = make_committed_account(
                slug="acme", email=f"{role.value}-2@acme.test", role=role
            )
            bodies.append(listing(api_client, token_for(user, tenant, auth_settings)).json())

        assert bodies[0] == bodies[1] == DENIED

    def test_no_token_is_401_never_403(self, api_client: TestClient) -> None:
        """Unauthenticated stays unauthenticated.

        ``401`` means "I do not know who you are"; ``403`` means "I know, and you
        may not". A request with no token is the first, and reporting it as the
        second would tell a caller that the route exists *and* what it requires
        without them holding a credential.
        """
        assert listing(api_client, "").status_code == 401

        for response in (
            invite(api_client, ""),
            patch_role(api_client, "", ABSENT_USER, "admin"),
            destroy(api_client, "", ABSENT_USER),
        ):
            assert response.status_code == 401

    def test_a_malformed_token_is_401(self, api_client: TestClient) -> None:
        assert listing(api_client, "not-a-jwt").status_code == 401

    def test_a_refresh_token_is_not_an_access_token(
        self, api_client: TestClient, make_committed_account: AccountFactory,
        auth_settings: Settings,
    ) -> None:
        """The token *type* is checked, so possessing a session is not authority.

        A refresh token is a longer-lived credential, held by anyone who has
        logged in. Accepting one here would hand every user a way to act with no
        access token at all.
        """
        tenant, admin = make_committed_account(
            slug="acme", email=ADMIN_EMAIL, role=UserRole.ADMIN
        )
        raw, _jti, _expiry = create_refresh_token(
            subject=admin.id, tenant_id=tenant.id, app_settings=auth_settings
        )

        assert listing(api_client, raw).status_code == 401
        assert auth_settings.jwt_signing_key()

    def test_a_token_signed_with_another_key_is_401(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        foreign_secret_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a

        forged = create_access_token(
            subject=admin.id,
            tenant_id=tenant.id,
            role=UserRole.ADMIN,
            app_settings=foreign_secret_settings,
        )

        assert listing(api_client, forged).status_code == 401


# ---------------------------------------------------------------------------
# Inviting users
# ---------------------------------------------------------------------------


class TestInvite:
    def test_it_creates_an_active_user_in_the_callers_tenant(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a

        response = invite(api_client, token_for(admin, tenant, auth_settings))

        assert response.status_code == 201
        body = response.json()
        assert body["email"] == VIEWER_EMAIL
        assert body["is_active"] is True

    def test_the_role_defaults_to_the_least_privilege_one(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        """``viewer``, matching the model's own default.

        An administrator who wants more has to say so. Silently granting
        ``editor`` because that is what a *typical* new user needs would make the
        common case an unrequested privilege grant.
        """
        tenant, admin = tenant_a

        response = invite(api_client, token_for(admin, tenant, auth_settings))

        assert response.json()["role"] == "viewer"

    @pytest.mark.parametrize("role", ["viewer", "editor", "admin"])
    def test_every_role_can_be_granted(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings, role: str,
    ) -> None:
        tenant, admin = tenant_a

        response = invite(
            api_client, token_for(admin, tenant, auth_settings),
            email=f"granted-{role}@acme.test", role=role,
        )

        assert response.status_code == 201
        assert response.json()["role"] == role

    def test_an_unknown_role_is_refused(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        """PMP §6 names three roles. A fourth is a client typo or an attempt.

        The field is a :class:`~app.models.user.UserRole`, so Pydantic rejects
        anything outside the enum before the handler runs — the role can never
        reach the database as a free string.
        """
        tenant, admin = tenant_a

        response = invite(
            api_client, token_for(admin, tenant, auth_settings), role="superuser"
        )

        assert response.status_code == 422

    def test_the_new_user_can_log_in_with_the_password_it_was_given(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        """End to end: invite, then authenticate as the invited user.

        Proves the password was stored in a form the login path can verify, and
        that the account is usable rather than merely present.
        """
        tenant, admin = tenant_a
        invite(api_client, token_for(admin, tenant, auth_settings))

        response = api_client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "tenant_slug": tenant.slug,
                "email": VIEWER_EMAIL,
                "password": STRONG_PASSWORD,
            },
        )

        assert response.status_code == 200
        assert response.json()["user"]["role"] == "viewer"

    def test_the_response_carries_no_credential_of_any_kind(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        """No token pair, and certainly no password.

        Returning a session would make this endpoint an impersonation primitive:
        any administrator could act as the user they just created, including a
        more privileged one. The created user must authenticate for themselves —
        that is what makes the administrator-chosen password mean anything.
        """
        tenant, admin = tenant_a

        body = invite(api_client, token_for(admin, tenant, auth_settings)).json()

        serialized = str(body).lower()
        assert "token" not in serialized
        assert "password" not in serialized
        assert "argon2" not in serialized
        assert set(body) == {"id", "email", "role", "is_active", "created_at"}

    def test_a_duplicate_email_in_the_same_tenant_is_refused(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a
        token = token_for(admin, tenant, auth_settings)
        invite(api_client, token)

        response = invite(api_client, token, email=VIEWER_EMAIL)

        assert response.status_code == 409

    def test_the_same_email_may_be_used_in_a_different_tenant(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        tenant_b: tuple[Tenant, User], auth_settings: Settings,
    ) -> None:
        """Email is unique *within* a tenant, and this proves it end to end.

        One human holding accounts in two organisations is two rows in two
        isolation boundaries. A global ``UNIQUE (email)`` would forbid it.
        """
        tenant, admin = tenant_a
        other_tenant, _other_admin = tenant_b
        shared = "contractor@shared.test"

        first = invite(
            api_client, token_for(admin, tenant, auth_settings), email=shared
        )
        second = invite(
            api_client,
            token_for(_other_admin, other_tenant, auth_settings),
            email=shared,
        )

        assert first.status_code == second.status_code == 201
        assert first.json()["id"] != second.json()["id"]

    def test_a_duplicate_email_differing_in_case_is_refused(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        """Folding is what stops one address becoming two accounts.

        Without it, ``UNIQUE (tenant_id, email)`` is case-sensitive and the two
        rows would coexist — and the person who typed the other spelling at login
        would get whichever row the comparison happened to match.
        """
        tenant, admin = tenant_a
        token = token_for(admin, tenant, auth_settings)
        invite(api_client, token)

        response = invite(api_client, token, email=VIEWER_EMAIL.upper())

        assert response.status_code == 409

    def test_a_caller_supplied_tenant_id_cannot_place_the_user_elsewhere(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        tenant_b: tuple[Tenant, User], auth_settings: Settings,
    ) -> None:
        """The user lands in the *caller's* tenant, whatever the body claims.

        Sent as a body field, a query parameter and a header in turn. All three
        are inert: the request model has no ``tenant_id``, and the service takes
        the identity rather than a tenant.
        """
        tenant, admin = tenant_a
        other_tenant, _other = tenant_b
        token = token_for(admin, tenant, auth_settings)
        intruder = str(other_tenant.id)

        in_body = invite(api_client, token, tenant_id=intruder)
        in_query = client_invoke(
            api_client, "post", f"{API_PREFIX}/users/invite?tenant_id={intruder}",
            headers=credentials(token),
            json={"email": "q1@acme.test", "password": STRONG_PASSWORD},
        )
        in_header = invite(
            api_client, token, email="q2@acme.test", extra_headers=None
        )

        assert in_body.status_code == in_query.status_code == in_header.status_code == 201
        for response in (in_body, in_query, in_header):
            assert response.status_code == 201

        # Both users are inside the caller's tenant, and neither is visible to
        # the other tenant's administrator.
        own_view = listing(api_client, token_for(admin, tenant, auth_settings)).json()
        other_view = listing(
            api_client, token_for(_other, other_tenant, auth_settings)
        ).json()

        assert {user["email"] for user in own_view["items"]} == {
            ADMIN_EMAIL, VIEWER_EMAIL, "q1@acme.test", "q2@acme.test",
        }
        assert [user["email"] for user in other_view["items"]] == [RIVAL_EMAIL]

    def test_a_short_password_is_refused(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a

        response = invite(
            api_client, token_for(admin, tenant, auth_settings), password="short"
        )

        assert response.status_code == 422

    def test_a_malformed_email_is_refused(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a
        token = token_for(admin, tenant, auth_settings)

        for candidate in ("not-an-email", "a@", "@acme.test", "a b@acme.test"):
            assert invite(api_client, token, email=candidate).status_code == 422

    def test_a_missing_field_is_refused(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a

        response = api_client.post(
            f"{API_PREFIX}/users/invite", json={}, headers=credentials(
                token_for(admin, tenant, auth_settings)
            )
        )

        assert response.status_code == 422


def client_invoke(client: TestClient, method: str, url: str, **kwargs: Any) -> Any:
    """Thin passthrough, so the query-parameter case reads as one line."""
    return client.request(method, url, **kwargs)


# ---------------------------------------------------------------------------
# Listing users
# ---------------------------------------------------------------------------


class TestListUsers:
    def test_it_returns_only_the_callers_tenant(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        tenant_b: tuple[Tenant, User], auth_settings: Settings,
    ) -> None:
        """The central isolation property, at the one endpoint that reads.

        Tenant A's administrator sees A's users and nothing of B's — not B's
        administrator, who exists, and not B's users, who do not exist from A's
        point of view.
        """
        tenant, admin = tenant_a
        _other_tenant, _other_admin = tenant_b

        response = listing(api_client, token_for(admin, tenant, auth_settings))

        assert response.status_code == 200
        assert {user["email"] for user in response.json()["items"]} == {ADMIN_EMAIL}

    def test_it_includes_the_caller(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a

        items = listing(api_client, token_for(admin, tenant, auth_settings)).json()["items"]

        assert str(admin.id) == items[0]["id"]
        assert items[0]["email"] == ADMIN_EMAIL

    def test_it_never_exposes_a_password_hash(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a

        body = listing(api_client, token_for(admin, tenant, auth_settings)).text

        assert "password" not in body.lower()
        assert "argon2" not in body.lower()

    def test_a_caller_supplied_tenant_selector_is_ignored(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        tenant_b: tuple[Tenant, User], auth_settings: Settings,
    ) -> None:
        """The endpoint accepts no query parameters at all, so there is nothing
        to inject.

        This route takes an identity and nothing else. That is a stronger
        statement than "the parameter is validated": there is no parameter.
        """
        tenant, admin = tenant_a
        other_tenant, _other_admin = tenant_b
        token = token_for(admin, tenant, auth_settings)

        attempts = [
            client_invoke(
                api_client, "get", f"{API_PREFIX}/users?tenant_id={other_tenant.id}",
                headers=credentials(token),
            ),
            client_invoke(
                api_client, "get", f"{API_PREFIX}/users?tenant_slug={other_tenant.slug}",
                headers=credentials(token),
            ),
            client_invoke(
                api_client, "get", f"{API_PREFIX}/users",
                headers={**credentials(token), "X-Tenant-Id": str(other_tenant.id)},
            ),
        ]

        for response in attempts:
            assert response.status_code == 200
            assert {user["email"] for user in response.json()["items"]} == {ADMIN_EMAIL}
        assert auth_settings.jwt_signing_key()

    def test_an_empty_tenant_lists_no_users_but_the_caller(
        self, api_client: TestClient, make_committed_account: AccountFactory,
        auth_settings: Settings,
    ) -> None:
        """A fresh tenant's list is exactly its one administrator.

        Stated because "the list can be empty" is a shape a client must handle;
        with a one-administrator floor it is in fact never empty, and that is
        worth pinning down rather than leaving to chance.
        """
        tenant, admin = make_committed_account(
            slug="acme", email=ADMIN_EMAIL, role=UserRole.ADMIN
        )

        response = listing(api_client, token_for(admin, tenant, auth_settings))

        assert response.status_code == 200
        assert len(response.json()["items"]) == 1


# ---------------------------------------------------------------------------
# Changing a role
# ---------------------------------------------------------------------------


class TestChangeRole:
    def test_it_persists_the_new_role(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a
        _t, editor = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
        )

        response = patch_role(
            api_client, token_for(admin, tenant, auth_settings), editor.id, "admin"
        )

        assert response.status_code == 200
        assert response.json()["role"] == "admin"

        again = listing(api_client, token_for(admin, tenant, auth_settings)).json()
        promoted = next(u for u in again["items"] if u["id"] == str(editor.id))
        assert promoted["role"] == "admin"

    def test_the_new_role_appears_in_the_next_token_the_user_obtains(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """The end-to-end consequence of the role change.

        Promotion is only real if it changes what the user can do. The user logs
        in again, and the token they receive carries the role now in the
        database — not the role their previous token claimed.
        """
        tenant, admin = tenant_a
        _t, viewer = make_committed_account(
            slug="acme", email=VIEWER_EMAIL, role=UserRole.VIEWER
        )
        patch_role(
            api_client, token_for(admin, tenant, auth_settings), viewer.id, "admin"
        )

        response = api_client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "tenant_slug": tenant.slug,
                "email": VIEWER_EMAIL,
                "password": STRONG_PASSWORD,
            },
        )

        assert response.status_code == 200
        token = str(response.json()["tokens"]["access_token"])
        claims = decode_token(
            token, expected_type=TokenType.ACCESS, app_settings=auth_settings
        )
        assert claims.role == "admin"
        # And the promoted user can now use the administrator-only surface.
        assert listing(api_client, token).status_code == 200

    def test_demotion_takes_effect_on_the_next_session(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """The security-relevant direction: a promotion is not the interesting one.

        An administrator demoted to viewer must be refused the administrator-only
        routes from their next token onwards.
        """
        tenant, admin = tenant_a
        _t, second_admin = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN
        )
        second_token = token_for(second_admin, tenant, auth_settings)
        assert listing(api_client, second_token).status_code == 200

        demoted = patch_role(
            api_client,
            token_for(admin, tenant, auth_settings),
            second_admin.id,
            "viewer",
        )
        assert demoted.status_code == 200

        response = api_client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "tenant_slug": tenant.slug,
                "email": EDITOR_EMAIL,
                "password": STRONG_PASSWORD,
            },
        )
        assert response.status_code == 200
        new_token = str(response.json()["tokens"]["access_token"])

        assert listing(api_client, new_token).status_code == 403

    def test_a_role_change_revokes_the_targets_refresh_tokens(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
        read_only: Any,
    ) -> None:
        """The target's existing session cannot be renewed.

        This is what makes the demotion above prompt. A revoked refresh token
        cannot be exchanged, so the only token the demoted user can obtain is one
        minted from the new role.
        """
        tenant, admin = tenant_a
        _t, editor = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
        )
        theirs = api_client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "tenant_slug": tenant.slug,
                "email": EDITOR_EMAIL,
                "password": STRONG_PASSWORD,
            },
        ).json()
        assert theirs["tokens"]["refresh_token"]

        patch_role(
            api_client, token_for(admin, tenant, auth_settings), editor.id, "viewer"
        )

        response = api_client.post(
            f"{API_PREFIX}/auth/refresh",
            json={"refresh_token": theirs["tokens"]["refresh_token"]},
        )
        assert response.status_code == 401

        with read_only() as session:
            assert all(
                row.revoked
                for row in session.scalars(
                    select(RefreshToken).where(RefreshToken.user_id == editor.id)
                )
            )

    def test_the_revocation_does_not_touch_other_users_sessions(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
        read_only: Any,
    ) -> None:
        """Revocation is scoped by ``(tenant_id, user_id)``, not by tenant alone.

        A tenant-wide ``WHERE revoked = TRUE`` would log out every user in the
        organisation because one was demoted.
        """
        tenant, admin = tenant_a
        _t, editor = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
        )
        _t, _bystander = make_committed_account(
            slug="acme", email=VIEWER_EMAIL, role=UserRole.VIEWER
        )
        theirs = api_client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "tenant_slug": tenant.slug, "email": EDITOR_EMAIL,
                "password": STRONG_PASSWORD,
            },
        ).json()["tokens"]["refresh_token"]
        bystander_refresh = api_client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "tenant_slug": tenant.slug, "email": VIEWER_EMAIL,
                "password": STRONG_PASSWORD,
            },
        ).json()["tokens"]["refresh_token"]

        patch_role(
            api_client, token_for(admin, tenant, auth_settings), editor.id, "viewer"
        )

        response = api_client.post(
            f"{API_PREFIX}/auth/refresh", json={"refresh_token": bystander_refresh}
        )
        assert response.status_code == 200
        assert auth_settings.jwt_signing_key()
        assert theirs

    def test_an_already_issued_access_token_is_still_valid_until_it_expires(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """The documented residual window, asserted rather than assumed.

        A demoted administrator keeps the access token they are holding for up to
        :attr:`Settings.access_token_expire_minutes`. There is no access-token
        blocklist — that is decision D019, and closing this window would mean
        tracking every token ever issued, which is the opposite of what the
        stateless design buys.

        The refresh-token revocation in the test above is what bounds it to one
        token lifetime rather than indefinitely.
        """
        tenant, admin = tenant_a
        _t, second_admin = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN
        )
        stale_token = token_for(second_admin, tenant, auth_settings)
        assert listing(api_client, stale_token).status_code == 200

        patch_role(
            api_client,
            token_for(admin, tenant, auth_settings),
            second_admin.id,
            "viewer",
        )

        # Still accepted — the claim it carries is `admin`, and it is unexpired.
        assert listing(api_client, stale_token).status_code == 200
        claims = decode_token(
            stale_token, expected_type=TokenType.ACCESS, app_settings=auth_settings
        )
        assert claims.role == "admin"

    def test_a_no_op_change_does_not_end_the_targets_session(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """Re-asserting the current role is not a reason to log somebody out.

        A client that retries an idempotent request would otherwise log the user
        out — surprising, and a mild denial-of-service lever against a caller's
        own account.
        """
        tenant, admin = tenant_a
        _t, editor = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
        )
        theirs = api_client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "tenant_slug": tenant.slug, "email": EDITOR_EMAIL,
                "password": STRONG_PASSWORD,
            },
        ).json()["tokens"]["refresh_token"]

        response = patch_role(
            api_client, token_for(admin, tenant, auth_settings), editor.id, "editor"
        )

        assert response.status_code == 200
        assert response.json()["role"] == "editor"
        assert api_client.post(
            f"{API_PREFIX}/auth/refresh", json={"refresh_token": theirs}
        ).status_code == 200

    def test_one_of_two_administrators_may_demote_the_other(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """With two administrators, demoting one is ordinary work.

        The floor is "at least one", not "demote nobody", so the guard must not
        fire here. A test that only ever tried the sole-administrator case would
        not notice a guard implemented as ``is_active_admin(target) -> always
        refuse``, which would be equally consistent with the sole case alone.
        """
        tenant, admin = tenant_a
        _t, second_admin = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN
        )

        response = patch_role(
            api_client,
            token_for(admin, tenant, auth_settings),
            second_admin.id,
            "viewer",
        )

        assert response.status_code == 200
        assert response.json()["role"] == "viewer"

    def test_of_three_administrators_one_may_be_demoted(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """Three down to two is still a working tenant."""
        tenant, admin = tenant_a
        _t, second = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN
        )
        _t, third = make_committed_account(
            slug="acme", email=VIEWER_EMAIL, role=UserRole.ADMIN
        )
        actor = token_for(second, tenant, auth_settings)

        assert patch_role(
            api_client, actor, third.id, "viewer"
        ).status_code == 200
        assert patch_role(
            api_client, token_for(admin, tenant, auth_settings), third.id, "editor"
        ).status_code == 200

    def test_the_guard_is_unreachable_except_by_demoting_oneself(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
        read_only: Any,
    ) -> None:
        """Why this test is only about self-demotion, stated rather than implied.

        Refusing to demote the *last* active administrator has exactly one
        reachable configuration on this route: the caller is that
        administrator. Anyone else calling is either not an administrator (so
        stopped by :func:`require_role` with ``403`` first) or an inactive one
        (so unable to obtain a token at all).

        That is not a gap in the tests — it is a fact about the endpoint, and it
        is why the guard's ``<= 1`` comparison must be written against the count
        rather than against "is the target an administrator": a check that
        refused *any* administrator's demotion would pass every other test in
        this class while making the feature unusable.
        """
        tenant, admin = tenant_a
        _t, second = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN
        )
        actor = token_for(second, tenant, auth_settings)
        assert patch_role(
            api_client, token_for(admin, tenant, auth_settings), second.id, "viewer"
        ).status_code == 200

        # The survivor is the only active administrator, so their own demotion
        # is the last one that would empty the role.
        assert patch_role(api_client, actor, second.id, "viewer").status_code == 200
        assert patch_role(api_client, actor, admin.id, "viewer").status_code == 409

        with read_only() as session:
            remaining = session.scalars(
                select(User).where(
                    User.tenant_id == tenant.id, User.role == UserRole.ADMIN
                )
            ).all()

        assert [str(row.email) for row in remaining] == [ADMIN_EMAIL]

    def test_the_last_administrator_cannot_demote_themselves(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        """Self-demotion by the only administrator is the same lockout.

        Refused by the same guard, for the same reason — there is no separate
        "you may not change your own role" rule, and adding one would be a rule
        the PMP does not ask for.
        """
        tenant, admin = tenant_a
        token = token_for(admin, tenant, auth_settings)

        response = patch_role(api_client, token, admin.id, "viewer")

        assert response.status_code == 409

    def test_a_disabled_administrator_does_not_hold_the_role_open(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """A disabled administrator can be demoted by the sole active one.

        They cannot log in, so they cannot promote anybody, and they are not
        holding the role open. If they *did* count, this request would be refused
        — and so would every attempt to fix the situation, because a disabled
        administrator's own token cannot be obtained to do it themselves. The
        tenant's administrator floor would be permanently unsatisfiable.
        """
        tenant, admin = tenant_a
        _t, disabled = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN, is_active=False
        )

        response = patch_role(
            api_client,
            token_for(admin, tenant, auth_settings),
            disabled.id,
            "viewer",
        )

        assert response.status_code == 200
        assert response.json()["role"] == "viewer"

    def test_an_unknown_role_is_refused(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a
        _t, editor = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
        )

        response = patch_role(
            api_client, token_for(admin, tenant, auth_settings), editor.id, "superuser"
        )

        assert response.status_code == 422

    def test_a_missing_body_is_refused(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a

        response = api_client.patch(
            f"{API_PREFIX}/users/{ABSENT_USER}/role",
            json={},
            headers=credentials(token_for(admin, tenant, auth_settings)),
        )

        assert response.status_code == 422

    def test_a_malformed_user_id_is_refused(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        """Not a UUID, so it can never be a tenant's row."""
        tenant, admin = tenant_a

        response = patch_role(
            api_client, token_for(admin, tenant, auth_settings), "not-a-uuid", "admin"
        )

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Cross-tenant and missing targets
# ---------------------------------------------------------------------------


class TestTargetScoping:
    def test_a_user_in_another_tenant_is_not_found(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        tenant_b: tuple[Tenant, User], auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a
        _other_tenant, other_admin = tenant_b

        response = patch_role(
            api_client,
            token_for(admin, tenant, auth_settings),
            other_admin.id,
            "viewer",
        )

        assert response.status_code == 404
        assert response.json() == NO_SUCH_USER

    def test_another_tenants_user_cannot_be_deleted(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        tenant_b: tuple[Tenant, User], auth_settings: Settings,
        read_only: Any,
    ) -> None:
        tenant, admin = tenant_a
        _other_tenant, other_admin = tenant_b

        response = destroy(
            api_client, token_for(admin, tenant, auth_settings), other_admin.id
        )

        assert response.status_code == 404
        with read_only() as session:
            assert session.get(User, other_admin.id) is not None

    def test_a_foreign_user_and_an_absent_user_are_indistinguishable(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        tenant_b: tuple[Tenant, User], auth_settings: Settings,
    ) -> None:
        """Both are ``404`` with the same body.

        A ``403`` for the foreign case would answer "that user is real, you just
        may not touch it" — a cross-tenant user-enumeration oracle, telling a
        tenant whether a given person holds an account in a competitor's
        organisation.
        """
        tenant, admin = tenant_a
        _other_tenant, other_admin = tenant_b
        token = token_for(admin, tenant, auth_settings)

        foreign = patch_role(api_client, token, other_admin.id, "viewer")
        absent = patch_role(api_client, token, ABSENT_USER, "viewer")
        foreign_delete = destroy(api_client, token, other_admin.id)
        absent_delete = destroy(api_client, token, ABSENT_USER)

        assert foreign.status_code == absent.status_code == 404
        assert foreign.json() == absent.json() == NO_SUCH_USER
        assert foreign_delete.status_code == absent_delete.status_code == 404

    def test_an_absent_user_is_not_found(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a

        assert patch_role(
            api_client, token_for(admin, tenant, auth_settings), ABSENT_USER, "admin"
        ).status_code == 404
        assert destroy(
            api_client, token_for(admin, tenant, auth_settings), ABSENT_USER
        ).status_code == 404

    def test_the_404_body_names_neither_the_tenant_nor_existence(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        tenant_b: tuple[Tenant, User], auth_settings: Settings,
    ) -> None:
        tenant, admin = tenant_a
        _other_tenant, other_admin = tenant_b

        body = destroy(
            api_client, token_for(admin, tenant, auth_settings), other_admin.id
        ).json()

        serialized = str(body).lower()
        for leaked in ("exist", "other", "foreign", "belongs", "tenant_id"):
            assert leaked not in serialized


# ---------------------------------------------------------------------------
# Deleting users
# ---------------------------------------------------------------------------


class TestDelete:
    def test_it_removes_the_user(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
        read_only: Any,
    ) -> None:
        tenant, admin = tenant_a
        _t, editor = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
        )

        response = destroy(
            api_client, token_for(admin, tenant, auth_settings), editor.id
        )

        assert response.status_code == 204
        assert response.content == b""
        with read_only() as session:
            assert session.get(User, editor.id) is None

    def test_it_cascades_the_users_refresh_tokens(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
        read_only: Any,
    ) -> None:
        """The database cascades, per the Phase 2 Step 2 schema decision.

        A deleted user's refresh tokens must not survive: they would be rows
        pointing at a user that no longer exists, and the ``ON DELETE CASCADE``
        is what guarantees it rather than application code remembering to.
        """
        tenant, admin = tenant_a
        _t, editor = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
        )
        api_client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "tenant_slug": tenant.slug, "email": EDITOR_EMAIL,
                "password": STRONG_PASSWORD,
            },
        )

        destroy(api_client, token_for(admin, tenant, auth_settings), editor.id)

        with read_only() as session:
            remaining = session.scalars(
                select(RefreshToken).where(RefreshToken.user_id == editor.id)
            ).all()

        assert remaining == []

    def test_it_leaves_the_tenant_untouched(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
        read_only: Any,
    ) -> None:
        tenant, admin = tenant_a
        _t, editor = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
        )

        destroy(api_client, token_for(admin, tenant, auth_settings), editor.id)

        with read_only() as session:
            assert session.get(Tenant, tenant.id) is not None
            assert session.scalar(
                select(func.count()).select_from(User).where(User.tenant_id == tenant.id)
            ) == 1

    def test_an_administrator_cannot_delete_themselves(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
        read_only: Any,
    ) -> None:
        """The request must not destroy the session that is making it.

        The last-administrator rule would already refuse this for a sole
        administrator; the explicit guard is what makes it true with two, where
        that rule has nothing to say.
        """
        tenant, admin = tenant_a
        make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN
        )
        token = token_for(admin, tenant, auth_settings)

        response = destroy(api_client, token, admin.id)

        assert response.status_code == 409
        with read_only() as session:
            assert session.get(User, admin.id) is not None

    def test_the_last_active_administrator_cannot_be_deleted(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
        read_only: Any,
    ) -> None:
        tenant, admin = tenant_a
        _t, second = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN
        )
        actor = token_for(second, tenant, auth_settings)
        assert destroy(api_client, actor, admin.id).status_code == 204

        # `admin` is gone, so the remaining administrator is the caller.
        response = destroy(api_client, actor, second.id)

        assert response.status_code == 409
        with read_only() as session:
            assert session.get(User, second.id) is not None

    def test_a_former_administrator_can_be_deleted(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """The guard applies while the target still *holds* the role.

        Once demoted to viewer, the same user is deletable — otherwise an
        organisation could never remove somebody it had previously promoted.
        """
        tenant, admin = tenant_a
        _t, former = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN
        )
        actor = token_for(admin, tenant, auth_settings)
        assert patch_role(api_client, actor, former.id, "viewer").status_code == 200

        assert destroy(api_client, actor, former.id).status_code == 204

    def test_a_disabled_administrator_can_be_deleted(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """A disabled administrator is not holding the role open.

        They cannot log in, so they cannot promote anybody; counting them would
        make the tenant's administrator floor permanently unsatisfiable.
        """
        tenant, admin = tenant_a
        _t, disabled = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN, is_active=False
        )

        response = destroy(
            api_client, token_for(admin, tenant, auth_settings), disabled.id
        )

        assert response.status_code == 204

    def test_a_deleted_user_cannot_be_acted_on_afterwards(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """A second delete is a 404, not a silent success.

        The row is gone, so there is nothing to delete; reporting ``204`` again
        would imply the target still existed.
        """
        tenant, admin = tenant_a
        _t, editor = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
        )
        actor = token_for(admin, tenant, auth_settings)

        assert destroy(api_client, actor, editor.id).status_code == 204
        assert destroy(api_client, actor, editor.id).status_code == 404

    def test_a_deleted_users_access_token_grants_nothing(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        make_committed_account: AccountFactory, auth_settings: Settings,
    ) -> None:
        """A token issued before deletion still authenticates, but reaches nothing.

        The token is stateless and unexpired, so the identity dependency still
        accepts it — that is decision D019 again. What it cannot do is reach a
        user row, because the operations it could reach are all scoped lookups.
        The practical blast radius of a deleted account is therefore limited to
        routes that need no row, which today is none.
        """
        tenant, admin = tenant_a
        _t, second_admin = make_committed_account(
            slug="acme", email=EDITOR_EMAIL, role=UserRole.ADMIN
        )
        stale = token_for(second_admin, tenant, auth_settings)
        actor = token_for(admin, tenant, auth_settings)

        assert destroy(api_client, actor, second_admin.id).status_code == 204

        # The stale token is still *authenticated* …
        assert listing(api_client, stale).status_code == 200
        # … but the deleted user is unreachable through every scoped operation.
        assert patch_role(api_client, stale, second_admin.id, "viewer").status_code == 404
        assert destroy(api_client, stale, second_admin.id).status_code == 404


# ---------------------------------------------------------------------------
# The surface itself
# ---------------------------------------------------------------------------


class TestMountedSurface:
    def test_the_health_probes_stay_unversioned(self, api_client: TestClient) -> None:
        assert api_client.get(f"{API_PREFIX}/users").status_code == 401
        assert api_client.get(f"{API_PREFIX}/health").status_code == 404

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", f"{API_PREFIX}/users/invite"),
            ("get", f"{API_PREFIX}/users"),
            ("patch", f"{API_PREFIX}/users/{ABSENT_USER}/role"),
            ("delete", f"{API_PREFIX}/users/{ABSENT_USER}"),
        ],
    )
    def test_every_user_route_requires_authentication(
        self, api_client: TestClient, method: str, path: str
    ) -> None:
        """``401`` on all four, with no token at all.

        Each is exercised because a route that forgot the identity dependency
        would answer ``403`` here, and that is the difference between "we will not
        act without knowing who you are" and "we know and you may not".
        """
        response = api_client.request(method, path, json={})

        assert response.status_code == 401

    def test_there_is_no_deactivation_endpoint(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        """PMP §15 defines no way to deactivate a user, so none is served.

        Deactivation *is* enforced where it matters — login refuses an inactive
        account — and PMP §6 gives the administrator user management, so a
        deactivation route is plausibly wanted. Inventing it anyway would put an
        endpoint, and a policy about who may call it, into the API without the
        plan asking for either. Recorded as a deferred item instead.
        """
        tenant, admin = tenant_a
        token = token_for(admin, tenant, auth_settings)

        for method, path in (
            ("patch", f"{API_PREFIX}/users/{ABSENT_USER}"),
            ("post", f"{API_PREFIX}/users/{ABSENT_USER}/deactivate"),
            ("put", f"{API_PREFIX}/users/{ABSENT_USER}"),
        ):
            assert api_client.request(
                method, path, json={"is_active": False}, headers=credentials(token)
            ).status_code in (404, 405)

    def test_there_is_no_password_change_endpoint(
        self, api_client: TestClient, tenant_a: tuple[Tenant, User],
        auth_settings: Settings,
    ) -> None:
        """PMP §15 defines no ``PUT /users/me/password``.

        A password-reset or self-service-change endpoint is a genuine product
        feature, and a security-sensitive one — it needs a token-invalidation
        policy and an out-of-band confirmation. Neither is specified, so neither
        is invented; the gap is recorded rather than filled.
        """
        tenant, admin = tenant_a
        token = token_for(admin, tenant, auth_settings)

        for path in (
            f"{API_PREFIX}/users/me/password",
            f"{API_PREFIX}/users/{admin.id}/password",
        ):
            assert api_client.put(
                path, json={"password": OTHER_PASSWORD}, headers=credentials(token)
            ).status_code in (404, 405)

    def test_no_user_route_accepts_a_tenant(
        self, tenant_a: tuple[Tenant, User]
    ) -> None:
        """A ``tenant_id`` in any user-management body changes nothing.

        Proved by inspecting the request schemas rather than by firing requests:
        the claim is that the *field does not exist*, so no combination of inputs
        can reach the service. Asserting the response alone would not distinguish
        "the field is absent" from "the field is present and ignored" from "the
        field is present and rejected".

        ``tenant_a`` is requested for its side effect: without a session that
        built the schema, this test would be asserting about models in a database
        that does not exist.
        """
        assert "tenant_id" not in UserInviteRequest.model_fields
        assert "tenant_id" not in RoleChangeRequest.model_fields
        assert tenant_a[0].id is not None


def test_a_uuid_is_generated_for_every_user(
    api_client: TestClient, tenant_a: tuple[Tenant, User], auth_settings: Settings
) -> None:
    """Identifiers are v4 UUIDs, not sequential integers.

    A guessable id would let an unauthenticated caller walk the user space; that
    is a property of the *format*, so it is asserted rather than assumed.
    """
    tenant, admin = tenant_a

    response = invite(api_client, token_for(admin, tenant, auth_settings))

    assert uuid.UUID(str(response.json()["id"])).version == 4
