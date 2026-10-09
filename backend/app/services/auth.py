"""Login, refresh and revocation: the service that turns a token into a session.

This is where the two halves meet. :mod:`app.core.security` can prove a token is
genuine but cannot tell you who the user *is*; this module resolves a login
against the database and then delegates every cryptographic decision back to
``security``. It contains no hashing and no signing of its own — that
separation is what keeps one implementation path per concern.

The tenant-selection rule
-------------------------
``users.email`` is unique only within a tenant (PMP §12, Phase 2 Step 1), so
``email + password`` cannot identify a user. :func:`authenticate` therefore
requires a ``tenant_slug``.

The distinction that matters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``tenant_slug`` is **untrusted input used to choose which tenant to search**.
``identity.tenant_id`` and the token's ``tenant_id`` claim are **the trusted
tenant identity**, and they are only ever read from a verified token or from a
row this module looked up itself.

Concretely:

* Allowed — the client names ``tenant_slug`` to select a tenant to authenticate
  against.
* Allowed — after a token is verified, the application reads ``tenant_id`` from
  its claims.
* **Forbidden** — the client supplies ``tenant_id`` and the application treats
  it as the authenticated tenant. No code path here accepts one.

The practical effect: the slug is checked against a real tenant row, and the
user is then looked up *within that tenant's id*. A bad slug and a wrong
password produce the same opaque failure, so this module cannot be used to
enumerate which tenant slugs are registered.

Scope note
----------
This module is the identity foundation only. It does not expose routes, does not
create users, and performs no authorization: returning a valid
:class:`~app.core.identity.AuthenticatedIdentity` says who the caller is, never
what they may do.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.config import settings as process_settings
from app.core.identity import AuthenticatedIdentity
from app.core.security import (
    TokenType,
    TokenValidationError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_refresh_token,
    verify_password,
    verify_refresh_token,
)
from app.models.refresh_token import RefreshToken
from app.models.tenant import Tenant
from app.models.user import User

#: Raised for every failed authentication, whatever the cause.
#:
#: One exception type with one fixed message is the whole point. Callers cannot
#: distinguish "no such tenant" from "no such user" from "wrong password" from
#: "account disabled", so no ``except`` clause here can accidentally be turned
#: into an enumeration oracle. It is an alias of
#: :class:`~app.core.security.TokenValidationError` because from the HTTP layer's
#: point of view a rejected credential and a rejected token are the same event.
InvalidCredentialsError = TokenValidationError

#: The ``token_type`` value returned to clients. A literal rather than a new
#: constant, because RFC 6750 defines exactly one such scheme and this project
#: issues nothing else.
BEARER_SCHEME = "Bearer"


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    """A freshly minted access/refresh pair plus what was persisted.

    ``refresh_token`` is the raw token and exists only in this object. It is
    the sole way the client ever learns it — the database receives
    :func:`~app.core.security.hash_refresh_token`'s digest and never the token.
    """

    access_token: str
    refresh_token: str
    token_type: str
    expires_in_seconds: int
    jti: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """Outcome of a successful token exchange.

    ``rotated`` separates "exchanged an existing refresh token for a new pair"
    from "issued a first pair at login". ``rotated_token_jti`` names the token
    that was consumed and revoked, which is what a client would present again to
    test whether it is still usable.
    """

    user: User
    tenant: Tenant
    tokens: IssuedTokens
    rotated: bool
    rotated_token_jti: str | None = None


def _now() -> datetime:
    """Timezone-aware UTC now; same clock source as :mod:`app.core.security`."""
    return datetime.now(UTC)


def _resolve(app_settings: Settings | None) -> Settings:
    """Return the settings to use, defaulting to the process-wide instance.

    Mirrors :func:`app.core.security._resolve_settings`, and exists so every
    function here has the same optional-settings convention.
    """
    return process_settings if app_settings is None else app_settings


def authenticate(
    session: Session,
    *,
    tenant_slug: str,
    email: str,
    password: str,
    app_settings: Settings | None = None,
) -> tuple[User, Tenant, IssuedTokens]:
    """Resolve a login and issue a token pair.

    Args:
        session: an open database session. The caller owns the transaction.
        tenant_slug: client-supplied tenant selection. **Untrusted**; used only
            to find which tenant row to search within.
        email: client-supplied email, unique only within that tenant.
        password: verified with Argon2 against the located user's hash.
        app_settings: settings override, for tests.

    Returns:
        ``(user, tenant, tokens)``. ``user.tenant_id`` is the tenant that was
        actually matched in the database, never the slug the client sent.

    Raises:
        InvalidCredentialsError: on any failure — unknown slug, unknown user,
            wrong password, or a disabled account. The message is byte-identical
            in every case.
    """
    config = _resolve(app_settings)

    tenant = session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
    if tenant is None:
        raise InvalidCredentialsError("invalid tenant, email or password")

    # Scoped by ``tenant.id``, not by the slug. The slug has already been
    # resolved to an id by this point, so from here on every lookup is an
    # ordinary tenant-scoped query — the same shape a repository will take.
    user = session.scalar(
        select(User).where(
            User.tenant_id == tenant.id,
            User.email == email,
        )
    )
    if user is None:
        raise InvalidCredentialsError("invalid tenant, email or password")

    # The password check comes after the lookup, never before it. Argon2 is
    # deliberately expensive, so verifying against every candidate would make
    # the login endpoint a free CPU-and-memory exhaustion primitive for an
    # unauthenticated caller. Rate limiting is a later step; this ordering is
    # the part that costs nothing to get right now.
    if not verify_password(password, user.password_hash):
        raise InvalidCredentialsError("invalid tenant, email or password")

    # Checked *after* the password so a disabled account is not distinguishable
    # by timing, and reported as the same opaque error so it is not
    # distinguishable by message either.
    if not user.is_active:
        raise InvalidCredentialsError("invalid tenant, email or password")

    tokens = issue_token_pair(
        user=user,
        tenant=tenant,
        session=session,
        app_settings=config,
    )
    return user, tenant, tokens


def issue_token_pair(
    *,
    user: User,
    tenant: Tenant,
    session: Session,
    app_settings: Settings | None = None,
) -> IssuedTokens:
    """Mint an access/refresh pair and persist the refresh token's digest.

    The role written into the access token is read from ``user`` **now**, at
    issue time, rather than copied from any stored token. That is what makes a
    role change take effect on the next login instead of persisting until the
    previous token's expiry.

    The tenant id in both tokens is ``tenant.id``, and ``user.tenant_id`` is
    asserted to equal it. A mismatch means two database rows disagree about who
    owns this user; issuing a token anyway would launder that inconsistency into
    a valid credential.
    """
    config = _resolve(app_settings)

    if user.tenant_id != tenant.id:
        raise ValueError(
            "cannot issue tokens: user.tenant_id does not match the supplied "
            "tenant id"
        )

    access_token = create_access_token(
        subject=user.id,
        tenant_id=tenant.id,
        role=user.role.value,
        app_settings=config,
    )
    refresh_token, jti, expires_at = create_refresh_token(
        subject=user.id,
        tenant_id=tenant.id,
        app_settings=config,
    )

    # Only the digest is persisted. The raw token leaves this function and is
    # never bound to an ORM object.
    session.add(
        RefreshToken(
            tenant_id=tenant.id,
            user_id=user.id,
            jti=jti,
            token_hash=hash_refresh_token(refresh_token),
            expires_at=expires_at,
        )
    )

    return IssuedTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type=BEARER_SCHEME,
        expires_in_seconds=config.access_token_expire_minutes * 60,
        jti=jti,
        expires_at=expires_at,
    )


def exchange_refresh_token(
    session: Session,
    *,
    refresh_token: str,
    app_settings: Settings | None = None,
) -> RefreshResult:
    """Verify a refresh token and exchange it for a fresh pair.

    Revocation is **rotate-on-use**: the presented token is marked revoked in
    the same transaction that mints its replacement, so two presentations of the
    same token cannot both succeed. That is the property which stops a stolen
    refresh token from outliving a single use.

    Checks applied, in order:

    1. Signature, algorithm, expiry, claim shape (``decode_token``).
    2. Claim ``type`` is ``refresh`` — an access token cannot be exchanged.
    3. A ``refresh_tokens`` row exists for the ``jti``.
    4. That row's stored digest matches the presented token, compared in
       constant time.
    5. The row is neither revoked nor past ``expires_at``.
    6. The row's ``tenant_id`` matches the token's ``tenant_id`` claim.
    7. The user still exists, is still active, and still belongs to that tenant.

    Raises:
        InvalidCredentialsError: on any failure. The message is generic by
            design; the specific reason is in the exception chain for logs.
    """
    config = _resolve(app_settings)

    claims = decode_token(
        refresh_token,
        expected_type=TokenType.REFRESH,
        app_settings=config,
    )

    row = session.scalar(select(RefreshToken).where(RefreshToken.jti == claims.jti))
    if row is None:
        raise InvalidCredentialsError("refresh token is not recognised")

    # Comparing digests rather than trusting the jti lookup: a database that
    # was tampered with — a digest copied onto a different row — is caught here
    # instead of being treated as a valid session.
    if not verify_refresh_token(refresh_token, row.token_hash):
        raise InvalidCredentialsError("refresh token is not recognised")

    if row.revoked:
        raise InvalidCredentialsError("refresh token has been revoked")

    if row.is_expired(now=_now()):
        raise InvalidCredentialsError("refresh token has expired")

    if row.tenant_id != claims.tenant_id:
        # A correctly-signed token whose tenant claim disagrees with the row it
        # points at. Trust neither side.
        raise InvalidCredentialsError("refresh token tenant mismatch")

    user = session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise InvalidCredentialsError("refresh token subject is no longer active")

    if user.tenant_id != row.tenant_id:
        raise InvalidCredentialsError("refresh token tenant mismatch")

    tenant = session.get(Tenant, row.tenant_id)
    if tenant is None:
        raise InvalidCredentialsError("refresh token tenant no longer exists")

    row.revoked = True

    tokens = issue_token_pair(
        user=user,
        tenant=tenant,
        session=session,
        app_settings=config,
    )
    return RefreshResult(
        user=user,
        tenant=tenant,
        tokens=tokens,
        rotated=True,
        rotated_token_jti=claims.jti,
    )


def identity_from_access_token(token: str) -> AuthenticatedIdentity:
    """Verify a bearer access token and return the identity it establishes.

    Re-exported here so a caller consuming a token has one obvious entry point
    that cannot accidentally skip verification.
    """
    return AuthenticatedIdentity.from_access_token(token)


def revoke_refresh_token(
    session: Session,
    *,
    refresh_token: str,
    app_settings: Settings | None = None,
) -> None:
    """Revoke a single refresh token. Used by logout.

    Revocation is **idempotent and silent**: revoking an already-revoked or
    unknown token succeeds quietly. That is correct for logout — the caller's
    intent ("make this stop working") is already satisfied — and it avoids
    turning logout into an oracle for which tokens exist.

    The token is still signature-verified first. If it were not, an attacker
    could revoke arbitrary sessions by guessing ``jti`` values, turning a
    post-logout endpoint into a denial-of-service primitive.
    """
    try:
        claims = decode_token(
            refresh_token,
            expected_type=TokenType.REFRESH,
            app_settings=_resolve(app_settings),
        )
    except TokenValidationError:
        # Unknown or forged: there is nothing to revoke, and it was never valid.
        return

    row = session.scalar(select(RefreshToken).where(RefreshToken.jti == claims.jti))
    if row is None:
        return
    row.revoked = True


def revoke_all_refresh_tokens(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> int:
    """Revoke every live refresh token for one user. Returns how many.

    Takes an explicit ``tenant_id`` (PMP §12: "every database repository will
    require a tenant identifier") and filters on it even though ``user_id`` is
    already globally unique. The extra filter is not redundancy for its own
    sake — it means a caller that passes the wrong tenant id fails closed with a
    count of zero instead of revoking some other tenant's sessions.

    This is the "log out everywhere" primitive. Role changes and deactivation
    both need it, which is why it exists even though no route calls it yet.
    """
    rows = session.scalars(
        select(RefreshToken).where(
            RefreshToken.tenant_id == tenant_id,
            RefreshToken.user_id == user_id,
            RefreshToken.revoked.is_(False),
        )
    ).all()
    for row in rows:
        row.revoked = True
    return len(rows)