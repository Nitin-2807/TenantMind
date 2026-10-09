"""Password hashing and JSON Web Token primitives.

This module is the single implementation path for both. It owns exactly two
pieces of cryptography-adjacent logic — Argon2 password hashing and HS256
signing/verification — and nothing else. It knows nothing about the ORM, about
FastAPI, about tenants, or about HTTP. That is deliberate: a module that both
signs tokens and resolves tenants is a module where a tenant lookup can end up
on the wrong side of a trust boundary.

What this module deliberately does not contain
----------------------------------------------
* **No user lookup.** Nothing here reads the database, so nothing here can
  decide who somebody is. That is :mod:`app.services.auth`.
* **No authorization.** Producing a valid :class:`TokenClaims` proves *who*
  the caller is. It says nothing about what they may do. Route-level RBAC is a
  later step and must not leak in here.
* **No fallback secret.** :meth:`Settings.jwt_signing_key` raises rather than
  inventing a development key. A default secret that works locally is a secret
  that ships.

Layering
--------
``security`` depends on :mod:`app.core.config` only. It does **not** import
``app.models``: the token layer works in UUIDs and strings, and keeping it
 ignorant of the ORM is what lets :mod:`app.core.identity` be the single place
where a claim is converted into a typed domain value.

Claim policy (PMP §17, §8)
--------------------------
Access tokens carry ``sub``, ``tenant_id``, ``role``, ``type``, ``iat``,
``exp``. Refresh tokens carry ``sub``, ``tenant_id``, ``type``, ``iat``,
``exp``, ``jti``.

``tenant_id`` is in the token precisely so that the tenant context is derived
from *authenticated identity* rather than from request data — PMP §8
("Tenant identity must come from authenticated identity rather than arbitrary
request data") and PMP §12 ("The application must never trust a tenant ID
supplied by the client"). A token without a tenant claim could not satisfy
that requirement, because the next step would then have to read the tenant from
the request.

``role`` is in the access token and *not* in the refresh token. A refresh token
is exchanged for a fresh access token, so if the role travelled in the refresh
token too it would become a second, slower-moving source of authority that
survives a demotion. The role in a new access token is always read from the
current database row instead — see :func:`app.services.auth.issue_token_pair`.

Deliberately absent from every token: password hashes, email addresses, and
anything else personal. A token is bearer credential that gets logged by
proxies; it carries the minimum needed to establish identity and nothing that
would be damaging to disclose.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from jwt.exceptions import InvalidTokenError as PyJWTInvalidTokenError

from app.core.config import Settings, settings

# --------------------------------------------------------------------------
# Password hashing (PMP §17 "Argon2 or bcrypt password hashing")
# --------------------------------------------------------------------------

#: Argon2id parameters.
#:
#: These are module constants, not :class:`~app.core.config.Settings` fields, and
#: the reason is a property of Argon2 rather than a preference: the PHC string
#: produced by :func:`hash_password` **embeds** its own ``m``, ``t`` and ``p``
#: values, and :func:`verify_password` reads them back out of the stored hash
#: instead of using these. So raising the cost later does not invalidate a
#: single password already in the database — existing hashes keep verifying at
#: their original cost and new hashes get the new cost. That is what makes it
#: safe to defer the tuning decision; ``test_password_hash_parameters_come_from
#: _the_stored_hash`` in ``tests/test_security_passwords.py`` pins the
#: behaviour so the guarantee cannot be lost silently.
#:
#: The values are the OWASP Password Storage Cheat Sheet's first
#: recommendation: 19 MiB of memory (64 656 KiB is rounded to 64 MiB for
#: allocator friendliness), 2 iterations, 1 lane — chosen over Argon2's own
#: defaults of 64 MiB / 3 / 4 because a 64 MiB x 4-lane allocation is
#: ~256 MiB of resident memory *per concurrent login*, and the login endpoint
#: is precisely the place an unauthenticated caller can cause concurrency.
#: These are the floor, not the ceiling; raise them when the deployment can
#: afford it.
ARGON2_MEMORY_COST_KIB: Final = 64 * 1024
ARGON2_TIME_COST: Final = 2
ARGON2_PARALLELISM: Final = 1
ARGON2_HASH_LENGTH: Final = 32
ARGON2_SALT_LENGTH: Final = 16

#: One shared hasher. ``PasswordHasher`` is stateless — it holds parameters,
#: not salts — so a single instance is both correct and cheaper than building
#: one per call.
_PASSWORD_HASHER: Final = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST_KIB,
    parallelism=ARGON2_PARALLELISM,
    hash_len=ARGON2_HASH_LENGTH,
    salt_len=ARGON2_SALT_LENGTH,
    type=Type.ID,
)


def hash_password(password: str) -> str:
    """Hash ``password`` with Argon2id and a fresh random salt.

    A new 16-byte salt is generated by ``argon2-cffi`` for every call, from
    the OS CSPRNG. Two calls with the same input therefore produce different
    strings, which is what stops a database of hashes from revealing which
    accounts share a password.

    The returned string is a PHC-format digest of roughly 97 characters. That
    is why ``users.password_hash`` is ``VARCHAR(255)`` (PMP §14 gives no
    length; Phase 2 Step 1 chose 255 to hold an Argon2 PHC string with room to
    spare as the parameters above are raised).

    Raises:
        argon2.exceptions.InvalidHashError: never for a valid password; the
            library only rejects invalid *parameters*.
    """
    if not password:
        # Hashing "" would produce a valid hash that verifies against "". It
        # is never a legitimate password, and refusing here keeps a validation
        # bug from creating an account that authenticates with an empty
        # string. Length/strength rules belong at the request boundary.
        raise ValueError("password must not be empty")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return ``True`` only when ``password`` matches ``password_hash``.

    **Every** failure mode returns ``False``: a wrong password, an empty
    password, a hash that is not a valid PHC string at all, and a hash whose
    parameters are unusable. Callers cannot distinguish them, which is
    deliberate — a verifier that says "this hash is malformed" is an oracle for
    probing what is in the database.

    Note what this costs. Argon2 is deliberately slow and memory-hungry, so
    this function must only ever be reached *after* the account has been
    located. Using it as the first step of a login flow turns the login endpoint
    into a free CPU-and-memory exhaustion primitive for an unauthenticated
    caller; rate limiting is a later step and is the durable answer, but the
    call ordering in :func:`app.services.auth.authenticate` is the cheap part of
    it.
    """
    if not password or not password_hash:
        return False
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --------------------------------------------------------------------------
# JWT claims and token types (PMP §17)
# --------------------------------------------------------------------------


