"""Authentication endpoints (PMP §15, Phase 2.3).

    POST /auth/register-tenant
    POST /auth/login
    POST /auth/refresh
    POST /auth/logout

Mounted by :func:`app.main.create_app` under
:attr:`~app.core.config.Settings.api_v1_prefix`, so the paths above are
relative — the served paths are ``/api/v1/auth/...``. The health probes stay at
the application root for the reason given in :mod:`app.api.health`.

Handlers are ``def``, not ``async def``
--------------------------------------
These endpoints do blocking I/O — synchronous SQLAlchemy sessions, and Argon2,
which is deliberately expensive. FastAPI runs a plain ``def`` handler in a
worker thread and an ``async def`` handler on the event loop, so declaring these
``async def`` would let one login block every other in-flight request in the
process. The synchronous choice is a correctness matter here, not a style one.

This module contains no hashing, no signing and no password logic
----------------------------------------------------------------
Every credential decision is delegated: registration and invitation hash through
:func:`app.core.security.hash_password`, login through
:func:`app.services.auth.authenticate`, and issuance, exchange and revocation
through :mod:`app.services.auth`. Duplicating any of it in the router would
create a second implementation path — the specific failure mode Phase 2.2 was
built to avoid — so the router's job is limited to validating input, choosing a
status code, and shaping a response.

Failure responses carry no information
--------------------------------------
Every authentication failure returns the *same* ``401`` and the same
``detail`` string, reused from :mod:`app.api.deps` rather than spelled again
here. That covers "no such tenant", "no such user", "wrong password", "disabled
account", "unknown token", "revoked token" and "expired token" identically, so
none of them can be used to enumerate accounts. The precise reason is in the
exception's message, which is logged by the service layer and never returned.

Signature order
---------------
``app_settings`` is declared before ``session`` in every handler, and that is not
a preference. ``session`` uses the project's existing ``DbSession`` alias, which
is a default value, so a parameter without one cannot follow it. ``app_settings``
uses an ``Annotated`` dependency, which carries no default, so it must come
first.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import UNAUTHORIZED_DETAIL, WWW_AUTHENTICATE_VALUE, AppSettings
from app.db.session import DbSession
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    RefreshRequest,
    TenantRegistrationRequest,
    TenantRegistrationResponse,
    TenantResponse,
    TokenPairResponse,
)
from app.schemas.users import UserResponse
from app.services.auth import (
    InvalidCredentialsError,
    IssuedTokens,
    authenticate,
    exchange_refresh_token,
    revoke_refresh_token,
)
from app.services.tenants import TenantSlugUnavailableError, register_tenant

router = APIRouter(prefix="/auth", tags=["auth"])

_logger = logging.getLogger(__name__)

#: Returned when a slug is already registered.
#:
#: A 409, not a 400: the request was well-formed and the conflict is with
#: existing state, which is what ``409 Conflict`` means. The message names the
#: slug field only — it deliberately does not echo the stored tenant's name, id
#: or owner, because this endpoint is unauthenticated and any of those would be a
#: disclosure about somebody else's registration.
SLUG_UNAVAILABLE_DETAIL = "That tenant slug is already registered"


def _unauthorized() -> HTTPException:
    """The single ``401`` every authentication failure produces.

    Built by a function rather than repeated at each ``raise`` so the status
    code, the detail and the ``WWW-Authenticate`` header cannot drift apart
    between the login, refresh and logout paths.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": WWW_AUTHENTICATE_VALUE},
    )


def _token_pair(tokens: IssuedTokens) -> TokenPairResponse:
    """Shape an :class:`~app.services.auth.IssuedTokens` for the wire.

    Fields are listed one by one rather than handing the service object to
    Pydantic. That is what keeps ``jti`` and ``expires_at`` out of the response:
    they are not secret, but a response field is a field with no stated purpose,
    and naming each one makes an accidental addition visible in review.
    """
    return TokenPairResponse(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        token_type=tokens.token_type,
        expires_in_seconds=tokens.expires_in_seconds,
    )


