"""Database engine construction for TenantMind.

Design constraints that shaped this module:

* **No connection at import time.** Creating an :class:`~sqlalchemy.Engine`
  opens no sockets; it only prepares a connection pool. The first socket is
  opened lazily, on first use. Importing this module therefore never requires
  a running PostgreSQL server, which keeps the test suite and application
  startup infrastructure-free.
* **No hard-coded URL.** The DSN comes from
  :attr:`~app.core.config.Settings.database_url`. This module never calls
  ``os.getenv``.
* **Deterministic creation.** :func:`create_engine_from_url` is a pure
  function of its inputs, so the same arguments always produce the same
  engine. The module-level engine is a lazily initialised singleton with an
  explicit cache-reset hook for tests.
* **Optional configuration.** ``DATABASE_URL`` is legitimately unset in
  development and in the test environment. Absence is reported as a precise
  error when something actually needs an engine, rather than being papered over
  with a default DSN.

Driver note
-----------
SQLAlchemy's bare ``postgresql://`` scheme still resolves to the **psycopg2**
dialect, which this project does not install (it uses psycopg 3). A DSN
supplied as ``postgresql://`` would therefore fail with ``ModuleNotFoundError:
No module named 'psycopg2'`` at engine creation. :func:`normalize_dsn` rewrites
the bare scheme to ``postgresql+psycopg`` so that the natural DSN in
``.env.example`` works, while an explicitly qualified DSN is left untouched.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url

from app.core.config import Settings, settings

#: Pool sizing for PostgreSQL. Deliberately modest and conventional.
#:
#: ``pool_size`` / ``max_overflow`` bound how many connections this process
#: may hold, so a horizontally scaled deployment cannot exhaust the server's
#: ``max_connections`` through sheer replica count. ``pool_pre_ping`` validates
#: a pooled connection before handing it out, which matters because
#: infrastructure (proxies, failovers, idle timeouts) can drop connections that
#: look alive to the pool; without it a request can fail with a stale-socket
#: error. ``pool_recycle`` retires connections before typical
#: infrastructure idle timeouts close them.
#:
#: These are conservative starting values, not tuned numbers. Revisit against
#: real production traffic rather than guessed at here.
POOL_SIZE = 5
MAX_OVERFLOW = 10
POOL_TIMEOUT_SECONDS = 30
POOL_RECYCLE_SECONDS = 1800
POOL_PRE_PING = True

#: Applied to SQLite URLs in tests only. PostgreSQL enforces its own write
#: concurrency, and forcing this in production would weaken durability
#: guarantees. SQLAlchemy's default already is ``DEFERRED``; this makes the
#: intent explicit at the call site where it is passed.
SQLITE_FOREIGN_KEYS_PRAGMA = "PRAGMA foreign_keys=ON"


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when an engine is requested but ``DATABASE_URL`` is unset.

    This is a configuration error, not a connection error. It is deliberately
    distinct from a driver failure so that operators can tell "you have not
    configured a database" apart from "PostgreSQL is unreachable".
    """


def normalize_dsn(dsn: str) -> str:
    """Qualify a bare ``postgresql://`` DSN to the psycopg 3 driver.

    Leaves every other DSN untouched, including an already-qualified
    ``postgresql+psycopg://`` and any non-PostgreSQL URL such as the SQLite
    URLs used by unit tests.
    """
    if dsn.startswith("postgresql://"):
        return "postgresql+psycopg://" + dsn.removeprefix("postgresql://")
    return dsn


def resolve_dsn(app_settings: Settings | None = None) -> str:
    """Return the configured, driver-qualified DSN.

    Raises:
        DatabaseNotConfiguredError: if ``DATABASE_URL`` is unset or empty.
    """
    config = settings if app_settings is None else app_settings
    if config.database_url is None:
        raise DatabaseNotConfiguredError(
            "DATABASE_URL is not set. Configure it in the environment or .env "
            "to use the database. It is optional in development so the "
            "application can start without PostgreSQL."
        )

    raw = config.database_url.get_secret_value().strip()
    if not raw:
        raise DatabaseNotConfiguredError("DATABASE_URL is set but empty.")

    return normalize_dsn(raw)