class TokenType(StrEnum):
    """The ``type`` claim: what a token is *for*.

    Having the type inside the signed payload is what makes token confusion
    impossible rather than merely unlikely. Without it, a refresh token is a
    perfectly valid bearer credential at every endpoint that accepts a signed
    token, and the only thing standing between an attacker and indefinite
    access is each endpoint remembering to check something the token does not
    say. Both :func:`decode_token` and :func:`create_access_token` therefore
    require the expected type explicitly, and a mismatch is a hard failure.
    """

    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """A verified token's claims, after signature and expiry have been checked.

    Constructing one of these means the signature verified against the
    configured key and algorithm and ``exp`` is in the future. It does *not*
    mean the subject still exists, is still active, or still holds ``role`` —
    that is database state, and :mod:`app.services.auth` is what checks it.
    Keeping those two questions separate is what lets a token be a
    self-contained proof of authentication without becoming a cache of
    authorization state that silently goes stale.
    """

    subject: uuid.UUID
    tenant_id: uuid.UUID
    type: TokenType
    issued_at: datetime
    expires_at: datetime
    #: Present on refresh tokens only. A 256-bit random identifier, and the
    #: reason two refresh tokens minted in the same second for the same user
    #: are still distinct strings.
    jti: str | None = None
    #: Present on access tokens only. Left as the raw string; converting it to
    #: :class:`~app.models.user.UserRole` is
    #: :mod:`app.core.identity`'s job, so that a role can never be a free-text
    #: string at the authentication boundary.
    role: str | None = None


class TokenValidationError(ValueError):
    """A token was rejected.

    One exception for every rejection — bad signature, wrong type, expired,
    malformed, missing claim, unparseable UUID — on purpose. The reason is
    observable by a caller and gets logged; it is never returned to the client.
    Callers in the HTTP layer translate this into a single undifferentiated
    ``401`` (see :func:`app.api.deps.get_current_identity`), because telling a
    caller *why* a token failed tells an attacker whether their forgery is
    getting warmer.

    Subclasses :class:`ValueError` so that it cannot be confused with a
    programming error such as a wrong argument type.
    """


