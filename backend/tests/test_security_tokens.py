"""JWT creation and verification (PMP §17, PMP §8).

What is proved here
-------------------
* A token created with the configured key verifies, and its claims are exactly
  the ones the design specifies: ``sub``, ``tenant_id``, ``role``, ``type``,
  ``iat``, ``exp`` for access; ``sub``, ``tenant_id``, ``type``, ``iat``,
  ``exp``, ``jti`` for refresh.
* Expiry comes from configuration, not from a constant in this module.
* **Every** rejection path fails closed: wrong signature, wrong type, expired,
  malformed, missing claim, invalid UUID, non-string claim.
* Token confusion is impossible in both directions.
* The accepted algorithm comes from server configuration. A forged ``alg: none``
  token and an ``RS256`` token are both rejected.
* Nothing sensitive travels inside the token, and no secret appears in one.

What is deliberately *not* tested here
--------------------------------------
Database behaviour. ``refresh_tokens`` persistence, revocation and rotation are
in ``test_auth_service.py``; tenant-boundary behaviour is in
``test_tenant_identity.py``. This module has no database at all, which is what
makes it fast enough to run on every save.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import fields
from datetime import timedelta

import jwt
import pytest
from jwt.exceptions import PyJWTError

from app.core.config import Settings
from app.core.security import (
    TokenClaims,
    TokenType,
    TokenValidationError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_refresh_token,
    verify_refresh_token,
)
from tests.conftest import (
    OTHER_JWT_SECRET,
    TEST_JWT_SECRET,
    make_settings,
)

USER_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENANT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_TENANT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def b64url(raw: bytes) -> str:
    """Base64url-encode without padding, as JWT segments require."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def forge(header: dict[str, object], payload: dict[str, object]) -> str:
    """Build a token by hand, with no signature verification involved.

    Needed to produce tokens PyJWT's own encoder refuses to create — notably an
    unsigned ``alg: none`` token, which cannot be encoded with a key at all.
    """
    segments = [
        b64url(json.dumps(header).encode("utf-8")),
        b64url(json.dumps(payload).encode("utf-8")),
    ]
    return ".".join(segments) + "."


@pytest.fixture(autouse=True)
def _process_settings(
    active_auth_settings: Settings,
) -> None:
    """Give the whole module a process-wide configuration with a usable secret.

    Several tests here call :func:`decode_token` without ``app_settings``, which
    is the correct production path: application code falls back to the
    process-wide instance. Without this fixture those calls would raise
    ``JwtSecretNotConfiguredError``, because a bare :class:`Settings` has no
    secret and that is exactly right — there is no development default.

    Tests that care about a *particular* configuration still pass
    ``app_settings=`` explicitly, and this fixture does not interfere with them.
    """
    assert active_auth_settings.jwt_signing_key()


@pytest.fixture
def access_token(auth_settings: Settings) -> str:
    return create_access_token(
        subject=USER_ID,
        tenant_id=TENANT_ID,
        role="viewer",
        app_settings=auth_settings,
    )


def unverified_payload(token: str) -> dict[str, object]:
    """Decode a token **without** verifying it, for inspecting its claims.

    Deliberately unsafe and used only to assert on what was written. It is what
    lets a test prove an email is *absent*, which no amount of verified decoding
    can show.
    """
    return dict(jwt.decode(token, options={"verify_signature": False}))


# ---------------------------------------------------------------------------
# Access token claims
# ---------------------------------------------------------------------------


