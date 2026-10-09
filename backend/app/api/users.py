"""User-management endpoints (PMP §15, Phase 2.3).

    POST   /users/invite
    GET    /users
    PATCH  /users/{user_id}/role
    DELETE /users/{user_id}

All four require an authenticated administrator. Where that comes from
------------------------------------------------------------------
:data:`CurrentAdmin` is a single object built once at import time by
:func:`app.api.rbac.require_role`. It is used as the identity type of all four
routes, so the access rule appears in exactly one place and every route below
reads the same way:

* the caller is authenticated (:func:`app.api.deps.get_current_identity`, hence
  a 401 without a valid token);
* the caller's role is ``admin``, from the token (hence a 403 otherwise);
* the resulting identity — and therefore the tenant — is passed to the handler
  as a parameter.

No route in this module reads a role, and no route contains a role comparison.
That is enforced by a test rather than by convention: it parses this file and
fails if a bare ``role ==``/``role in`` ever appears, so the invariant cannot
quietly erode as the router grows.

The tenant boundary is the identity, not a parameter
-----------------------------------------------------
:mod:`app.services.users` takes the identity and no ``tenant_id``, so no route
here can pass one. A path parameter names *which* user; it never names *whose*
tenant they are looked up in. See that module for why a user id from another
tenant is reported as 404 rather than 403.

Handlers are ``def``, not ``async def`` — see :mod:`app.api.auth` for why.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.rbac import require_role
from app.core.identity import AuthenticatedIdentity
from app.db.session import DbSession
from app.models.user import UserRole
from app.schemas.users import (
    RoleChangeRequest,
    UserInviteRequest,
    UserListResponse,
    UserResponse,
)
from app.services.users import (
    EmailAlreadyRegisteredError,
    LastAdministratorError,
    SelfDeletionError,
    UserNotFoundError,
    change_user_role,
    delete_user,
    invite_user,
    list_users,
)

router = APIRouter(prefix="/users", tags=["users"])

_logger = logging.getLogger(__name__)

#: Every route in this module is administrator-only.
#:
#: PMP §16 grants "Invite users" and "Change user roles" to the administrator
#: alone, and PMP §6 gives the Organization Administrator user management while
#: the Editor and Viewer have none. Listing and deleting users are the same
#: class of operation — they expose or destroy the tenant's user directory — so
#: all four routes take the same allowlist. PMP §16's table does not name "list
#: users" explicitly; restricting it is the conservative reading of a table
#: whose other two user-management rows are administrator-only, and an
#: over-broad list is the more damaging error of the two.
ADMIN_ONLY = require_role(UserRole.ADMIN)

#: The annotated identity every user-management route runs on.
CurrentAdmin = Annotated[AuthenticatedIdentity, Depends(ADMIN_ONLY)]

#: Shared 404 body. One constant so the "no such user" and "no such user in
#: your tenant" cases cannot diverge into distinguishable responses.
USER_NOT_FOUND_DETAIL = "No such user in this tenant"

#: Shared 409 body for the two guardrails that keep a tenant administrable.
LAST_ADMINISTRATOR_DETAIL = "A tenant must keep at least one active administrator"

#: Shared 409 body for the self-deletion guard.
SELF_DELETION_DETAIL = "An administrator cannot delete their own account"

#: 409 body for an email already used inside this tenant.
#:
#: Scoped to one tenant, and the message says so, because the same address
#: legitimately exists in other organisations — ``users`` is unique on
#: ``(tenant_id, email)``, never on ``email`` alone. Saying "that email is
#: already registered" without qualification would be wrong in a way that
#: matters: it would tell an administrator they cannot onboard a colleague
#: whose address is already registered somewhere else entirely.
EMAIL_UNAVAILABLE_DETAIL = "That email is already registered in this tenant"


@router.post(
    "/invite",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user in the caller's tenant",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Not authenticated."},
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator."},
        status.HTTP_409_CONFLICT: {"description": "The email is taken in this tenant."},
    },
)
def invite(
    payload: UserInviteRequest,
    identity: CurrentAdmin,
    session: Session = DbSession,
) -> UserResponse:
    """Create a user inside the caller's own tenant.

    Creates the account the PMP defines; it does not send mail, because the PMP
    defines no mail. See :class:`app.schemas.users.UserInviteRequest` for what
    that means in practice and why no token pair is returned.

    The response carries no credential material. Returning one would let any
    administrator act as the user they just created.
    """
    try:
        user = invite_user(
            session,
            identity=identity,
            email=payload.email,
            password=payload.password,
            role=payload.role,
        )
    except EmailAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=EMAIL_UNAVAILABLE_DETAIL
        ) from exc

    return UserResponse.model_validate(user)


@router.get(
    "",
    response_model=UserListResponse,
    summary="List the caller's tenant's users",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Not authenticated."},
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator."},
    },
)
def index(
    identity: CurrentAdmin,
    session: Session = DbSession,
) -> UserListResponse:
    """List every user in the authenticated caller's tenant.

    The tenant is ``identity.tenant_id`` and there is no query parameter, header
    or body field that can change it. A request naming another tenant's slug or
    id is ignored entirely — this endpoint accepts no query parameters at all,
    so there is nothing to inject.
    """
    users = list_users(session, identity=identity)
    return UserListResponse(items=[UserResponse.model_validate(user) for user in users])


@router.patch(
    "/{user_id}/role",
    response_model=UserResponse,
    summary="Set a user's role",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Not authenticated."},
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator."},
        status.HTTP_404_NOT_FOUND: {"description": "No such user in this tenant."},
        status.HTTP_409_CONFLICT: {"description": "It is the last active administrator."},
    },
)
def update_role(
    user_id: uuid.UUID,
    payload: RoleChangeRequest,
    identity: CurrentAdmin,
    session: Session = DbSession,
) -> UserResponse:
    """Set a user's role, ending their existing sessions if the role changed.

    ``user_id`` is a path parameter and is the *only* client-supplied identifier
    in the call. The tenant comes from ``identity``, so a user id from another
    tenant resolves to 404 — indistinguishable from an id that does not exist.

    The changed user's live refresh tokens are revoked in the same transaction,
    so the next token they obtain carries the new role. The access token they
    are already holding is not revoked: there is no access-token blocklist, by
    design, and access tokens expire within
    :attr:`~app.core.config.Settings.access_token_expire_minutes` minutes. That
    residual window is documented in :mod:`app.services.users` rather than
    hidden.
    """
    try:
        user = change_user_role(
            session,
            identity=identity,
            user_id=user_id,
            role=payload.role,
        )
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND_DETAIL
        ) from exc
    except LastAdministratorError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=LAST_ADMINISTRATOR_DETAIL
        ) from exc

    return UserResponse.model_validate(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Delete a user and their sessions",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Not authenticated."},
        status.HTTP_403_FORBIDDEN: {"description": "Not an administrator."},
        status.HTTP_404_NOT_FOUND: {"description": "No such user in this tenant."},
        status.HTTP_409_CONFLICT: {"description": "Refused by a guardrail."},
    },
)
def destroy(
    user_id: uuid.UUID,
    identity: CurrentAdmin,
    session: Session = DbSession,
) -> Response:
    """Delete a user, cascading their refresh tokens away in the database.

    Two operations are refused, both with ``409``: deleting the tenant's last
    active administrator, and deleting your own account. Either can leave an
    organisation with no one able to administer it, which is unrecoverable
    through the API.
    """
    try:
        delete_user(session, identity=identity, user_id=user_id)
    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=USER_NOT_FOUND_DETAIL
        ) from exc
    except SelfDeletionError as exc:
        _logger.info(
            "self-deletion refused: user=%s tenant=%s",
            identity.user_id,
            identity.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=SELF_DELETION_DETAIL
        ) from exc
    except LastAdministratorError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=LAST_ADMINISTRATOR_DETAIL
        ) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)
