"""Database foundation for TenantMind (PMP §22 Phase 1, Step 4).

Public surface, and nothing more:

* :mod:`app.db.base` — the single :class:`~app.db.base.Base` every model must
  inherit, carrying the shared ``MetaData`` that Alembic autogenerate reads.
* :mod:`app.db.engine` — DSN resolution and engine construction. No engine is
  built at import time, and importing this package never connects.
* :mod:`app.db.session` — the session factory, the transaction pattern, and
  the FastAPI ``get_db`` dependency.
* :mod:`app.db.health` — a ``SELECT 1`` connectivity probe that reports a
  value instead of raising, so a driver error can never reach a client.

Models are **not** re-exported here. They live in :mod:`app.models`, and importing
``app.db`` must not pull them in: ``app.models`` imports ``app.db.base``, so
re-exporting here would make the dependency circular. Import ``app.models``
explicitly when you need the schema.

Importing this package is deliberately free of side effects, so ``import app.db``
works with no database configured.
"""

from app.db.base import Base
from app.db.engine import (
    DatabaseNotConfiguredError,
    create_engine_from_settings,
    create_engine_from_url,
    get_engine,
    reset_engine,
    resolve_dsn,
)
from app.db.health import DatabaseHealth, DatabaseStatus, check_database
from app.db.session import (
    DbSession,
    build_session_factory,
    enable_sqlite_foreign_keys,
    get_db,
    session_scope,
)

__all__ = [
    "Base",
    "DatabaseHealth",
    "DatabaseNotConfiguredError",
    "DatabaseStatus",
    "DbSession",
    "build_session_factory",
    "check_database",
    "create_engine_from_settings",
    "create_engine_from_url",
    "enable_sqlite_foreign_keys",
    "get_db",
    "get_engine",
    "reset_engine",
    "resolve_dsn",
    "session_scope",
]