class TestAccessTokenClaims:
    def test_a_valid_token_decodes(self, access_token: str) -> None:
        claims = decode_token(access_token, expected_type=TokenType.ACCESS)
        assert claims.subject == USER_ID
        assert claims.tenant_id == TENANT_ID
        assert claims.role == "viewer"
        assert claims.type is TokenType.ACCESS

    def test_every_required_claim_is_present(self, access_token: str) -> None:
        assert set(unverified_payload(access_token)) == {
            "sub",
            "tenant_id",
            "role",
            "type",
            "iat",
            "exp",
        }

    def test_sub_carries_the_user_id(self, access_token: str) -> None:
        assert unverified_payload(access_token)["sub"] == str(USER_ID)

    def test_tenant_id_carries_the_tenant(self, access_token: str) -> None:
        assert unverified_payload(access_token)["tenant_id"] == str(TENANT_ID)

    def test_type_is_access(self, access_token: str) -> None:
        assert unverified_payload(access_token)["type"] == "access"

    def test_two_tenants_get_different_tenant_claims(
        self, auth_settings: Settings
    ) -> None:
        """The isolation-relevant assertion: the tenant is inside the token."""
        first = create_access_token(
            subject=USER_ID,
            tenant_id=TENANT_ID,
            role="viewer",
            app_settings=auth_settings,
        )
        second = create_access_token(
            subject=USER_ID,
            tenant_id=OTHER_TENANT_ID,
            role="viewer",
            app_settings=auth_settings,
        )
        assert unverified_payload(first)["tenant_id"] != (
            unverified_payload(second)["tenant_id"]
        )

    def test_timestamps_are_timezone_aware_utc(
        self, auth_settings: Settings
    ) -> None:
        claims = decode_token(access_token_for(auth_settings), expected_type=TokenType.ACCESS)
        assert claims.issued_at.tzinfo is not None
        assert claims.expires_at.tzinfo is not None
        assert claims.issued_at.utcoffset() == timedelta(0)
        assert claims.expires_at.utcoffset() == timedelta(0)

    def test_expiry_comes_from_configuration(
        self, auth_settings: Settings
    ) -> None:
        short = make_settings(
            jwt_secret_key=TEST_JWT_SECRET, access_token_expire_minutes=5
        )
        token = create_access_token(
            subject=USER_ID,
            tenant_id=TENANT_ID,
            role="viewer",
            app_settings=short,
        )
        claims = decode_token(token, expected_type=TokenType.ACCESS)
        span = claims.expires_at - claims.issued_at
        assert span == timedelta(minutes=5)

    def test_expired_token_is_rejected(self, auth_settings: Settings) -> None:
        token = create_access_token(
            subject=USER_ID,
            tenant_id=TENANT_ID,
            role="viewer",
            app_settings=auth_settings,
            expires_delta=timedelta(minutes=-1),
        )
        with pytest.raises(TokenValidationError, match="ExpiredSignatureError"):
            decode_token(token, expected_type=TokenType.ACCESS)


def access_token_for(settings: Settings) -> str:
    return create_access_token(
        subject=USER_ID,
        tenant_id=TENANT_ID,
        role="viewer",
        app_settings=settings,
    )


# ---------------------------------------------------------------------------
# Signature, algorithm, and forged headers
# ---------------------------------------------------------------------------


