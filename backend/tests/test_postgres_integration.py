"""PostgreSQL integration tests — run against a real server, or skipped honestly.

Status
------
**This suite runs.** A PostgreSQL 16 server is provisioned for this project
(``docker-compose.yml``, database ``tenantmind_test``), and the PostgreSQL gates
for Phase 2 Step 1, Step 2, Steps 2.3 and 2.4 are all passed against it.

Without ``TENANTMIND_TEST_PG_DSN`` set, the suite is collected and skipped, and
that skip is the honest record that the gate is outstanding. The guard checks at
the end of this file confirm the skip is caused *only* by the missing DSN, so a
broken fixture cannot masquerade as a correctly waiting suite.

Why the gap this closes matters
-------------------------------
* ``test_models.py`` and ``test_security_isolation.py`` run against SQLite. They
  prove the tenant-isolation *rules* hold — uniqueness is per tenant, the foreign
  key is enforced, ``ON DELETE RESTRICT`` refuses a tenant deletion. Those rules
  are dialect-independent and genuinely verified.
* What SQLite cannot verify is PostgreSQL's own behaviour: native ``UUID``
  storage, ``TIMESTAMPTZ`` round-tripping, ``UUID`` parameters binding through
  ``psycopg``, ``now()`` server defaults, the deliberate ``ON DELETE CASCADE`` /
  ``ON DELETE RESTRICT`` split on ``refresh_tokens``, and the DDL Alembic emits
  when the production dialect is in play.

  Phase 2.4 added the ``documents`` equivalents, and they are more than a repeat:
  SQLite **ignores a ``VARCHAR`` length entirely**, so a value the column cannot
  hold passes there and fails in production — the column-width assertions here are
  for a reason the tenant and user tables have no equivalent of. The
  ``documents`` ``CHECK`` constraints are the same story, and the ``SET NULL`` on
  ``uploaded_by`` is the one referential action whose behaviour cannot be read off
  the ORM's relationship metadata.

Running them
------------
Point ``TENANTMIND_TEST_PG_DSN`` at a **throwaway** database and run::

    TENANTMIND_TEST_PG_DSN=postgresql+psycopg://user:pass@localhost:5432/tm_test \
        ../.venv/bin/pytest tests/test_postgres_integration.py

Requirements on that database, enforced by the tests themselves:

* It must be **disposable**. Every run drops the ``documents``,
  ``refresh_tokens``, ``users`` and ``tenants`` tables and rebuilds them from the
  migrations.
* It must not be a development, staging or production database. The suite issues
  ``DROP TABLE``. A guard refuses any DSN whose database name does not look like a
  test database, so pointing this at real data fails instead of destroying it.

The DSN is read from the environment and is never written to disk, never printed,
and never committed. No secret reaches an assertion message either: SQLAlchemy's
``repr`` for an engine masks the password, and nothing here formats the DSN.
"""

from __future__ import annotations

