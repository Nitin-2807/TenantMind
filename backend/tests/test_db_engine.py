"""Tests for engine construction and DSN resolution.

The central claim under test: **importing the database layer performs no I/O
and requires no database.** Everything else here supports that, so that the
ordinary test suite can run on a machine with no PostgreSQL server.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db import engine as engine_module
from app.db.engine import (
    MAX_OVERFLOW,
    POOL_PRE_PING,
    POOL_RECYCLE_SECONDS,
    POOL_SIZE,
    POOL_TIMEOUT_SECONDS,
    DatabaseNotConfiguredError,
    build_engine_kwargs,
    create_engine_from_settings,
    create_engine_from_url,
    get_engine,
    normalize_dsn,
    reset_engine,
    resolve_dsn,
    sqlite_dsn,
)
from tests.conftest import make_settings, queue_pool


def read_module_source(module: ModuleType) -> str:
    """Return a module's source text, for static safety assertions."""
    path = module.__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


def environment_accesses(source: str) -> set[str]:
    """Return every ``os.getenv`` / ``os.environ`` access in ``source``.

    Parses the source and inspects real attribute accesses, so mentions inside
    docstrings and comments are correctly ignored. ``os.getenv`` and
    ``os.environ.get`` are reported as ``os.getenv`` and ``os.environ``
    respectively.
    """
    tree = ast.parse(source)
    found: set[str] = set()

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr in {"getenv", "environ"}
        ):
            found.add(f"os.{node.attr}")

        # `from os import getenv` / `from os import environ`
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            found.update(f"os.{alias.name}" for alias in node.names)

    return found


# ---------------------------------------------------------------------------
# Configuration — the DSN comes from Settings
# ---------------------------------------------------------------------------


def test_dsn_is_read_from_settings() -> None:
    settings = make_settings(
        database_url="postgresql+psycopg://app_user:s3cret@db.internal:5432/tenantmind"
    )

    assert resolve_dsn(settings) == (
        "postgresql+psycopg://app_user:s3cret@db.internal:5432/tenantmind"
    )


def test_bare_postgresql_dsn_is_upgraded_to_psycopg() -> None:
    """A natural DSN must not fail on SQLAlchemy's psycopg2 default.

    SQLAlchemy resolves bare ``postgresql://`` to the psycopg2 dialect, which
    is not installed. Without this normalisation a developer's ordinary DSN
    would raise ``ModuleNotFoundError: No module named 'psycopg2'``.
    """
    settings = make_settings(database_url="postgresql://u:p@localhost:5432/db")

    assert resolve_dsn(settings).startswith("postgresql+psycopg://")


def test_engine_module_contains_no_hardcoded_dsn() -> None:
    """No PostgreSQL DSN may be baked into the source.

    The module legitimately mentions the ``postgresql://`` *scheme* to explain
    the psycopg2-default hazard, so the check is for a complete DSN — scheme
    plus host — rather than for the scheme string itself.
    """
    text = read_module_source(engine_module)

    # A full DSN would look like postgresql://<user>:<pw>@<host>:<port>/<db>
    assert not re.search(r"postgresql(?:\+[\w]+)?://[^\s\"']*@", text)
    assert "postgresql://localhost" not in text


def test_engine_module_never_reads_the_environment_directly() -> None:
    """Configuration must flow through Settings, not os.getenv.

    Checked against the parsed syntax tree rather than raw text, because the
    module docstring mentions ``os.getenv`` precisely to state that it is not
    used. An AST walk sees only real code, so prose cannot produce a false
    result in either direction.
    """
    accesses = environment_accesses(read_module_source(engine_module))

    assert accesses == set(), (
        f"database layer reads the environment directly: {sorted(accesses)}"
    )


def test_unset_database_url_raises_a_precise_error() -> None:
    """Absence must be reported as configuration, not as a connection fault."""
    settings = make_settings(database_url=None)

    with pytest.raises(DatabaseNotConfiguredError) as excinfo:
        resolve_dsn(settings)

    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "not set" in message


