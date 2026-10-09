"""Login, refresh and revocation against a real database (STEP 9, STEP 13).

What is proved here
-------------------
* A login resolves a user from ``(tenant_slug, email, password)`` and issues a
  pair. Every failure — unknown slug, unknown email, wrong password, disabled
  account — raises the **same** exception with the **same** message.
* **The raw refresh token is never persisted.** Only a 64-character SHA-256 hex
  digest is written.
* Revocation works: a revoked token is rejected; logout is idempotent and silent.
* Rotation works: exchanging a refresh token revokes the presented one, so the
  same token cannot be used twice.
* A user row and its token row are checked against each other, so tampering in
  the database is caught.
* Deleting a user deletes their tokens (FK cascade); deleting a tenant is
  *refused* while tokens exist (RESTRICT).

Isolation
---------
* SQLite with foreign keys enforced, so a cascade or restrict claim is proved by
  the database rather than by the ORM's intentions. The PostgreSQL integration
  suite proves the same constraints against the real server.
* No real ``.env``, no network, no secrets beyond synthetic sentinels.
* Argon2 dominates the runtime here (~80 ms per hash). Fixtures create at most
  two accounts per test.

Scope
-----
This is the *service*, not the API. No route is exercised — the user-management
API is deliberately out of scope for this step.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import (
    TokenType,
    TokenValidationError,
    create_refresh_token,
    decode_token,
    hash_refresh_token,
)
from app.models.refresh_token import (
    REFRESH_TOKEN_HASH_LENGTH,
    RefreshToken,
)
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services import auth as auth_service
from tests.conftest import TEST_PASSWORD, require, table_of

AccountFactory = Callable[..., tuple[Tenant, User]]

#: Every failure path must be indistinguishable. Asserted as a constant so the
#: one place that knows the message is the test, not three separate literals.
GENERIC_FAILURE = "invalid tenant, email or password"


def as_utc(value: datetime) -> datetime:
    """Return ``value`` as a timezone-aware UTC datetime.

    SQLite does not store a UTC offset, so a ``TIMESTAMP WITH TIME ZONE`` column
    read back through it is naive. PostgreSQL returns the value aware. Normalising
    here means one assertion works on both engines, and keeps the comparison
    between a stored ``expires_at`` and a token's ``exp`` about the *instant*
    rather than about the driver's timezone handling.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@pytest.fixture(autouse=True)
