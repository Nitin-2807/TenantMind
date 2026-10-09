"""Password hashing and verification (PMP §17, PMP §8).

What is proved here
-------------------
* A correct password verifies and an incorrect one does not.
* The same password hashed twice gives **different** strings, which is the only
  evidence that a real random salt is in play. Without this assertion a
  "hashing" function that ignored its salt would pass every other test in the
  suite.
* The stored value contains no plaintext and is a valid Argon2 PHC string.
* Malformed, truncated and corrupted hashes fail **closed** — returning ``False``
  — rather than raising, so a caller cannot distinguish "no such hash" from
  "wrong password".
* The hash carries its own cost parameters, so raising them later cannot
  invalidate a password already in the database.
* No password or hash reaches a log record.

Why this file is separate from ``test_security_isolation.py``
-------------------------------------------------------------
That file asks "is there a way to cross a tenant boundary?". This one asks "does
the password primitive behave correctly?". They overlap only in subject matter,
and merging them would put a slow Argon2 test next to fast structural checks and
make the cheap safety net harder to run on its own.

Argon2 is deliberately slow (see ``HASH_COST_NOTE`` in ``conftest.py``), so
this module keeps its hash count low and reuses hashes through fixtures.
"""

from __future__ import annotations

import logging
import time

import pytest
from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError

from app.core.config import JwtSecretNotConfiguredError, Settings
from app.core.security import (
    ARGON2_HASH_LENGTH,
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_SALT_LENGTH,
    ARGON2_TIME_COST,
    hash_password,
    verify_password,
)
from tests.conftest import TEST_PASSWORD, make_settings

OTHER_PASSWORD = "a completely different password"

#: The PHC prefix Argon2id emits. Asserted on directly because a library
#: default change from ``argon2id`` to ``argon2i`` would weaken the hash without
#: breaking any functional test, and that substitution should be visible here.
ARGON2ID_PREFIX = "$argon2id$"


class TestHashPassword:
    """``hash_password`` produces a usable, salted Argon2id digest."""

    def test_returns_an_argon2id_phc_string(self) -> None:
        digest = hash_password(TEST_PASSWORD)
        assert digest.startswith(ARGON2ID_PREFIX)

    def test_hash_does_not_contain_the_plaintext(self) -> None:
        digest = hash_password(TEST_PASSWORD)
        assert TEST_PASSWORD not in digest
        # Also assert on a distinctive substring, so a hash that merely omitted
        # the whole password as one token still fails.
        assert "battery" not in digest

    def test_repeated_hashing_of_one_password_differs(self) -> None:
        """The salt is random, so two hashes of one password are not equal.

        This is the test that would fail for a constant-salt implementation.
        """
        assert hash_password(TEST_PASSWORD) != hash_password(TEST_PASSWORD)

    def test_hash_fits_the_column_width(self) -> None:
        """A digest must fit ``users.password_hash`` (VARCHAR(255)).

        Asserted against the model's own constant rather than a literal, so
        tightening the column without revisiting Argon2 fails here.
        """
        from app.models.user import USER_PASSWORD_HASH_MAX_LENGTH

        assert len(hash_password(TEST_PASSWORD)) <= USER_PASSWORD_HASH_MAX_LENGTH

    def test_empty_password_is_refused(self) -> None:
        """An empty password cannot be hashed into a valid credential.

        Hashing ``""`` would succeed and produce something that *verifies*
        against ``""``. That is not a hypothetical: it is what a missing-field
        validation bug would produce, and the result is an account that
        authenticates with nothing.
        """
        with pytest.raises(ValueError, match="empty"):
            hash_password("")