def test_empty_database_url_raises() -> None:
    settings = make_settings(database_url="   ")

    with pytest.raises(DatabaseNotConfiguredError) as excinfo:
        resolve_dsn(settings)

    assert "empty" in str(excinfo.value)


def test_optional_database_config_is_compatible_with_development() -> None:
    """Phase 1 development runs with no database, by design."""
    settings = make_settings(app_env="development", database_url=None)

    assert settings.database_url is None
    # The application still builds; only engine creation needs a DSN.
    from app.main import create_app

    assert create_app(settings).title == "TenantMind"


def test_dsn_is_never_returned_in_a_repr_or_error() -> None:
    """The masked SecretStr must stay masked through the db layer."""
    sentinel = "sup3rs3cret-value"
    settings = make_settings(database_url=f"postgresql://u:{sentinel}@db:5432/tenantmind")

    assert sentinel not in repr(settings)
    assert sentinel not in str(settings)
    # Resolution yields the real DSN, because the driver needs it.
    assert sentinel in resolve_dsn(settings)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@h:5432/d", "postgresql+psycopg://u:p@h:5432/d"),
        (
            "postgresql+psycopg://u:p@h:5432/d",
            "postgresql+psycopg://u:p@h:5432/d",
        ),
        (
            "postgresql+psycopg2://u:p@h:5432/d",
            "postgresql+psycopg2://u:p@h:5432/d",
        ),
        ("sqlite+pysqlite://", "sqlite+pysqlite://"),
        ("sqlite://", "sqlite://"),
    ],
)
def test_normalize_dsn_only_touches_the_bare_postgresql_scheme(
    raw: str, expected: str
) -> None:
    assert normalize_dsn(raw) == expected


# ---------------------------------------------------------------------------
# Engine construction
# ---------------------------------------------------------------------------


def test_engine_creation_does_not_connect() -> None:
    """Creating an engine must not open a socket.

    A PostgreSQL DSN pointing at an unreachable host is used deliberately: if
    construction tried to connect, this would raise.
    """
    engine = create_engine_from_url(
        "postgresql+psycopg://user:pw@127.0.0.1:59999/tenantmind"
    )

    try:
        assert isinstance(engine, Engine)
    finally:
        engine.dispose()


def test_importing_app_db_opens_no_connection(no_network: None) -> None:
    """Importing the package must be free of side effects, including network.

    The ``no_network`` fixture makes any real socket connection raise, so this
    is stronger than asserting on connection counts: an engine built eagerly at
    import time, or a driver doing its own version handshake, would fail here.
    """
    import importlib

    import app.db

    importlib.reload(app.db)

    assert app.db.Base is not None


def test_building_an_engine_opens_no_socket(no_network: None) -> None:
    """Engine construction prepares a pool; it does not dial the server.

    The DSN points at a host that would refuse immediately, so a construction
    path that connected would raise here rather than merely being slow.
    """
    engine = create_engine_from_url("postgresql+psycopg://u:p@db.invalid:5432/d")

    try:
        assert engine.url.get_backend_name() == "postgresql"
    finally:
        engine.dispose()


def test_get_engine_is_lazy_and_raises_when_unconfigured() -> None:
    """The first request for an engine, not the import, is where config matters."""
    reset_engine()

    with pytest.raises(DatabaseNotConfiguredError):
        get_engine()


def test_get_engine_is_cached() -> None:
    settings = make_settings(database_url=sqlite_dsn("/tmp/tenantmind_engine_cache.db"))

    reset_engine()
    try:
        first = get_engine(settings)
        second = get_engine()

        assert first is second
    finally:
        reset_engine()