@router.post(
    "/register-tenant",
    response_model=TenantRegistrationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a tenant and its first administrator",
    responses={
        status.HTTP_409_CONFLICT: {"description": "The tenant slug is taken."},
    },
)
def register(
    payload: TenantRegistrationRequest,
    app_settings: AppSettings,
    session: Session = DbSession,
) -> TenantRegistrationResponse:
    """Create a tenant, its first administrator, and sign that administrator in.

    Unauthenticated by necessity: this is how a tenant comes into existence, so
    there is no identity to authenticate yet. The tenant id is generated
    server-side and no request field can influence it.

    The whole operation is one transaction. If the user insert fails, the tenant
    does not exist either — there is no window in which an organisation exists
    with nobody able to administer it.
    """
    try:
        result = register_tenant(
            session,
            name=payload.name,
            slug=payload.slug,
            admin_email=payload.admin_email,
            admin_password=payload.admin_password,
            app_settings=app_settings,
        )
    except TenantSlugUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=SLUG_UNAVAILABLE_DETAIL
        ) from exc

    return TenantRegistrationResponse(
        tenant=TenantResponse(
            id=result.tenant.id,
            name=result.tenant.name,
            slug=result.tenant.slug,
            created_at=result.tenant.created_at,
        ),
        user=UserResponse.model_validate(result.admin),
        tokens=_token_pair(result.tokens),
    )


@router.post(
    "/login",
    response_model=LoginResponse,
    summary="Authenticate against one tenant and receive a token pair",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Credentials rejected."},
    },
)
def login(
    payload: LoginRequest,
    app_settings: AppSettings,
    session: Session = DbSession,
) -> LoginResponse:
    """Authenticate a user and issue an access/refresh pair.

    The request carries a tenant **slug**, not a tenant id. The slug is untrusted
    input that selects which tenant to search; the tenant identity used for
    everything afterwards is the ``tenant.id`` the service resolved it to. There
    is no request field that can name the authenticated tenant.
    """
    try:
        user, _tenant, tokens = authenticate(
            session,
            tenant_slug=payload.tenant_slug,
            email=payload.email,
            password=payload.password,
            app_settings=app_settings,
        )
    except InvalidCredentialsError as exc:
        # The message is the specific reason — wrong password, unknown tenant —
        # and is exactly what the log needs. The client gets the shared detail.
        _logger.info("login rejected: %s", exc)
        raise _unauthorized() from exc

    return LoginResponse(
        user=UserResponse.model_validate(user),
        tokens=_token_pair(tokens),
    )


@router.post(
    "/refresh",
    response_model=LoginResponse,
    summary="Exchange a refresh token for a new pair",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "The refresh token is not usable."},
    },
)
def refresh(
    payload: RefreshRequest,
    app_settings: AppSettings,
    session: Session = DbSession,
) -> LoginResponse:
    """Exchange a refresh token for a fresh access/refresh pair.

    Rotate-on-use, as established in Phase 2.2: the presented token is revoked
    in the same transaction that issues its replacement, so a token that is
    presented twice cannot be exchanged twice. This endpoint therefore *is* the
    replay protection — there is no separate mechanism here and none is needed.
    """
    try:
        result = exchange_refresh_token(
            session,
            refresh_token=payload.refresh_token,
            app_settings=app_settings,
        )
    except InvalidCredentialsError as exc:
        _logger.info("refresh rejected: %s", exc)
        raise _unauthorized() from exc

    return LoginResponse(
        user=UserResponse.model_validate(result.user),
        tokens=_token_pair(result.tokens),
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Revoke a refresh token",
)
def logout(
    payload: LogoutRequest,
    app_settings: AppSettings,
    session: Session = DbSession,
) -> Response:
    """Revoke one refresh token. Idempotent, and silent about the outcome.

    Returns ``204`` whether the token was live, already revoked, unknown, or
    never valid at all. That is the correct semantics for logout — the caller's
    intent is "make this stop working", and it either did or it already had —
    and it means the response cannot be used to find out whether a guessed token
    ever existed. The token is still signature-verified first inside the service,
    so a forged token cannot be used to revoke somebody else's session by
    guessing an identifier.

    An access token that was already issued stays valid until it expires. That
    is the established design (decision D019): access tokens are stateless and
    short-lived, and adding a blocklist to invalidate them would mean tracking
    every token ever issued. Revoking the refresh token is what stops the
    session being renewed.
    """
    revoke_refresh_token(
        session,
        refresh_token=payload.refresh_token,
        app_settings=app_settings,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
