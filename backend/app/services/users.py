"""User management inside one tenant: PMP §15's ``/users`` surface, PMP FR-03.

Every function here takes an :class:`~app.core.identity.AuthenticatedIdentity`
and **no tenant identifier**
------------------------------------------------
This is the single most important signature in the module, so it is worth being
precise about what it buys.

A tenant-scoped function that took a ``tenant_id`` parameter would rely on every
caller passing the right one. A function that takes the *authenticated identity*
cannot be pointed at the wrong tenant without constructing a forged identity —
and forging one requires a validly signed access token, because
:class:`~app.core.identity.AuthenticatedIdentity` is only built from verified
claims. So ``tenant_id`` is not a parameter here; it is read from
``identity.tenant_id``, which came from a token this application signed.

That is why no function in this module accepts a tenant from a request body, a
query parameter, a header or a path. PMP §12 requires "the authenticated
identity determines the tenant context", and the way to make that true rather
than merely intended is to leave the service no way to be told otherwise.

404, not 403, for another tenant's user
----------------------------------------
A user id belonging to a different tenant is reported as *not found*, exactly
like an id that does not exist. A ``403`` would confirm "that user is real, you
just may not touch it", which is a free user-enumeration oracle across tenant
boundaries — a tenant could probe for a specific person's account in a
competitor's organisation and learn the answer from the status code alone.
Indistinguishability is the point, so :class:`UserNotFoundError` covers both
cases and the message names neither.

The administrator guardrails
----------------------------
Two operations can leave a tenant in a state nobody can undo:

* deleting the last active administrator;
* demoting the last active administrator.

Either leaves an organisation with no one able to change roles, invite users,
or delete anybody — permanently, because every user-management route requires
an authenticated administrator. Both are refused with
:class:`LastAdministratorError`.

Deleting one's *own* account is refused separately, with
:class:`SelfDeletionError`. The last-administrator rule already covers the
unrecoverable case, so this guard is not about lockout; it is about the request
destroying its own caller's session mid-flight, which turns an ordinary
administrative action into a confusing and unauditable one. An administrator who
wants to leave is demoted by another administrator, or deleted after promoting
someone else.

What a role change does to existing sessions
--------------------------------------------
When a role actually changes, every live refresh token for the target user is
revoked. That is
:func:`~app.services.auth.revoke_all_refresh_tokens` — a primitive Phase 2.2
built for this exact purpose and, until now, uncalled — and it is what makes
"newly issued access tokens reflect the current database role" true within one
refresh cycle rather than one access-token lifetime.

It does **not** retroactively invalidate the access token the target is
currently holding. There is no access-token blocklist, by decision D019: PMP §8
requires short access-token lifetimes and
:attr:`~app.core.config.Settings.access_token_expire_minutes` is 15, so the
window in which a demoted administrator still holds an administrator token is at
most 15 minutes, and their *session* — the part that would let them keep coming
back — is ended immediately. Introducing a blocklist to close the remaining gap
would mean a server-side store of every issued token, which is the opposite of
what the stateless design buys.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.identity import AuthenticatedIdentity
from app.core.security import hash_password
from app.models.user import User, UserRole
from app.services.auth import revoke_all_refresh_tokens


class UserNotFoundError(LookupError):
    """No such user in the caller's tenant — or no such user at all.

    One exception for both, so the HTTP layer cannot accidentally distinguish
    them. See the module docstring.
    """


class EmailAlreadyRegisteredError(ValueError):
    """A user with that email already exists in this tenant."""


class LastAdministratorError(ValueError):
    """The change would leave the tenant with no active administrator."""


class SelfDeletionError(ValueError):
    """An administrator tried to delete their own account."""


def _scoped_user(
    session: Session, identity: AuthenticatedIdentity, user_id: uuid.UUID
) -> User:
    """Load a user by id, scoped to the caller's tenant.

    The single lookup every mutating function uses, so the tenant filter cannot
    be forgotten at a call site. ``session.get`` is deliberately not used: it
    takes a primary key and offers nowhere to add the tenant predicate, and a
    primary-key-only lookup is precisely the bug this project must not ship.
    """
    user = session.scalar(
        select(User).where(
            User.id == user_id,
            User.tenant_id == identity.tenant_id,
        )
    )
    if user is None:
        raise UserNotFoundError("no such user in this tenant")
    return user


def _active_administrator_count(
    session: Session, identity: AuthenticatedIdentity
) -> int:
    """How many active administrators the caller's tenant currently has."""
    return session.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.tenant_id == identity.tenant_id,
            User.role == UserRole.ADMIN,
            User.is_active.is_(True),
        )
    ) or 0


def list_users(session: Session, *, identity: AuthenticatedIdentity) -> Sequence[User]:
    """Every user in the caller's tenant, oldest first.

    Tenant-scoped by construction: the ``WHERE`` clause is built from
    ``identity.tenant_id`` and there is no parameter through which a different
    tenant could be named.

    Ordered by creation time with the id as a tie-break. The tie-break is not
    cosmetic: two accounts created in the same transaction share a
    ``created_at``, and without a second key their relative order is whatever the
    database happens to return — which would make pagination (when it exists)
    and test assertions unstable.
    """
    return session.scalars(
        select(User)
        .where(User.tenant_id == identity.tenant_id)
        .order_by(User.created_at, User.id)
    ).all()