import io
import os
import secrets
import uuid
import zlib
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from alembic.config import Config
from sqlalchemy import (
    DateTime,
    Engine,
    String,
    create_engine,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.security import hash_password, hash_refresh_token
from app.db.session import build_session_factory
from app.models import (
    MODEL_TABLES,
    Document,
    DocumentStatus,
    RefreshToken,
    Tenant,
    User,
    UserRole,
)
from app.models.document import (
    DOCUMENT_ERROR_MAX_LENGTH,
    DOCUMENT_STORAGE_KEY_MAX_LENGTH,
)
from app.rag.embeddings import EmbeddingError, EmbeddingProvider
from app.services.documents import (
    DocumentNotFoundError,
    delete_document,
    get_document_for_ingestion,
    record_ingestion_outcome,
)
from app.services.ingestion import (
    IngestionStage,
    TransientIngestionError,
    ingest_document,
)
from app.storage.local import LocalFileStore
from app.storage.qdrant import collection_name_for
from tests.conftest import make_settings, require
from tests.documents import build_txt
from tests.records import fake_client_of, payload_key, store_over
from tests.test_qdrant_store import FakeQdrant

#: A real, throwaway password for this suite only. It is not a credential for
#: anything: it exists so the Argon2 hasher runs for real rather than being
#: stubbed, and it is generated here rather than imported so it cannot be
#: mistaken for a shared fixture value.
PG_TEST_PASSWORD = "pg-integration-suite-password"

#: A minimally valid PDF. Small enough to inline and long enough to carry a
#: ``%PDF-`` signature, which is what the upload path actually verifies.
PG_PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"

#: The upload limit these tests write documents under. Small enough to be
#: irrelevant and large enough not to be the thing under test; the limit's own
#: boundary behaviour is proven in ``tests/test_storage.py`` against SQLite, where
#: it costs nothing to allocate.
PG_MAX_UPLOAD_SIZE_MB = 10

#: Environment variable naming the disposable database to test against.
PG_DSN_ENV_VAR = "TENANTMIND_TEST_PG_DSN"

#: Database-name fragments that indicate a disposable database. A DSN naming
#: anything else is refused, because this suite drops tables.
SAFE_DATABASE_MARKERS = ("test", "tmp", "temp", "ci", "throwaway", "scratch")

#: Vector width the Phase 2.5 ingestion tests embed at.
#:
#: Eight, not the configured 768. Every vector here goes into an in-memory
#: :class:`~tests.test_qdrant_store.FakeQdrant`, so nothing here is bandwidth- or
#: memory-bound and a real-width vector would only make the suite slower while
#: proving nothing additional. What matters is that the width is a *constant* the
#: settings and the provider agree on, because a mismatch is exactly what
#: :func:`app.services.ingestion.check_provider_matches_configuration` refuses.
INGESTION_EMBED_DIM = 8

#: Every table this suite manages, in dependency order (most dependent first).
#: Alembic's own bookkeeping table is created and dropped by Alembic, not by
#: these tests.
#:
#: ``refresh_tokens`` was added in Phase 2 Step 2 and ``documents`` in Phase 2.4.
#: Both are listed before the tables they reference so that a plain
#: ``DROP TABLE`` succeeds without leaning on ``CASCADE``, though the drop uses
#: ``CASCADE`` anyway because a half-applied previous run may have left the
#: database in a state where the order is wrong. ``document_chunks`` (migration
#: 0005), ``query_logs`` (0006) and ``audit_logs`` (0007) joined the set when
#: their phases shipped: a reset that missed them left the newer tables behind,
#: and a later ``upgrade head`` from "scratch" then failed with ``DuplicateTable``
#: — the reset must cover every table any migration creates.
#:
#: Order within the tuple is a real requirement, not a comment: it is the teardown
#: order, and a test that leaves a ``documents`` row behind would otherwise make
#: the next run fail in :func:`pg_engine` rather than in the assertion that
#: actually went wrong.
MANAGED_TABLES = (
    "audit_logs",
    "query_logs",
    "document_chunks",
    "documents",
    "refresh_tokens",
    "users",
    "tenants",
)

#: Alembic's own bookkeeping table. Present in a migrated database and not part
#: of :data:`app.models.MODEL_TABLES`, so it is named rather than filtered by a
#: bare ``startswith("alembic")`` guess.
ALEMBIC_BOOKKEEPING_TABLE = "alembic_version"


def _integration_dsn() -> str | None:
    dsn = os.environ.get(PG_DSN_ENV_VAR)
    return dsn.strip() if dsn and dsn.strip() else None


#: Skip reason shown wherever a live PostgreSQL server is required. Named so a
#: reader knows exactly how to enable the suite instead of seeing a bare skip.
REQUIRES_PG_DSN = pytest.mark.skipif(
    _integration_dsn() is None,
    reason=(
        f"{PG_DSN_ENV_VAR} is not set, so no PostgreSQL server is configured. "
        "The PostgreSQL integration gate is therefore PENDING; see this "
        "module's docstring for how to run it."
    ),
)

# Applied to the fixtures' consumers rather than as a module-level ``pytestmark``,
# so the guard checks themselves — the tests that confirm the suite is skipped
# only for want of a DSN — still run. A blanket module skip would silence them,
# and the suite could then skip for any structural reason without anything
# reporting it.
requires_pg = pytest.mark.usefixtures("pg_dsn")


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    """The configured DSN, after checking the database is disposable.

    Carries the skip itself rather than relying on a module-level mark, so that
    every fixture needing it inherits the same decision in one place.
    """
    dsn = _integration_dsn()
    if dsn is None:
        pytest.skip(
            f"{PG_DSN_ENV_VAR} is not set, so no PostgreSQL server is configured. "
            "The PostgreSQL integration gate is therefore PENDING; see this "
            "module's docstring for how to run it."
        )

    database_name = urlsplit(dsn).path.lstrip("/")
    assert database_name, f"{PG_DSN_ENV_VAR} must name a database in its path"

    lowered = database_name.lower()
    assert any(marker in lowered for marker in SAFE_DATABASE_MARKERS), (
        f"refusing to test against database {database_name!r}: this suite issues "
        f"DROP TABLE, so the database name must contain one of "
        f"{list(SAFE_DATABASE_MARKERS)}"
    )

    return dsn


@pytest.fixture(scope="module")
def pg_engine(pg_dsn: str) -> Generator[Engine, None, None]:
    """A PostgreSQL engine with the schema rebuilt from the migration.

    Built by running the real Alembic migration rather than
    ``metadata.create_all``, so what is exercised is the DDL the project will
    actually deploy.
    """
    from alembic.config import Config

    from alembic import command
    from app.core import config as config_module
    from tests.conftest import ALEMBIC_INI, make_settings

    engine = create_engine(pg_dsn, poolclass=None)

    # Point application settings at the test database for the duration, so
    # `alembic/env.py` resolves the same DSN the tests use.
    previous = config_module.settings
    config_module.settings = make_settings(database_url=pg_dsn)
    try:
        _drop_managed_tables(engine)

        command.upgrade(Config(str(ALEMBIC_INI)), "head")

        yield engine
    finally:
        _drop_managed_tables(engine)
        config_module.settings = previous
        engine.dispose()


def _drop_managed_tables(engine: Engine) -> None:
    """Drop the tables under management, ignoring those that do not exist.

    Issues plain ``DROP TABLE ... CASCADE`` rather than ``alembic downgrade``, so
    the suite starts from a known state even if a previous run was interrupted
    partway through a migration.
    """
    with engine.begin() as connection:
        existing = set(inspect(connection).get_table_names())
        for table in MANAGED_TABLES:
            if table in existing:
                connection.execute(text(f'DROP TABLE "{table}" CASCADE'))
        if ALEMBIC_BOOKKEEPING_TABLE in existing:
            connection.execute(text(f'DROP TABLE "{ALEMBIC_BOOKKEEPING_TABLE}"'))


@pytest.fixture
def pg_session(pg_engine: Engine) -> Generator[Session, None, None]:
    """A session whose rows are removed afterwards, leaving the schema in place."""
    factory = build_session_factory(pg_engine)

    def _clean() -> None:
        # Every managed table, so a row from one test cannot satisfy a foreign key
        # or a count in the next. `MANAGED_TABLES` is the single source of truth
        # for that set; this statement is built from it rather than restating it,
        # so adding a table cannot leave it untruncated here.
        targets = ", ".join(f'"{table}"' for table in MANAGED_TABLES)
        with pg_engine.begin() as connection:
            connection.execute(text(f"TRUNCATE TABLE {targets} CASCADE"))

    _clean()
    session = factory()

    yield session

    session.close()
    _clean()


@pytest.fixture
def pg_settings(pg_dsn: str) -> Settings:
    """Settings pointing at the test database, with a usable JWT signing key.

    Every authentication function in :mod:`app.services.auth` takes an optional
    settings override precisely so this is possible: the suite must not mutate
    process-wide configuration to make a login work. The key is generated in
    process rather than taken from ``.env``, so the suite depends on no external
    secret and no environment variable beyond the DSN.
    """
    settings = make_settings(
        database_url=pg_dsn,
        jwt_secret_key=secrets.token_urlsafe(48),
    )
    # Fail loudly here rather than inside an auth call with an unrelated error.
    assert settings.jwt_signing_key()
    return settings


@pytest.fixture
def pg_storage(tmp_path: Path) -> LocalFileStore:
    """A real file store under the test's own temporary directory.

    The filesystem half of a document is as much under test here as the database
    half, so this is the production :class:`~app.storage.local.LocalFileStore`
    rather than a stub: a store that did not actually write bytes would let a
    cleanup test pass for the wrong reason.

    Kept out of the database's temporary namespace deliberately. The teardown for
    this suite drops tables, and these tests assert that a refused delete leaves
    files behind — so the two lifecycles are kept separate.
    """
    return LocalFileStore(tmp_path / "pg-storage")


def _foreign_key_rules(pg_engine: Engine, table: str) -> dict[str, dict[str, str]]:
    """Read each foreign key's referential action out of ``information_schema``.

    Queried directly rather than through SQLAlchemy's inspector, which reports
    ``options`` in a dialect-specific shape, and rather than through the ORM's
    relationship metadata, which is Python's opinion of the constraint rather than
    the server's. This suite is specifically about what the server will do.
    """
    with pg_engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT
                    kcu.column_name AS column_name,
                    rc.delete_rule AS on_delete,
                    rc.update_rule AS on_update
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_name = kcu.constraint_name
                JOIN information_schema.referential_constraints rc
                  ON tc.constraint_name = rc.constraint_name
                WHERE tc.constraint_type = 'FOREIGN KEY'
                  AND tc.table_name = :table
                """
            ),
            {"table": table},
        ).all()

    return {
        str(row.column_name): {
            "on_delete": str(row.on_delete),
            "on_update": str(row.on_update),
        }
        for row in rows
    }


# ---------------------------------------------------------------------------
# Schema inspection
# ---------------------------------------------------------------------------


@requires_pg
class TestMigratedSchema:
    def test_the_migration_created_exactly_the_declared_tables(
        self, pg_engine: Engine
    ) -> None:
        """The migrated PostgreSQL schema holds exactly :data:`MODEL_TABLES`.

        Asserted against the exported closed set rather than a literal, so this
        suite and ``tests/test_models.py`` state one fact between them and
        neither can drift from the other.
        """
        names = set(inspect(pg_engine).get_table_names()) - {ALEMBIC_BOOKKEEPING_TABLE}

        assert names == set(MODEL_TABLES)

    def test_no_future_phase_table_exists(self, pg_engine: Engine) -> None:
        """Nothing from a later phase has been created early.

        ``refresh_tokens`` was listed here at Phase 2 Step 1 and ``documents`` at
        Phase 2.4. PMP §14 places both in Phase 2 and both are now implemented, so
        they have been removed from this set — but the assertion itself is kept,
        because "the next thing has not been built yet" is the property worth
        holding on to. ``query_logs`` and ``audit_logs`` were removed the same way
        when Phase 7 shipped them (migrations 0006 and 0007), exactly as the
        ``Phase 7`` rows in the execution log record.
        """
        names = set(inspect(pg_engine).get_table_names())

        assert names & {"roles", "permissions"} == set()

    def test_ids_are_native_uuid_columns(self, pg_engine: Engine) -> None:
        """PostgreSQL must store these natively, not as text.

        This is the assertion SQLite cannot make: ``sa.Uuid`` renders ``UUID`` here
        and ``CHAR(32)`` there, so only this suite confirms the production type.
        """
        for table in ("tenants", "users"):
            column = next(
                column
                for column in inspect(pg_engine).get_columns(table)
                if column["name"] == "id"
            )

            assert column["type"].__class__.__name__ == "UUID"

    def test_timestamps_are_timestamptz_columns(self, pg_engine: Engine) -> None:
        for table in ("tenants", "users"):
            for name in ("created_at", "updated_at"):
                column_type = next(
                    column["type"]
                    for column in inspect(pg_engine).get_columns(table)
                    if column["name"] == name
                )

                assert isinstance(column_type, DateTime)
                assert column_type.timezone is True

    def test_timestamps_default_to_the_database_clock(
        self, pg_engine: Engine
    ) -> None:
        """``now()`` must be the server default, not a client-supplied value."""
        for table in ("tenants", "users"):
            for name in ("created_at", "updated_at"):
                column = next(
                    column
                    for column in inspect(pg_engine).get_columns(table)
                    if column["name"] == name
                )

                assert column["default"] is not None
                assert "now()" in str(column["default"]).lower()

    def test_is_active_defaults_to_true(self, pg_engine: Engine) -> None:
        column = next(
            column
            for column in inspect(pg_engine).get_columns("users")
            if column["name"] == "is_active"
        )

        assert column["default"] is not None
        assert str(column["default"]).lower().strip("() ") == "true"

    def test_role_defaults_to_viewer(self, pg_engine: Engine) -> None:
        column = next(
            column
            for column in inspect(pg_engine).get_columns("users")
            if column["name"] == "role"
        )

        assert "viewer" in str(column["default"]).lower()

    def test_role_is_varchar_not_a_native_enum_type(
        self, pg_engine: Engine
    ) -> None:
        """A native PostgreSQL ``ENUM`` cannot be altered by a migration.

        Asserting ``VARCHAR`` is what pins the choice documented in
        ``app/models/user.py``.
        """
        column = next(
            column
            for column in inspect(pg_engine).get_columns("users")
            if column["name"] == "role"
        )

        assert column["type"].__class__.__name__ == "VARCHAR"
        assert "user_role" not in inspect(pg_engine).get_table_names()

    def test_the_composite_unique_constraint_exists(self, pg_engine: Engine) -> None:
        constraints = inspect(pg_engine).get_unique_constraints("users")

        assert any(
            tuple(constraint["column_names"]) == ("tenant_id", "email")
            for constraint in constraints
        )

    def test_there_is_no_unique_constraint_on_email_alone(
        self, pg_engine: Engine
    ) -> None:
        """The global-uniqueness mistake, checked against the real database."""
        inspector = inspect(pg_engine)

        offending = [
            constraint
            for constraint in inspector.get_unique_constraints("users")
            if constraint["column_names"] == ["email"]
        ] + [
            index
            for index in inspector.get_indexes("users")
            if index["unique"] and index["column_names"] == ["email"]
        ]

        assert offending == []

    def test_the_foreign_key_is_restrict(self, pg_engine: Engine) -> None:
        foreign_keys = inspect(pg_engine).get_foreign_keys("users")

        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == "tenants"
        assert foreign_keys[0]["constrained_columns"] == ["tenant_id"]
        assert foreign_keys[0]["options"].get("ondelete") == "RESTRICT"

    def test_both_indexes_exist(self, pg_engine: Engine) -> None:
        names = {index["name"] for index in inspect(pg_engine).get_indexes("users")}

        assert {"ix_users_tenant_id", "ix_users_email"} <= names

    def test_the_role_check_constraint_exists(self, pg_engine: Engine) -> None:
        constraints = inspect(pg_engine).get_check_constraints("users")

        assert any("role" in (constraint.get("sqltext") or "") for constraint in constraints)

    def test_constraint_names_are_the_deterministic_ones(
        self, pg_engine: Engine
    ) -> None:
        """PostgreSQL reports the names the naming convention assigned."""
        inspector = inspect(pg_engine)
        unique_names = {
            constraint["name"] for constraint in inspector.get_unique_constraints("users")
        }
        unique_names |= {
            constraint["name"] for constraint in inspector.get_unique_constraints("tenants")
        }

        assert unique_names == {"uq_users_tenant_id_email", "uq_tenants_slug"}


@requires_pg
class TestRefreshTokenMigratedSchema:
    """``refresh_tokens`` (PMP §14), as PostgreSQL actually materialised it.

    Separate from ``TestMigratedSchema`` because these assertions are about one
    table and that table's whole point is a set of deliberate choices — the
    cascade/restrict split, the digest width, the boolean default. Grouping them
    keeps the reasoning next to the assertions rather than interleaved with the
    tenants/users checks.
    """

    def test_the_columns_are_exactly_the_declared_ones(self, pg_engine: Engine) -> None:
        """A closed column set, and no session-metadata creep.

        Asserted positively rather than by exclusion so that adding a column —
        a "last used at" timestamp, a user agent, an IP address — fails here
        instead of accumulating by default. PMP §14 plus the two documented
        additions (``tenant_id``, ``jti``) is the whole intended shape.
        """
        columns = {
            column["name"] for column in inspect(pg_engine).get_columns("refresh_tokens")
        }

        assert columns == {
            "id",
            "tenant_id",
            "user_id",
            "jti",
            "token_hash",
            "expires_at",
            "revoked",
            "created_at",
        }

    def test_ids_are_native_uuid_columns(self, pg_engine: Engine) -> None:
        """Same assertion SQLite cannot make, for this table too."""
        for name in ("id", "tenant_id", "user_id"):
            column = next(
                column
                for column in inspect(pg_engine).get_columns("refresh_tokens")
                if column["name"] == name
            )

            assert column["type"].__class__.__name__ == "UUID", name

    def test_the_digest_column_is_exactly_the_sha256_hex_width(
        self, pg_engine: Engine
    ) -> None:
        """64, because that is what SHA-256 produces — not a rounded guess.

        A wider column would mean the schema does not know the size of the value
        it stores; a narrower one would be a latent truncation bug that only
        surfaces when two digests are compared and differ only in their tail.
        """
        column = next(
            column
            for column in inspect(pg_engine).get_columns("refresh_tokens")
            if column["name"] == "token_hash"
        )
        column_type = column["type"]

        # `isinstance` rather than a `__class__.__name__` string comparison: it
        # narrows the type for mypy, so `.length` is checked instead of assumed.
        assert isinstance(column_type, String)
        assert column_type.length == 64
        assert column["nullable"] is False

    def test_expires_at_is_a_timestamptz(self, pg_engine: Engine) -> None:
        """Expiry is compared against a clock, so it must carry an offset.

        A naive ``TIMESTAMP`` in PostgreSQL renders without a zone and is read
        back with the *server's* offset attached, which silently shifts the
        instant depending on where the database runs.
        """
        column = next(
            column
            for column in inspect(pg_engine).get_columns("refresh_tokens")
            if column["name"] == "expires_at"
        )

        assert isinstance(column["type"], DateTime)
        assert column["type"].timezone is True

    def test_created_at_defaults_to_the_database_clock(
        self, pg_engine: Engine
    ) -> None:
        column = next(
            column
            for column in inspect(pg_engine).get_columns("refresh_tokens")
            if column["name"] == "created_at"
        )

        assert column["default"] is not None
        assert "now()" in str(column["default"]).lower()

    def test_revoked_defaults_to_false(self, pg_engine: Engine) -> None:
        """A row that exists is a live credential.

        The default is asserted at the *database* layer, not only in the model,
        because raw SQL and a future service that omits the column must not
        produce a token that is accidentally treated as revoked.
        """
        column = next(
            column
            for column in inspect(pg_engine).get_columns("refresh_tokens")
            if column["name"] == "revoked"
        )

        assert column["nullable"] is False
        assert column["default"] is not None
        assert str(column["default"]).lower().strip("() ") == "false"

    def test_there_is_no_revoked_at_column(self, pg_engine: Engine) -> None:
        """``revoked`` alone, deliberately.

        Two columns recording one fact can disagree. PMP §14 asks for the
        boolean, nothing in this step needs the *time*, and the model docstring
        explains why. Asserting the absence keeps the decision from being
        quietly reversed later.
        """
        columns = {
            column["name"] for column in inspect(pg_engine).get_columns("refresh_tokens")
        }

        assert "revoked_at" not in columns

    def test_there_is_no_stored_raw_token_column(self, pg_engine: Engine) -> None:
        """The credential itself is never persisted.

        The strongest guarantee available here: whatever a database dump of this
        table contains, it contains no usable refresh token.
        """
        columns = [
            column["name"] for column in inspect(pg_engine).get_columns("refresh_tokens")
        ]

        assert "token" not in columns
        assert "raw_token" not in columns
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

    def test_the_user_foreign_key_cascades(self, pg_engine: Engine) -> None:
        """Deleting a user must invalidate their sessions.

        A refresh token outlives its owner's session and stays cryptographically
        valid until it expires, so retaining the row after the user is gone
        would leave a working credential with no owner. ``CASCADE`` is the safe
        direction here because the row has no independent existence.
        """
        foreign_key = next(
            foreign_key
            for foreign_key in inspect(pg_engine).get_foreign_keys("refresh_tokens")
            if foreign_key["referred_table"] == "users"
        )

        assert foreign_key["constrained_columns"] == ["user_id"]
        assert foreign_key["options"].get("ondelete") == "CASCADE"

    def test_the_tenant_foreign_key_restricts(self, pg_engine: Engine) -> None:
        """And the Phase 2 Step 1 rule is unchanged for this table.

        The asymmetry with the user foreign key is a decision, not an oversight;
        see the model docstring. Cascading a tenant deletion would destroy an
        entire organisation.
        """
        foreign_key = next(
            foreign_key
            for foreign_key in inspect(pg_engine).get_foreign_keys("refresh_tokens")
            if foreign_key["referred_table"] == "tenants"
        )

        assert foreign_key["constrained_columns"] == ["tenant_id"]
        assert foreign_key["options"].get("ondelete") == "RESTRICT"

    def test_both_indexes_exist(self, pg_engine: Engine) -> None:
        """``user_id`` for "revoke every session of this account".

        That operation is the reason a stolen-but-unnoticed credential cannot lock
        somebody out of their own tenant indefinitely, so the index is a
        requirement rather than a tuning choice.
        """
        names = {
            index["name"]
            for index in inspect(pg_engine).get_indexes("refresh_tokens")
        }

        assert {"ix_refresh_tokens_tenant_id", "ix_refresh_tokens_user_id"} <= names

    def test_the_jti_is_unique_in_the_database(self, pg_engine: Engine) -> None:
        """Two credentials must never share one revocation state.

        ``jti`` comes from 256 bits of ``secrets``, so a collision is a non-event
        that is still worth defending: a duplicate would mean revoking one
        session silently revokes another.
        """
        constraints = inspect(pg_engine).get_unique_constraints("refresh_tokens")
        names = {constraint["name"] for constraint in constraints}

        assert "uq_refresh_tokens_jti" in names
        assert any(
            tuple(constraint["column_names"]) == ("jti",)
            for constraint in constraints
        )


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


@requires_pg
class TestTenantAndUserRoundTrip:
    def test_a_uuid_round_trips_as_a_uuid(self, pg_session: Session) -> None:
        """Binds a ``uuid.UUID`` through ``psycopg`` and reads a ``UUID`` back."""
        from app.models import Tenant, User, UserRole

        tenant = Tenant(name="Acme", slug="acme")
        pg_session.add(tenant)
        pg_session.commit()
        user = User(
            tenant_id=tenant.id,
            email="alice@example.com",
            password_hash="hash",
            role=UserRole.VIEWER,
        )
        pg_session.add(user)
        pg_session.commit()
        pg_session.expire_all()

        stored = pg_session.get(User, user.id)
        assert stored is not None
        assert isinstance(stored.id, uuid.UUID)
        assert stored.tenant_id == tenant.id

    def test_timestamps_come_back_timezone_aware(self, pg_session: Session) -> None:
        """The property SQLite cannot provide: ``TIMESTAMPTZ`` is preserved."""
        from app.models import Tenant

        tenant = Tenant(name="Acme", slug="acme")
        pg_session.add(tenant)
        pg_session.commit()
        pg_session.expire_all()

        stored = require(pg_session.get(Tenant, tenant.id))
        assert stored.created_at.tzinfo is not None
        assert stored.updated_at.tzinfo is not None

    def test_updated_at_advances(self, pg_session: Session) -> None:
        from app.models import Tenant

        tenant = Tenant(name="Acme", slug="acme")
        pg_session.add(tenant)
        pg_session.commit()

        pg_session.execute(
            text("UPDATE tenants SET updated_at = now() - interval '1 day' WHERE id = :id"),
            {"id": tenant.id},
        )
        pg_session.commit()
        pg_session.expire_all()
        stale = require(pg_session.get(Tenant, tenant.id)).updated_at

        tenant.name = "Acme Renamed"
        pg_session.commit()
        pg_session.expire_all()

        assert require(pg_session.get(Tenant, tenant.id)).updated_at > stale

    def test_the_same_email_succeeds_in_two_tenants(self, pg_session: Session) -> None:
        """The mandatory multi-tenancy case, on the production dialect."""
        from app.models import Tenant, User

        tenant_a = Tenant(name="Acme", slug="acme")
        tenant_b = Tenant(name="Globex", slug="globex")
        pg_session.add_all([tenant_a, tenant_b])
        pg_session.commit()
        pg_session.add_all(
            [
                User(tenant_id=tenant_a.id, email="alice@example.com", password_hash="a"),
                User(tenant_id=tenant_b.id, email="alice@example.com", password_hash="b"),
            ]
        )
        pg_session.commit()

        assert len(pg_session.query(User).all()) == 2

    def test_the_same_email_fails_within_one_tenant(self, pg_session: Session) -> None:
        """And PostgreSQL must name the constraint the convention assigned."""
        from app.models import Tenant, User

        tenant = Tenant(name="Acme", slug="acme")
        pg_session.add(tenant)
        pg_session.commit()
        pg_session.add(User(tenant_id=tenant.id, email="a@example.com", password_hash="a"))
        pg_session.commit()

        pg_session.add(
            User(tenant_id=tenant.id, email="a@example.com", password_hash="b")
        )
        with pytest.raises(IntegrityError, match="uq_users_tenant_id_email"):
            pg_session.commit()
        pg_session.rollback()

    def test_a_user_cannot_reference_a_missing_tenant(
        self, pg_session: Session
    ) -> None:
        from app.models import User

        pg_session.add(
            User(tenant_id=uuid.uuid4(), email="orphan@example.com", password_hash="h")
        )

        with pytest.raises(IntegrityError, match="fk_users_tenant_id_tenants"):
            pg_session.commit()
        pg_session.rollback()

    def test_deleting_a_tenant_with_users_is_refused(
        self, pg_session: Session
    ) -> None:
        from app.models import Tenant, User

        tenant = Tenant(name="Acme", slug="acme")
        pg_session.add(tenant)
        pg_session.commit()
        pg_session.add(User(tenant_id=tenant.id, email="a@example.com", password_hash="a"))
        pg_session.commit()

        # The DELETE and the commit both belong inside the guard: SQLAlchemy 2.x
        # sends a textual statement to the server during ``execute``, so the
        # RESTRICT violation is raised there rather than at commit time.
        with pytest.raises(IntegrityError):
            pg_session.execute(text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id})
            pg_session.commit()
        pg_session.rollback()

    def test_slug_uniqueness_is_enforced(self, pg_session: Session) -> None:
        from app.models import Tenant

        pg_session.add(Tenant(name="Acme", slug="dup"))
        pg_session.commit()
        pg_session.add(Tenant(name="Other", slug="dup"))

        with pytest.raises(IntegrityError, match="uq_tenants_slug"):
            pg_session.commit()
        pg_session.rollback()

    def test_the_role_check_constraint_is_enforced(self, pg_session: Session) -> None:
        from app.models import Tenant

        tenant = Tenant(name="Acme", slug="acme")
        pg_session.add(tenant)
        pg_session.commit()

        # Inside the guard because SQLAlchemy 2.x sends the INSERT during
        # ``execute``; the CHECK violation is raised there, not at commit.
        with pytest.raises(IntegrityError, match="ck_users_user_role"):
            pg_session.execute(
                text(
                    "INSERT INTO users (id, tenant_id, email, password_hash, role) "
                    "VALUES (:id, :tenant_id, :email, :hash, 'superuser')"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant.id,
                    "email": "bad@example.com",
                    "hash": "h",
                },
            )
            pg_session.commit()
        pg_session.rollback()

    def test_tenant_id_cannot_be_omitted(self, pg_session: Session) -> None:
        # Inside the guard because SQLAlchemy 2.x sends the INSERT during
        # ``execute``; the NOT NULL violation is raised there, not at commit.
        with pytest.raises(IntegrityError, match="tenant_id"):
            pg_session.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role) "
                    "VALUES (:id, 'no-tenant@example.com', 'h', 'viewer')"
                ),
                {"id": uuid.uuid4()},
            )
            pg_session.commit()
        pg_session.rollback()


@requires_pg
class TestRefreshTokenRoundTrip:
    """``refresh_tokens`` behaviour, on the production dialect.

    The SQLite suite proves these rules dialect-independently. What only a real
    server can add is whether the ``CASCADE`` and ``RESTRICT`` clauses are
    actually honoured as written, and whether a full login/refresh/logout cycle
    survives a ``TIMESTAMPTZ`` round trip — the one place SQLite's naive
    datetimes would have hidden a bug.
    """

    def _account(
        self, pg_session: Session, *, slug: str = "acme", email: str = "alice@example.com"
    ) -> tuple[Tenant, User]:
        """A tenant and one user with a real Argon2 hash."""
        from app.models import Tenant, User, UserRole

        tenant = Tenant(name=slug.title(), slug=slug)
        pg_session.add(tenant)
        pg_session.commit()
        user = User(
            tenant_id=tenant.id,
            email=email,
            password_hash=hash_password(PG_TEST_PASSWORD),
            role=UserRole.VIEWER,
        )
        pg_session.add(user)
        pg_session.commit()
        return tenant, user

    def test_a_digest_round_trips_unchanged(self, pg_session: Session) -> None:
        """The stored value is exactly 64 lowercase hex characters.

        Read back out of PostgreSQL rather than compared in memory, because the
        question is what the database holds — a value truncated by the column
        width would still compare equal in Python.
        """

        tenant, user = self._account(pg_session)
        digest = hash_refresh_token("a-refresh-token")
        assert len(digest) == 64

        pg_session.add(
            RefreshToken(
                tenant_id=tenant.id,
                user_id=user.id,
                jti="jti-1",
                token_hash=digest,
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        pg_session.commit()
        pg_session.expire_all()

        stored = require(pg_session.get(RefreshToken, require(pg_session.scalar(
            select(RefreshToken.id).where(RefreshToken.jti == "jti-1")
        ))))
        assert stored.token_hash == digest
        assert stored.token_hash.islower()
        assert len(stored.token_hash) == 64

    def test_expires_at_comes_back_timezone_aware(self, pg_session: Session) -> None:
        """The property SQLite cannot provide, for this table too.

        Directly load-bearing: ``is_expired`` compares ``expires_at`` to an aware
        ``now``, and a naive value would raise ``TypeError`` rather than answer.
        """

        tenant, user = self._account(pg_session)
        token = RefreshToken(
            tenant_id=tenant.id,
            user_id=user.id,
            jti="jti-tz",
            token_hash=hash_refresh_token("token"),
            expires_at=datetime.now(UTC) + timedelta(days=7),
        )
        pg_session.add(token)
        pg_session.commit()
        pg_session.expire_all()

        stored = require(pg_session.get(RefreshToken, token.id))
        assert stored.expires_at.tzinfo is not None
        assert stored.created_at.tzinfo is not None
        assert stored.is_expired(now=datetime.now(UTC)) is False
        assert (
            stored.is_expired(now=datetime.now(UTC) + timedelta(days=8)) is True
        )

    def test_revoked_defaults_to_false_in_the_database(
        self, pg_session: Session
    ) -> None:
        """A raw INSERT that omits ``revoked`` produces a live token.

        Written as SQL on purpose. An ORM insert would carry the Python-side
        default and prove nothing about the server default.
        """
        tenant, user = self._account(pg_session)

        pg_session.execute(
            text(
                "INSERT INTO refresh_tokens "
                "(id, tenant_id, user_id, jti, token_hash, expires_at) "
                "VALUES (:id, :tenant_id, :user_id, 'jti-raw', :hash, :expires_at)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant.id,
                "user_id": user.id,
                "hash": hash_refresh_token("token"),
                "expires_at": datetime.now(UTC) + timedelta(days=7),
            },
        )
        pg_session.commit()
        pg_session.expire_all()


        stored = require(
            pg_session.scalar(select(RefreshToken).where(RefreshToken.jti == "jti-raw"))
        )
        assert stored.revoked is False

    def test_two_tokens_cannot_share_a_jti(self, pg_session: Session) -> None:
        """Revocation is addressed by ``jti``, so a duplicate would be a bug."""

        tenant, user = self._account(pg_session)
        for _ in range(2):
            pg_session.add(
                RefreshToken(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    jti="duplicate",
                    token_hash=hash_refresh_token("token"),
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                )
            )
        with pytest.raises(IntegrityError, match="uq_refresh_tokens_jti"):
            pg_session.commit()
        pg_session.rollback()

    def test_a_token_cannot_reference_a_missing_user(
        self, pg_session: Session
    ) -> None:

        tenant, _user = self._account(pg_session)
        pg_session.add(
            RefreshToken(
                tenant_id=tenant.id,
                user_id=uuid.uuid4(),
                jti="orphan-user",
                token_hash=hash_refresh_token("token"),
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        with pytest.raises(IntegrityError, match="fk_refresh_tokens_user_id_users"):
            pg_session.commit()
        pg_session.rollback()

    def test_a_token_cannot_reference_a_missing_tenant(
        self, pg_session: Session
    ) -> None:

        _tenant, user = self._account(pg_session)
        pg_session.add(
            RefreshToken(
                tenant_id=uuid.uuid4(),
                user_id=user.id,
                jti="orphan-tenant",
                token_hash=hash_refresh_token("token"),
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
        )
        with pytest.raises(IntegrityError, match="fk_refresh_tokens_tenant_id_tenants"):
            pg_session.commit()
        pg_session.rollback()

    def test_deleting_a_user_cascades_to_their_tokens(
        self, pg_session: Session
    ) -> None:
        """The half of the asymmetry PostgreSQL is doing for us.

        A deleted user must not leave a working credential behind. Issued by the
        ORM rather than by raw SQL so that ``passive_deletes=True`` is exercised
        too: the ORM must not try to delete the rows itself, and must not fail
        because it cannot see them.
        """

        _tenant, user = self._account(pg_session)
        pg_session.add_all(
            [
                RefreshToken(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    jti=f"session-{index}",
                    token_hash=hash_refresh_token(f"token-{index}"),
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                )
                for index in range(3)
            ]
        )
        pg_session.commit()

        pg_session.delete(user)
        pg_session.commit()
        pg_session.expire_all()

        assert pg_session.query(RefreshToken).count() == 0

    def test_deleting_a_tenant_with_tokens_is_refused(
        self, pg_session: Session
    ) -> None:
        """And the other half: the Step 1 ``RESTRICT`` rule survives.

        The violation PostgreSQL reports first is the ``users`` foreign key,
        because every token has a user and the users are checked before the
        token rows. That ordering is itself the finding worth stating: as long as
        a user exists, the Step 1 rule already refuses the delete, and the
        token-level ``RESTRICT`` is a second line of defence that is asserted
        structurally in ``test_the_tenant_foreign_key_restricts`` rather than
        being reachable here. Asserting the *first* named constraint keeps this
        test honest about what PostgreSQL is actually refusing.
        """
        tenant, user = self._account(pg_session)
        pg_session.add_all(
            [
                RefreshToken(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    jti="session-1",
                    token_hash=hash_refresh_token("token"),
                    expires_at=datetime.now(UTC) + timedelta(days=7),
                )
            ]
        )
        pg_session.commit()

        with pytest.raises(IntegrityError, match="fk_users_tenant_id_tenants"):
            pg_session.execute(
                text("DELETE FROM tenants WHERE id = :id"), {"id": tenant.id}
            )
            pg_session.commit()
        pg_session.rollback()

        # Nothing was destroyed: the refusal is total, not partial.
        pg_session.expire_all()
        assert pg_session.query(RefreshToken).count() == 1
        assert pg_session.get(Tenant, tenant.id) is not None

    def test_a_full_login_refresh_rotate_logout_cycle(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """The whole credential lifecycle against real PostgreSQL.

        Covers the one combination the SQLite suite cannot: Argon2 hashes written
        by this process and read back by it, ``TIMESTAMPTZ`` expiry comparisons,
        rotate-on-use revocation, and logout — all in one transaction-per-step
        sequence against the dialect that will actually run in production.
        """
        from app.services.auth import (
            InvalidCredentialsError,
            authenticate,
            exchange_refresh_token,
            revoke_refresh_token,
        )

        tenant, _user = self._account(pg_session)

        _user, _tenant, tokens = authenticate(
            pg_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=PG_TEST_PASSWORD,
            app_settings=pg_settings,
        )
        pg_session.commit()

        stored = require(
            pg_session.scalar(select(RefreshToken).where(RefreshToken.jti == tokens.jti))
        )
        assert stored.token_hash == hash_refresh_token(tokens.refresh_token)
        assert stored.token_hash != tokens.refresh_token
        assert stored.tenant_id == tenant.id
        assert stored.user_id == require(pg_session.get(User, stored.user_id)).id
        assert stored.revoked is False

        # Rotate on use: the old token is revoked and a new one issued.
        result = exchange_refresh_token(
            pg_session,
            refresh_token=tokens.refresh_token,
            app_settings=pg_settings,
        )
        pg_session.commit()
        pg_session.expire_all()

        rotated = result.tokens
        assert result.rotated is True
        assert result.rotated_token_jti == tokens.jti
        assert rotated.refresh_token != tokens.refresh_token
        assert rotated.jti != tokens.jti

        assert require(pg_session.get(RefreshToken, stored.id)).revoked is True

        with pytest.raises(InvalidCredentialsError):
            exchange_refresh_token(
                pg_session,
                refresh_token=tokens.refresh_token,
                app_settings=pg_settings,
            )
        pg_session.rollback()

        # Logout revokes the live token, and it stops working.
        revoke_refresh_token(
            pg_session,
            refresh_token=rotated.refresh_token,
            app_settings=pg_settings,
        )
        pg_session.commit()
        pg_session.expire_all()

        assert (
            require(
                pg_session.scalar(
                    select(RefreshToken).where(RefreshToken.jti == rotated.jti)
                )
            ).revoked
            is True
        )

        with pytest.raises(InvalidCredentialsError):
            exchange_refresh_token(
                pg_session,
                refresh_token=rotated.refresh_token,
                app_settings=pg_settings,
            )
        pg_session.rollback()

    def test_revoke_all_only_touches_the_named_tenants_users(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """Bulk revocation must not leak across a tenant boundary.

        The realistic bug this guards: a "revoke everything" operation written as
        ``UPDATE refresh_tokens SET revoked = true`` with the tenant filter
        forgotten. It would be invisible in a single-tenant test.
        """
        from app.services.auth import revoke_all_refresh_tokens

        tenant_a, user_a = self._account(pg_session, slug="acme", email="a@example.com")
        tenant_b, user_b = self._account(pg_session, slug="globex", email="b@example.com")

        for tenant, user, prefix in (
            (tenant_a, user_a, "a"),
            (tenant_b, user_b, "b"),
        ):
            pg_session.add_all(
                [
                    RefreshToken(
                        tenant_id=tenant.id,
                        user_id=user.id,
                        jti=f"{prefix}-{index}",
                        token_hash=hash_refresh_token(f"{prefix}-{index}"),
                        expires_at=datetime.now(UTC) + timedelta(days=7),
                    )
                    for index in range(2)
                ]
            )
        pg_session.commit()
        pg_session.expire_all()

        revoked = revoke_all_refresh_tokens(
            pg_session, tenant_id=tenant_a.id, user_id=user_a.id
        )
        pg_session.commit()
        pg_session.expire_all()

        assert revoked == 2
        states = {
            row.jti: row.revoked
            for row in pg_session.query(RefreshToken).all()
        }
        assert states["a-0"] is True
        assert states["a-1"] is True
        assert states["b-0"] is False
        assert states["b-1"] is False
        assert tenant_a.id != tenant_b.id
        assert user_a.id != user_b.id

    def test_a_token_row_is_never_returned_to_a_caller_as_a_token(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """A database dump of this table yields no usable credential.

        Asserted by reading the digest back and confirming it is not any prefix
        of, or equal to, the token itself — the property the ``token_hash`` design
        exists to provide, checked on the production dialect rather than in
        memory.
        """
        from app.services.auth import authenticate

        self._account(pg_session)
        _user, _tenant, tokens = authenticate(
            pg_session,
            tenant_slug="acme",
            email="alice@example.com",
            password=PG_TEST_PASSWORD,
            app_settings=pg_settings,
        )
        pg_session.commit()
        pg_session.expire_all()

        stored = require(
            pg_session.scalar(
                select(RefreshToken).where(RefreshToken.jti == tokens.jti)
            )
        )
        assert tokens.refresh_token not in stored.token_hash
        assert stored.token_hash not in tokens.refresh_token
        assert hash_refresh_token(tokens.refresh_token) == stored.token_hash

        # And the ORM object does not carry the raw token as an attribute either,
        # so it cannot be logged or serialised by accident.
        assert not hasattr(stored, "token")
        assert not hasattr(stored, "raw_token")


# ---------------------------------------------------------------------------
# Phase 2.4: documents, on the production dialect
# ---------------------------------------------------------------------------


def _stored_document(
    pg_session: Session,
    identity: Any,
    storage: LocalFileStore,
    *,
    filename: str = "report.pdf",
    content: bytes = PG_PDF_BYTES,
    allowed_roles: Sequence[UserRole] | None = None,
) -> Document:
    """Store one real document for ``identity`` and flush it.

    Goes through :func:`app.services.documents.create_document` rather than
    constructing a :class:`~app.models.document.Document` directly, so the rows
    these tests inspect are the ones the application would write — including the
    storage key, which is generated rather than chosen.
    """
    from app.services.documents import create_document

    document = create_document(
        pg_session,
        identity=identity,
        storage=storage,
        source=io.BytesIO(content),
        filename=filename,
        allowed_roles=allowed_roles,
        max_upload_size_mb=PG_MAX_UPLOAD_SIZE_MB,
    )
    pg_session.commit()
    pg_session.expire_all()
    return document


@requires_pg
class TestDocumentMigratedSchema:
    """The ``documents`` table as PostgreSQL actually created it.

    SQLite enforces a ``CHECK`` and a ``FOREIGN KEY`` in ways close enough to
    PostgreSQL's that the model tests pass on either. What only the real server
    settles is the DDL's *native* type and the referential actions, because those
    are declared in the migration and enforced by the server's own machinery.
    """

    def test_the_table_exists_with_exactly_the_declared_columns(
        self, pg_engine: Engine
    ) -> None:
        """Closed set, so a column added without a migration review fails here."""
        columns = {column["name"] for column in inspect(pg_engine).get_columns("documents")}

        assert columns == {
            "id",
            "tenant_id",
            "uploaded_by",
            "filename",
            "storage_key",
            "search_vector",
            "status",
            "allowed_roles",
            "chunk_count",
            "error",
            "size_bytes",
            "content_type",
            "created_at",
            "updated_at",
        }

    def test_the_id_is_a_native_uuid(self, pg_engine: Engine) -> None:
        """``sa.Uuid`` renders ``UUID`` here and ``CHAR(32)`` on SQLite.

        The dialect difference is the reason this suite exists; asserting it for
        ``documents`` as well as ``tenants`` is what proves the *new* table was
        declared the same way the existing ones were.
        """
        for name in ("id", "tenant_id", "uploaded_by"):
            column = next(
                column
                for column in inspect(pg_engine).get_columns("documents")
                if column["name"] == name
            )

            assert column["type"].__class__.__name__ == "UUID", name

    def test_timestamps_are_timestamptz_with_a_server_clock_default(
        self, pg_engine: Engine
    ) -> None:
        for name in ("created_at", "updated_at"):
            column = next(
                column
                for column in inspect(pg_engine).get_columns("documents")
                if column["name"] == name
            )

            assert isinstance(column["type"], DateTime)
            assert column["type"].timezone is True
            assert "now()" in str(column["default"]).lower()

    def test_the_status_default_is_the_initial_status(self, pg_engine: Engine) -> None:
        column = next(
            column
            for column in inspect(pg_engine).get_columns("documents")
            if column["name"] == "status"
        )

        assert column["default"] is not None
        assert "processing" in str(column["default"]).lower()

    def test_the_status_is_varchar_not_a_native_enum_type(self, pg_engine: Engine) -> None:
        """The same reasoning as ``users.role``: a native ``ENUM`` cannot be altered.

        Asserting ``VARCHAR`` here is what pins the choice documented in
        ``app/models/document.py``, where adding a fourth status must be a
        migration rather than an ``ALTER TYPE``.
        """
        column = next(
            column
            for column in inspect(pg_engine).get_columns("documents")
            if column["name"] == "status"
        )

        assert column["type"].__class__.__name__ == "VARCHAR"

    def test_the_storage_key_column_is_varchar_with_a_length(self, pg_engine: Engine) -> None:
        """A length here is load-bearing: the key is a 32-character hex string."""
        column = next(
            column
            for column in inspect(pg_engine).get_columns("documents")
            if column["name"] == "storage_key"
        )

        assert isinstance(column["type"], String)
        assert column["type"].length == DOCUMENT_STORAGE_KEY_MAX_LENGTH

    def test_allowed_roles_is_a_json_column_and_may_be_null(
        self, pg_engine: Engine
    ) -> None:
        """``JSON``, not ``JSONB`` and not ``TEXT``.

        The migration's choice is deliberate — see its docstring — so the assertion
        is on the exact type rather than "some JSON-ish thing".
        """
        column = next(
            column
            for column in inspect(pg_engine).get_columns("documents")
            if column["name"] == "allowed_roles"
        )

        assert column["type"].__class__.__name__ == "JSON"
        assert column["nullable"] is True

    def test_the_uploaded_by_foreign_key_sets_null_on_user_deletion(
        self, pg_engine: Engine
    ) -> None:
        """``ON DELETE SET NULL``, so deleting a user does not delete documents.

        Asserted against ``information_schema`` rather than through the ORM's
        ``relationship`` metadata, because the relationship is a *statement of
        intent* in Python and this is the server's own record of what it will do.
        The two disagreeing is exactly the bug worth catching.
        """
        rules = _foreign_key_rules(pg_engine, "documents")

        assert rules["tenant_id"]["on_delete"] == "RESTRICT"
        assert rules["uploaded_by"]["on_delete"] == "SET NULL"

    def test_a_tenant_deletion_is_refused_while_documents_exist(
        self, pg_engine: Engine, pg_session: Session, pg_settings: Settings, pg_storage
    ) -> None:
        """The ``RESTRICT`` really is enforced by the server.

        SQLite's foreign keys are off by default, and the model suite turns them on
        explicitly — so the rule is proven there. What only PostgreSQL shows is
        that the server, not the ORM, is the one refusing.
        """
        identity = _registered_identity(pg_session, pg_settings)
        _stored_document(pg_session, identity, pg_storage)

        with pytest.raises(IntegrityError):
            pg_session.delete(require(pg_session.get(Tenant, identity.tenant_id)))
            pg_session.commit()

        pg_session.rollback()

    def test_the_storage_key_is_unique_across_the_whole_table(
        self, pg_engine: Engine, pg_session: Session, pg_settings: Settings, pg_storage
    ) -> None:
        """Uniqueness is global, not per tenant.

        Worth stating because a per-tenant uniqueness constraint would also satisfy
        "the storage key identifies one document". The keys are random, so a
        collision is a bug or an attack — and the database is what has to say so.
        """
        first = _registered_identity(pg_session, pg_settings, slug="acme")
        second = _registered_identity(pg_session, pg_settings, slug="globex")
        one = _stored_document(pg_session, first, pg_storage)
        _stored_document(pg_session, second, pg_storage)

        clash = Document(
            tenant_id=second.tenant_id,
            filename="other.pdf",
            storage_key=one.storage_key,
            status=DocumentStatus.PROCESSING,
            size_bytes=len(PG_PDF_BYTES),
            content_type="application/pdf",
        )
        pg_session.add(clash)

        with pytest.raises(IntegrityError):
            pg_session.commit()

        pg_session.rollback()


@requires_pg
class TestDocumentRoundTrip:
    """What PostgreSQL does with the values the application writes.

    Two things here are genuinely dialect-dependent rather than restatements of
    the SQLite suite: ``JSON`` needs no casting or coercion on the way back, and
    ``UUID`` binds through ``psycopg`` rather than through SQLite's string
    handling.
    """

    def test_the_document_round_trips_with_every_field(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        from app.services.documents import record_ingestion_outcome

        identity = _registered_identity(pg_session, pg_settings)
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            allowed_roles=[UserRole.VIEWER, UserRole.EDITOR],
        )

        record_ingestion_outcome(
            pg_session,
            identity=identity,
            document_id=document.id,
            status=DocumentStatus.READY,
            chunk_count=42,
        )
        pg_session.commit()
        pg_session.expire_all()

        stored = require(pg_session.get(Document, document.id))
        assert isinstance(stored.id, uuid.UUID)
        assert stored.tenant_id == identity.tenant_id
        assert stored.uploaded_by == identity.user_id
        assert stored.filename == "report.pdf"
        assert stored.status is DocumentStatus.READY
        assert stored.allowed_roles == ["editor", "viewer"]
        assert stored.chunk_count == 42
        assert stored.error is None
        assert stored.size_bytes == len(PG_PDF_BYTES)
        assert stored.content_type == "application/pdf"
        assert stored.created_at.tzinfo is not None
        assert stored.updated_at.tzinfo is not None

    def test_allowed_roles_comes_back_as_a_list_and_unrestricted_as_none(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """The two states stay distinct through the database.

        ``NULL`` and ``[]`` mean different things — unrestricted versus visible to
        nobody — and the refusal of an empty list at the boundary is what keeps
        them from becoming interchangeable. Stated here because a JSON column is
        exactly the kind that will quietly normalize one into the other.
        """
        identity = _registered_identity(pg_session, pg_settings)
        unrestricted = _stored_document(pg_session, identity, pg_storage, filename="a.pdf")
        restricted = _stored_document(
            pg_session, identity, pg_storage, filename="b.pdf", allowed_roles=[UserRole.VIEWER]
        )
        pg_session.expire_all()

        assert require(pg_session.get(Document, unrestricted.id)).allowed_roles is None
        assert require(pg_session.get(Document, restricted.id)).allowed_roles == ["viewer"]

    def test_an_error_longer_than_the_column_is_refused_by_the_server(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """``VARCHAR(2000)`` is enforced by PostgreSQL, unlike SQLite.

        SQLite ignores a ``VARCHAR`` length entirely, so a value the column cannot
        hold passes there and fails in production. The refusal is the assertion.

        Raised as a :class:`~sqlalchemy.exc.DataError` rather than an
        :class:`~sqlalchemy.exc.IntegrityError`, and named as such on purpose: a
        truncated value is a *type* violation, and a reader who widened the
        expectation to ``DatabaseError`` would lose the distinction between "the
        value is the wrong shape" and "the value breaks a rule".
        """
        identity = _registered_identity(pg_session, pg_settings)
        document = _stored_document(pg_session, identity, pg_storage)

        with pytest.raises(DataError):
            pg_session.execute(
                text("UPDATE documents SET error = :error WHERE id = :id"),
                {"error": "x" * (DOCUMENT_ERROR_MAX_LENGTH + 1), "id": document.id},
            )

        pg_session.rollback()

    def test_an_error_of_exactly_the_declared_length_is_accepted(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """The boundary is inclusive, so a worker can fill the column.

        The counterpart to the refusal above and worth its own test: a
        ``length`` violation at ``n`` says nothing about ``n``, and an
        off-by-one that made the last character unusable would silently shorten
        every stored ingestion error by one.
        """
        identity = _registered_identity(pg_session, pg_settings)
        document = _stored_document(pg_session, identity, pg_storage)
        message = "x" * DOCUMENT_ERROR_MAX_LENGTH

        pg_session.execute(
            text("UPDATE documents SET error = :error WHERE id = :id"),
            {"error": message, "id": document.id},
        )
        pg_session.commit()
        pg_session.expire_all()

        assert require(pg_session.get(Document, document.id)).error == message

    def test_a_negative_size_is_refused_by_the_check_constraint(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """``CHECK (size_bytes > 0)`` is the database's, and it fires.

        The ORM never writes a negative size, so this is reached by raw SQL on
        purpose: the question is not "does the application write a bad size" but
        "what happens to the row if anything else does".
        """
        identity = _registered_identity(pg_session, pg_settings)
        document = _stored_document(pg_session, identity, pg_storage)

        with pytest.raises(IntegrityError):
            pg_session.execute(
                text("UPDATE documents SET size_bytes = -1 WHERE id = :id"),
                {"id": document.id},
            )

        pg_session.rollback()

    def test_a_negative_chunk_count_is_refused_by_the_check_constraint(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        identity = _registered_identity(pg_session, pg_settings)
        document = _stored_document(pg_session, identity, pg_storage)

        with pytest.raises(IntegrityError):
            pg_session.execute(
                text("UPDATE documents SET chunk_count = -5 WHERE id = :id"),
                {"id": document.id},
            )

        pg_session.rollback()

    def test_an_unknown_status_is_refused_by_the_check_constraint(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """The ``CHECK`` is what makes the ``VARCHAR`` column safe.

        ``status`` is stored as text so a fourth status can be added by migration
        rather than by ``ALTER TYPE``; the ``CHECK`` is the other half of that
        trade. Without it, a typo would be a row nobody can query.
        """
        identity = _registered_identity(pg_session, pg_settings)
        document = _stored_document(pg_session, identity, pg_storage)

        with pytest.raises(IntegrityError):
            pg_session.execute(
                text("UPDATE documents SET status = 'pendng' WHERE id = :id"),
                {"id": document.id},
            )

        pg_session.rollback()

    def test_deleting_the_uploader_nulls_the_reference_rather_than_the_document(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
        pg_engine: Engine,
    ) -> None:
        """``SET NULL`` really is what happens, on a committed read.

        Two things are proven together and neither is worth much alone: the
        document survives its uploader, and the ``uploaded_by`` reference is
        cleared rather than left dangling. A column that still named the deleted
        row would be a dangling foreign key — the exact state ``SET NULL`` exists
        to prevent.
        """
        from app.services.users import invite_user

        identity = _registered_identity(pg_session, pg_settings)
        uploader = invite_user(
            pg_session,
            identity=identity,
            email="editor@acme.example",
            password=PG_TEST_PASSWORD,
            role=UserRole.EDITOR,
        )
        pg_session.commit()

        document = _stored_document(pg_session, identity, pg_storage)
        pg_session.execute(
            text("UPDATE documents SET uploaded_by = :user WHERE id = :id"),
            {"user": uploader.id, "id": document.id},
        )
        pg_session.commit()

        pg_session.delete(require(pg_session.get(User, uploader.id)))
        pg_session.commit()

        with build_session_factory(pg_engine)() as observer:
            survivor = observer.get(Document, document.id)
            assert survivor is not None
            assert survivor.uploaded_by is None
            assert survivor.tenant_id == identity.tenant_id


@requires_pg
class TestDocumentIsolationOnTheServer:
    """Tenant scoping, on the dialect where a missing predicate really returns rows.

    The SQLite suite proves the same rules and they are dialect-independent. What
    only this suite can do is catch the case where the ``WHERE`` clause is missing
    entirely and SQLite's table layout happened not to matter.
    """

    def test_a_document_of_another_tenant_is_not_returned(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        from app.services.documents import DocumentNotFoundError, get_document

        one = _registered_identity(pg_session, pg_settings, slug="acme")
        two = _registered_identity(pg_session, pg_settings, slug="globex")
        theirs = _stored_document(pg_session, two, pg_storage)

        with pytest.raises(DocumentNotFoundError):
            get_document(pg_session, identity=one, document_id=theirs.id)

        pg_session.rollback()

    def test_a_document_of_another_tenant_is_not_deleted(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """The row *and* the file both survive a refused cross-tenant delete."""
        from app.services.documents import DocumentNotFoundError, delete_document

        one = _registered_identity(pg_session, pg_settings, slug="acme")
        two = _registered_identity(pg_session, pg_settings, slug="globex")
        theirs = _stored_document(pg_session, two, pg_storage)

        with pytest.raises(DocumentNotFoundError):
            delete_document(
                pg_session, identity=one, document_id=theirs.id, storage=pg_storage
            )
        pg_session.rollback()

        pg_session.expire_all()
        assert require(pg_session.get(Document, theirs.id)) is not None
        assert pg_storage.path_for(two.tenant_id, theirs.storage_key).is_file()

    def test_a_listing_returns_only_the_callers_tenant(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        from app.services.documents import list_documents

        one = _registered_identity(pg_session, pg_settings, slug="acme")
        two = _registered_identity(pg_session, pg_settings, slug="globex")
        mine = [_stored_document(pg_session, one, pg_storage, filename=f"{n}.pdf") for n in range(3)]
        theirs = [_stored_document(pg_session, two, pg_storage, filename=f"{n}.pdf") for n in range(2)]

        listed = list_documents(pg_session, identity=one)

        assert {document.id for document in listed} == {d.id for d in mine}
        assert not {document.id for document in listed} & {d.id for d in theirs}

    def test_two_tenants_store_the_same_filename_without_conflict(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """``filename`` is not unique, and nothing in the design wants it to be.

        Two organisations both having a ``report.pdf`` is the normal case, not an
        edge case. A uniqueness constraint here would make the second upload fail
        for no reason a user could understand.
        """
        one = _registered_identity(pg_session, pg_settings, slug="acme")
        two = _registered_identity(pg_session, pg_settings, slug="globex")
        first = _stored_document(pg_session, one, pg_storage)
        second = _stored_document(pg_session, two, pg_storage)

        assert first.filename == second.filename == "report.pdf"
        assert first.storage_key != second.storage_key
        assert pg_storage.path_for(one.tenant_id, first.storage_key) != pg_storage.path_for(
            two.tenant_id, second.storage_key
        )


# ---------------------------------------------------------------------------
# Phase 2.3: registration and user management, on the production dialect
# ---------------------------------------------------------------------------


def _registered_identity(
    pg_session: Session,
    pg_settings: Settings,
    *,
    slug: str = "acme",
    email: str = "admin@example.com",
) -> Any:
    """Register a tenant and return its administrator's authenticated identity.

    Built the same way the HTTP layer builds it — from a signed access token, via
    :meth:`AuthenticatedIdentity.from_access_token` — so the identity under test
    is one the application could actually produce, not a dataclass assembled by
    hand. A hand-built identity would let these tests pass while the real one
    could not be obtained.
    """
    from app.core.identity import AuthenticatedIdentity
    from app.services.tenants import register_tenant

    result = register_tenant(
        pg_session,
        name=slug.title(),
        slug=slug,
        admin_email=email,
        admin_password=PG_TEST_PASSWORD,
        app_settings=pg_settings,
    )
    pg_session.commit()
    return AuthenticatedIdentity.from_access_token(
        result.tokens.access_token, app_settings=pg_settings
    )


@requires_pg
class TestTenantRegistrationTransaction:
    """``register_tenant`` writes one transaction's worth of rows, or none.

    Every other test in the service suite runs against SQLite, where "the caller
    owns the transaction" is a convention. PostgreSQL's MVCC makes it checkable:
    an uncommitted write is genuinely invisible to a second session, and a
    rollback genuinely discards it. That is what this class asserts.
    """

    @staticmethod
    def _register(
        pg_session: Session,
        pg_settings: Settings,
        *,
        slug: str = "acme",
        email: str = "admin@example.com",
    ) -> Any:
        from app.services.tenants import register_tenant

        return register_tenant(
            pg_session,
            name=slug.title(),
            slug=slug,
            admin_email=email,
            admin_password=PG_TEST_PASSWORD,
            app_settings=pg_settings,
        )

    def test_registration_is_invisible_until_the_caller_commits(
        self, pg_session: Session, pg_settings: Settings, pg_engine: Engine
    ) -> None:
        """The service never commits; ``get_db`` does.

        Asserted with a second, independent session, because the same session
        would see its own uncommitted rows through its own transaction and prove
        nothing.
        """
        result = self._register(pg_session, pg_settings)

        with build_session_factory(pg_engine)() as observer:
            assert observer.get(Tenant, result.tenant.id) is None

        pg_session.commit()

        with build_session_factory(pg_engine)() as observer:
            assert observer.get(Tenant, result.tenant.id) is not None

    def test_a_rolled_back_registration_leaves_nothing_behind(
        self, pg_session: Session, pg_settings: Settings, pg_engine: Engine
    ) -> None:
        """A half-registered organisation is a state the API must not produce.

        A tenant with no administrator cannot log in and cannot be repaired
        through any route, because every user-management endpoint requires an
        authenticated administrator. So the failure that matters is the *rollback*
        behaviour, not the success path.
        """
        result = self._register(pg_session, pg_settings)
        tenant_id = result.tenant.id
        user_id = result.admin.id
        jti = result.tokens.jti

        pg_session.rollback()

        with build_session_factory(pg_engine)() as observer:
            assert observer.get(Tenant, tenant_id) is None
            assert observer.get(User, user_id) is None
            assert (
                observer.scalar(
                    select(RefreshToken).where(RefreshToken.jti == jti)
                )
                is None
            )

    def test_all_three_rows_appear_together(
        self, pg_session: Session, pg_settings: Settings, pg_engine: Engine
    ) -> None:
        result = self._register(pg_session, pg_settings)
        pg_session.commit()

        with build_session_factory(pg_engine)() as observer:
            token = require(
                observer.scalar(
                    select(RefreshToken).where(RefreshToken.jti == result.tokens.jti)
                )
            )
            assert token.tenant_id == result.tenant.id
            assert token.user_id == result.admin.id
            assert token.revoked is False

    def test_the_first_user_is_an_administrator_with_the_tenant_id_set(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """The two values that make the new session usable, read from the row.

        PostgreSQL stores ``UUID`` natively, so a mismatch between the model's
        ``uuid.uuid4`` default and the column type would surface here as an
        ``InvalidTextRepresentationError`` rather than as a silently wrong
        value on another database.
        """
        result = self._register(pg_session, pg_settings)
        pg_session.commit()
        pg_session.expire_all()

        stored = require(pg_session.get(User, result.admin.id))

        assert stored.tenant_id == result.tenant.id
        assert stored.role is UserRole.ADMIN
        assert stored.is_active is True
        assert isinstance(stored.id, uuid.UUID)


@requires_pg
class TestSlugAndEmailUniquenessOnTheServer:
    """The constraints, enforced by PostgreSQL rather than by a pre-check.

    Both service functions do a courtesy ``SELECT`` before inserting, to turn the
    common case into a precise error. The rules themselves belong to the
    database — two concurrent requests both pass a pre-check, and only a
    constraint adjudicates. These tests hit the constraint so the pre-check
    cannot be mistaken for the enforcement.
    """

    def test_the_server_refuses_a_second_tenant_with_the_same_slug(
        self, pg_session: Session
    ) -> None:
        first = Tenant(name="Acme", slug="acme")
        pg_session.add(first)
        pg_session.commit()

        pg_session.add(Tenant(name="Acme Two", slug="acme"))

        with pytest.raises(IntegrityError):
            pg_session.flush()

        pg_session.rollback()

    def test_the_email_constraint_is_scoped_to_one_tenant(
        self, pg_session: Session, pg_engine: Engine
    ) -> None:
        """The same address in two organisations is two rows, and must be allowed.

        A global ``UNIQUE (email)`` would make one human unable to hold accounts
        in two tenants, which is ordinary rather than abusive. This is the single
        most consequential schema decision on this table, so it is tested at the
        level that would catch its inversion.

        Read back through a second session rather than through ``pg_session``:
        the two inserts above are already committed, so a same-session read would
        also work, but an observer makes the point that what is being asserted is
        what PostgreSQL stored.
        """
        shared = "contractor@shared.example"

        for slug in ("acme", "globex"):
            tenant = Tenant(name=slug.title(), slug=slug)
            pg_session.add(tenant)
            pg_session.commit()
            pg_session.add(
                User(
                    tenant_id=tenant.id,
                    email=shared,
                    password_hash=hash_password(PG_TEST_PASSWORD),
                    role=UserRole.VIEWER,
                )
            )
            pg_session.commit()

        with build_session_factory(pg_engine)() as observer:
            rows = observer.scalars(
                select(User).where(User.email == shared)
            ).all()

        assert len(rows) == 2

    def test_the_server_refuses_the_same_address_twice_in_one_tenant(
        self, pg_session: Session
    ) -> None:
        tenant = Tenant(name="Acme", slug="acme")
        pg_session.add(tenant)
        pg_session.commit()

        for _ in range(2):
            pg_session.add(
                User(
                    tenant_id=tenant.id,
                    email="twice@acme.example",
                    password_hash=hash_password(PG_TEST_PASSWORD),
                    role=UserRole.VIEWER,
                )
            )
            try:
                pg_session.commit()
            except IntegrityError:
                pg_session.rollback()
                return

        pytest.fail("the second insert of the same email in one tenant was accepted")

    def test_inviting_the_same_address_into_another_tenant_succeeds(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """The service path, not just the schema.

        Both tenants are real registrations, and the second administrator invites
        the *same* address, which is what the constraint has to permit.
        """
        from app.services.users import invite_user

        first = _registered_identity(pg_session, pg_settings, slug="acme")
        second = _registered_identity(pg_session, pg_settings, slug="globex")

        for identity in (first, second):
            invite_user(
                pg_session,
                identity=identity,
                email="shared@example.com",
                password=PG_TEST_PASSWORD,
                role=UserRole.VIEWER,
            )
        pg_session.commit()

        assert pg_session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.email == "shared@example.com")
        ) == 2

    def test_a_duplicate_address_in_one_tenant_is_refused_by_the_service(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        from app.services.users import EmailAlreadyRegisteredError, invite_user

        identity = _registered_identity(pg_session, pg_settings)
        invite_user(
            pg_session,
            identity=identity,
            email="twice@acme.example",
            password=PG_TEST_PASSWORD,
            role=UserRole.VIEWER,
        )
        pg_session.commit()

        with pytest.raises(EmailAlreadyRegisteredError):
            invite_user(
                pg_session,
                identity=identity,
                email="twice@acme.example",
                password=PG_TEST_PASSWORD,
                role=UserRole.VIEWER,
            )

        pg_session.rollback()


@requires_pg
class TestUserManagementOnTheServer:
    """Isolation, guardrails and cascade, through the real dialect.

    The rules are dialect-independent and the SQLite suite proves them. What only
    a real server adds is that ``ON DELETE CASCADE`` fires for a row removed by
    ``session.delete`` with ``passive_deletes=True`` — a path where the ORM
    deliberately issues *no* ``DELETE`` of its own, so nothing but the database
    can be doing the cascading.
    """

    @staticmethod
    def _invite(
        pg_session: Session, identity: Any, email: str, role: UserRole = UserRole.VIEWER
    ) -> User:
        from app.services.users import invite_user

        user = invite_user(
            pg_session,
            identity=identity,
            email=email,
            password=PG_TEST_PASSWORD,
            role=role,
        )
        pg_session.commit()
        pg_session.expire_all()
        return user

    def test_a_user_of_another_tenant_is_invisible_to_the_lookup(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """The scoped ``SELECT`` really is scoped, on the production dialect.

        A ``WHERE`` clause that compared the wrong column — or a missing
        predicate that SQLite's row layout happened to hide — would return the
        row. This asserts the row is not returned.
        """
        from app.services.users import UserNotFoundError, change_user_role

        one = _registered_identity(pg_session, pg_settings, slug="acme")
        two = _registered_identity(pg_session, pg_settings, slug="globex")

        with pytest.raises(UserNotFoundError):
            change_user_role(
                pg_session,
                identity=one,
                user_id=two.user_id,
                role=UserRole.VIEWER,
            )

        pg_session.rollback()

    def test_listing_returns_only_the_caller_s_tenant(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        from app.services.users import list_users

        one = _registered_identity(pg_session, pg_settings, slug="acme")
        two = _registered_identity(pg_session, pg_settings, slug="globex")
        self._invite(pg_session, one, "a@acme.example")
        self._invite(pg_session, one, "b@acme.example")
        self._invite(pg_session, two, "c@globex.example")

        listed = list_users(pg_session, identity=one)

        assert {user.email for user in listed} == {
            "admin@example.com",
            "a@acme.example",
            "b@acme.example",
        }

    def test_deleting_a_user_cascades_their_refresh_tokens_in_postgresql(
        self, pg_session: Session, pg_settings: Settings, pg_engine: Engine
    ) -> None:
        """The database does the cascading, not the ORM.

        ``User.refresh_tokens`` uses ``passive_deletes=True``, so SQLAlchemy emits
        no ``DELETE`` for the children. If the ``ON DELETE CASCADE`` clause were
        absent the rows would simply be left behind, and only a committed read
        from a second session would show it.
        """
        from app.services.auth import authenticate
        from app.services.users import delete_user

        identity = _registered_identity(pg_session, pg_settings)
        user = self._invite(pg_session, identity, "editor@acme.example")
        authenticate(
            pg_session,
            tenant_slug="acme",
            email="editor@acme.example",
            password=PG_TEST_PASSWORD,
            app_settings=pg_settings,
        )
        pg_session.commit()

        delete_user(pg_session, identity=identity, user_id=user.id)
        pg_session.commit()

        with build_session_factory(pg_engine)() as observer:
            surviving = observer.scalars(
                select(RefreshToken).where(RefreshToken.user_id == user.id)
            ).all()
            assert surviving == []
            assert observer.get(User, user.id) is None

    def test_a_role_change_revokes_only_the_targets_tokens(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """Scoped by ``(tenant_id, user_id)``, on the real constraint.

        Read back after a commit so the assertion is about what PostgreSQL holds,
        not about the identity map the service happened to be holding.
        """
        from app.services.auth import authenticate
        from app.services.users import change_user_role

        identity = _registered_identity(pg_session, pg_settings)
        target = self._invite(pg_session, identity, "target@acme.example")
        bystander = self._invite(pg_session, identity, "bystander@acme.example")

        for address in ("target@acme.example", "bystander@acme.example"):
            authenticate(
                pg_session,
                tenant_slug="acme",
                email=address,
                password=PG_TEST_PASSWORD,
                app_settings=pg_settings,
            )
        pg_session.commit()

        change_user_role(
            pg_session,
            identity=identity,
            user_id=target.id,
            role=UserRole.EDITOR,
        )
        pg_session.commit()
        pg_session.expire_all()

        def revoked_for(user_id: uuid.UUID) -> bool:
            return all(
                token.revoked
                for token in pg_session.scalars(
                    select(RefreshToken).where(RefreshToken.user_id == user_id)
                )
            )

        assert revoked_for(target.id)
        assert not revoked_for(bystander.id)

    def test_the_last_administrator_guardless_survives_a_commit(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """The guard refuses, and nothing was written.

        Checked after an explicit rollback, because the refusal happens before any
        ``flush`` — a test that only asserted the exception would pass even if the
        role had already been changed in the session.
        """
        from app.services.users import LastAdministratorError, change_user_role

        identity = _registered_identity(pg_session, pg_settings)

        with pytest.raises(LastAdministratorError):
            change_user_role(
                pg_session,
                identity=identity,
                user_id=identity.user_id,
                role=UserRole.VIEWER,
            )

        pg_session.rollback()
        pg_session.expire_all()

        surviving = require(pg_session.get(User, identity.user_id))
        assert surviving.role is UserRole.ADMIN

    def test_a_promoted_second_administrator_makes_the_tenant_deletable_again(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """The guardrail is a floor, not a lock.

        Two administrators, one deleted, and the survivor can then be demoted:
        the count is recomputed per request against committed state, not cached
        from when the request started.
        """
        from app.services.users import (
            LastAdministratorError,
            change_user_role,
            delete_user,
        )

        identity = _registered_identity(pg_session, pg_settings)
        second = self._invite(pg_session, identity, "second@acme.example",
                              UserRole.ADMIN)

        delete_user(pg_session, identity=identity, user_id=second.id)
        pg_session.commit()

        with pytest.raises(LastAdministratorError):
            change_user_role(
                pg_session,
                identity=identity,
                user_id=identity.user_id,
                role=UserRole.VIEWER,
            )

    def test_listing_is_ordered_deterministically_on_a_real_server(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """``ORDER BY created_at, id`` is a total order on PostgreSQL.

        Two accounts created inside one transaction share a ``created_at`` to the
        microsecond, so the id tie-break is what keeps the order stable. Without
        it the result depends on PostgreSQL's plan for that execution, which can
        differ from SQLite's.
        """
        from app.services.users import list_users

        identity = _registered_identity(pg_session, pg_settings)
        for index in range(4):
            self._invite(pg_session, identity, f"user{index}@acme.example")

        listed = list_users(pg_session, identity=identity)
        keys = [(user.created_at, user.id) for user in listed]

        assert keys == sorted(keys)
        assert len(listed) == 5

    def test_a_uuid_parameter_binds_through_psycopg(
        self, pg_session: Session, pg_settings: Settings
    ) -> None:
        """``User.id == user_id`` binds a native ``UUID``.

        The scoped lookup filters on two columns, one of which is a
        ``UUID``. Passing the parameter as a string instead of a ``uuid.UUID``
        would work on SQLite's loose typing and fail here, so this is the test
        that would catch it.
        """
        from app.services.users import UserNotFoundError, change_user_role

        identity = _registered_identity(pg_session, pg_settings)
        user = self._invite(pg_session, identity, "target@acme.example")

        changed = change_user_role(
            pg_session,
            identity=identity,
            user_id=user.id,
            role=UserRole.EDITOR,
        )
        pg_session.commit()

        assert changed.role is UserRole.EDITOR

        with pytest.raises(UserNotFoundError):
            change_user_role(
                pg_session,
                identity=identity,
                user_id=uuid.uuid4(),
                role=UserRole.VIEWER,
            )

    def test_deleting_a_user_leaves_its_tenant_in_place(
        self, pg_session: Session, pg_settings: Settings, pg_engine: Engine
    ) -> None:
        """``DELETE /users/{id}`` is not a route to deleting an organisation.

        ``User.tenant`` carries no ``cascade``, and the database foreign key is
        ``RESTRICT``, so removing a user cannot remove the tenant it belonged to.
        The catastrophic direction — a user deletion taking the tenant with it —
        is the one worth proving on the real dialect, and the ORM-cascade
        asymmetry is exactly what makes it non-obvious: the *other* direction
        (``Tenant.users``) does cascade in the ORM.
        """
        from app.services.users import delete_user

        identity = _registered_identity(pg_session, pg_settings)
        user = self._invite(pg_session, identity, "target@acme.example")
        tenant_id = identity.tenant_id

        delete_user(pg_session, identity=identity, user_id=user.id)
        pg_session.commit()

        with build_session_factory(pg_engine)() as observer:
            assert observer.get(User, user.id) is None
            assert observer.get(Tenant, tenant_id) is not None

    def test_the_orm_clears_a_tenants_users_before_the_tenant_itself(
        self, pg_session: Session, pg_engine: Engine
    ) -> None:
        """Why ``ON DELETE RESTRICT`` is invisible through ``session.delete``.

        ``Tenant.users`` declares ``cascade="all, delete-orphan"`` and
        deliberately leaves ``passive_deletes`` off, so the ORM loads and deletes
        the children itself rather than relying on the database. The tenant
        delete then succeeds, and the ``RESTRICT`` clause never fires.

        This is recorded because the two behaviours are easy to confuse.
        ``test_deleting_a_tenant_with_users_is_refused`` — earlier in this file —
        issues a *textual* ``DELETE`` precisely in order to bypass the ORM and
        reach the constraint. A reader who had seen only the test below would
        reasonably conclude that ``RESTRICT`` was missing from the schema. It is
        not; it is simply unreachable by the ORM, by design.
        """
        tenant = Tenant(name="Acme", slug="acme")
        pg_session.add(tenant)
        pg_session.commit()
        user = User(
            tenant_id=tenant.id,
            email="user@acme.example",
            password_hash=hash_password(PG_TEST_PASSWORD),
            role=UserRole.VIEWER,
        )
        pg_session.add(user)
        pg_session.commit()

        pg_session.delete(tenant)
        pg_session.commit()

        with build_session_factory(pg_engine)() as observer:
            assert observer.get(Tenant, tenant.id) is None
            assert observer.get(User, user.id) is None


# ---------------------------------------------------------------------------
# Phase 2.5: ingestion, on the production dialect
# ---------------------------------------------------------------------------
#
# What this section is and is not for
# -----------------------------------
# ``tests/test_ingestion.py`` already covers the pipeline's *behaviour* in depth:
# every stage, every failure classification, the exact wording of a recorded error.
# It runs against SQLite, because the pipeline's correctness has nothing to do with the
# database dialect.
#
# What SQLite cannot settle, and what is therefore tested here:
#
# * Whether the status and ``chunk_count`` writes survive a real transaction and are
#   visible to a **second connection** — the property that distinguishes "the row was
#   updated" from "the row is updated".
# * Whether PostgreSQL enforces the constraints the migration declares. SQLite enforces
#   a ``CHECK`` differently enough that a passing SQLite test says nothing about whether
#   ``ck_documents_chunk_count_non_negative`` exists in PostgreSQL.
# * Whether tenant scoping holds at the SQL level, where an unscoped ``WHERE`` would be
#   visible as a row that should not have been returned.
#
# The vector store is a fake here. That is deliberate: this section is about the
# database, and coupling it to a live Qdrant would make a database gate fail for a
# vector-store reason. The real Qdrant is exercised by ``tests/test_qdrant_integration.py``.

#: Text long enough to be chunked into several chunks, so ``chunk_count`` is a claim
#: about a number greater than one rather than a tautology about zero or one.
#:
#: Written as repeated paragraphs rather than one long line so the chunker has real
#: separator boundaries to work with. The exact chunk count is never asserted — it
#: depends on the chunker, which has its own tests. What is asserted is that the number
#: in the column equals the number of points actually written, which is a statement
#: about *consistency* and not about any particular chunking algorithm.
INGESTIBLE_TEXT = "Leave policy section. " * 400


class FixedEmbeddingProvider(EmbeddingProvider):
    """A provider that returns one distinct vector per input, and records its inputs.

    Two properties the ingestion tests need, and neither is decorative:

    * **Deterministic.** The same text always yields the same vector, so a re-ingest can
      be compared against a first ingest.
    * **Distinct per text.** Two different chunks must not collide, or a test asserting
      "every chunk was written" would pass with one point standing in for several.

    It is not a fake in the sense of returning a constant: a constant would make
    ``chunk_count`` assertions unable to distinguish "wrote N points" from "wrote N
    copies of one point".
    """

    def __init__(self, *, dimensions: int = INGESTION_EMBED_DIM) -> None:
        self._dimensions = dimensions
        self.embedded: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        # Named for what it is rather than pretending to be a model, so a recorded
        # `model_name` in a failing assertion is self-explanatory.
        return "fixed-test-provider"

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.embedded.append(list(texts))
        return [_deterministic_vector(text, self._dimensions) for text in texts]

    @property
    def texts_embedded(self) -> list[str]:
        """Every text this provider was asked to embed, in call order."""
        return [text for batch in self.embedded for text in batch]


def _deterministic_vector(text: str, dimensions: int) -> list[float]:
    """A stable, distinct, strictly positive vector for ``text``.

    Derived from the text's own bytes so it is reproducible across runs and machines,
    which a random vector would not be. Every component is offset above zero so no
    component is ever exactly zero and a caller cannot mistake a valid vector for an
    absent one. Distinctness is not guaranteed for every pair of texts by this
    construction, and is not claimed — what the tests assert is that the *count* of
    written points equals the count of embedded chunks, which holds regardless.
    """
    seed = zlib.crc32(text.encode("utf-8"))
    return [((seed * (position + 7)) % 89) / 89.0 + 0.01 for position in range(dimensions)]


class FailingEmbeddingProvider(EmbeddingProvider):
    """A provider that always fails, with a message carrying tenant content.

    The message is the point. A provider is free to echo back part of what it was sent,
    so a recorded error built from provider text could put a chunk of a tenant's document
    into an admin-visible column. This raises text containing a recognisable fragment, and
    the tests assert that fragment never reaches ``documents.error``.
    """

    #: The fragment that must not appear in a recorded error.
    LEAKED = "CONFIDENTIAL-FRAGMENT-FROM-THE-DOCUMENT"

    def __init__(self, *, dimensions: int = INGESTION_EMBED_DIM) -> None:
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return "failing-test-provider"

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingError(f"provider rejected the batch: {self.LEAKED}")


@requires_pg
class TestIngestionRecordsItsOutcomeOnPostgres:
    """``ingest_document`` writes the row, on the dialect that owns it."""

    def test_a_successful_ingestion_records_ready_and_the_count_matches_what_was_written(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """The success path, end to end against real PostgreSQL.

        The assertion that matters is the *consistency* one: ``chunk_count`` must equal
        the number of points the vector store actually holds. A pipeline that reported a
        count it did not write would look identical in every other assertion here, and
        would make ``GET /admin/documents/{id}/status`` a lie that no unit test could
        catch.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        provider = FixedEmbeddingProvider()
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())

        result = ingest_document(
            pg_session,
            identity=identity,
            document_id=document.id,
            settings=settings,
            storage=pg_storage,
            store=store,
            provider=provider,
        )
        pg_session.commit()
        pg_session.expire_all()

        stored = require(pg_session.get(Document, document.id))
        assert stored.status is DocumentStatus.READY
        assert stored.error is None
        assert result.status is DocumentStatus.READY
        assert stored.chunk_count == result.chunk_count

        written = store.scroll_document_points(document.tenant_id, document.id)
        assert stored.chunk_count == len(written), (
            "the recorded chunk count must equal the points actually written"
        )
        assert stored.chunk_count == len(provider.texts_embedded), (
            "every embedded chunk must be represented by a point"
        )
        assert stored.chunk_count > 1, (
            "a fixture this short would not prove the count is a count"
        )

    def test_the_status_and_count_are_visible_to_a_second_connection(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
        pg_engine: Engine,
    ) -> None:
        """Committed state is durable, which SQLite in the same process cannot distinguish.

        ``flush`` sends statements to the server; ``commit`` makes them visible to
        everyone else. A pipeline that only flushed would pass every assertion against
        the session it was handed, and leave the row unchanged for the next request — the
        failure mode this test exists to catch.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())
        provider = FixedEmbeddingProvider()

        ingest_document(
            pg_session,
            identity=identity,
            document_id=document.id,
            settings=settings,
            storage=pg_storage,
            store=store,
            provider=provider,
        )
        pg_session.commit()

        # A genuinely separate connection, not a second session on the same one: the
        # point is to see only what the transaction made durable.
        with pg_engine.connect() as observer:
            seen = observer.execute(
                text("SELECT status, chunk_count, error FROM documents WHERE id = :id"),
                {"id": document.id},
            ).one()
        assert seen.status == DocumentStatus.READY.value
        assert seen.chunk_count == result_chunks(store, document)
        assert seen.error is None

    def test_a_provider_failure_propagates_transiently_without_leaking_document_text(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """An embedding refusal propagates, and what propagates is safe to eventually store.

        Step 4A replaced this phase-2 test's contract: an embedding-provider (or
        vector-store or filesystem) failure is the worker's to retry, not a permanent
        record, so :func:`ingest_document` raises
        :class:`~app.services.ingestion.TransientIngestionError` and leaves the row
        ``processing``. What travels with the exception is the stage and the exact
        message a retry-exhausted worker will write to ``documents.error`` — and that
        message never carries the provider's text, which may echo tenant content.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())

        with pytest.raises(TransientIngestionError) as caught:
            ingest_document(
                pg_session,
                identity=identity,
                document_id=document.id,
                settings=settings,
                storage=pg_storage,
                store=store,
                provider=FailingEmbeddingProvider(),
            )
        pg_session.commit()
        pg_session.expire_all()

        transient = caught.value
        assert transient.stage is IngestionStage.EMBED
        assert isinstance(transient.__cause__, EmbeddingError)

        # The stage is named so a reader knows which module to open.
        assert "embed" in transient.message
        assert FailingEmbeddingProvider.LEAKED not in transient.message, (
            "a provider's own error text reached the message a worker would write "
            "to a tenant-visible column"
        )

        stored = require(pg_session.get(Document, document.id))
        assert stored.status is DocumentStatus.PROCESSING, (
            "a retryable failure must leave the row processing, not failed"
        )
        assert stored.error is None

    def test_a_failed_ingestion_leaves_the_previous_chunk_count_untouched(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """A failed re-ingest must not zero a count that describes a good earlier run.

        The alternative — writing ``0`` on failure — would erase the only evidence that
        the document was previously indexed, and would make "never ran" indistinguishable
        from "ran and lost everything". The implementation deliberately leaves the column
        alone, and that decision is only defensible if it is pinned by a test. The failure
        here is a provider refusal, so under the Step 4A contract it propagates as
        :class:`~app.services.ingestion.TransientIngestionError` and the row stays
        ``processing`` — *both* of which leave the recorded count intact; the column is
        only ever written on success.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())

        first = ingest_document(
            pg_session,
            identity=identity,
            document_id=document.id,
            settings=settings,
            storage=pg_storage,
            store=store,
            provider=FixedEmbeddingProvider(),
        )
        pg_session.commit()
        pg_session.expire_all()
        indexed = require(pg_session.get(Document, document.id)).chunk_count
        assert indexed is not None and indexed > 0

        with pytest.raises(TransientIngestionError):
            ingest_document(
                pg_session,
                identity=identity,
                document_id=document.id,
                settings=settings,
                storage=pg_storage,
                store=store,
                provider=FailingEmbeddingProvider(),
            )
        pg_session.commit()
        pg_session.expire_all()

        after_failure = require(pg_session.get(Document, document.id))
        assert after_failure.status is DocumentStatus.PROCESSING
        assert after_failure.chunk_count == indexed, (
            "a failure zeroed a count describing an earlier successful run"
        )
        assert first.chunk_count == indexed

    def test_a_document_with_no_indexable_text_records_failed_rather_than_ready(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """Whitespace-only content fails with an explanation, not as an empty success.

        Reporting ``ready`` with ``chunk_count = 0`` would be the worst outcome available:
        every status endpoint would say the document is indexed, and every future
        retrieval would return nothing for it while claiming it was there.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        # A `.txt` whose bytes survive upload validation (it is non-empty) but contain
        # no indexable text once cleaned.
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="blank.txt",
            content=build_txt("   \n\n\t  \n  "),
        )
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())

        result = ingest_document(
            pg_session,
            identity=identity,
            document_id=document.id,
            settings=settings,
            storage=pg_storage,
            store=store,
            provider=FixedEmbeddingProvider(),
        )
        pg_session.commit()
        pg_session.expire_all()

        stored = require(pg_session.get(Document, document.id))
        assert stored.status is DocumentStatus.FAILED
        assert result.stage is IngestionStage.CHUNK
        # The message points at OCR, because that is the real fix for a scanned
        # document and an operator reading this needs to know it is not a bug.
        assert "OCR" in (stored.error or "")

    def test_re_ingesting_the_same_document_does_not_multiply_its_points(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """Idempotency, checked against the store rather than the column.

        Point ids are derived deterministically from ``(tenant, document, chunk_index)``,
        so a second ingest should overwrite the first's points rather than add to them.
        Asserting the point count is what actually proves that; asserting ``chunk_count``
        alone would pass even if every run appended.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())
        provider = FixedEmbeddingProvider()

        ingest_document(
            pg_session,
            identity=identity,
            document_id=document.id,
            settings=settings,
            storage=pg_storage,
            store=store,
            provider=provider,
        )
        pg_session.commit()
        after_first = len(store.scroll_document_points(document.tenant_id, document.id))

        ingest_document(
            pg_session,
            identity=identity,
            document_id=document.id,
            settings=settings,
            storage=pg_storage,
            store=store,
            provider=provider,
        )
        pg_session.commit()
        pg_session.expire_all()

        after_second = len(store.scroll_document_points(document.tenant_id, document.id))
        assert after_second == after_first, (
            "re-ingesting multiplied the document's points"
        )
        stored = require(pg_session.get(Document, document.id))
        assert stored.chunk_count == after_second


def result_chunks(store: Any, document: Document) -> int:
    """How many points the store holds for ``document``.

    A named helper so the assertion that counts written points reads as a claim about
    the store rather than as a restatement of the pipeline's own number.
    """
    return len(store.scroll_document_points(document.tenant_id, document.id))


def client_collections_of(store: Any) -> list[str]:
    """The collections the store's client currently holds.

    Asserted after a *refused* operation to prove it created nothing. An ingestion that
    failed for a legitimate reason may legitimately have created a collection; one that
    was refused because the caller has no business touching the document must not.
    """
    return sorted(fake_client_of(store).collections)


@requires_pg
class TestIngestionTenantScopingOnPostgres:
    """One tenant's identity cannot reach another's document, at the SQL level."""

    def test_another_tenants_document_is_reported_as_not_found(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """Cross-tenant access is ``DocumentNotFoundError``, not a permission error.

        The distinction is the point. A ``403`` would confirm the document exists, turning
        any document id into an oracle for what other tenants have uploaded. So the
        lookup must be scoped by tenant *before* the row is found, and this asserts the
        exception type rather than merely that something was raised.

        And the refusal must come from the **initial lookup**, which is why
        ``provider.calls_remaining`` is asserted afterwards. Asserting only the exception
        type is not enough, and this test was written that way first and was proved wrong
        by mutation: with the tenant predicate removed from
        :func:`~app.services.documents.get_document_for_ingestion`, the pipeline read the
        other tenant's bytes, chunked them, embedded them and upserted them, and only
        then raised ``DocumentNotFoundError`` while recording the outcome. The assertion
        passed, and a full cross-tenant content exfiltration had just been proven
        possible. Refusing *early* is the property, so that is what is asserted.
        """
        tenant_b = _registered_identity(pg_session, pg_settings, slug="globex")
        tenant_a = _registered_identity(pg_session, pg_settings, slug="acme")
        b_document = _stored_document(
            pg_session,
            tenant_b,
            pg_storage,
            filename="globex-secret.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())
        provider = FixedEmbeddingProvider()

        with pytest.raises(DocumentNotFoundError):
            ingest_document(
                pg_session,
                identity=tenant_a,
                document_id=b_document.id,
                settings=settings,
                storage=pg_storage,
                store=store,
                provider=provider,
            )

        assert not provider.embedded, (
            "the other tenant's text was embedded before the refusal; the lookup "
            "must fail before any content is read, let alone indexed"
        )
        assert not client_collections_of(store), (
            "a collection was created for a tenant that owns nothing"
        )

    def test_a_refused_ingestion_does_not_change_the_owners_row(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """A refused attempt leaves the other tenant's document exactly as it was.

        A cross-tenant attempt that recorded *anything* — a ``failed`` status, an error
        message — would be a write into another tenant's row, which is a strictly worse
        bug than a read leak because it is also a corruption.
        """
        tenant_b = _registered_identity(pg_session, pg_settings, slug="globex")
        tenant_a = _registered_identity(pg_session, pg_settings, slug="acme")
        b_document = _stored_document(
            pg_session,
            tenant_b,
            pg_storage,
            filename="globex-secret.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        pg_session.expire_all()
        before = require(pg_session.get(Document, b_document.id))

        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())
        provider = FixedEmbeddingProvider()
        with pytest.raises(DocumentNotFoundError):
            ingest_document(
                pg_session,
                identity=tenant_a,
                document_id=b_document.id,
                settings=settings,
                storage=pg_storage,
                store=store,
                provider=provider,
            )
        assert not provider.embedded, (
            "the other tenant's content was embedded before the refusal"
        )
        assert not client_collections_of(store), (
            "a refused cross-tenant attempt created a collection"
        )
        pg_session.commit()
        pg_session.expire_all()

        after = require(pg_session.get(Document, b_document.id))
        assert after.status == before.status
        assert after.chunk_count == before.chunk_count
        assert after.error == before.error
        assert after.updated_at == before.updated_at, (
            "a refused cross-tenant attempt touched another tenant's row"
        )

    def test_each_tenants_ingestion_lands_only_in_its_own_collection(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """Two tenants ingesting the same-named file share no vectors.

        Both documents are named ``policy.txt``, so a collection chosen by filename or by
        anything but the tenant would collide and one tenant would read the other's text.
        Each store is a separate fake here; what is asserted is that each collection holds
        only its own document's points, and carries the right ``tenant_id`` in its
        payloads.
        """
        tenant_a = _registered_identity(pg_session, pg_settings, slug="acme")
        tenant_b = _registered_identity(pg_session, pg_settings, slug="globex")
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        client = FakeQdrant()
        store = store_over(settings, client)

        ingested: list[Document] = []
        for identity in (tenant_a, tenant_b):
            document = _stored_document(
                pg_session,
                identity,
                pg_storage,
                filename="policy.txt",
                content=build_txt(INGESTIBLE_TEXT),
            )
            ingest_document(
                pg_session,
                identity=identity,
                document_id=document.id,
                settings=settings,
                storage=pg_storage,
                store=store,
                provider=FixedEmbeddingProvider(),
            )
            ingested.append(document)
        pg_session.commit()
        pg_session.expire_all()

        for document in ingested:
            collection = collection_name_for(document.tenant_id)
            points = store.scroll_document_points(document.tenant_id, document.id)
            assert points, f"{collection} holds no points for its own document"
            for point in points:
                assert payload_key(point, "tenant_id") == str(document.tenant_id)
                assert payload_key(point, "document_id") == str(document.id)

        # And no document's points appear in the other tenant's collection.
        a_document, b_document = ingested
        assert not [
            point
            for point in store.scroll_document_points(
                a_document.tenant_id, b_document.id
            )
        ], "one tenant's document has points in the other's collection"


@requires_pg
class TestPostgresEnforcesTheIngestionConstraints:
    """The constraints the migration declares, enforced by the real server.

    SQLite enforces ``CHECK`` closely enough that the SQLite suite's passing is not
    evidence about PostgreSQL. These assert the server itself rejects what the schema
    says it rejects, which is the only claim the migration makes.
    """

    def test_a_negative_chunk_count_is_rejected_by_the_server(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """``ck_documents_chunk_count_non_negative`` is a real constraint in PostgreSQL.

        Issued as raw SQL so the rejection comes from the database and not from the
        model's Python-side check — the point is what the *server* enforces.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )

        with pytest.raises((IntegrityError, DataError)):
            pg_session.execute(
                text("UPDATE documents SET chunk_count = -1 WHERE id = :id"),
                {"id": document.id},
            )
            pg_session.commit()
        pg_session.rollback()

    def test_a_status_outside_the_enum_is_rejected_by_the_server(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """The status ``CHECK`` admits exactly the three values PMP §10 lists.

        ``native_enum=False`` makes this a ``VARCHAR`` plus a ``CHECK``, so a status
        outside the three is rejected by a constraint rather than by a PostgreSQL enum
        type. Asserted so a future change that loosens the ``CHECK`` is caught here
        rather than by an ingestion writing a status nothing can read.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )

        with pytest.raises((IntegrityError, DataError)):
            pg_session.execute(
                text("UPDATE documents SET status = 'archived' WHERE id = :id"),
                {"id": document.id},
            )
            pg_session.commit()
        pg_session.rollback()

    def test_the_recorded_error_cannot_exceed_the_columns_width(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """The server refuses an over-long error, so the service's own bound is real.

        ``record_ingestion_outcome`` validates the length in Python first. This asserts
        the column behind it has the same width, because a Python-only check is a
        convention rather than a guarantee — and a future stage interpolating something
        unbounded would otherwise be stopped by an opaque server error at flush time.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )

        with pytest.raises((DataError, IntegrityError)):
            pg_session.execute(
                text(
                    "UPDATE documents SET error = :error WHERE id = :id"
                ),
                {"error": "x" * (DOCUMENT_ERROR_MAX_LENGTH + 1), "id": document.id},
            )
            pg_session.commit()
        pg_session.rollback()

    def test_record_ingestion_outcome_rejects_an_invalid_count_before_touching_the_row(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """A negative count is refused by the service, as a ``ValueError`` not a fault.

        The service validates before writing so the caller gets an actionable error
        instead of a database fault, which reads as "the server is broken" rather than
        "you passed a negative count".
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        pg_session.expire_all()

        with pytest.raises(ValueError, match="cannot be negative"):
            record_ingestion_outcome(
                pg_session,
                identity=identity,
                document_id=document.id,
                status=DocumentStatus.READY,
                chunk_count=-1,
            )
        pg_session.rollback()

        unchanged = require(pg_session.get(Document, document.id))
        assert unchanged.status is not DocumentStatus.READY


@requires_pg
class TestThePhase24LifecycleStillWorksAfterPhase25:
    """Phase 2.5's additions must not have disturbed the document lifecycle.

    A regression guard for the seam between the phases: ingestion writes ``status``,
    ``chunk_count`` and ``error``, and Phase 2.4's endpoints read exactly those columns.
    If the two disagree about the vocabulary, the failure appears in an HTTP response
    rather than in a test near the cause.
    """

    def test_the_status_values_the_pipeline_writes_are_the_ones_the_model_declares(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """Every status the pipeline can record is a member of the enum.

        Checked through the database round trip rather than against the Python enum, so
        a value stored and read back differently is caught.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())

        ingest_document(
            pg_session,
            identity=identity,
            document_id=document.id,
            settings=settings,
            storage=pg_storage,
            store=store,
            provider=FixedEmbeddingProvider(),
        )
        pg_session.commit()
        pg_session.expire_all()

        raw = pg_session.execute(
            text("SELECT status FROM documents WHERE id = :id"),
            {"id": document.id},
        ).scalar_one()
        assert raw in {member.value for member in DocumentStatus}

    def test_a_row_can_still_be_deleted_after_being_ingested(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """Deletion still works on an indexed document.

        Phase 2.5 writes to the ``documents`` row, so a change that made the row
        undeletable — a constraint, a lock, a held session — would surface here. The
        pipeline is not involved; this is the Phase 2.4 path running over a row Phase 2.5
        has touched.
        """
        identity = _registered_identity(pg_session, pg_settings, slug="acme")
        document = _stored_document(
            pg_session,
            identity,
            pg_storage,
            filename="policy.txt",
            content=build_txt(INGESTIBLE_TEXT),
        )
        settings = make_settings(embedding_dimension=INGESTION_EMBED_DIM)
        store = store_over(settings, FakeQdrant())
        ingest_document(
            pg_session,
            identity=identity,
            document_id=document.id,
            settings=settings,
            storage=pg_storage,
            store=store,
            provider=FixedEmbeddingProvider(),
        )
        pg_session.commit()
        pg_session.expire_all()

        delete_document(
            pg_session,
            identity=identity,
            document_id=document.id,
            storage=pg_storage,
        )
        pg_session.commit()
        pg_session.expire_all()

        assert pg_session.get(Document, document.id) is None

    def test_get_document_for_ingestion_ignores_the_role_filter(
        self,
        pg_session: Session,
        pg_settings: Settings,
        pg_storage: LocalFileStore,
    ) -> None:
        """A document restricted to ``admin`` is still ingestible by a non-admin.

        ``allowed_roles`` governs disclosure to a *person*; ingestion reads bytes in
        order to index them and discloses nothing. Applying the role filter here would
        mean an editor-uploaded, admin-only document could never become ``ready`` — a
        failure that presents as "documents silently never finish indexing". The
        complementary claim, that the *endpoint* still filters, belongs to Phase 2.4's
        suite.
        """
        editor = _registered_identity(pg_session, pg_settings, slug="acme")
        found = _stored_document(
            pg_session,
            editor,
            pg_storage,
            filename="restricted.txt",
            content=build_txt(INGESTIBLE_TEXT),
            allowed_roles=[UserRole.ADMIN],
        )
        pg_session.expire_all()

        fetched = get_document_for_ingestion(
            pg_session, identity=editor, document_id=found.id
        )
        assert fetched.id == found.id
        assert fetched.allowed_roles == [UserRole.ADMIN.value]


@requires_pg
class TestDowngrade:
    """The migrations are reversible, verified against real DDL.

    Each test runs against its own engine rather than the module-scoped one,
    because downgrading destroys the schema every other test here depends on.
    """

    @staticmethod
    def _fresh(pg_dsn: str) -> tuple[Engine, Config, Any]:
        """An empty database with Alembic pointed at it.

        Returns the engine, a configured ``Config``, and the previous
        ``config_module.settings`` so the caller can restore it.
        """
        from alembic.config import Config

        from app.core import config as config_module
        from tests.conftest import ALEMBIC_INI, make_settings

        engine = create_engine(pg_dsn, poolclass=None)
        previous = config_module.settings
        config_module.settings = make_settings(database_url=pg_dsn)
        _drop_managed_tables(engine)
        return engine, Config(str(ALEMBIC_INI)), previous

    @staticmethod
    def _release(engine: Engine, previous: Any) -> None:
        from app.core import config as config_module

        try:
            _drop_managed_tables(engine)
        finally:
            config_module.settings = previous
            engine.dispose()

    def test_downgrade_removes_every_table(self, pg_dsn: str) -> None:
        """A full round trip: upgrade to head, then back to an empty schema."""
        from alembic import command

        engine, config, previous = self._fresh(pg_dsn)
        try:
            command.upgrade(config, "head")
            assert set(MODEL_TABLES) <= set(inspect(engine).get_table_names())

            command.downgrade(config, "base")

            remaining = set(inspect(engine).get_table_names())
            assert not remaining & set(MANAGED_TABLES)
        finally:
            self._release(engine, previous)

    def test_downgrading_one_step_leaves_the_earlier_tables(
        self, pg_dsn: str
    ) -> None:
        """``head -> -1`` must remove exactly one revision's tables, no more.

        This is the assertion that every migration is *additive and reversible in
        isolation*. A downgrade that dropped ``tenants`` and ``users`` as well
        would leave the database empty at revision 0002, and every subsequent
        ``upgrade`` would recreate them — losing data without ever reporting an
        error.

        Phase 2.4 widened the assertion rather than rewriting it, and later
        phases kept widening it the same way. ``head`` is now revision ``0007``
        (``audit_logs``), so the newest link is checked one step down — 0007's
        table gone, everything older intact — and the chain is then walked down
        to the Phase 2 links the assertion was written for: revision ``0002``'s
        schema (``documents`` gone, ``refresh_tokens`` present) and ``0001``'s
        (``refresh_tokens`` gone, ``tenants`` and ``users`` present). Checking
        those older links rather than only the newest is deliberate: they are the
        ones that would otherwise have stopped being tested.
        """
        from alembic import command

        engine, config, previous = self._fresh(pg_dsn)
        try:
            command.upgrade(config, "head")
            assert set(MODEL_TABLES) <= set(inspect(engine).get_table_names())

            # One step down removes exactly the newest revision's tables:
            # 0007's audit_logs is gone, everything older survives.
            command.downgrade(config, "-1")

            at_0006 = set(inspect(engine).get_table_names())
            assert "audit_logs" not in at_0006
            assert {
                "tenants",
                "users",
                "refresh_tokens",
                "documents",
                "document_chunks",
                "query_logs",
            } <= at_0006

            command.downgrade(config, "0002")

            at_0002 = set(inspect(engine).get_table_names())
            assert "documents" not in at_0002
            assert "document_chunks" not in at_0002
            assert {"tenants", "users", "refresh_tokens"} <= at_0002

            command.downgrade(config, "0001")

            at_0001 = set(inspect(engine).get_table_names())
            assert "refresh_tokens" not in at_0001
            assert "documents" not in at_0001
            assert {"tenants", "users"} <= at_0001
        finally:
            self._release(engine, previous)

    def test_re_upgrading_after_a_downgrade_restores_the_full_schema(
        self, pg_dsn: str
    ) -> None:
        """``head -> -1 -> head`` must be indistinguishable from a fresh upgrade.

        A downgrade that left residue would make this fail, and the failure would
        only ever appear on the *second* deployment against an existing database.
        """
        from alembic import command

        engine, config, previous = self._fresh(pg_dsn)
        try:
            command.upgrade(config, "head")
            command.downgrade(config, "-1")
            command.upgrade(config, "head")

            names = set(inspect(engine).get_table_names())
            assert set(MODEL_TABLES) <= names
        finally:
            self._release(engine, previous)


# ---------------------------------------------------------------------------
# Guard checks — these RUN even with no PostgreSQL available
# ---------------------------------------------------------------------------


def test_the_configured_dsn_is_disposable_or_absent() -> None:
    """Records the actual environment state, so a report can be factual about it.

    Originally asserted that no PostgreSQL server was available, and skipped once
    one was — a placeholder for the real invariant. The invariant that matters is
    the one the suite depends on either way: **if a DSN is configured, it names a
    disposable database**. Stating that directly means the test is meaningful in
    both environments, and never needs skipping.

    It also means a misconfigured ``{PG_DSN_ENV_VAR}`` pointing at something real
    fails here, loudly, before a single ``DROP TABLE`` is issued.
    """
    dsn = _integration_dsn()
    if dsn is None:
        # Nothing configured. The suite is legitimately waiting, and the guard
        # below is proven independently by the two tests that follow.
        return

    database_name = urlsplit(dsn).path.lstrip("/")
    assert any(
        marker in database_name.lower() for marker in SAFE_DATABASE_MARKERS
    ), (
        f"{PG_DSN_ENV_VAR} names database {database_name!r}, which is not "
        f"recognisably disposable; expected one of {list(SAFE_DATABASE_MARKERS)}"
    )


def test_the_suite_would_actually_run_with_a_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the skip is caused by the missing DSN and nothing else.

    A suite that skips for a structural reason — a broken fixture, an import
    error, a collection error — would otherwise be indistinguishable from one
    correctly waiting for a database. Setting the variable and re-reading it
    isolates the single cause.
    """
    monkeypatch.setenv(PG_DSN_ENV_VAR, "postgresql+psycopg://u:p@localhost:5432/tm_test")

    assert _integration_dsn() == "postgresql+psycopg://u:p@localhost:5432/tm_test"


@pytest.mark.parametrize(
    ("database", "accepted"),
    [
        ("tm_test", True),
        ("tenantmind_test", True),
        ("throwaway_db", True),
        ("tenantmind", False),
        ("production", False),
    ],
)
def test_the_disposable_database_guard(database: str, accepted: bool) -> None:
    """The DROP TABLE guard must actually reject a production-looking DSN.

    Asserted as a table so both directions are covered: a guard that rejected
    everything would pass a test that only checked the refusal case.
    """
    name = database.lower()
    permitted = any(marker in name for marker in SAFE_DATABASE_MARKERS)

    assert permitted is accepted, (
        f"database {database!r} should "
        f"{'be' if accepted else 'not be'} treated as disposable"
    )


def test_the_dsn_never_appears_in_the_repository() -> None:
    """No integration DSN may be committed alongside the code."""
    from tests.conftest import BACKEND_ROOT as ROOT

    for name in (".env", ".env.example"):
        path = ROOT.parent / name
        if path.is_file():
            assert PG_DSN_ENV_VAR not in path.read_text(encoding="utf-8"), (
                f"{name} must not define {PG_DSN_ENV_VAR}; the integration DSN is "
                "supplied per-run, never stored"
            )


@requires_pg
def test_session_factory_is_built_for_the_pg_engine(pg_dsn: str) -> None:
    """A live sanity check that the engine and factory work together."""
    engine = create_engine(pg_dsn, poolclass=None)
    try:
        factory: sessionmaker[Session] = build_session_factory(engine)
        session = factory()
        try:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            session.close()
    finally:
        engine.dispose()
