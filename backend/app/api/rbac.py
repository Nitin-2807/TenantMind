"""Route-level role authorization: the one place a role is checked.

Authentication versus authorization, one more time
---------------------------------------------------
:mod:`app.api.deps` answers *who is this request* and refuses to say anything
about permissions. This module answers *what may they do*, and — this is the
part that matters — it can only answer it from what :mod:`app.api.deps`
returned.

The mechanism
-------------
:func:`require_role` is a dependency *factory*. Calling it with the roles a route
accepts produces a small dependency that:

1. takes the authenticated identity as its only input, which means the role is
   read from a verified token and from nowhere else;
2. raises ``403`` if the identity's role is not in the allowlist;
3. returns the identity unchanged, so it chains with
   :data:`~app.api.deps.CurrentIdentity` instead of the route having to declare
   both.

Usage, once per route::

    CurrentAdmin = Annotated[AuthenticatedIdentity, Depends(require_role(UserRole.ADMIN))]

Why a factory and not a policy engine
-------------------------------------
PMP §16 is a table of seven operations against three roles. A general policy
engine would be machinery for answering a question this project has not asked:
nobody is adding a role, a resource, a scope or a wildcard here. What *is*
needed is one place where a route states its own requirement, so that a reviewer
can read the requirement off the route and a test can enumerate it.

The fail-closed properties are what the design is actually protecting:

* An **empty** allowlist denies everyone. ``require_role()`` with no arguments
  is a closed door, not an open one — the default for a security check must not
  be "allow".
* An **unrecognised** role denies. The identity's role is a
  :class:`~app.models.user.UserRole`, so this cannot happen through a token:
  :meth:`~app.core.identity.AuthenticatedIdentity.from_claims` refuses to build
  an identity from an unknown role, so authentication fails first with a 401.
  The allowlist check is nonetheless written as a plain membership test, which
  is false for anything it does not recognise, so a future role added to the
  enum without a route decision defaults to *denied* rather than to *allowed*.
* **Unauthenticated** stays unauthenticated. This module never issues a 401; the
  403 it raises is only reachable once a valid identity exists, so the two
  failure modes cannot be confused.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from fastapi import HTTPException, status

from app.api.deps import CurrentIdentity
from app.core.identity import AuthenticatedIdentity
from app.models.user import UserRole

_logger = logging.getLogger(__name__)

#: The single message returned for every authorization failure.
#:
#: Fixed for the same reason :data:`app.api.deps.UNAUTHORIZED_DETAIL` is: a
#: message that varies with the reason — "you are a viewer", "this route needs
#: an administrator" — is a description of the access-control rules handed to
#: whoever is probing them. It is deliberately *not* the same constant as the
#: 401 detail, so a client can still tell "you are not signed in" from "you are
#: signed in and may not do this", which is a distinction it needs in order to
#: act correctly.
FORBIDDEN_DETAIL = "Not enough permissions"

#: The signature of a dependency produced by :func:`require_role`.
#:
#: Named so route modules can annotate the result, and so mypy sees a concrete
#: type instead of inferring a fresh callable per call site.
RoleChecker = Callable[[AuthenticatedIdentity], AuthenticatedIdentity]


def require_role(*allowed: UserRole) -> RoleChecker:
    """Build a dependency that admits only the listed roles.

    Args:
        allowed: the roles permitted on the route. Order is irrelevant; passing
            none denies every caller.

    Returns:
        A FastAPI dependency taking an
        :class:`~app.core.identity.AuthenticatedIdentity` and returning it
        unchanged when its role is permitted.

    Raises:
        fastapi.HTTPException: ``403`` with :data:`FORBIDDEN_DETAIL` when the
            authenticated caller's role is not permitted. The caller's role is
            logged, never returned — a client does not need to be told what it
            already is, and the log is where a misconfiguration becomes visible.
    """
    permitted = frozenset(allowed)

    def _enforce(identity: CurrentIdentity) -> AuthenticatedIdentity:
        # `not in` rather than `==` against a list, so the check is a single
        # set lookup and an unrecognised role object is false rather than an
        # equality that happens to succeed.
        if identity.role not in permitted:
            # Logged at INFO, not WARNING. A 403 is often a *correct* answer —
            # an editor reaching for an administrator-only route is the access
            # model working — so WARNING would train operators to ignore the
            # line. It is still recorded, because a user reporting "it says I
            # have no permission" is otherwise unanswerable. The user and tenant
            # ids are logged because a role check is meaningless without knowing
            # whose role was considered; neither is a secret.
            #
            # `identity.role` and the permitted set are formatted with `%s`
            # rather than read through `.value`. The enum is a `StrEnum`, so the
            # two spellings are identical for every real role — but a role the
            # enum does not contain has no `.value`, and reaching for it here
            # would turn the clean 403 this function exists to raise into a
            # 500 from the logging call.
            _logger.info(
                "authorization denied: user=%s tenant=%s role=%s permitted=%s",
                identity.user_id,
                identity.tenant_id,
                identity.role,
                sorted(map(str, permitted)),
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        return identity

    return _enforce