def invite_user(
    session: Session,
    *,
    identity: AuthenticatedIdentity,
    email: str,
    password: str,
    role: UserRole,
) -> User:
    """Create a user inside the caller's tenant.

    No token pair is issued and no credential material is returned. Returning
    one would let any administrator mint a working session as any other user —
    including a more privileged one — which would make this endpoint an
    impersonation primitive rather than an account-management one. The new user
    authenticates with the password their administrator gave them.

    Args:
        session: an open database session. The caller owns the transaction.
        identity: the calling administrator. Its ``tenant_id`` is the only
            tenant the new user can belong to.
        email: the new user's login address, already folded to lower case by the
            request schema.
        password: their initial password in plaintext, hashed here.
        role: the role to grant.

    Raises:
        EmailAlreadyRegisteredError: the email is already used in this tenant.
            Note this is scoped to one tenant: the same address may legitimately
            exist in a different organisation.
    """
    existing = session.scalar(
        select(User.id).where(
            User.tenant_id == identity.tenant_id,
            User.email == email,
        )
    )
    if existing is not None:
        raise EmailAlreadyRegisteredError("that email is already registered")

    user = User(
        tenant_id=identity.tenant_id,
        email=email,
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    try:
        session.flush()
    except IntegrityError as exc:
        # The pre-check above is a courtesy that produces a precise error in the
        # common case; the composite unique constraint is what actually
        # enforces the rule, and two concurrent invites can both pass the check.
        session.rollback()
        raise EmailAlreadyRegisteredError("that email is already registered") from exc

    return user


def change_user_role(
    session: Session,
    *,
    identity: AuthenticatedIdentity,
    user_id: uuid.UUID,
    role: UserRole,
) -> User:
    """Set a user's role, and end their existing sessions if it changed.

    See the module docstring for why the session revocation is here and what it
    does not do.

    A no-op change (setting the role a user already has) performs no
    revocation. Revoking on a no-op would be defensible, but it would also mean
    a client that retries an idempotent request logs the user out — surprising,
    and a mild denial-of-service lever against a caller's own account.

    Args:
        session: an open database session. The caller owns the transaction.
        identity: the calling administrator. Also the tenant boundary.
        user_id: the user to change. Must be inside that tenant.
        role: the role to set.

    Raises:
        UserNotFoundError: no such user in the caller's tenant.
        LastAdministratorError: the change would remove the tenant's last
            *active* administrator. A disabled administrator does not count, so
            demoting one is always allowed.
    """
    user = _scoped_user(session, identity, user_id)

    if user.role is not role:
        # Named rather than inlined into the `if` because the condition is four
        # clauses wide and "is this the last administrator?" is the whole point of
        # the guard — a reader should not have to re-derive it.
        #
        # `user.is_active` matters as much as the role: a *disabled*
        # administrator cannot promote anybody, so they are not holding the role
        # open. Without it a tenant with one active and one disabled
        # administrator could not demote the disabled one, and the floor would be
        # permanently unsatisfiable. `delete_user` applies the same reasoning.
        demotes_the_last_administrator = (
            user.role is UserRole.ADMIN
            and user.is_active
            and role is not UserRole.ADMIN
            and _active_administrator_count(session, identity) <= 1
        )
        if demotes_the_last_administrator:
            raise LastAdministratorError(
                "a tenant must keep at least one active administrator"
            )

        user.role = role
        # Ends the target's sessions, so their next access token is minted from
        # the role just written. Part of this request's transaction: if the role
        # change is rolled back, so is the revocation.
        revoke_all_refresh_tokens(
            session,
            tenant_id=identity.tenant_id,
            user_id=user.id,
        )
        session.flush()

    return user


def delete_user(
    session: Session,
    *,
    identity: AuthenticatedIdentity,
    user_id: uuid.UUID,
) -> None:
    """Delete a user and cascade their sessions away.

    A hard delete, not a deactivation, and the reason is already established:
    PMP §15 exposes ``DELETE /users/{id}``, and Phase 2 Step 2 gave
    ``refresh_tokens.user_id`` an ``ON DELETE CASCADE`` with
    ``passive_deletes=True`` for exactly this endpoint. A user row that outlived
    its ``DELETE`` would be a state the API cannot produce but the schema would
    still permit, so the two would disagree.

    The database does the cascading, so no refresh-token row is loaded into
    memory to be deleted — see :attr:`~app.models.user.User.refresh_tokens`.
    Anything a deleted user owned that must survive would need a different
    decision, and none of the tables that exist today is such a case.

    Deactivation is a *different* operation and is not this one. It is already
    enforced where it matters — :func:`app.services.auth.authenticate` refuses a
    user whose ``is_active`` is false, and
    :func:`app.services.auth.exchange_refresh_token` refuses to refresh for one
    — but PMP §15 defines no endpoint that sets it, so none is invented. That
    gap is recorded in the execution log as a deferred item rather than filled
    with an endpoint the PMP does not ask for.

    Args:
        session: an open database session. The caller owns the transaction.
        identity: the calling administrator. Also the tenant boundary.
        user_id: the user to delete. Must be inside that tenant.

    Raises:
        UserNotFoundError: no such user in the caller's tenant.
        SelfDeletionError: the caller is deleting their own account.
        LastAdministratorError: the target is the tenant's only active
            administrator.
    """
    user = _scoped_user(session, identity, user_id)

    if user.id == identity.user_id:
        raise SelfDeletionError("an administrator cannot delete their own account")

    # Only a *live* administrator counts towards the tenant's last
    # administrator: a disabled administrator cannot promote anybody, so they
    # are not holding the role open.
    removes_the_last_administrator = (
        user.role is UserRole.ADMIN
        and user.is_active
        and _active_administrator_count(session, identity) <= 1
    )
    if removes_the_last_administrator:
        raise LastAdministratorError(
            "a tenant must keep at least one active administrator"
        )

    session.delete(user)
    session.flush()
