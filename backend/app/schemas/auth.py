"""Request and response schemas for the authentication API (PMP §15, Phase 2.3).

Every model here is unauthenticated except where noted, and none of them
contains a ``tenant_id``. That is the central security property of this
module, and it is stated in one place so a later reader does not have to
rediscover it:

* :class:`LoginRequest` carries a tenant **slug** — untrusted input that only
  chooses which tenant to search, resolved to a real row before any lookup.
  It is never the tenant identity.
* :class:`TenantRegistrationRequest` creates a tenant, so there is no tenant
  identity to trust yet; the tenant's id is generated server-side.
* :class:`RefreshRequest` and :class:`LogoutRequest` carry a refresh token, and
  a refresh token is itself a credential — its ``tenant_id`` claim is only read
  after its signature has been verified.

The endpoint path is ``/auth/register-tenant``, not ``/auth/register``. PMP §15
lists ``POST /auth/register-tenant`` and that is the name used here; a shorter
alias is not added, because two URLs for one operation is a second thing to keep
in sync and a second name in the API documentation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from app.schemas.fields import (
    EmailAddress,
    NewPassword,
    Password,
    TenantName,
    TenantSlug,
)
from app.schemas.users import UserResponse


class TokenPairResponse(BaseModel):
    """An access/refresh pair as returned to the client that owns it.

    Built from :class:`~app.services.auth.IssuedTokens`. The raw refresh token
    appears here and nowhere else in the system: the database stores only its
    SHA-256 digest, so this response is the single moment at which the client
    learns it.

    ``jti`` and ``expires_at`` are omitted even though the service has them.
    Neither is secret — ``jti`` is a readable claim inside the token — but a
    client has no use for them, and a response field is a field that must be
    reasoned about in every future review.
    """

    access_token: str = Field(
        description="Short-lived bearer token. Send as 'Authorization: Bearer <token>'."
    )
    refresh_token: str = Field(
        description=(
            "Single-use token. Exchange it at the refresh endpoint for a new "
            "pair; the presented token is revoked in the same transaction."
        )
    )
    token_type: str = Field(
        description="The HTTP authentication scheme. Always 'Bearer' (RFC 6750)."
    )
    expires_in_seconds: int = Field(
        description="Lifetime of the access token, in seconds."
    )


class TenantResponse(BaseModel):
    """A tenant, as returned to a caller who belongs to it."""

    id: uuid.UUID = Field(description="The tenant's immutable identifier.")
    name: str = Field(description="The tenant's display name.")
    slug: str = Field(description="The tenant's URL slug, in folded lower case.")
    created_at: datetime = Field(description="When the tenant was created.")


class TenantRegistrationRequest(BaseModel):
    """Create a tenant together with its first administrator.

    PMP FR-01 ("The system shall allow an organization to create a tenant
    account") and PMP §15 (``POST /auth/register-tenant``).

    Three things are absent on purpose:

    * ``tenant_id`` — the tenant's identifier is generated server-side. A client
      that could choose its own tenant id could, in a database where it guessed
      or leaked one, collide with or overwrite an existing tenant.
    * ``role`` — the first user of a tenant is always an administrator, because
      PMP FR-03 (admins assign roles) is unreachable otherwise: a tenant whose
      only user is a viewer can never promote anybody. Making the role a request
      field would allow a tenant to be created with no way to ever administer
      it.
    * ``is_active`` — a newly created account is active. There is no flow that
      creates a user who cannot immediately use the credential they were given.
    """

    name: TenantName = Field(description="The organisation's display name.")
    slug: TenantSlug = Field(
        description=(
            "The organisation's URL slug. Folded to lower case; unique across "
            "all tenants."
        )
    )
    admin_email: EmailAddress = Field(
        description="Login address of the tenant's first administrator."
    )
    admin_password: NewPassword = Field(
        description="That administrator's password. At least 12 characters."
    )


class LoginResponse(BaseModel):
    """A successful authentication.

    Carries the identity fields a client needs in order to render "who am I"
    without a second round trip, and no credential material beyond the tokens
    themselves.
    """

    user: UserResponse = Field(description="The authenticated user.")
    tokens: TokenPairResponse = Field(description="The newly issued token pair.")


class TenantRegistrationResponse(BaseModel):
    """A successful tenant registration.

    A token pair is returned so the registering administrator is signed in
    immediately. That is a choice, not a PMP requirement — the PMP specifies no
    response body for this endpoint at all — and it is made because the caller
    has just proven they hold the tenant's only credential, and a separate
    ``/auth/login`` call immediately afterwards would accomplish nothing except
    one more Argon2 verification.

    It does not widen the attack surface: the same tokens are obtainable by
    logging in with the same slug, email and password, so this response reveals
    nothing an attacker could not get by making that second request.
    """

    tenant: TenantResponse = Field(description="The tenant that was created.")
    user: UserResponse = Field(description="Its first administrator.")
    tokens: TokenPairResponse = Field(description="A token pair for that user.")


class LoginRequest(BaseModel):
    """Credentials plus the tenant to search (PMP §15 ``POST /auth/login``).

    ``tenant_slug`` is **not** the tenant identity. It is a selector: an
    untrusted value that :func:`app.services.auth.authenticate` resolves to a
    tenant row and then discards in favour of ``tenant.id`` for every subsequent
    query. The rule this endpoint cannot break is that no caller-supplied
    ``tenant_id`` is ever treated as the authenticated tenant — which is why
    there is no such field here.
    """

    tenant_slug: TenantSlug = Field(
        description="Which tenant to authenticate against. Untrusted selector."
    )
    email: EmailAddress = Field(description="The user's login address.")
    password: Password = Field(
        description=(
            "The user's password. Only a maximum length is enforced here; a "
            "verification path must not apply the creation policy."
        )
    )


class RefreshRequest(BaseModel):
    """Exchange a refresh token for a new pair (PMP §15 ``POST /auth/refresh``)."""

    refresh_token: Annotated[
        str,
        Field(
            min_length=1,
            max_length=4096,
            description=(
                "The refresh token being exchanged. It is revoked in the same "
                "transaction that issues its replacement."
            ),
        ),
    ]


class LogoutRequest(BaseModel):
    """Revoke a refresh token (PMP §15 ``POST /auth/logout``).

    No access token is required, and that asymmetry with every other endpoint is
    deliberate. :func:`app.services.auth.revoke_refresh_token` verifies the
    refresh token's signature first and is idempotent afterwards, so requiring a
    *second* credential would make logout fail exactly when it is most needed —
    after the access token has expired — and would buy nothing: the refresh token
    is already a credential in its own right, and an attacker holding one gains
    no new capability by revoking it.

    Logout returns no body at all (HTTP 204), so there is no response an
    attacker could compare to learn whether a guessed token had ever existed.
    """

    refresh_token: Annotated[
        str,
        Field(
            min_length=1,
            max_length=4096,
            description="The refresh token to revoke. Revocation is idempotent.",
        ),
    ]