#: Claims that every token of the given type must carry. ``role`` is excluded
#: for refresh tokens and ``jti`` for access tokens.
_REQUIRED_CLAIMS: Final[dict[TokenType, tuple[str, ...]]] = {
    TokenType.ACCESS: ("sub", "tenant_id", "role", "type", "iat", "exp"),
    TokenType.REFRESH: ("sub", "tenant_id", "type", "iat", "exp", "jti"),
}


def _resolve_settings(app_settings: Settings | None) -> Settings:
    """Return the settings to use, defaulting to the process-wide instance.

    Mirrors :func:`app.db.engine.resolve_dsn`: every function that needs
    configuration takes an optional instance so tests can pass an isolated one
    instead of mutating process state.
    """
    return settings if app_settings is None else app_settings


def _now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime, to whole seconds.

    Truncating to the second is not cosmetic — it is required for correctness.
    A JWT NumericDate (RFC 7519 §2) is an integer number of seconds since the
    epoch, so encoding ``now + 7 days`` truncates the microseconds away. If this
    function returned a microsecond-resolution value, the ``expires_at`` handed
    back by :func:`create_refresh_token` — which the caller writes straight into
    ``refresh_tokens.expires_at`` — would differ from the token's own ``exp`` by
    up to 999 ms. Two sources of truth for one expiry is exactly the kind of
    drift that later shows up as a token that expires before the database thinks
    it does. Truncating here makes the two the same value by construction.
    """
    return datetime.now(UTC).replace(microsecond=0)


def create_access_token(
    *,
    subject: uuid.UUID,
    tenant_id: uuid.UUID,
    role: str,
    app_settings: Settings | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a signed, expiring access token.

    Args:
        subject: the user id, placed in ``sub``.
        tenant_id: the authenticated tenant, placed in ``tenant_id``.
        role: the user's current role, placed in ``role``. Passed as a string
            so this module stays independent of the ORM; callers holding a
            :class:`~app.models.user.UserRole` may pass it directly because it
            is a ``StrEnum``.
        app_settings: settings override, for tests.
        expires_delta: override the lifetime. Present so a test can produce an
            already-expired token without freezing the clock. Production code
            must leave it unset and take
            :attr:`Settings.access_token_expire_minutes`; nothing else should
            be able to choose an access token's lifetime.

    Returns:
        The compact-serialization JWS, as a string.
    """
    config = _resolve_settings(app_settings)
    lifetime = (
        timedelta(minutes=config.access_token_expire_minutes)
        if expires_delta is None
        else expires_delta
    )
    now = _now()
    payload: dict[str, Any] = {
        "sub": str(subject),
        "tenant_id": str(tenant_id),
        "role": str(role),
        "type": TokenType.ACCESS.value,
        "iat": now,
        "exp": now + lifetime,
    }
    return jwt.encode(
        payload,
        config.jwt_signing_key(),
        algorithm=config.jwt_algorithm,
    )


def create_refresh_token(
    *,
    subject: uuid.UUID,
    tenant_id: uuid.UUID,
    app_settings: Settings | None = None,
    expires_delta: timedelta | None = None,
) -> tuple[str, str, datetime]:
    """Create a signed refresh token and return ``(token, jti, expires_at)``.

    The ``jti`` is a fresh :func:`secrets.token_urlsafe` value, not a UUID and
    not a counter. It matters for more than uniqueness: HS256 is a *signature*
    over the claims, so a refresh token built only from ``sub``/``tenant_id``
    and a second-resolution ``iat`` would be **byte-identical** for two refreshes
    of the same account in the same second. That would collapse two
    independently-issued credentials into one, making per-token revocation
    meaningless. 256 bits of randomness in a signed payload removes the
    problem at the source.

    Returns:
        A 3-tuple of the encoded token, its ``jti``, and the absolute UTC
        expiry. The expiry is returned as well as embedded so the caller can
        store it in ``refresh_tokens.expires_at`` without re-deriving it and
        risking a mismatch with the token itself.
    """
    config = _resolve_settings(app_settings)
    lifetime = (
        timedelta(days=config.refresh_token_expire_days)
        if expires_delta is None
        else expires_delta
    )
    now = _now()
    expires_at = now + lifetime
    jti = secrets.token_urlsafe(32)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "tenant_id": str(tenant_id),
        "type": TokenType.REFRESH.value,
        "jti": jti,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(
        payload,
        config.jwt_signing_key(),
        algorithm=config.jwt_algorithm,
    )
    return token, jti, expires_at


