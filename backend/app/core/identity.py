"""The authenticated identity: what a verified token establishes.

:class:`AuthenticatedIdentity` is the boundary object of the whole application.
It is the answer to "who is this request?" and — because it carries
``tenant_id`` — also to "which tenant is this request operating on?".

Why a separate type at all
--------------------------
Three reasons, in order of importance:

1. **It makes the trust boundary explicit and greppable.** Every downstream
   tenant-scoped operation takes ``AuthenticatedIdentity.tenant_id``. There is
   no other legitimate source of a tenant identifier in the codebase, so "could
   this tenant id have come from a request body?" has a mechanical answer
   instead of a review answer.

2. **It carries no database session.** PMP §12 says "every database repository
   will require a tenant identifier" and that "the authenticated identity
   determines the tenant context". A type that cannot hold a ``Session`` makes
   it impossible to accidentally treat uncommitted database state as identity.
   Conversely, the ``role`` here is a *claim from a token*, not a column read
   in this request — see the caveat below.

3. **It converts claims into domain types once.** ``tenant_id`` becomes a
   :class:`uuid.UUID` and ``role`` becomes a :class:`UserRole`, in one place,
   at the edge. Downstream code then cannot compare a role against a typo, and
   cannot accidentally treat a malformed string as a tenant id.

Exactly two construction sites, both on this type
------------------------------------------------
:meth:`AuthenticatedIdentity.from_access_token`, for a verified bearer claim, and
:meth:`AuthenticatedIdentity.for_tenant_service`, for a tenant read off a
persisted row by a worker that holds no token. Both live here rather than at
their call sites so that "where can an identity come from?" has one file to
read, and neither is reachable from a request.

Staleness, stated plainly
-------------------------
``role`` is what the token said at issue time. It is a claim, not a fact about
the current database row, and it will not change if an administrator edits the
role until the access token expires. This is the standard and deliberate
trade-off for stateless JWTs, and the PMP accepts it: §8 requires only that
access tokens have *short* expirations, and
:attr:`Settings.access_token_expire_minutes` is 15. A later step that needs
immediate role changes must re-read the role from the database in the
authorization dependency rather than trusting this field — it must not fix the
problem by trusting a client-supplied role either.

One exception, added in Phase 5 Step 2: :meth:`for_tenant_service`
------------------------------
:meth:`AuthenticatedIdentity.for_tenant_service` is a second construction
site, and the module docstring's exclusivity is about *untrusted* sources rather
than about there being only one. Its identity comes from a row this application
wrote, which is a strictly stronger source than a token for the purpose: a claim
can be replayed until it expires, and a row is simply what the database says
now. Its ``role`` is :attr:`UserRole.VIEWER` — least privilege, so that adding
a role filter to any service it touches denies this actor rather than granting
it.

``email`` is intentionally not a field. Email changes, and a token that
embeds a stale address is a source of bugs with no offsetting benefit: nothing
in this step needs to know who the user is *by name*.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.core.config import Settings
from app.core.security import TokenClaims, TokenType, TokenValidationError, decode_token
from app.models.user import UserRole

#: The ``user_id`` carried by an identity that no person produced.
#:
#: The all-zeros UUID. Chosen because it is unmistakable in a log, impossible
#: to confuse with a real ``uuid4()``, and impossible to mistake for a person —
#: which is exactly what it is: the absence of one.
#:
#: It is **not** a row in ``users``. It is used only as the ``user_id`` of an
#: identity built by :meth:`AuthenticatedIdentity.for_tenant_service`, and no
#: code on that path writes it to a foreign key. A later step that wants a real
#: actor — "which user re-triggered this ingest" — must record the real
#: ``uploaded_by`` at the call site rather than assume this value is a person.
SYSTEM_ACTOR_ID: Final[uuid.UUID] = uuid.UUID(int=0)


class IdentitySource(StrEnum):
    """Where an :class:`AuthenticatedIdentity` came from.

    Every member names a source this project **trusts**, and no member names
    anything a client supplied. That is the invariant the set exists to express,
    and it is what a test asserts: a future bug could plausibly add a
    ``REQUEST_BODY`` or ``HEADER`` member, and the enum turns that into a visible
    change to an enumerated set rather than an extra argument on a constructor.

    :attr:`BEARER_ACCESS_TOKEN`
        A claim this application signed, verified by
        :meth:`AuthenticatedIdentity.from_access_token`. The HTTP path.
    :attr:`PERSISTED_ROW`
        A ``tenant_id`` read from a row in PostgreSQL that this application
        itself wrote. Added in Phase 5 Step 2 for
        :meth:`AuthenticatedIdentity.for_tenant_service`, which the Celery
        ingestion task uses because a worker holds no bearer token.

    There is deliberately still no ``REQUEST_BODY`` or ``HEADER`` member. Login
    accepts a tenant *slug* from the client, but that is untrusted input used
    to pick which tenant to look in; it never becomes an
    :class:`AuthenticatedIdentity`, and the absence of those members is the
    clearest possible statement of that.

    Login accepting a slug is the reason this distinction has to be drawn rather
    than assumed. A slug **selects** a tenant and proves nothing about the
    caller's membership in it; the row does the latter by existing under a
    tenant the caller authenticated into. :attr:`PERSISTED_ROW` is a member of
    this enum precisely because it is a *stronger* source than a token for the
    purpose it serves — the tenant cannot be renamed or re-pointed by replaying
    a claim — and precisely because it is only safe when the row it names was
    selected by something already scoped.
    """

    BEARER_ACCESS_TOKEN = "bearer_access_token"
    PERSISTED_ROW = "persisted_row"


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    """A verified caller: user id, tenant id, and role. Immutable."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID
    role: UserRole
    source: IdentitySource = IdentitySource.BEARER_ACCESS_TOKEN

    @classmethod
    def from_access_token(
        cls, token: str, *, app_settings: Settings | None = None
    ) -> AuthenticatedIdentity:
        """Verify a bearer access token and build an identity from it.

        The one and only place *claims* become an identity. It exists so that a
        caller cannot assemble an :class:`AuthenticatedIdentity` from anything
        else: :func:`app.api.deps.get_current_identity` calls this, and nothing
        else should. The one other construction site on this type is
        :meth:`for_tenant_service`, which is not reachable from a request.

        Args:
            token: the raw compact-serialization JWS.
            app_settings: settings override. Present so that
                :func:`app.api.deps.get_current_identity` can take its settings
                by dependency injection rather than reaching for a module
                global, which is what makes the HTTP layer testable without
                mutating process state.

        Raises:
            TokenValidationError: the token is malformed, expired, wrongly
                signed, of the wrong type, or missing a claim. The message
                carries the reason for the log; callers must not return it to
                the client.
            TokenValidationError: also raised when the ``role`` claim is not a
                valid :class:`UserRole`, because a token whose role is
                unrecognised must not authenticate anyone. Silently defaulting
                it would be the worst possible failure here.
        """
        claims = decode_token(
            token, expected_type=TokenType.ACCESS, app_settings=app_settings
        )
        return cls.from_claims(claims)

    @classmethod
    def from_claims(cls, claims: TokenClaims) -> AuthenticatedIdentity:
        """Convert already-verified access-token claims into an identity.

        Separated from :meth:`from_access_token` so tests can build an identity
        from hand-written claims without minting a signed token. It does **not**
        re-verify anything — it trusts ``claims`` to be verified, exactly as
        its name says.
        """
        if claims.type is not TokenType.ACCESS:
            raise TokenValidationError(
                "an authenticated identity can only be built from an access "
                f"token, not a {claims.type.value} token"
            )
        if claims.role is None:
            raise TokenValidationError("access token is missing the 'role' claim")

        try:
            role = UserRole(claims.role)
        except ValueError as exc:
            # An unknown role is an authentication failure, not a default.
            raise TokenValidationError(
                f"access token carries an unrecognised role {claims.role!r}"
            ) from exc

        return cls(
            user_id=claims.subject,
            tenant_id=claims.tenant_id,
            role=role,
        )

    @classmethod
    def for_tenant_service(
        cls, *, tenant_id: uuid.UUID
    ) -> AuthenticatedIdentity:
        """Build the identity of work the system performs on its own behalf.

        The second and last way to construct one. Added by Phase 5 Step 2,
        because the Celery ingestion task must run
        :func:`app.services.ingestion.ingest_document` and that function takes an
        :class:`AuthenticatedIdentity` — while a worker process holds no bearer
        token, and minting one would mean signing a token to work as itself.

        Why a named constructor rather than calling ``cls(...)`` directly
        ------------------------------------------------------------------
        :meth:`from_access_token`'s exclusivity is a security property: if any
        other module can build one, "tenant context comes from a verified token"
        stops being a mechanical answer and becomes a review question. This is
        the *one* other construction site, it is here rather than in the worker,
        and it is named after what it trusts. A direct call from
        ``app/workers/`` would leave every existing structural test green while
        quietly creating exactly the second call site those tests exist to
        prevent — the guard reads ``identity.py``'s source, so it would never
        see it.

        Why the tenant may be trusted here
        ----------------------------------
        The caller of this method is not trusted, and this method does not care:
        ``tenant_id`` is keyword-only and supplied by code that read it off a
        :class:`~app.models.document.Document` row. The safety property is not
        about *this* method's argument at all — it is that the only path to this
        method in the shipped application is the Celery task, and that task
        accepts a ``document_id`` and **no** tenant argument at all. There is no
        argument for a caller to supply a tenant *with*, so the pairing
        ``(document_id=A, tenant_id=B)`` is not merely rejected, it is
        unrepresentable.

        What the two placeholder fields mean
        --------------------------------------
        :attr:`role` is :attr:`UserRole.VIEWER` — the **least** privileged member
        of the enum, chosen so that if a later step ever adds a role filter to
        the ingestion lookup, this identity is refused rather than silently
        granted administrator access. The direction a mistake should fail in is
        the one that costs the least.

        :attr:`user_id` is :data:`SYSTEM_ACTOR_ID`, because there is no user: the
        work is triggered by nobody. Neither field is consulted by the ingestion
        path today — it reads :attr:`tenant_id` and nothing else, which is what
        makes them placeholders rather than live security inputs — but they are
        filled rather than left optional so that a future reader cannot mistake
        this for a partially-populated identity.

        Args:
            tenant_id: the tenant the system is acting within, read from a
                persisted row. Keyword-only so that adding a second parameter
                later cannot silently reorder an existing call.

        Returns:
            An identity whose :attr:`source` is :attr:`IdentitySource.PERSISTED_ROW`.
        """
        return cls(
            user_id=SYSTEM_ACTOR_ID,
            tenant_id=tenant_id,
            role=UserRole.VIEWER,
            source=IdentitySource.PERSISTED_ROW,
        )