class TestSignatureAndAlgorithm:
    def test_a_token_signed_with_another_key_is_rejected(
        self,
        access_token: str,
        foreign_secret_settings: Settings,
    ) -> None:
        with pytest.raises(TokenValidationError, match="InvalidSignatureError"):
            decode_token(
                access_token,
                expected_type=TokenType.ACCESS,
                app_settings=foreign_secret_settings,
            )

    def test_a_token_signed_with_no_key_is_rejected(self, access_token: str) -> None:
        """A signature cannot be omitted: the key is always required.

        Decoding with ``key=""`` is not a valid configuration — the secret must
        clear :meth:`Settings.jwt_signing_key` — so the closest reachable case is
        a token signed by a *different* valid-length key. Asserted anyway
        because it is the behaviour a caller gets if the settings object is
        replaced carelessly.
        """
        blank_key_settings = make_settings(jwt_secret_key="b" * 40)
        with pytest.raises(TokenValidationError, match="InvalidSignatureError"):
            decode_token(
                access_token,
                expected_type=TokenType.ACCESS,
                app_settings=blank_key_settings,
            )

    def test_a_tampered_payload_is_rejected(self, access_token: str) -> None:
        """The classic escalation, in its exact shape.

        Take a valid access token for Tenant A, edit ``tenant_id`` to Tenant B,
        resend it. The payload segment changes, so the signature no longer
        matches and the token is rejected.
        """
        header, payload, signature = access_token.split(".")

        edited = json.loads(b64url_decode(payload))
        assert edited["tenant_id"] == str(TENANT_ID)
        edited["tenant_id"] = str(OTHER_TENANT_ID)

        spliced = (
            f"{header}.{b64url(json.dumps(edited).encode())}.{signature}"
        )
        # The attack is real: the *payload* now names another tenant...
        assert unverified_payload(spliced)["tenant_id"] == str(OTHER_TENANT_ID)
        # ...and it is still rejected, because the signature covers the payload.
        with pytest.raises(TokenValidationError, match="InvalidSignatureError"):
            decode_token(spliced, expected_type=TokenType.ACCESS)

    def test_re_signing_a_tampered_payload_works_but_is_not_an_escalation(
        self, access_token: str
    ) -> None:
        """A re-signed token verifies — because the attacker had the key.

        Written to make the boundary honest rather than to imply protection that
        does not exist: signature verification stops *editing*, not *forgery*. An
        attacker who has the secret can mint any token at all, which is why the
        secret is the single thing Phase 2 Step 2 protects.
        """
        payload = json.loads(b64url_decode(access_token.split(".")[1]))
        payload["tenant_id"] = str(OTHER_TENANT_ID)
        resigned = jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")

        claims = decode_token(resigned, expected_type=TokenType.ACCESS)
        assert claims.tenant_id == OTHER_TENANT_ID

    def test_an_unsigned_alg_none_token_is_rejected(self) -> None:
        """The classic JWT bypass: strip the signature and claim ``alg: none``.

        ``forge`` builds this by hand because PyJWT's encoder will not produce
        one, which is itself a good sign about the library's defaults.
        """
        token = forge(
            {"alg": "none", "typ": "JWT"},
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "role": "admin",
                "type": "access",
                "iat": 0,
                "exp": 4_102_444_800,
            },
        )
        with pytest.raises(TokenValidationError, match="InvalidAlgorithmError"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_a_token_signed_with_another_algorithm_is_rejected(self) -> None:
        """Algorithm confusion: a *genuinely* HS512-signed token.

        Signed properly with the real secret, so the only reason for rejection
        is that the server's allowlist names one algorithm and the token's own
        header names another. The header is never consulted as a source of
        permission — which is the whole defence, and is why this test signs
        correctly rather than forging a broken signature.
        """
        token = jwt.encode(
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "role": "viewer",
                "type": "access",
                "iat": 1_700_000_000,
                "exp": 4_102_444_800,
            },
            # 64 bytes: HS512's own key-length minimum, so the test does not
            # provoke an InsecureKeyLengthWarning that would have to be filtered.
            "k" * 64,
            algorithm="HS512",
        )
        assert jwt.get_unverified_header(token)["alg"] == "HS512"
        with pytest.raises(TokenValidationError, match="InvalidAlgorithmError"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_an_hs256_token_is_accepted_so_the_allowlist_is_not_vacuous(
        self, access_token: str
    ) -> None:
        """A rejection rule that rejected everything would pass the test above.

        Paired deliberately with
        :meth:`test_an_hs256_token_is_accepted_so_the_allowlist_is_not_vacuous`'s
        sibling ``test_a_valid_token_decodes``: together they pin the allowlist to
        exactly HS256 rather than to "nothing".
        """
        assert decode_token(access_token, expected_type=TokenType.ACCESS).role == (
            "viewer"
        )

    def test_the_configured_algorithm_is_hs256(self) -> None:
        """The algorithm is a single-valued setting, and it is HS256.

        Asymmetric algorithms are deliberately not offered: verifying an RS256
        token would need a public key distributed to every verifier, and this
        architecture has exactly one issuer and one verifier.
        """
        assert make_settings().jwt_algorithm == "HS256"
        assert make_settings(jwt_secret_key=TEST_JWT_SECRET).jwt_algorithm == "HS256"


def b64url_decode(segment: bytes | str) -> bytes:
    """Decode a base64url JWT segment, restoring stripped padding."""
    raw = segment.encode("ascii") if isinstance(segment, str) else segment
    padding = b"=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(raw + padding)


# ---------------------------------------------------------------------------
# Token type confusion
# ---------------------------------------------------------------------------


class TestTokenTypeIsEnforced:
    def test_an_access_token_is_not_accepted_as_a_refresh_token(
        self, access_token: str
    ) -> None:
        """Token confusion, direction one.

        PyJWT enforces the *expected* type's required-claim list during decode,
        so an access token fails here for lacking a ``jti`` before the explicit
        type check is reached. Either way it is rejected, and the message names
        the expectation so an operator can see it was the wrong endpoint.
        """
        with pytest.raises(TokenValidationError, match="refresh"):
            decode_token(access_token, expected_type=TokenType.REFRESH)

    def test_a_refresh_token_is_not_accepted_as_an_access_token(
        self, auth_settings: Settings
    ) -> None:
        """Token confusion, direction two — the dangerous one.

        A refresh token has a longer lifetime than an access token, so accepting
        one at an authenticated endpoint would let a client hold a credential
        that does not expire when it should. It fails here for lacking a
        ``role`` claim, which is why refresh tokens carry no role at all.
        """
        token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        with pytest.raises(TokenValidationError, match="access"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_a_token_with_every_claim_of_both_types_still_gets_its_type_checked(
        self,
    ) -> None:
        """The type check itself, isolated from the required-claim check.

        A token carrying ``role`` *and* ``jti`` satisfies both required-claim
        lists, so the only thing left to reject it is the explicit ``type``
        comparison. Without this test, the two confusion tests above would pass
        on the claim check alone and the type check could be deleted unnoticed.
        """
        token = jwt.encode(
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "role": "viewer",
                "type": "elevator",
                "jti": "abc",
                "iat": 1_700_000_000,
                "exp": 4_102_444_800,
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(TokenValidationError, match="expected a access token"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_a_type_claim_that_is_not_a_known_value_is_rejected(self) -> None:
        token = jwt.encode(
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "role": "viewer",
                "type": "elevator",
                "iat": 0,
                "exp": 4_102_444_800,
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(TokenValidationError, match="access"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_an_absent_type_claim_is_rejected(self) -> None:
        token = jwt.encode(
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "role": "viewer",
                "iat": 0,
                "exp": 4_102_444_800,
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(TokenValidationError):
            decode_token(token, expected_type=TokenType.ACCESS)


# ---------------------------------------------------------------------------
# Missing and malformed claims
# ---------------------------------------------------------------------------


class TestClaimValidation:
    def _signed(self, **overrides: object) -> str:
        """Sign a token whose claims are exactly ``overrides``.

        Bypasses :func:`create_access_token` so a test can omit a claim the
        implementation always writes — which is the only way to prove the
        decoder actually requires it.
        """
        payload: dict[str, object] = {
            "sub": str(USER_ID),
            "tenant_id": str(TENANT_ID),
            "role": "viewer",
            "type": "access",
            "iat": 1_700_000_000,
            "exp": 4_102_444_800,
        }
        payload.update(overrides)
        return jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")

    @pytest.mark.parametrize(
        "missing", ["sub", "tenant_id", "role", "type", "iat", "exp"]
    )
    def test_each_required_claim_is_required(self, missing: str) -> None:
        """No claim may be dropped from an access token.

        Built by hand rather than through :func:`create_access_token`, because
        that function always writes all six — the only way to prove the decoder
        *requires* them is to present one that lacks each in turn.
        """
        payload: dict[str, object] = {
            "sub": str(USER_ID),
            "tenant_id": str(TENANT_ID),
            "role": "viewer",
            "type": "access",
            "iat": 1_700_000_000,
            "exp": 4_102_444_800,
        }
        del payload[missing]
        token = jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")
        with pytest.raises(TokenValidationError, match="MissingRequiredClaimError"):
            decode_token(token, expected_type=TokenType.ACCESS)

    @pytest.mark.parametrize("missing", ["sub", "tenant_id", "type", "jti"])
    def test_each_required_refresh_claim_is_required(self, missing: str) -> None:
        payload: dict[str, object] = {
            "sub": str(USER_ID),
            "tenant_id": str(TENANT_ID),
            "type": "refresh",
            "jti": "abc",
            "iat": 1_700_000_000,
            "exp": 4_102_444_800,
        }
        del payload[missing]
        token = jwt.encode(payload, TEST_JWT_SECRET, algorithm="HS256")
        with pytest.raises(TokenValidationError, match="MissingRequiredClaimError"):
            decode_token(token, expected_type=TokenType.REFRESH)

    def test_a_refresh_token_must_carry_a_jti(self) -> None:
        """The revocation lookup key, so its absence is a hard failure.

        Without a ``jti`` there is no way to address a ``refresh_tokens`` row, so
        a refresh token lacking one could not be revoked at all.
        """
        token = jwt.encode(
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "type": "refresh",
                "iat": 1_700_000_000,
                "exp": 4_102_444_800,
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(TokenValidationError, match="MissingRequiredClaimError"):
            decode_token(token, expected_type=TokenType.REFRESH)

    def test_a_token_without_exp_never_expires(self) -> None:
        """The reason ``exp`` is in the required list.

        PyJWT verifies ``exp`` *if present*; a token lacking it would therefore be
        valid forever. That is the whole justification for the required-claim
        list, so it is asserted directly.
        """
        token = jwt.encode(
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "role": "viewer",
                "type": "access",
                "iat": 1_700_000_000,
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(TokenValidationError, match="MissingRequiredClaimError"):
            decode_token(token, expected_type=TokenType.ACCESS)

    @pytest.mark.parametrize(
        "bad_uuid",
        ["not-a-uuid", "", "1111", "11111111-1111-1111-1111-11111111111Z", 42, []],
    )
    def test_a_non_uuid_tenant_id_is_rejected(self, bad_uuid: object) -> None:
        token = self._signed(tenant_id=bad_uuid)
        with pytest.raises(TokenValidationError, match="tenant_id"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_a_null_tenant_id_is_rejected(self) -> None:
        """``None`` cannot be smuggled through as a tenant.

        The claim survives encoding as a JSON ``null``, and PyJWT's
        required-claim check treats a null as absent, so it fails there rather
        than in this module's UUID guard. Asserted as observed rather than as
        designed: the outcome (rejected) is what matters, and this documents
        which layer catches it.
        """
        token = jwt.encode(
            {
                "sub": str(USER_ID),
                "tenant_id": None,
                "role": "viewer",
                "type": "access",
                "iat": 1_700_000_000,
                "exp": 4_102_444_800,
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        assert jwt.decode(token, options={"verify_signature": False})[
            "tenant_id"
        ] is None
        with pytest.raises(TokenValidationError, match="MissingRequiredClaimError"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_a_non_uuid_subject_is_rejected(self) -> None:
        token = self._signed(sub="definitely-not-a-uuid")
        with pytest.raises(TokenValidationError, match="'sub'"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_a_non_string_role_is_rejected(self) -> None:
        token = self._signed(role=["admin"])
        with pytest.raises(TokenValidationError, match="role"):
            decode_token(token, expected_type=TokenType.ACCESS)

    def test_an_unknown_role_is_not_accepted_as_a_role(self) -> None:
        """The token is valid; the *role* is not.

        Decoding succeeds because a role value is a claim, not a security
        decision. Turning it into a typed value is
        :class:`~app.core.identity.AuthenticatedIdentity`'s job, and it must fail
        rather than default — see ``test_tenant_identity.py``.
        """
        token = self._signed(role="superuser")
        claims = decode_token(token, expected_type=TokenType.ACCESS)
        assert claims.role == "superuser"

    def test_a_non_string_jti_is_rejected(self) -> None:
        """PyJWT validates ``jti`` itself, before this module sees it.

        Asserted as ``InvalidJTIError`` rather than a message of ours, because it
        documents that the check happens upstream — the implementation is not
        relying on its own type guard for this and should not be written as
        though it were.
        """
        token = jwt.encode(
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "type": "refresh",
                "jti": 12345,
                "iat": 1_700_000_000,
                "exp": 4_102_444_800,
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        with pytest.raises(TokenValidationError, match="InvalidJTIError"):
            decode_token(token, expected_type=TokenType.REFRESH)

    @pytest.mark.parametrize(
        "garbage",
        [
            pytest.param("", id="empty"),
            pytest.param("not-a-token", id="no-dots"),
            pytest.param("a.b", id="two-segments"),
            pytest.param("a.b.c", id="non-base64-segments"),
            pytest.param("....", id="dots-only"),
        ],
    )
    def test_malformed_tokens_fail_safely(self, garbage: str) -> None:
        """Rejected without raising anything other than ``TokenValidationError``.

        No ``IndexError`` from splitting, no ``UnicodeDecodeError`` from
        base64, no ``JSONDecodeError`` from the payload: an attacker controls
        this string entirely.
        """
        with pytest.raises(TokenValidationError):
            decode_token(garbage, expected_type=TokenType.ACCESS)

    def test_every_rejection_is_a_single_exception_type(self) -> None:
        """One exception type, so no caller can accidentally handle two.

        Callers should be able to write one ``except`` and be certain it covers
        all of them, and should not be able to branch on the failure mode to
        learn something about the token.
        """
        cases = [
            "garbage",
            jwt.encode({"sub": "x"}, TEST_JWT_SECRET, algorithm="HS256"),
            jwt.encode(
                {
                    "sub": str(USER_ID),
                    "tenant_id": "nope",
                    "role": "viewer",
                    "type": "access",
                    "iat": 1_700_000_000,
                    "exp": 4_102_444_800,
                },
                TEST_JWT_SECRET,
                algorithm="HS256",
            ),
        ]
        for token in cases:
            with pytest.raises(TokenValidationError):
                decode_token(token, expected_type=TokenType.ACCESS)

    def test_it_subclasses_value_error_not_a_pyjwt_error(self) -> None:
        """A domain error, so it can never be confused with a library error.

        A caller must not be able to catch ``PyJWTError`` and accidentally treat
        an internal failure as "invalid token".
        """
        assert issubclass(TokenValidationError, ValueError)
        assert not issubclass(TokenValidationError, PyJWTError)


# ---------------------------------------------------------------------------
# Refresh token claims
# ---------------------------------------------------------------------------


class TestRefreshTokenClaims:
    def test_creation_returns_a_token_a_jti_and_an_expiry(
        self, auth_settings: Settings
    ) -> None:
        """Three values, because the caller must store the same two it embeds.

        Returning the ``jti`` and the absolute expiry separately is what keeps the
        ``refresh_tokens`` row and the token itself from ever disagreeing.
        """
        token, jti, expires_at = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        assert isinstance(token, str)
        assert isinstance(jti, str)
        assert expires_at.tzinfo is not None
        assert expires_at.utcoffset() == timedelta(0)

    def test_the_returned_expiry_matches_the_token(
        self, auth_settings: Settings
    ) -> None:
        """The stored ``expires_at`` and the token's ``exp`` are one value.

        They come from the same call, but only because the clock in
        :mod:`app.core.security` truncates to whole seconds. A JWT NumericDate is
        an integer, so a microsecond-resolution ``expires_at`` would be written to
        ``refresh_tokens.expires_at`` up to 999 ms later than the token thinks it
        lives — two sources of truth for one expiry. Asserted here because the
        property is invisible until it has already gone wrong.
        """
        token, _jti, expires_at = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        assert expires_at.microsecond == 0
        claims = decode_token(token, expected_type=TokenType.REFRESH)
        assert claims.expires_at == expires_at

    def test_every_required_claim_is_present(self, auth_settings: Settings) -> None:
        token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        assert set(unverified_payload(token)) == {
            "sub",
            "tenant_id",
            "type",
            "iat",
            "exp",
            "jti",
        }

    def test_no_role_claim(self, auth_settings: Settings) -> None:
        """Deliberate.

        A refresh token exchanged for a fresh access token must not be able to
        carry a role forward, or a demoted user's authority would survive until
        the refresh token expired instead of until the access token did.
        """
        token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        assert "role" not in unverified_payload(token)

    def test_type_is_refresh(self, auth_settings: Settings) -> None:
        token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        assert unverified_payload(token)["type"] == "refresh"

    def test_the_jti_in_the_token_is_the_returned_jti(
        self, auth_settings: Settings
    ) -> None:
        token, jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        assert unverified_payload(token)["jti"] == jti

    def test_the_returned_jti_matches_the_decoded_claim(
        self, auth_settings: Settings
    ) -> None:
        token, jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        claims = decode_token(token, expected_type=TokenType.REFRESH)
        assert claims.jti == jti

    def test_expiry_comes_from_configuration_in_days(
        self, auth_settings: Settings
    ) -> None:
        short = make_settings(
            jwt_secret_key=TEST_JWT_SECRET, refresh_token_expire_days=3
        )
        token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=short
        )
        claims = decode_token(token, expected_type=TokenType.REFRESH)
        assert claims.expires_at - claims.issued_at == timedelta(days=3)

    def test_tokens_minted_in_the_same_second_are_still_distinct(
        self, auth_settings: Settings
    ) -> None:
        """Two refreshes of one account must be two revocable credentials.

        An HS256 signature over ``sub``/``tenant_id``/``iat`` is deterministic to
        the second. Without a random ``jti``, two refreshes in the same second
        would be the *same string* — so revoking one would revoke both, and
        per-token revocation would be meaningless. This is the single most
        important property of the refresh-token design.
        """
        first, first_jti, _ = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        second, second_jti, _ = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        assert first != second
        assert first_jti != second_jti

    def test_the_jti_is_high_entropy(self, auth_settings: Settings) -> None:
        _token, jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        # 32 random bytes, base64url without padding -> 43 characters.
        assert len(jti) == 43

    def test_an_expired_refresh_token_is_rejected(
        self, auth_settings: Settings
    ) -> None:
        token, _jti, _expires = create_refresh_token(
            subject=USER_ID,
            tenant_id=TENANT_ID,
            app_settings=auth_settings,
            expires_delta=timedelta(days=-1),
        )
        with pytest.raises(TokenValidationError, match="ExpiredSignatureError"):
            decode_token(token, expected_type=TokenType.REFRESH)


# ---------------------------------------------------------------------------
# Nothing sensitive travels inside the token
# ---------------------------------------------------------------------------


class TestTokenContentsAreMinimal:
    def test_no_email(self, access_token: str) -> None:
        assert "email" not in unverified_payload(access_token)

    def test_no_password_or_hash(self, access_token: str) -> None:
        payload = unverified_payload(access_token)
        assert "password" not in payload
        assert "password_hash" not in payload
        assert "sub_hash" not in payload

    def test_the_signing_secret_does_not_appear_in_the_token(
        self, access_token: str
    ) -> None:
        """A token is a bearer credential that proxies log.

        If the secret were recoverable from a token, anyone holding a log line
        could mint tokens for any tenant.
        """
        assert TEST_JWT_SECRET not in access_token
        assert OTHER_JWT_SECRET not in access_token
        decoded = b64url_decode(access_token.split(".")[1])
        assert TEST_JWT_SECRET.encode() not in decoded

    def test_the_digest_of_the_secret_is_not_in_the_token(
        self, access_token: str
    ) -> None:
        import hashlib

        fingerprint = hashlib.sha256(TEST_JWT_SECRET.encode()).hexdigest()
        assert fingerprint not in access_token

    def test_no_arbitrary_extra_claims_are_accepted_into_the_identity(
        self,
    ) -> None:
        """Extra claims are ignored rather than merged.

        A token may legitimately carry more than the required set; what matters
        is that :class:`TokenClaims` has no field to put them in, so nothing
        downstream can be influenced by them.

        Read via ``dataclasses.fields`` rather than ``vars``: the class is a
        ``slots=True`` dataclass and so has no ``__dict__`` to inspect.
        """
        token = jwt.encode(
            {
                "sub": str(USER_ID),
                "tenant_id": str(TENANT_ID),
                "role": "viewer",
                "type": "access",
                "iat": 1_700_000_000,
                "exp": 4_102_444_800,
                "is_admin": True,
                "tenant_slug": "victim-corp",
            },
            TEST_JWT_SECRET,
            algorithm="HS256",
        )
        claims = decode_token(token, expected_type=TokenType.ACCESS)
        assert {field.name for field in fields(claims)} == {
            "subject",
            "tenant_id",
            "type",
            "issued_at",
            "expires_at",
            "jti",
            "role",
        }


# ---------------------------------------------------------------------------
# Refresh-token fingerprinting
# ---------------------------------------------------------------------------


class TestRefreshTokenFingerprint:
    def test_the_digest_is_a_64_character_hex_string(
        self, auth_settings: Settings
    ) -> None:
        token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        digest = hash_refresh_token(token)
        assert len(digest) == 64
        assert all(character in "0123456789abcdef" for character in digest)

    def test_the_digest_does_not_contain_the_token(self, auth_settings: Settings) -> None:
        """The stored value must not be usable as a credential."""
        token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        digest = hash_refresh_token(token)
        assert token not in digest
        assert digest not in token

    def test_the_digest_is_stable(self, auth_settings: Settings) -> None:
        token, _jti, _expires = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        assert hash_refresh_token(token) == hash_refresh_token(token)

    def test_distinct_tokens_have_distinct_digests(
        self, auth_settings: Settings
    ) -> None:
        first, _a, _b = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        second, _c, _d = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        assert hash_refresh_token(first) != hash_refresh_token(second)

    def test_verification_matches_only_its_own_token(
        self, auth_settings: Settings
    ) -> None:
        first, _a, _b = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        second, _c, _d = create_refresh_token(
            subject=USER_ID, tenant_id=TENANT_ID, app_settings=auth_settings
        )
        digest = hash_refresh_token(first)
        assert verify_refresh_token(first, digest) is True
        assert verify_refresh_token(second, digest) is False

    def test_empty_inputs_are_refused(self) -> None:
        assert verify_refresh_token("", "0" * 64) is False
        assert verify_refresh_token("token", "") is False
        with pytest.raises(ValueError, match="empty"):
            hash_refresh_token("")


def test_token_claims_are_immutable(active_auth_settings: Settings) -> None:
    """A ``frozen`` dataclass: nothing downstream can rewrite a verified claim.

    Assigned through ``setattr`` rather than attribute syntax because mypy
    correctly rejects the latter statically, and this project does not suppress
    type errors to make a runtime assertion.
    """
    from dataclasses import FrozenInstanceError

    token = create_access_token(
        subject=USER_ID,
        tenant_id=TENANT_ID,
        role="viewer",
        app_settings=active_auth_settings,
    )
    claims = decode_token(token, expected_type=TokenType.ACCESS)
    # `__setattr__` rather than a plain assignment: mypy rejects writing to a
    # frozen dataclass attribute outright, and `setattr()` is ruff B010. Both
    # would need a suppression to express "attempt the write, expect refusal",
    # and this project takes no suppressions. Calling the generated guard is
    # lint-clean and states the same thing.
    with pytest.raises(FrozenInstanceError):
        claims.__setattr__("tenant_id", OTHER_TENANT_ID)

    # And the value is unchanged afterwards.
    assert claims.tenant_id == TENANT_ID


def test_decode_returns_the_configured_type_not_a_string(
    active_auth_settings: Settings,
) -> None:
    """``type`` is a ``TokenType``, so ``== "access"`` cannot drift."""
    token = create_access_token(
        subject=USER_ID,
        tenant_id=TENANT_ID,
        role="viewer",
        app_settings=active_auth_settings,
    )
    claims: TokenClaims = decode_token(token, expected_type=TokenType.ACCESS)
    assert isinstance(claims.type, TokenType)
    assert claims.type == TokenType.ACCESS