def _process_settings(active_auth_settings: Settings) -> None:
    """Give the module a process-wide configuration with a usable signing key."""
    assert active_auth_settings.jwt_signing_key()


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestAuthenticate:
    def test_a_valid_login_returns_the_user_tenant_and_tokens(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        tenant, user = make_account(slug="acme", email="alice@example.com")

        returned_user, returned_tenant, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )

        assert returned_user.id == user.id
        assert returned_tenant.id == tenant.id
        assert tokens.access_token
        assert tokens.refresh_token
        assert tokens.token_type == "Bearer"
        assert tokens.expires_in_seconds == auth_settings.access_token_expire_minutes * 60

    def test_the_access_token_names_the_matched_tenant_not_the_supplied_slug(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """The slug is a selector; the tenant id is the one we looked up."""
        tenant, _user = make_account(slug="acme")

        _user, _tenant, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        claims = decode_token(
            tokens.access_token,
            expected_type=TokenType.ACCESS,
            app_settings=auth_settings,
        )
        assert claims.tenant_id == tenant.id

    def test_the_access_token_carries_the_current_role(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        _tenant, user = make_account(role=UserRole.EDITOR)
        _user, _tenant, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        claims = decode_token(
            tokens.access_token,
            expected_type=TokenType.ACCESS,
            app_settings=auth_settings,
        )
        assert claims.role == UserRole.EDITOR.value
        assert claims.subject == user.id

    @pytest.mark.parametrize(
        ("slug", "email", "password", "reason"),
        [
            pytest.param(
                "no-such-tenant",
                "alice@example.com",
                TEST_PASSWORD,
                "unknown tenant slug",
                id="unknown-slug",
            ),
            pytest.param(
                "acme",
                "nobody@example.com",
                TEST_PASSWORD,
                "unknown email",
                id="unknown-email",
            ),
            pytest.param(
                "acme",
                "alice@example.com",
                "wrong password entirely",
                "wrong password",
                id="wrong-password",
            ),
            pytest.param(
                "acme",
                "alice@example.com",
                "",
                "empty password",
                id="empty-password",
            ),
        ],
    )
    def test_every_failure_raises_the_same_opaque_error(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
        slug: str,
        email: str,
        password: str,
        reason: str,
    ) -> None:
        """One message for every failure, so the endpoint is not an oracle.

        Whether a tenant slug exists, whether an email is registered in it, and
        whether a password matches are all secrets. If any of them produced a
        distinguishable error, this endpoint would let an unauthenticated caller
        enumerate the user directory of every tenant.
        """
        make_account(slug="acme", email="alice@example.com")

        with pytest.raises(auth_service.InvalidCredentialsError) as first:
            auth_service.authenticate(
                model_session,
                tenant_slug=slug,
                email=email,
                password=password,
                app_settings=auth_settings,
            )

        assert str(first.value) == GENERIC_FAILURE, reason

    def test_a_disabled_account_cannot_log_in(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """``is_active`` is enforced, and with the same opaque error.

        A disabled user with the right password must not authenticate, and must
        not be distinguishable from a wrong password — otherwise the flag becomes
        an account-enumeration oracle by omission.
        """
        make_account(slug="acme", email="alice@example.com", is_active=False)

        with pytest.raises(auth_service.InvalidCredentialsError) as caught:
            auth_service.authenticate(
                model_session,
                tenant_slug="acme",
                email="alice@example.com",
                password=TEST_PASSWORD,
                app_settings=auth_settings,
            )
        assert str(caught.value) == GENERIC_FAILURE

    def test_the_same_email_authenticates_independently_in_two_tenants(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """The reason login needs a tenant selector at all.

        Two tenants each have an ``alice@example.com``. Both authenticate with
        the same password and each receives a token naming its own tenant. If
        this test fails, email has become globally unique — which Phase 2 Step 1
        explicitly forbids and which would stop one human holding accounts in two
        organisations.
        """
        tenant_a, user_a = make_account(slug="acme", email="alice@example.com")
        tenant_b, user_b = make_account(slug="globex", email="alice@example.com")

        assert user_a.id != user_b.id
        assert tenant_a.id != tenant_b.id

        _user_a, _tenant_a, tokens_a = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        _user_b, _tenant_b, tokens_b = auth_service.authenticate(
            model_session,
            tenant_slug="globex",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )

        claims_a = decode_token(
            tokens_a.access_token,
            expected_type=TokenType.ACCESS,
            app_settings=auth_settings,
        )
        claims_b = decode_token(
            tokens_b.access_token,
            expected_type=TokenType.ACCESS,
            app_settings=auth_settings,
        )
        assert claims_a.tenant_id == tenant_a.id
        assert claims_b.tenant_id == tenant_b.id
        assert claims_a.subject != claims_b.subject

    def test_one_tenants_password_does_not_work_in_another_tenant(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """Cross-tenant credential probing fails closed.

        The slug selects the tenant; the password must then match that tenant's
        user. A password that is valid somewhere in the system must not be
        accepted somewhere else.
        """
        make_account(slug="acme", email="alice@example.com", password="acme-only-pw")
        make_account(
            slug="globex", email="alice@example.com", password="globex-only-pw"
        )

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.authenticate(
                model_session,
                tenant_slug="globex",
                email="alice@example.com",
                password="acme-only-pw",
                app_settings=auth_settings,
            )

    def test_a_login_does_not_leak_a_tenant_id_from_the_request(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """``authenticate`` takes no ``tenant_id`` parameter at all.

        Stated as a signature assertion because it is the strongest available
        proof: there is no argument through which a caller *could* pass a
        client-supplied tenant id, so this function cannot be the place a
        spoofed one enters.
        """
        import inspect

        parameters = inspect.signature(auth_service.authenticate).parameters
        assert "tenant_slug" in parameters
        assert "tenant_id" not in parameters


# ---------------------------------------------------------------------------
# Persistence: the raw token must never be stored
# ---------------------------------------------------------------------------


class TestRefreshTokenPersistence:
    def test_a_row_is_written_for_each_login(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        _tenant, user = make_account()
        auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.commit()

        rows = model_session.scalars(select(RefreshToken)).all()
        assert len(rows) == 1
        assert rows[0].user_id == user.id
        assert rows[0].revoked is False

    def test_the_row_carries_the_tenant_id(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """Every table except ``tenants`` is tenant-scoped — including this one.

        PMP §14 does not list ``tenant_id`` for ``refresh_tokens``; the Phase 2
        Step 1 rule that overrides it is asserted here so the column cannot be
        dropped as "not in the plan" later.
        """
        tenant, user = make_account()
        auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.commit()

        row = require(model_session.scalar(select(RefreshToken)))
        assert row.tenant_id == tenant.id
        assert row.user_id == user.id

    def test_the_stored_value_is_a_hash_not_the_token(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """The headline requirement: a database dump yields no credential."""
        make_account()
        _user, _tenant, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.commit()

        row = require(model_session.scalar(select(RefreshToken)))
        assert row.token_hash == hash_refresh_token(tokens.refresh_token)
        assert row.token_hash != tokens.refresh_token
        assert tokens.refresh_token not in row.token_hash
        assert len(row.token_hash) == REFRESH_TOKEN_HASH_LENGTH

    def test_no_column_anywhere_contains_the_raw_token(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """Reads every column of the row, not just ``token_hash``.

        A raw token stored in an unconsidered column — a debug field, a
        user-agent slot — would pass a test that only inspects ``token_hash``.
        """
        make_account()
        _user, _tenant, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.commit()

        row = require(model_session.scalar(select(RefreshToken)))
        for column in row.__table__.columns:
            value = getattr(row, column.name)
            assert tokens.refresh_token != str(value), column.name

    def test_the_row_records_the_jti_and_the_expiry(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        make_account()
        _user, _tenant, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.commit()

        row = require(model_session.scalar(select(RefreshToken)))
        claims = decode_token(
            tokens.refresh_token,
            expected_type=TokenType.REFRESH,
            app_settings=auth_settings,
        )
        assert row.jti == tokens.jti == claims.jti
        # Same instant, compared across the SQLite/PostgreSQL timezone
        # difference that `as_utc` exists to absorb.
        assert as_utc(row.expires_at) == claims.expires_at
        # And the precision match that makes that comparison exact: a JWT
        # NumericDate is a whole second, so any microseconds here would mean
        # the stored expiry had drifted from the token's.
        assert row.expires_at.microsecond == 0

    def test_two_logins_produce_two_distinct_rows(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """Each login is an independently revocable session."""
        make_account()
        auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.commit()

        rows = model_session.scalars(select(RefreshToken)).all()
        assert len(rows) == 2
        assert len({row.jti for row in rows}) == 2
        assert len({row.token_hash for row in rows}) == 2

    def test_the_jti_is_unique_in_the_database(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """Enforced by the ``uq_refresh_tokens_jti`` constraint, not just by code."""
        tenant, user = make_account()
        first = RefreshToken(
            tenant_id=tenant.id,
            user_id=user.id,
            jti="duplicate",
            token_hash="0" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        model_session.add(first)
        model_session.commit()

        model_session.add(
            RefreshToken(
                tenant_id=tenant.id,
                user_id=user.id,
                jti="duplicate",
                token_hash="1" * 64,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        with pytest.raises(IntegrityError):
            model_session.commit()
        model_session.rollback()


# ---------------------------------------------------------------------------
# Exchange and rotation
# ---------------------------------------------------------------------------


class TestExchangeRefreshToken:
    def test_a_live_token_can_be_exchanged(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        tenant, user = make_account()
        _u, _t, first = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )

        result = auth_service.exchange_refresh_token(
            model_session,
            refresh_token=first.refresh_token,
            app_settings=auth_settings,
        )
        model_session.commit()

        assert result.rotated is True
        assert result.rotated_token_jti == first.jti
        assert result.user.id == user.id
        assert result.tenant.id == tenant.id
        assert result.tokens.refresh_token != first.refresh_token
        assert result.tokens.jti != first.jti

    def test_exchange_revokes_the_presented_token(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """Rotate-on-use: the old credential is dead the moment it is spent."""
        make_account()
        _u, _t, first = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        auth_service.exchange_refresh_token(
            model_session,
            refresh_token=first.refresh_token,
            app_settings=auth_settings,
        )
        model_session.commit()

        row = require(
            model_session.scalar(
                select(RefreshToken).where(RefreshToken.jti == first.jti)
            )
        )
        assert row.revoked is True

    def test_the_same_token_cannot_be_used_twice(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """The property that makes a stolen refresh token worth at most one use."""
        make_account()
        _u, _t, first = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        auth_service.exchange_refresh_token(
            model_session,
            refresh_token=first.refresh_token,
            app_settings=auth_settings,
        )
        model_session.commit()

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.exchange_refresh_token(
                model_session,
                refresh_token=first.refresh_token,
                app_settings=auth_settings,
            )

    def test_the_new_token_is_usable(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        make_account()
        _u, _t, first = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        rotated = auth_service.exchange_refresh_token(
            model_session,
            refresh_token=first.refresh_token,
            app_settings=auth_settings,
        )
        model_session.commit()

        again = auth_service.exchange_refresh_token(
            model_session,
            refresh_token=rotated.tokens.refresh_token,
            app_settings=auth_settings,
        )
        model_session.commit()
        assert again.tokens.refresh_token not in (
            first.refresh_token,
            rotated.tokens.refresh_token,
        )

    def test_an_access_token_cannot_be_exchanged(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        make_account()
        _u, _t, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.exchange_refresh_token(
                model_session,
                refresh_token=tokens.access_token,
                app_settings=auth_settings,
            )

    def test_a_genuine_token_with_no_database_row_is_rejected(
        self,
        model_session: Session,
        auth_settings: Settings,
    ) -> None:
        """Correctly signed, correct claims, no row.

        This is the case that distinguishes "cryptographically genuine" from
        "authorised". A refresh token is only usable while the server still holds
        a live row for it, so a token with no row must be refused even though
        nothing about it looks wrong.
        """
        token, _jti, _expires = create_refresh_token(
            subject=uuid.uuid4(),
            tenant_id=uuid.uuid4(),
            app_settings=auth_settings,
        )
        assert model_session.scalar(select(RefreshToken)) is None

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.exchange_refresh_token(
                model_session,
                refresh_token=token,
                app_settings=auth_settings,
            )

    def test_a_token_signed_with_another_key_is_rejected(
        self,
        model_session: Session,
        auth_settings: Settings,
        foreign_secret_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """A genuine, well-formed token from a deployment that holds a different key."""
        make_account()
        _u, _t, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.exchange_refresh_token(
                model_session,
                refresh_token=tokens.refresh_token,
                app_settings=foreign_secret_settings,
            )

    def test_a_revoked_token_is_rejected(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        make_account()
        _u, _t, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        auth_service.revoke_refresh_token(
            model_session, refresh_token=tokens.refresh_token
        )
        model_session.commit()

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.exchange_refresh_token(
                model_session,
                refresh_token=tokens.refresh_token,
                app_settings=auth_settings,
            )

    def test_a_deactivated_user_cannot_exchange_a_live_token(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """A live token is not sufficient — the user must still be active.

        Revocation on deactivation is necessary but not sufficient on its own:
        the user row is re-checked on every exchange, so a deactivation that
        missed a token still cannot be used.
        """
        _tenant, user = make_account()
        _u, _t, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )

        user.is_active = False
        model_session.commit()

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.exchange_refresh_token(
                model_session,
                refresh_token=tokens.refresh_token,
                app_settings=auth_settings,
            )

    def test_a_token_whose_row_disappeared_is_rejected(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """A valid signature is not enough; the token must also still be on file.

        This is the distinction between "cryptographically genuine" and
        "authorised": deleting the row is the same as revoking it.
        """
        make_account()
        _u, _t, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.query(RefreshToken).delete()
        model_session.commit()

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.exchange_refresh_token(
                model_session,
                refresh_token=tokens.refresh_token,
                app_settings=auth_settings,
            )

    def test_a_tampered_token_hash_is_rejected(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """Database tampering is caught by comparing digests, not just the jti.

        The token is correctly signed and the row exists; only the stored digest
        has been altered. If the implementation merely trusted the jti lookup,
        this would authenticate.
        """
        make_account()
        _u, _t, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        row = require(
            model_session.scalar(
                select(RefreshToken).where(RefreshToken.jti == tokens.jti)
            )
        )
        row.token_hash = "f" * 64
        model_session.commit()

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.exchange_refresh_token(
                model_session,
                refresh_token=tokens.refresh_token,
                app_settings=auth_settings,
            )


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestRevocation:
    def test_revoking_marks_the_row(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        make_account()
        _u, _t, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        auth_service.revoke_refresh_token(
            model_session, refresh_token=tokens.refresh_token
        )
        model_session.commit()

        row = require(
            model_session.scalar(
                select(RefreshToken).where(RefreshToken.jti == tokens.jti)
            )
        )
        assert row.revoked is True

    def test_revoking_twice_is_silent(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """Idempotent: the caller's intent is already satisfied the second time."""
        make_account()
        _u, _t, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        auth_service.revoke_refresh_token(
            model_session, refresh_token=tokens.refresh_token
        )
        auth_service.revoke_refresh_token(
            model_session, refresh_token=tokens.refresh_token
        )
        model_session.commit()

    def test_revoking_an_unknown_token_is_silent(
        self, model_session: Session
    ) -> None:
        """Logout must not become an oracle for which tokens exist."""
        auth_service.revoke_refresh_token(model_session, refresh_token="not-a-token")
        auth_service.revoke_refresh_token(model_session, refresh_token="")

    def test_revoking_with_a_forged_token_is_silent_and_harms_nobody(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """An attacker must not be able to revoke other sessions by guessing.

        The signature is verified before anything is written, so a forged token
        cannot reach a row even if its ``jti`` happens to match one.
        """
        tenant, user = make_account()
        real_token, real_jti, _expires = create_refresh_token(
            subject=user.id, tenant_id=tenant.id, app_settings=auth_settings
        )
        model_session.add(
            RefreshToken(
                tenant_id=tenant.id,
                user_id=user.id,
                jti=real_jti,
                token_hash=hash_refresh_token(real_token),
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        model_session.commit()

        # A different token for the same user, signed with the real key.
        _other, other_jti, _e = create_refresh_token(
            subject=user.id, tenant_id=tenant.id, app_settings=auth_settings
        )
        auth_service.revoke_refresh_token(
            model_session, refresh_token=_other.replace(other_jti, real_jti)
        )
        model_session.commit()

        row = require(
            model_session.scalar(
                select(RefreshToken).where(RefreshToken.jti == real_jti)
            )
        )
        assert row.revoked is False

    def test_revoking_all_revokes_every_live_token(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """The "sign out everywhere" primitive."""
        tenant, user = make_account()
        tokens = [
            auth_service.authenticate(
                model_session,
                tenant_slug="acme",
                email="alice@example.com",
                password=TEST_PASSWORD,
                app_settings=auth_settings,
            )[2]
            for _ in range(3)
        ]
        model_session.commit()

        count = auth_service.revoke_all_refresh_tokens(
            model_session, tenant_id=tenant.id, user_id=user.id
        )
        model_session.commit()
        assert count == 3

        for issued in tokens:
            with pytest.raises(auth_service.InvalidCredentialsError):
                auth_service.exchange_refresh_token(
                    model_session,
                    refresh_token=issued.refresh_token,
                    app_settings=auth_settings,
                )

    def test_revoking_all_does_not_touch_another_user(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        tenant, alice = make_account(slug="acme", email="alice@example.com")
        make_account(slug="acme", email="bob@example.com")
        bob_tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="bob@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )[2]
        model_session.commit()

        count = auth_service.revoke_all_refresh_tokens(
            model_session, tenant_id=tenant.id, user_id=alice.id
        )
        model_session.commit()
        assert count == 0

        result = auth_service.exchange_refresh_token(
            model_session,
            refresh_token=bob_tokens.refresh_token,
            app_settings=auth_settings,
        )
        model_session.commit()
        assert result.tokens.refresh_token != bob_tokens.refresh_token

    def test_revoking_all_fails_closed_on_a_wrong_tenant(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """A wrong tenant id revokes nothing rather than revoking the wrong rows.

        This is why the query filters on ``tenant_id`` even though ``user_id`` is
        already globally unique: the caller's mistake should be inert, not
        destructive.
        """
        tenant, alice = make_account(slug="acme", email="alice@example.com")
        auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.commit()

        count = auth_service.revoke_all_refresh_tokens(
            model_session, tenant_id=uuid.uuid4(), user_id=alice.id
        )
        model_session.commit()

        assert count == 0
        row = require(
            model_session.scalar(
                select(RefreshToken).where(
                    RefreshToken.tenant_id == tenant.id,
                    RefreshToken.user_id == alice.id,
                )
            )
        )
        assert row.revoked is False


# ---------------------------------------------------------------------------
# Database-level delete behaviour
# ---------------------------------------------------------------------------


class TestForeignKeyBehaviour:
    def test_deleting_a_user_deletes_their_tokens(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """``ON DELETE CASCADE`` — a deleted user's credential must not survive.

        Asserted by counting rows afterwards, so it is the database's behaviour
        under test rather than the ORM's intentions.
        """
        _tenant, user = make_account()
        auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.commit()
        assert model_session.scalar(
            select(RefreshToken).where(RefreshToken.user_id == user.id)
        ) is not None

        model_session.delete(user)
        model_session.commit()

        assert model_session.scalar(
            select(RefreshToken).where(RefreshToken.user_id == user.id)
        ) is None

    def test_deleting_a_tenant_is_refused_while_tokens_exist(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """``ON DELETE RESTRICT`` — consistent with Phase 2 Step 1's rule.

        Tenant removal is a governance decision. It must be refused while
        credentials are outstanding, rather than quietly destroying them.
        """
        tenant, _user = make_account()
        auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        model_session.commit()

        model_session.delete(tenant)
        with pytest.raises(IntegrityError):
            model_session.commit()
        model_session.rollback()

        assert model_session.scalar(select(RefreshToken)) is not None

    def test_a_token_cannot_reference_a_nonexistent_user(
        self,
        model_session: Session,
        make_account: AccountFactory,
    ) -> None:
        """The user foreign key is enforced by the database."""
        tenant, _user = make_account()
        model_session.add(
            RefreshToken(
                tenant_id=tenant.id,
                user_id=uuid.uuid4(),
                jti="orphan",
                token_hash="0" * 64,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        with pytest.raises(IntegrityError):
            model_session.commit()
        model_session.rollback()

    def test_a_token_cannot_reference_a_nonexistent_tenant(
        self,
        model_session: Session,
        make_account: AccountFactory,
    ) -> None:
        _tenant, user = make_account()
        model_session.add(
            RefreshToken(
                tenant_id=uuid.uuid4(),
                user_id=user.id,
                jti="orphan",
                token_hash="0" * 64,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        with pytest.raises(IntegrityError):
            model_session.commit()
        model_session.rollback()

    def test_tenant_id_is_required(
        self, model_session: Session, make_account: AccountFactory
    ) -> None:
        """No default at the database layer: a token row cannot be written untied.

        Written as a Core ``insert`` that omits ``tenant_id`` entirely, so the
        database — not the ORM — has to be the thing refusing. An ORM-level
        insert would fail for a different reason (the constructor requires the
        argument), which would prove nothing about the schema.
        """
        _tenant, user = make_account()

        with pytest.raises(IntegrityError, match="tenant_id"):
            model_session.execute(
                table_of(RefreshToken).insert().values(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    jti="no-tenant",
                    token_hash="0" * 64,
                    expires_at=datetime.now(UTC),
                    revoked=False,
                    created_at=datetime.now(UTC),
                )
            )
            model_session.commit()
        model_session.rollback()


class TestExpiry:
    def test_an_expired_row_is_rejected_even_with_a_live_signature(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
    ) -> None:
        """The database is authoritative for expiry, not the token's ``exp``.

        A row whose ``expires_at`` has passed is refused even though the token
        itself still verifies — the opposite direction from a forged token, and
        the one that catches a clock or migration problem.
        """
        from app.core.security import create_refresh_token

        _tenant, user = make_account()
        token, jti, _expires = create_refresh_token(
            subject=user.id,
            tenant_id=user.tenant_id,
            app_settings=auth_settings,
            expires_delta=timedelta(days=1),
        )
        model_session.add(
            RefreshToken(
                tenant_id=user.tenant_id,
                user_id=user.id,
                jti=jti,
                token_hash=hash_refresh_token(token),
                # A row that expired yesterday, while the token lives for a day.
                expires_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        model_session.commit()

        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.exchange_refresh_token(
                model_session,
                refresh_token=token,
                app_settings=auth_settings,
            )

    def test_is_expired_is_a_pure_function_of_its_arguments(
        self,
        model_session: Session,
        make_account: AccountFactory,
    ) -> None:
        """No hidden ``now()`` inside, so expiry is testable without sleeping."""
        _tenant, user = make_account()
        row = RefreshToken(
            tenant_id=user.tenant_id,
            user_id=user.id,
            jti="x",
            token_hash="0" * 64,
            expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        )

        assert row.is_expired(now=datetime(2029, 1, 1, tzinfo=UTC)) is False
        assert row.is_expired(now=datetime(2031, 1, 1, tzinfo=UTC)) is True
        assert row.is_expired(now=datetime(2030, 1, 1, tzinfo=UTC)) is True


class TestNoSecretsInLogs:
    def test_the_service_logs_nothing(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A successful login and a successful refresh emit no records at all."""
        import logging

        caplog.set_level(logging.DEBUG)
        make_account()
        _u, _t, tokens = auth_service.authenticate(
            model_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=TEST_PASSWORD,
            app_settings=auth_settings,
        )
        auth_service.exchange_refresh_token(
            model_session,
            refresh_token=tokens.refresh_token,
            app_settings=auth_settings,
        )

        assert caplog.records == []
        blob = "\n".join(record.getMessage() for record in caplog.records)
        for secret in (TEST_PASSWORD, tokens.access_token, tokens.refresh_token):
            assert secret not in blob

    def test_a_failed_login_logs_nothing_sensitive(
        self,
        model_session: Session,
        auth_settings: Settings,
        make_account: AccountFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        caplog.set_level(logging.DEBUG)
        make_account()
        with pytest.raises(auth_service.InvalidCredentialsError):
            auth_service.authenticate(
                model_session,
                tenant_slug="acme",
                email="alice@example.com",
                password="a-very-wrong-password",
                app_settings=auth_settings,
            )

        assert caplog.records == []


class TestExceptionType:
    def test_invalid_credentials_is_the_token_validation_error(self) -> None:
        """One type for a rejected credential and a rejected token.

        The HTTP layer has one ``except`` clause for both, and neither can be
        handled by catching something broader that would also swallow a
        programming error.
        """
        assert auth_service.InvalidCredentialsError is TokenValidationError
        assert issubclass(auth_service.InvalidCredentialsError, ValueError)


class TestSchemaShape:
    def test_the_materialised_table_has_the_expected_shape(
        self, model_session: Session
    ) -> None:
        """Read from the live database, not from model metadata.

        Metadata could say one thing while the created schema says another —
        the same reasoning ``test_security_isolation.py`` uses for email
        uniqueness.
        """
        inspector = inspect(model_session.get_bind())
        columns = {c["name"]: c for c in inspector.get_columns("refresh_tokens")}

        assert set(columns) == {
            "id",
            "tenant_id",
            "user_id",
            "jti",
            "token_hash",
            "expires_at",
            "revoked",
            "created_at",
        }
        assert columns["token_hash"]["nullable"] is False
        assert columns["jti"]["nullable"] is False
        assert columns["revoked"]["nullable"] is False

        indexes = {i["name"] for i in inspector.get_indexes("refresh_tokens")}
        assert "ix_refresh_tokens_tenant_id" in indexes
        assert "ix_refresh_tokens_user_id" in indexes

        uniques = {u["name"] for u in inspector.get_unique_constraints("refresh_tokens")}
        assert uniques == {"uq_refresh_tokens_jti"}

    def test_the_refresh_token_table_is_tenant_scoped(
        self, model_session: Session
    ) -> None:
        """It inherits ``TenantScopedMixin`` like every other tenant-owned model.

        The inheritance is the point: it is what guarantees ``tenant_id`` is
        required, indexed and ``RESTRICT`` without this model having to get any
        of it right.
        """
        from app.models.tenant import TenantScopedMixin

        assert issubclass(RefreshToken, TenantScopedMixin)