def decode_token(
    token: str,
    *,
    expected_type: TokenType,
    app_settings: Settings | None = None,
) -> TokenClaims:
    """Verify ``token`` and return its claims.

    Every check a caller could otherwise forget is made here, and each failure
    raises :class:`TokenValidationError`:

    * **Signature**, using the configured key.
    * **Algorithm.** The allowed-algorithm list is
      :attr:`Settings.jwt_algorithm` — server configuration, never the token's
      own ``alg`` header. This single argument is what rejects a forged
      ``alg: none`` token and a ``RS256``/``HS256`` confusion attempt.
    * **Type**, against ``expected_type``, so an access token cannot be
      presented where a refresh token is required or vice versa.
    * **Expiration**, with zero leeway. No clock-skew allowance is configured
      because none is required by the PMP, and an unconfigured tolerance is
      one more thing to get wrong in the permissive direction.
    * **Presence** of every claim the token type requires.
    * **Shape** of the UUID and timestamp claims.

    Signature verification happens before the type check and PyJWT verifies it
    before reading the payload, so nothing in an unverified token is ever
    interpreted.
    """
    config = _resolve_settings(app_settings)
    required = _REQUIRED_CLAIMS[expected_type]

    try:
        raw = jwt.decode(
            token,
            config.jwt_signing_key(),
            # Server-side allowlist. Never `algorithms=None`, and never
            # anything derived from the token itself.
            algorithms=[config.jwt_algorithm],
            options={
                # Required claims are checked explicitly. PyJWT verifies
                # `exp` on its own when present, but does not require it, and
                # a token with no `exp` would otherwise never expire.
                "require": list(required),
                # `verify_signature` is left on. It is listed for the reader:
                # turning it off is the single change that would make this
                # function accept anything.
                "verify_signature": True,
                "verify_exp": True,
            },
        )
    except PyJWTInvalidTokenError as exc:
        # PyJWT's exception hierarchy carries the detail
        # (`ExpiredSignatureError`, `InvalidSignatureError`,
        # `MissingRequiredClaimError`, `InvalidJTIError`, `InvalidAlgorithmError`,
        # ...). The type name goes in the message for operators; the client never
        # sees it.
        #
        # The *expected* type is named too, because without it the log line for
        # "an access token was presented to the refresh endpoint" reads as a
        # generic decoding failure — the reader would not guess that the caller
        # used the wrong endpoint. Naming the expectation is diagnostic, not
        # disclosive: both the token type and the endpoint are known to whoever
        # is reading the log, and this message never reaches an HTTP client.
        raise TokenValidationError(
            f"token rejected while decoding as a {expected_type.value} token: "
            f"{type(exc).__name__}"
        ) from exc

    return _claims_from_payload(raw, expected_type=expected_type)


def _claims_from_payload(payload: dict[str, Any], *, expected_type: TokenType) -> TokenClaims:
    """Convert a PyJWT-verified payload into a typed :class:`TokenClaims`.

    Split out from :func:`decode_token` because the type check belongs here:
    it has to compare against ``expected_type`` rather than against whatever
    the token claims to be, and it needs the claim-presence rules that the
    successful decode already established.

    Raises:
        TokenValidationError: if any claim is absent, of the wrong JSON type, or
            not a valid UUID / timestamp. A token that decodes cleanly but
            carries ``"tenant_id": "not-a-uuid"`` is still not an identity.
    """
    declared_type = payload.get("type")
    if declared_type != expected_type.value:
        raise TokenValidationError(
            f"expected a {expected_type.value} token but the token declares "
            f"{declared_type!r}"
        )

    try:
        subject = _as_uuid(payload, "sub")
        tenant_id = _as_uuid(payload, "tenant_id")
        issued_at = _as_datetime(payload, "iat")
        expires_at = _as_datetime(payload, "exp")
    except TokenValidationError:
        raise
    except Exception as exc:
        # A backstop. `_as_uuid` and `_as_datetime` are written to convert every
        # failure into `TokenValidationError` themselves, so this should be
        # unreachable; it exists so that a future edit to those helpers cannot
        # turn a malformed claim into an unhandled `ValueError` escaping to an
        # HTTP layer that does not know what to do with it. Left unmarked so that
        # coverage keeps reporting it if it is ever reached.
        raise TokenValidationError("token claim is malformed") from exc

    jti = payload.get("jti")
    if jti is not None and not isinstance(jti, str):
        raise TokenValidationError("token claim 'jti' must be a string")

    role = payload.get("role")
    if role is not None and not isinstance(role, str):
        raise TokenValidationError("token claim 'role' must be a string")

    return TokenClaims(
        subject=subject,
        tenant_id=tenant_id,
        type=expected_type,
        issued_at=issued_at,
        expires_at=expires_at,
        jti=jti,
        role=role,
    )


