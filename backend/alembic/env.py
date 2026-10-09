"""Alembic migration environment for TenantMind.

Two responsibilities, and nothing else:

1. **Resolve the database URL from application configuration.** The DSN comes
   from :attr:`app.core.config.Settings.database_url` — the same source the
   application uses. ``alembic.ini`` deliberately ships with an empty
   ``sqlalchemy.url`` so that no credential is ever committed. This module
   fills it in at runtime, which means migrations can never drift from the
   URL the application actually uses.

2. **Expose the application metadata to autogenerate.** ``target_metadata`` is
   bound to :data:`app.db.base.Base.metadata`, the single registry every model
   inherits from. Pointing Alembic at a separate metadata object would make it
   silently miss every table.

   Importing :mod:`app.models` here is what populates that registry, and the
   import is verified rather than assumed: see :func:`_assert_models_registered`.
   Autogenerate against an empty ``target_metadata`` does not fail — it proposes
   dropping every table, which is the most destructive migration bug there is.

Offline mode (``alembic upgrade head --sql``) is supported and needs no
database connection. Online mode requires a reachable PostgreSQL server.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import Connection, engine_from_config, pool

from alembic import context

# TenantMind is a `backend/`-rooted application that is deliberately NOT
# installed as a package, so `app` is not importable by default when Alembic
# runs: sys.path[0] is the console-script directory in .venv/bin, not this
# directory. Alembic loads this file by path rather than as a package, so
# nothing puts the application root on the path for us.
#
# This is the standard remedy for a non-installed source layout, and it is why
# `../.venv/bin/alembic ...` must be run from `backend/`. It adds the root to
# sys.path; it does not install anything or change the environment.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.models
from app.core.config import settings
from app.db.base import Base
from app.db.engine import DatabaseNotConfiguredError, resolve_dsn

#: Alembic reads this to compare models against the live schema.
#:
#: Bound to the application base so that any model inheriting from
#: :class:`app.db.base.Base` is picked up automatically, with no import list to
#: maintain here.
target_metadata = Base.metadata


def _assert_models_registered() -> None:
    """Fail loudly if importing :mod:`app.models` did not populate the metadata.

    Autogenerate compares ``target_metadata`` against the live schema. If model
    discovery ever breaks — a rename, a module moved out of the package, an
    import made conditional — the result is an *empty* metadata, and autogenerate
    responds by proposing ``DROP TABLE`` for every table in the database. That
    failure is silent at generation time and catastrophic at apply time, so it is
    worth an explicit check.

    Raising here also fails ``alembic upgrade`` and ``alembic downgrade``, which
    do not need autogenerate at all. That is intentional collateral: if the
    models are not importable, nothing in this project should be trusted to run.

    Raises:
        RuntimeError: if the registered table names differ from
            :data:`app.models.MODEL_TABLES`.
    """
    registered = set(target_metadata.tables)
    expected = app.models.MODEL_TABLES
    if registered != expected:
        missing = sorted(expected - registered)
        unexpected = sorted(registered - expected)
        raise RuntimeError(
            "model discovery failed: Base.metadata has "
            f"{sorted(registered)}, expected {sorted(expected)}"
            f" (missing: {missing}, unexpected: {unexpected}). "
            "app.models must be imported for Alembic to see the schema."
        )


_assert_models_registered()

# `alembic.ini` logging config, applied once. `fileConfig` with
# `disable_existing_loggers=False` so importing this module does not silence
# the application logger.
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _configure_url() -> None:
    """Point Alembic at the application's configured database.

    Raises:
        DatabaseNotConfiguredError: if ``DATABASE_URL`` is unset. Alembic
            surfaces this as a clear configuration error rather than
            attempting to connect to a default DSN.
    """
    try:
        dsn = resolve_dsn(settings)
    except DatabaseNotConfiguredError as exc:
        raise DatabaseNotConfiguredError(
            f"{exc} Alembic requires a database; it cannot run offline against "
            "an unconfigured deployment."
        ) from exc

    # `set_main_option` escapes '%' for ConfigParser interpolation, so a
    # password containing a percent sign cannot corrupt the value.
    config.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DBAPI connection.

    Used by ``alembic upgrade head --sql`` to review or capture the SQL a
    migration would run, including in environments with no database at all.
    """
    _configure_url()

    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations against an already-established connection.

    The caller owns the connection, so Alembic does not create or dispose one.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database.

    Uses a ``NullPool``: migrations run once per process during a release, so
    pooling would add nothing but a connection that has to be cleaned up
    correctly on failure.
    """
    _configure_url()

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        do_run_migrations(connection)

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