class TestVerifyPassword:
    """``verify_password`` accepts only a genuine match, and never raises."""

    def test_correct_password_verifies(self) -> None:
        digest = hash_password(TEST_PASSWORD)
        assert verify_password(TEST_PASSWORD, digest) is True

    def test_wrong_password_is_rejected(self) -> None:
        digest = hash_password(TEST_PASSWORD)
        assert verify_password(OTHER_PASSWORD, digest) is False

    def test_empty_password_is_rejected(self) -> None:
        digest = hash_password(TEST_PASSWORD)
        assert verify_password("", digest) is False

    @pytest.mark.parametrize(
        "password_hash",
        [
            pytest.param("", id="empty"),
            pytest.param("not-a-hash", id="not-a-phc-string"),
            pytest.param("$argon2id$", id="prefix-only"),
            pytest.param(
                "$argon2id$v=19$m=65536,t=2,p=1$c2FsdHNhbHRzYWx0$", id="truncated"
            ),
            pytest.param(
                "$argon2id$v=19$m=65536,t=2,p=1$notbase64!!!$notbase64!!!",
                id="non-base64-payload",
            ),
        ],
    )
    def test_malformed_hash_fails_closed(self, password_hash: str) -> None:
        """A damaged stored hash returns ``False``, never an exception.

        Fails *closed* on purpose. A verifier that raised would either propagate
        a 500 to the client or be caught and turned into ``True``; both are worse
        than "this credential did not match", and the second is catastrophic.
        """
        assert verify_password(TEST_PASSWORD, password_hash) is False

    def test_empty_hash_and_empty_password_together_are_rejected(self) -> None:
        assert verify_password("", "") is False


class TestHashParametersTravelWithTheHash:
    """The PHC string embeds its cost parameters, so they can be raised later.

    Argon2 verification reads ``m``, ``t`` and ``p`` out of the stored string
    rather than from the verifier's own configuration. That is what makes it safe
    for ``app.core.security`` to keep them as module constants: increasing them
    in a later release strengthens every *new* hash without invalidating a
    single password already stored. If this test ever fails, that upgrade path is
    gone and the constants must become real migration-affecting configuration.
    """

    def test_a_hash_made_with_other_parameters_still_verifies(self) -> None:
        deliberately_different = PasswordHasher(
            time_cost=1,
            memory_cost=8 * 1024,
            parallelism=1,
            hash_len=16,
            salt_len=8,
        )
        digest = deliberately_different.hash(TEST_PASSWORD)

        # Encoded parameters differ from ours, yet verification still succeeds.
        assert digest != hash_password(TEST_PASSWORD)
        assert verify_password(TEST_PASSWORD, digest) is True

    def test_the_stored_hash_exposes_its_parameters(self) -> None:
        """``argon2.extract_parameters`` proves the values really are embedded."""
        from argon2 import extract_parameters

        digest = hash_password(TEST_PASSWORD)
        parameters = extract_parameters(digest)
        assert parameters.type is Type.ID
        assert parameters.memory_cost == ARGON2_MEMORY_COST_KIB
        assert parameters.time_cost == ARGON2_TIME_COST
        assert parameters.parallelism == ARGON2_PARALLELISM
        assert parameters.hash_len == ARGON2_HASH_LENGTH
        assert parameters.salt_len == ARGON2_SALT_LENGTH

    def test_a_hash_below_current_parameters_is_flagged_for_rehashing(
        self,
    ) -> None:
        """``check_needs_rehash`` is the supported way to notice a weaker hash.

        A cheap hash made before the cost parameters were raised still verifies —
        see the previous test — but it is weaker than the current policy, and
        argon2's public API says so. Nothing in this project rehashes on login
        yet; this asserts the *capability* exists, so that when a login flow is
        added it can be used without rediscovering the API.
        """
        deliberately_cheaper = PasswordHasher(
            time_cost=1,
            memory_cost=8 * 1024,
            parallelism=1,
        )
        cheap_digest = deliberately_cheaper.hash(TEST_PASSWORD)

        assert verify_password(TEST_PASSWORD, cheap_digest) is True
        assert deliberately_cheaper.check_needs_rehash(cheap_digest) is False
        assert PasswordHasher().check_needs_rehash(cheap_digest) is True


class TestNoSecretIsLogged:
    """Nothing in this module writes a password or a hash anywhere."""

    def test_hashing_and_verification_emit_no_log_records(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.DEBUG)
        digest = hash_password(TEST_PASSWORD)
        verify_password(TEST_PASSWORD, digest)
        verify_password(OTHER_PASSWORD, digest)

        assert caplog.records == []
        blob = "\n".join(record.getMessage() for record in caplog.records)
        assert TEST_PASSWORD not in blob
        assert digest not in blob