def test_reset_engine_allows_rebuilding() -> None:
    settings = make_settings(database_url=sqlite_dsn("/tmp/tenantmind_reset.db"))

    reset_engine()
    first = get_engine(settings)

    reset_engine()
    second = get_engine(settings)

    try:
        assert first is not second
    finally:
        reset_engine()


def test_create_engine_from_settings_uses_the_configured_dsn() -> None:
    settings = make_settings(database_url=sqlite_dsn("/tmp/tenantmind_cfg.db"))

    engine = create_engine_from_settings(settings)
    try:
        assert engine.url.get_backend_name() == "sqlite"
    finally:
        engine.dispose()


def test_engine_creation_is_deterministic() -> None:
    """Same DSN and settings must produce equivalent engines."""
    dsn = "postgresql+psycopg://u:p@localhost:5432/tenantmind"

    first = create_engine_from_url(dsn)
    second = create_engine_from_url(dsn)

    try:
        assert first.url == second.url
        assert type(first.dialect) is type(second.dialect)
        assert queue_pool(first).size() == queue_pool(second).size()
    finally:
        first.dispose()
        second.dispose()


def test_debug_setting_controls_echo() -> None:
    """Debug mode should surface SQL; the default must not."""
    quiet = create_engine_from_url(
        sqlite_dsn("/tmp/tenantmind_echo_off.db"),
        app_settings=make_settings(debug=False),
    )
    loud = create_engine_from_url(
        sqlite_dsn("/tmp/tenantmind_echo_on.db"),
        app_settings=make_settings(debug=True),
    )

    try:
        assert quiet.echo is False
        assert loud.echo is True
    finally:
        quiet.dispose()
        loud.dispose()


def test_missing_driver_fails_at_creation_not_mid_request() -> None:
    """A bad DSN must be caught at startup, not during a request."""
    with pytest.raises(ModuleNotFoundError):
        create_engine_from_url("postgresql+psycopg2://u:p@localhost:5432/db")


# ---------------------------------------------------------------------------
# Pool configuration
# ---------------------------------------------------------------------------


def test_postgres_pool_is_configured_production_sensibly() -> None:
    kwargs = build_engine_kwargs("postgresql+psycopg://u:p@h:5432/d")

    assert kwargs["pool_size"] == POOL_SIZE
    assert kwargs["max_overflow"] == MAX_OVERFLOW
    assert kwargs["pool_timeout"] == POOL_TIMEOUT_SECONDS
    assert kwargs["pool_recycle"] == POOL_RECYCLE_SECONDS
    assert kwargs["pool_pre_ping"] is POOL_PRE_PING


def test_pool_settings_reach_sqlalchemy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pool value must actually be handed to ``create_engine``.

    Asserted at the call boundary rather than by reading private attributes
    off the resulting pool, so the check tests what this module does rather
    than SQLAlchemy's internals.
    """
    captured: dict[str, Any] = {}
    real_create_engine = sqlalchemy.create_engine

    def _spy(url: Any, **kwargs: Any) -> Engine:
        captured.update(kwargs)
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(engine_module, "create_engine", _spy)

    engine = create_engine_from_url("postgresql+psycopg://u:p@localhost:5432/d")
    try:
        assert captured["pool_size"] == POOL_SIZE
        assert captured["max_overflow"] == MAX_OVERFLOW
        assert captured["pool_timeout"] == POOL_TIMEOUT_SECONDS
        assert captured["pool_recycle"] == POOL_RECYCLE_SECONDS
        assert captured["pool_pre_ping"] is POOL_PRE_PING
        assert captured["echo"] is False
    finally:
        engine.dispose()


def test_postgres_engine_uses_a_sized_queue_pool() -> None:
    """The pool must be a real sized pool, not a single-connection stub.

    Only the size is asserted here. Overflow and recycle are covered at the
    ``create_engine`` boundary in
    :func:`test_pool_settings_reach_sqlalchemy`, because they are not readable
    through public pool accessors.
    """
    engine = create_engine_from_url("postgresql+psycopg://u:p@localhost:5432/d")

    try:
        assert type(engine.pool).__name__ == "QueuePool"
        assert queue_pool(engine).size() == POOL_SIZE
        # Public accessor: the size is also reported in the status string.
        assert f"Pool size: {POOL_SIZE}" in queue_pool(engine).status()
    finally:
        engine.dispose()


def test_sqlite_receives_no_pool_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PostgreSQL pool sizing must not be applied to a SQLite engine.

    Checked at the ``create_engine`` boundary. Asserting on the resulting pool
    object would be misleading: SQLAlchemy gives file-backed SQLite a
    ``QueuePool`` whose default size happens to equal :data:`POOL_SIZE`, so the
    pool's observable state cannot distinguish "TenantMind configured this"
    from "SQLAlchemy defaulted to the same number".
    """
    captured: dict[str, Any] = {}
    real_create_engine = sqlalchemy.create_engine

    def _spy(url: Any, **kwargs: Any) -> Engine:
        captured.update(kwargs)
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr(engine_module, "create_engine", _spy)

    engine = create_engine_from_url(sqlite_dsn("/tmp/tenantmind_sqlite_pool.db"))
    try:
        for key in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle"):
            assert key not in captured, f"{key} must not be passed for SQLite"
    finally:
        engine.dispose()


