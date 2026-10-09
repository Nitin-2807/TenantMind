"""Session factory, transaction boundaries, and the FastAPI session dependency.

Transaction model
-----------------
SQLAlchemy 2.x sessions use "begin once, commit once" semantics, and this
module keeps that model rather than wrapping it in a unit-of-work abstraction:

* A session opens a transaction lazily on first use.
* :func:`get_db` — the FastAPI dependency — commits when the request handler
  returns successfully, and rolls back if the handler raises.
* The session is always closed in a ``finally`` block, so a failed request
  cannot leak a connection back into the pool as still checked out.

One consequence is worth stating explicitly, because it will otherwise be
rediscovered as a bug: a handler that *returns* an error ``Response`` rather
than raising has still succeeded from the framework's perspective, and **will**
commit. An endpoint that must reject a write without raising has to call
``session.rollback()`` itself. That is standard SQLAlchemy behaviour; it is
recorded here so the next phase inherits the knowledge.

Scope note: the session deliberately carries **no tenant identity**. Phase 2
Step 1 has now made ``tenant_id`` a required column on every tenant-owned row
(see :class:`app.models.tenant.TenantScopedMixin`), which is where scoping belongs.
Putting it here as a session attribute would invite exactly the mistake PMP §8
warns about: tenant identity derived from request data rather than from the
authenticated principal.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import Depends
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.db.engine import SQLITE_FOREIGN_KEYS_PRAGMA, get_engine


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a :class:`~sqlalchemy.orm.sessionmaker` bound to ``engine``.

    ``expire_on_commit=False`` is the setting that matters most. The default
    (``True``) discards every loaded attribute after commit, forcing a fresh
    SELECT for anything touched afterwards — surprising inside a request, and
    a routine source of ``DetachedInstanceError`` when a response is
    serialised after the session closes.

    ``autoflush`` stays at its ``True`` default: pending changes are flushed
    before a query, which surfaces statement-ordering mistakes early instead
    of silently returning stale rows.

    ``autocommit`` is never used. It was removed in SQLAlchemy 2.0 precisely
    because implicit commits make transaction boundaries invisible; the 2.x
    session is explicitly transactional.
    """
    return sessionmaker(
        bind=engine,
        class_=Session,
        expire_on_commit=False,
        autoflush=True,
    )


def enable_sqlite_foreign_keys(engine: Engine) -> None:
    """Turn on SQLite foreign-key enforcement for a test engine.

    SQLite ignores foreign keys unless each connection asks for them. Without
    this, unit tests would insert orphaned rows that PostgreSQL rejects, so
    the suite could pass against SQLite and fail against the real database.
    Registered as a connect-level event so it applies to every pooled
    connection, and a no-op for any non-SQLite engine.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: Any, _: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(SQLITE_FOREIGN_KEYS_PRAGMA)
        cursor.close()


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Yield a session, committing on success and rolling back on failure.

    The context-manager-shaped equivalent of :func:`get_db`, for code outside
    the request cycle such as scripts and background workers. Alembic
    deliberately does not use this: migrations manage their own transactions.

    Usable as ``with session_scope(factory) as session:`` — the commit happens
    when the block exits without raising, and a raised exception is
    re-raised after the rollback, so the caller still sees the original error.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped :class:`Session`.

    Declared with ``yield`` so FastAPI closes the session after the response
    has been produced, which is what prevents a connection being held for the
    lifetime of a streamed response.

    Annotated as a :class:`~collections.abc.Generator` rather than a plain
    ``Iterator`` because that is what it is: a generator is resumable and
    closable, and the narrower type documents the ``close()``-on-teardown
    contract that tests and callers rely on. FastAPI accepts any iterator, so
    the precise annotation costs nothing.

    Transaction handling is explicit and unconditional:

    * handler returns normally -> ``commit()``
    * handler raises            -> ``rollback()``, then re-raise
    * either way                -> ``close()``

    No tenant identity is attached. A repository that reads tenant-owned rows will
    require ``tenant_id`` as an explicit parameter, which the service layer will
    obtain from the authenticated principal once authentication exists.
    """
    factory = build_session_factory(get_engine())

    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


#: Convenience alias for route signatures, so routers read as
#: ``db: Session = DbSession`` instead of repeating ``Depends(get_db)``.
#:
#: Exposed as one object so a later phase can wrap the dependency in a single
#: place (tenant scoping, row-level security) rather than editing every router.
DbSession = Depends(get_db)