class TestSigningKeyIsRequired:
    """``jwt_signing_key`` refuses rather than inventing a development secret."""

    def test_missing_secret_is_refused(self) -> None:
        with pytest.raises(JwtSecretNotConfiguredError, match="not set"):
            make_settings().jwt_signing_key()

    def test_blank_secret_is_refused(self) -> None:
        with pytest.raises(JwtSecretNotConfiguredError, match="not set"):
            make_settings(jwt_secret_key="   ").jwt_signing_key()

    @pytest.mark.parametrize(
        "placeholder",
        ["secret", "changeme", "insecure", "your-secret-key-here"],
    )
    def test_placeholder_secret_is_refused_in_every_environment(
        self, placeholder: str
    ) -> None:
        """Not just in production.

        A placeholder that works in development is a placeholder that eventually
        reaches production. ``app_env`` is left at its default here precisely so
        this proves the refusal is unconditional.
        """
        with pytest.raises(JwtSecretNotConfiguredError, match="placeholder"):
            make_settings(jwt_secret_key=placeholder).jwt_signing_key()

    def test_short_secret_is_refused(self) -> None:
        """Shorter than RFC 7518 §3.2 requires for an HMAC key.

        Rejected here rather than warned about, because PyJWT would otherwise
        emit an ``InsecureKeyLengthWarning`` on every single token operation for
        the lifetime of the deployment.
        """
        with pytest.raises(JwtSecretNotConfiguredError, match="at least 32"):
            make_settings(jwt_secret_key="a" * 31).jwt_signing_key()

    def test_exactly_the_minimum_length_is_accepted(self) -> None:
        assert len(make_settings(jwt_secret_key="a" * 32).jwt_signing_key()) == 32

    def test_a_real_secret_round_trips(self) -> None:
        secret = "s" * 48
        assert Settings.jwt_signing_key(make_settings(jwt_secret_key=secret)) == secret


class TestHashingIsNotTriviallyReversible:
    """Sanity check that the primitive is a real one-way function."""

    def test_hashing_is_slow_enough_to_cost_an_attacker(self) -> None:
        """Argon2id at these parameters must not be instant.

        A 20 ms floor is far below the configured cost on any real machine, so
        this cannot flake on slow CI. It exists to catch a substitution of
        something cheap — an unsalted SHA-256, or ``hashlib.pbkdf2_hmac`` with a
        tiny iteration count — which every functional test above would accept.
        """
        started = time.perf_counter()
        hash_password(TEST_PASSWORD)
        elapsed = time.perf_counter() - started
        assert elapsed >= 0.02, f"hashing took only {elapsed:.4f}s"

    def test_digest_carries_no_reversible_encoding_of_the_password(self) -> None:
        digest = hash_password(TEST_PASSWORD)
        # An obvious base64/hex encoding of the password must not appear.
        import base64
        import binascii

        encoded = {
            base64.b64encode(TEST_PASSWORD.encode()).decode(),
            base64.urlsafe_b64encode(TEST_PASSWORD.encode()).decode(),
            binascii.hexlify(TEST_PASSWORD.encode()).decode(),
        }
        assert not any(candidate in digest for candidate in encoded)


def test_password_hasher_uses_argon2id_not_a_weaker_variant() -> None:
    """Guard the one parameter most likely to be weakened by a refactor.

    ``Type.I`` (argon2i) resists GPU cracking but is weaker against side-channel
    and time-memory trade-offs; ``Type.D`` (argon2d) is weaker still. All three
    types satisfy every *functional* assertion in this file, so without this
    assertion a substitution would pass the whole suite.
    """
    digest = hash_password(TEST_PASSWORD)
    assert extract_parameters(digest).type is Type.ID

    # And prove the weaker variants really are distinguishable, so the
    # assertion above is testing something rather than tautological.
    for weaker in (Type.I, Type.D):
        weaker_digest = PasswordHasher(type=weaker).hash(TEST_PASSWORD)
        assert extract_parameters(weaker_digest).type is weaker


def test_invalid_hash_error_is_the_one_the_verifier_catches() -> None:
    """Pins the exception family :func:`verify_password` is written against.

    If ``argon2-cffi`` changed which exception a malformed hash raises, this
    test fails loudly instead of ``verify_password`` starting to leak one.
    """
    with pytest.raises(InvalidHashError):
        PasswordHasher().verify("definitely not a hash", TEST_PASSWORD)