def test_postgres_url_parsing_needs_no_server() -> None:
    """DSN structure is validated locally."""
    dsn = "postgresql+psycopg://tenant_user:pw@db.internal:6543/tenantmind"

    parsed = make_url(dsn)

    assert parsed.get_backend_name() == "postgresql"
    assert parsed.host == "db.internal"
    assert parsed.port == 6543
    assert parsed.database == "tenantmind"
    assert parsed.username == "tenant_user"


def test_sqlite_gets_no_postgres_pool_arguments() -> None:
    """SQLAlchemy rejects PostgreSQL pool args for SQLite; they must be absent."""
    assert build_engine_kwargs(sqlite_dsn()) == {}


def test_sqlite_engine_is_usable(sqlite_engine: Engine) -> None:
    from sqlalchemy import text

    with sqlite_engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_sqlite_dsn_helper() -> None:
    """The in-memory form must be one SQLAlchemy actually accepts.

    ``sqlite+pysqlite://:memory:`` looks equivalent but is not: the URL parser
    reads ``:memory:`` as a port and raises ``ValueError``.
    """
    assert sqlite_dsn() == "sqlite+pysqlite://"
    assert sqlite_dsn("/tmp/x.db") == "sqlite+pysqlite:////tmp/x.db"

    engine = create_engine_from_url(sqlite_dsn())
    try:
        from sqlalchemy import text

        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


def test_metadata_holds_exactly_the_phase_2_tables() -> None:
    """The registry holds only the tables Phase 2 Step 1 declares, and naming
    conventions are unchanged.

    Phase 1 asserted this was empty. Phase 2 Step 1 adds the first two real
    tables, so the property to preserve is no longer "empty" but "exactly
    ``MODEL_TABLES``" — which still catches a table added without a migration and
    a future-phase table created early.
    """
    import app.models
    from app.db.base import NAMING_CONVENTION, Base

    assert set(Base.metadata.tables) == app.models.MODEL_TABLES
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert NAMING_CONVENTION["pk"] == "pk_%(table_name)s"
    assert "ix" in NAMING_CONVENTION
    assert "fk" in NAMING_CONVENTION
    assert "uq" in NAMING_CONVENTION


def test_base_metadata_is_shared_by_the_package_export() -> None:
    import app.db
    from app.db.base import Base

    assert app.db.Base is Base


def test_settings_type_is_unchanged_by_the_db_layer() -> None:
    """The configuration architecture must be preserved, not bypassed."""
    assert "database_url" in Settings.model_fields
    assert Settings.model_fields["database_url"].default is None
