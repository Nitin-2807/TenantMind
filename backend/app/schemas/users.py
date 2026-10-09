"""Request and response schemas for the user-management API (PMP §15, Phase 2.3).

The one rule that shapes every model here
----------------------------------------
No field a client sends can name a tenant. There is no ``tenant_id`` in any
request model in this module, and that is deliberate rather than an oversight:
:mod:`app.services.users` takes an
:class:`~app.core.identity.AuthenticatedIdentity` and reads
``identity.tenant_id``, so a tenant identifier has exactly one possible source
in the whole request. Adding a ``tenant_id`` field would be easy and would be a
cross-tenant vulnerability, so its absence is worth a test (see
``tests/test_users_api.py``).

What :class:`UserResponse` deliberately omits
--------------------------------------------
``password_hash`` is never serialised, and neither is ``tenant_id``:

* the hash is a credential equivalent — a leaked Argon2 hash is a target for
  offline cracking — and no legitimate caller has any use for it;
* the tenant is the caller's own context, already fixed by their token, so
  echoing it adds no information a client does not have.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.user import UserRole
from app.schemas.fields import EmailAddress, NewPassword


class UserResponse(BaseModel):
    """A user as seen by callers who are already inside that user's tenant.

    Built by the API layer from a :class:`~app.models.user.User` instance, not
    returned as the ORM object itself. Passing the model to FastAPI as a
    ``response_model`` would work today and is exactly the kind of convenience
    that stops being safe the first time a column is added: every new column
    becomes part of the public contract unless someone remembers to exclude it.
    Naming the fields makes that decision explicit and reviewable.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID = Field(description="The user's immutable identifier.")
    email: str = Field(
        description="The login address, stored and compared in folded lower case."
    )
    role: UserRole = Field(description="The role the user's access token carries.")
    is_active: bool = Field(
        description=(
            "Whether the account may authenticate. A user whose row is inactive "
            "cannot log in, and an already-issued refresh token stops being "
            "exchangable."
        )
    )
    created_at: datetime = Field(description="When the account was created.")


class UserInviteRequest(BaseModel):
    """Create a user inside the caller's own tenant (PMP §15 ``POST /users/invite``).

    The name is the PMP's. What the PMP does **not** define is the mechanics
    behind it, and the gap is worth stating plainly rather than papering over.

    PMP §15 names an *invite* endpoint; nothing in the PMP describes sending
    mail. Rather than invent an email transport, a background job and a
    one-time invitation token — a workflow the product never specified, and one
    whose failure modes (unreachable SMTP, expired links, a half-created
    account) are substantial — the endpoint does the part that is defined: it
    creates an active user in the caller's tenant, with the administrator
    supplying the initial password.

    Two consequences a reviewer should be aware of:

    * The administrator chooses the password, so the administrator knows it. That
      is a real difference from a self-service invite, and it is a deliberate
      consequence of not shipping email, not an oversight.
    * No token pair is returned. Handing the caller a working credential for a
      user they just created would make this endpoint an impersonation
      primitive: any administrator could mint a session as any other user,
      including a more privileged one. The created user must authenticate
      themselves, which is what makes their password meaningful.

    ``role`` defaults to the model's own least-privilege default, ``viewer``.
    An administrator who wants an editor or another administrator says so
    explicitly.
    """

    email: EmailAddress = Field(description="Login address, unique within the tenant.")
    password: NewPassword = Field(
        description=(
            "The new user's initial password. At least 12 characters; no "
            "composition rules are imposed."
        )
    )
    role: Annotated[
        UserRole,
        Field(description="The role to grant. Defaults to 'viewer'."),
    ] = UserRole.VIEWER


class RoleChangeRequest(BaseModel):
    """Set a user's role (PMP §15 ``PATCH /users/{id}/role``).

    A single-field body rather than a partial user update. PMP §15 defines
    exactly one mutation on a user, so a general-purpose update model would be
    an endpoint nobody asked for — and one that would need its own field-by-field
    authorisation rules.
    """

    role: UserRole = Field(description="The role to set.")


class UserListResponse(BaseModel):
    """The users in the caller's tenant, oldest first.

    A wrapper object rather than a bare JSON array, for one reason: a bare array
    cannot grow. The PMP specifies no pagination, so none is invented here — but
    the shape leaves room to add it without breaking any client, whereas
    returning ``list[UserResponse]`` would mean changing the response type the
    day that happens.
    """

    items: Annotated[
        list[UserResponse],
        Field(description="Every user in the authenticated caller's tenant."),
    ] = Field(default_factory=list)