def _as_uuid(payload: dict[str, Any], claim: str) -> uuid.UUID:
    """Return ``payload[claim]`` parsed as a UUID.

    PyJWT does not validate claim *shapes* — only that the claims it knows
    about are well-formed. Without this, a token carrying
    ``"tenant_id": "acme-corp"`` would decode successfully and hand a string
    to code that believes it holds a tenant identifier.
    """
    value = payload.get(claim)
    if not isinstance(value, str):
        raise TokenValidationError(
            f"token claim {claim!r} must be a string UUID, got "
            f"{type(value).__name__}"
        )
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise TokenValidationError(
            f"token claim {claim!r} is not a valid UUID"
        ) from exc


def _as_datetime(payload: dict[str, Any], claim: str) -> datetime:
    """Return ``payload[claim]`` as a timezone-aware UTC ``datetime``.

    A JWT NumericDate is a number of seconds since the epoch, and PyJWT verifies
    ``exp`` numerically but **hands it back as an ``int``** — it does not convert
    it. So a decoded payload has integer timestamps, and converting them here is
    this module's job.

    Both spellings are accepted because that is genuinely what is valid on the
    wire: RFC 7519 §2 defines NumericDate as a number, but a non-compliant
    encoder may send an ISO-8601 string, and rejecting one would be a needless
    interoperability failure while accepting it costs nothing.

    The result is always timezone-aware. Returning a naive value would push a
    ``TypeError`` into whatever caller did arithmetic on it, since comparing a
    naive and an aware ``datetime`` raises rather than assuming UTC.
    """
    value = payload.get(claim)

    if isinstance(value, bool):
        # bool is an int subclass, so `True` would otherwise become 1970-01-02.
        raise TokenValidationError(
            f"token claim {claim!r} must be a timestamp, got bool"
        )

    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise TokenValidationError(
                f"token claim {claim!r} is not a representable timestamp"
            ) from exc

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    raise TokenValidationError(
        f"token claim {claim!r} must be a timestamp, got {type(value).__name__}"
    )


# --------------------------------------------------------------------------
# Refresh-token fingerprinting (PMP §14 refresh_tokens.token_hash)
# --------------------------------------------------------------------------


def hash_refresh_token(token: str) -> str:
    """Return the hex SHA-256 digest of a raw refresh token.

    The raw token is never persisted (PMP §14 stores ``token_hash``, not the
    token), so a database disclosure does not hand an attacker working
    credentials. The digest is compared with :func:`hmac.compare_digest` — see
    :func:`app.services.auth.consume_refresh_token`.

    **Why SHA-256 and not Argon2**, given that passwords use Argon2? Because
    the two protect against different attacks. A password is low-entropy and
    chosen by a human, so it needs a deliberately expensive hash to make
    offline guessing costly. A refresh token here is :func:`create_refresh_token`
    output, which carries 256 bits of :mod:`secrets` randomness — it is not
    guessable at any price. Making verification of a high-entropy value cost
    64 MiB and tens of milliseconds would add that cost to every single token
    refresh for no security gain. This is the reasoning behind OAuth 2.0
    Security BCP §4.14: an at-rest copy of a refresh token is acceptable when
    the token itself is high-entropy.

    The assumption is enforced rather than merely documented:
    ``tests/test_security_refresh_tokens.py`` proves that tokens minted in the
    same second differ, which is what makes the digest non-reversible in
    practice.
    """
    if not token:
        raise ValueError("token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_refresh_token(token: str, token_hash: str) -> bool:
    """Constant-time comparison of a raw refresh token against its digest."""
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_refresh_token(token), token_hash)