def build_engine_kwargs(dsn: str) -> dict[str, Any]:
    """Return the engine keyword arguments appropriate for ``dsn``.

    Pool options apply to PostgreSQL only. SQLite uses different pooling
    semantics and rejects the PostgreSQL-specific arguments, so the two paths
    are kept explicitly separate rather than sharing one oversized dict.
    """
    url = make_url(dsn)

    if url.get_backend_name() != "postgresql":
        return {}

    return {
        "pool_size": POOL_SIZE,
        "max_overflow": MAX_OVERFLOW,
        "pool_timeout": POOL_TIMEOUT_SECONDS,
        "pool_recycle": POOL_RECYCLE_SECONDS,
        "pool_pre_ping": POOL_PRE_PING,
    }


def create_engine_from_url(
    dsn: str,
    *,
    echo: bool = False,
    app_settings: Settings | None = None,
) -> Engine:
    """Build an :class:`~sqlalchemy.Engine` for ``dsn`` without connecting.

    Args:
        dsn: A SQLAlchemy URL. A bare ``postgresql://`` is upgraded to
            ``postgresql+psycopg://``.
        echo: Log every statement. Off by default; statement logging can
            emit bound parameter values, which for TenantMind would include
            document content and user identifiers.
        app_settings: Optional settings override, used to read
            ``SQLALCHEMY_ECHO``-style configuration in the future. Accepted
            now so the signature does not need to change when the database
            section of ``Settings`` grows.

    Returns:
        A configured engine. No connection is opened.

    Raises:
        NoSuchModuleError: if the URL names a driver that is not installed.
            This surfaces a bad DSN at startup rather than mid-request.
    """
    config = settings if app_settings is None else app_settings
    qualified = normalize_dsn(dsn)

    return create_engine(
        qualified,
        echo=echo or config.debug,
        future=True,
        **build_engine_kwargs(qualified),
    )


def create_engine_from_settings(app_settings: Settings | None = None) -> Engine:
    """Build the engine from ``Settings.database_url``.

    Raises:
        DatabaseNotConfiguredError: if the DSN is unset.
    """
    return create_engine_from_url(
        resolve_dsn(app_settings), app_settings=app_settings
    )


_engine: Engine | None = None


def get_engine(app_settings: Settings | None = None) -> Engine:
    """Return the process-wide engine, creating it on first use.

    The engine is created lazily and cached, so importing this module does no
    work and the application does not require ``DATABASE_URL`` to boot. The
    cache holds an :class:`~sqlalchemy.Engine`, not a connection; connections
    are opened only when a session actually runs a statement.

    Raises:
        DatabaseNotConfiguredError: if ``DATABASE_URL`` is unset at the moment
            the engine is first requested.
    """
    global _engine

    if _engine is None:
        _engine = create_engine_from_settings(app_settings)

    return _engine


def is_engine_configured() -> bool:
    """Whether the process-wide engine has been built yet.

    Exposed so the question can be asked without reaching into this module for
    :data:`_engine`: a test asserting that importing the application opens no
    connection, and any future startup check that needs to know whether the
    database layer has been touched. The engine is created lazily, so ``False``
    here means no engine exists and therefore no connection was opened.
    """
    return _engine is not None


def reset_engine() -> None:
    """Dispose of the cached engine so the next call rebuilds it.

    Needed by tests that change configuration, and by any future reload path.
    Without this the cached engine would outlive the settings that produced
    it.
    """
    global _engine

    if _engine is not None:
        _engine.dispose()
    _engine = None


def sqlite_dsn(path: str = ":memory:") -> str:
    """Return a SQLite DSN for isolated tests.

    Provided so unit tests can exercise real SQL against a real database
    without a PostgreSQL server. The production database is not designed
    around SQLite; this exists only for tests.

    An in-memory database is spelled ``sqlite+pysqlite://`` with an empty
    database component. The more familiar-looking ``.../:memory:`` form is
    rejected by SQLAlchemy's URL parser, which reads ``:memory:`` as a port.
    """
    if path == ":memory:":
        return "sqlite+pysqlite://"
    return f"sqlite+pysqlite:///{path}"


__all__ = [
    "MAX_OVERFLOW",
    "POOL_PRE_PING",
    "POOL_RECYCLE_SECONDS",
    "POOL_SIZE",
    "POOL_TIMEOUT_SECONDS",
    "SQLITE_FOREIGN_KEYS_PRAGMA",
    "DatabaseNotConfiguredError",
    "build_engine_kwargs",
    "create_engine_from_settings",
    "create_engine_from_url",
    "get_engine",
    "normalize_dsn",
    "reset_engine",
    "resolve_dsn",
    "sqlite_dsn",
]
