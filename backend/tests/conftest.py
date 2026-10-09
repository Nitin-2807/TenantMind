"""Shared pytest fixtures for the TenantMind test suite.

Isolation rules this file enforces:

* No test reads the developer's real ``.env``. Fixtures that build
  ``Settings`` pass ``_env_file=None``, which disables env-file loading.
* No test requires a running PostgreSQL server. Tests that need a real
  database use an isolated SQLite engine, which exercises genuine SQL,
  transactions, and constraint enforcement without a server. The PostgreSQL
  integration suite is separate, opt-in, and skipped unless an explicit DSN is
  configured — see ``tests/test_postgres_integration.py``.
* The module-level engine cache in :mod:`app.db.engine` is reset between
  tests, so one test's engine can never leak into the next.
* ``app.models`` is imported here, so the model registry is populated for every
  test regardless of collection order. Tests that assert on
  ``Base.metadata.tables`` would otherwise depend on which module happened to be
  imported first.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Generator, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Constraint, Engine, Table, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.core import config as config_module
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.db import engine as engine_module
from app.db.base import Base
from app.db.session import (
    build_session_factory,
    enable_sqlite_foreign_keys,
    get_db,
    session_scope,
)
from app.main import create_app
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.storage.qdrant import get_vector_store

#: Bound by :func:`require` to whatever ``Session.get`` returned.
T = TypeVar("T")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = BACKEND_ROOT.parent / ".env.example"
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_ENV = BACKEND_ROOT / "alembic" / "env.py"
ALEMBIC_MAKO = BACKEND_ROOT / "alembic" / "script.py.mako"
ALEMBIC_VERSIONS = BACKEND_ROOT / "alembic" / "versions"


def make_settings(**overrides: Any) -> Settings:
    """Build ``Settings`` with env-file loading disabled.

    ``pydantic-settings`` accepts ``_env_file`` at runtime, but the
    ``BaseSettings`` constructor typing does not expose this parameter to
    mypy. The cast is confined to this helper, matching the pattern already
    used in ``tests/test_config.py`` and ``tests/test_main.py``, rather than
    weakening mypy project-wide.
    """
    constructor = cast(Any, Settings)
    return constructor(_env_file=None, **overrides)


# --------------------------------------------------------------------------
# Authentication fixtures (Phase 2 Step 2)
# --------------------------------------------------------------------------
#
# Reused unchanged by the Phase 2 Step 3 HTTP tests: the same test signing key,
# the same passwords, and the same ``make_account`` factory all mean a test can
# move between the service layer and the HTTP layer without inventing a second
# vocabulary for the same fixtures.

#: A signing secret for tests. Fixed rather than random so a failure is
#: reproducible, and long enough to satisfy :data:`Settings`'s length floor and
#: PyJWT's own minimum. It is **not** a credential: it exists only in this file,
#: is never accepted by any deployed configuration, and is not a value any
#: secret manager or ``.env`` will ever hold. Nothing in the suite prints it.
TEST_JWT_SECRET = "tenantmind-test-signing-key-not-a-real-credential-0123456789"

#: A second, different secret used to forge tokens in negative tests. Separate
#: from :data:`TEST_JWT_SECRET` so that "rejects a token signed by the wrong key"
#: is a genuine mismatch rather than a near-miss.
OTHER_JWT_SECRET = "a-different-test-signing-key-also-not-a-credential-987654"

#: Passwords used by the auth fixtures. Long enough that no future
#: password-strength rule in a request schema can start failing these tests for
#: an unrelated reason, and still obviously synthetic.
TEST_PASSWORD = "correct horse battery staple"

#: Argon2id at 64 MiB costs roughly 60-100 ms per hash. Hashing inside a test
#: is therefore the slowest thing in the suite, so the count is kept small and
#: fixtures reuse one hash where the test is not about hashing itself.
HASH_COST_NOTE = "one Argon2id hash ~= 60-100ms at these parameters"


@pytest.fixture
def auth_settings(tmp_path: Path) -> Settings:
    """Isolated settings carrying a usable JWT secret and default lifetimes.

    ``storage_root`` is redirected into ``tmp_path`` rather than left at its
    default of ``./storage``. Without that, any test that reaches the document
    endpoints would write into the repository's own storage directory, and two
    problems follow: the files outlive the test, and a test asserting on what is
    on disk could see a previous run's leftovers. Pointing at ``tmp_path`` makes
    each test's storage genuinely its own and makes pytest's cleanup authoritative.

    The path is absolute, which is the one shape
    :func:`app.core.config.Settings._resolve_storage_root` honours verbatim — so
    this fixture does not depend on which directory pytest was invoked from.
    """
    return make_settings(
        jwt_secret_key=TEST_JWT_SECRET,
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
        storage_root=tmp_path / "storage",
    )


@pytest.fixture
def foreign_secret_settings() -> Settings:
    """Isolated settings signed with a *different* key, for forgery tests."""
    return make_settings(
        jwt_secret_key=OTHER_JWT_SECRET,
        access_token_expire_minutes=15,
        refresh_token_expire_days=7,
    )


@pytest.fixture
def active_auth_settings(
    monkeypatch: pytest.MonkeyPatch, auth_settings: Settings
) -> Settings:
    """Make :data:`auth_settings` the settings the application code sees.

    ``app.core.security`` and ``app.services.auth`` do ``from
    app.core.config import settings``, which binds the value into their own
    module namespace at import time. Replacing ``config.settings`` therefore does
    *not* reach them; each module's own binding has to be repointed.

    This mirrors the existing :func:`configured_database` fixture, which exists
    for exactly the same reason with ``alembic/env.py``. Tests that need to prove
    something about a *specific* settings object should pass ``app_settings=``
    explicitly instead — this fixture is for the ones exercising code paths that
    legitimately fall back to the process-wide instance.
    """
    import app.core.security as security_module
    import app.services.auth as auth_service_module
    import app.services.tenants as tenant_service_module

    # The modules bind the singleton under different local names — `settings` and
    # `process_settings` — so each is named explicitly rather than looped over.
    # `hasattr` asserts first, so a rename in the application makes this fixture
    # fail loudly instead of silently patching nothing.
    for module, attribute in (
        (security_module, "settings"),
        (auth_service_module, "process_settings"),
        (tenant_service_module, "process_settings"),
    ):
        assert hasattr(module, attribute), (
            f"{module.__name__} no longer binds a module-level {attribute!r}; "
            "update the active_auth_settings fixture to match."
        )
        monkeypatch.setattr(module, attribute, auth_settings)
    return auth_settings


@pytest.fixture
def make_account(
    model_session: Session,
) -> Callable[..., tuple[Tenant, User]]:
    """Factory creating a tenant plus one user, returning both rows.

    Reusing an existing tenant when the slug already exists is what lets a test
    build two users in one tenant without duplicating the tenant, and what makes
    "same email, different tenants" a two-call test.

    Args:
        slug: tenant slug. Created if absent.
        email: the user's email. Unique only *within* the tenant, which is why
            the same email may be used twice with different slugs.
        password: plaintext; hashed with Argon2id before storage.
        role: the user's role. Defaults to ``viewer`` — the same least-privilege
            default the model declares.
        is_active: mirrors ``users.is_active``.
    """

    def _make(
        *,
        slug: str = "acme",
        email: str = "alice@example.com",
        password: str = TEST_PASSWORD,
        role: UserRole = UserRole.VIEWER,
        is_active: bool = True,
        tenant_name: str | None = None,
    ) -> tuple[Tenant, User]:
        tenant = model_session.scalar(select(Tenant).where(Tenant.slug == slug))
        if tenant is None:
            tenant = Tenant(name=tenant_name or slug.title(), slug=slug)
            model_session.add(tenant)
            model_session.flush()

        user = User(
            tenant_id=tenant.id,
            email=email,
            password_hash=hash_password(password),
            role=role,
            is_active=is_active,
        )
        model_session.add(user)
        model_session.flush()
        return tenant, user

    return _make


@pytest.fixture
def make_committed_account(
    make_account: Callable[..., tuple[Tenant, User]],
    model_session: Session,
) -> Callable[..., tuple[Tenant, User]]:
    """:func:`make_account`, committing each row it creates.

    :func:`make_account` flushes but does not commit, because the Phase 2 Step 2
    service tests want to inspect or roll back their own rows.

    An HTTP test cannot use it directly. The request runs on a *different*
    session — the one :func:`api_app` injects — and an uncommitted row is
    invisible to that session on any database. The failure mode is nasty because
    it reads as a genuine authentication bug: the account exists, the password
    is right, and the login still fails, because the row was never committed
    anywhere it can be seen from.

    This fixture exists so a test that needs a pre-existing account *over HTTP*
    gets a genuinely committed one.
    """

    def _make(**kwargs: Any) -> tuple[Tenant, User]:
        result = make_account(**kwargs)
        model_session.commit()
        return result

    return _make


# --------------------------------------------------------------------------
# HTTP API fixtures (Phase 2 Step 3)
# --------------------------------------------------------------------------


@pytest.fixture
def api_app(
    active_auth_settings: Settings,
    model_session: Session,
    sqlite_session_factory: sessionmaker[Session],
) -> FastAPI:
    """The real application, wired to an isolated database and a known key.

    This is the production application, not a purpose-built one: the routers,
    the identity dependency and the authorization dependency are the ones
    ``app.main`` mounts. Only two things are substituted, and each is a genuine
    seam the application provides for the purpose:

    * :func:`app.core.config.get_settings`, so token signing uses a fixed test
      key rather than a real ``.env``;
    * :func:`app.db.session.get_db`, so requests run against the isolated
      SQLite database instead of whatever ``DATABASE_URL`` happens to say.

    The session override delegates to :func:`app.db.session.session_scope`, so
    it keeps the real ``get_db`` contract — commit when the handler returns,
    roll back when it raises, always close. A test-only override that skipped
    the commit would pass here and fail in production, so the production
    transaction boundary is what gets exercised.
    """
    application = create_app(active_auth_settings)

    def _override_get_db() -> Generator[Session, None, None]:
        with session_scope(sqlite_session_factory) as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: active_auth_settings
    application.dependency_overrides[get_db] = _override_get_db
    return application


@pytest.fixture
def api_client(api_app: FastAPI) -> TestClient:
    """A ``TestClient`` over the wired application."""
    return TestClient(api_app)


@pytest.fixture
def read_only(
    model_session: Session,
    sqlite_session_factory: sessionmaker[Session],
) -> Callable[[], AbstractContextManager[Session]]:
    """A factory for a short-lived session used only to read committed state.

    Deliberately *not* :func:`model_session` itself. Once a session has run a
    ``SELECT`` against SQLite it holds an open read transaction, and SQLite then
    serves that session a pinned snapshot — so a session that read before an HTTP
    request committed would not see that request's writes, and the failure would
    look like an application bug. A brand-new session per read has no such
    history.

    ``model_session`` is requested anyway, and only for its side effect: it
    creates the schema. Without it, a test that reads before any other fixture
    touched the database would query a table that does not exist.

    Used with ``with`` so the session is always closed::

        with read_only() as session:
            assert session.scalar(select(User)...) is not None
    """

    @contextmanager
    def _session() -> Iterator[Session]:
        session = sqlite_session_factory()
        try:
            yield session
        finally:
            session.close()

    return _session


@pytest.fixture(autouse=True)
def models_registered() -> Generator[None, None, None]:
    """Import the model package once for the whole session.

    Model discovery is an explicit import (``import app.models``), not something
    that happens by accident through some other import path. Autouse, so the
    registry is populated before any test runs regardless of collection order,
    which matters because several tests assert on ``Base.metadata.tables``.

    Also verifies the registry matches what the package declares. A mismatch here
    means a table was registered without being added to ``MODEL_TABLES``, which
    would silently opt out of every "exactly these tables" assertion in the suite.
    """
    import app.models

    registered = set(Base.metadata.tables)
    expected = app.models.MODEL_TABLES
    assert registered == expected, (
        f"model registry mismatch: {sorted(registered)} != {sorted(expected)}. "
        "Add any new table to app.models.MODEL_TABLES in the same change."
    )

    yield


@pytest.fixture(autouse=True)
def reset_engine_cache() -> Generator[None, None, None]:
    """Clear the module-level engine cache around every test.

    Autouse because a leaked engine would let one test's SQLite DSN satisfy
    another test's PostgreSQL expectation, which is the kind of cross-test
    coupling that produces order-dependent failures.
    """
    engine_module.reset_engine()
    yield
    engine_module.reset_engine()


@pytest.fixture(autouse=True)
def reset_vector_store_cache() -> Generator[None, None, None]:
    """Clear the cached Qdrant store around every test.

    The same argument as :func:`reset_engine_cache`, applied to
    :func:`app.storage.qdrant.get_vector_store`, which is cached because the
    ``QdrantClient`` it owns holds a connection pool.

    Left uncached this would fail in a way that points at the wrong cause: a store built
    from one test's settings — pointing at one Qdrant URL — would be handed to a test that
    pointed Qdrant somewhere else, and the symptom would be "Qdrant is unreachable" rather
    than "this object is older than it looks". Worse, it would make a test pass or fail
    based on the order it happened to run in, which is the failure mode this fixture exists
    to remove.
    """
    get_vector_store.cache_clear()
    yield
    get_vector_store.cache_clear()


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Generator[Engine, None, None]:
    """A file-backed SQLite engine with foreign keys enforced.

    File-backed rather than ``:memory:`` so that the same connection is
    reused across the engine's pool. An in-memory SQLite database is private
    to each connection, which would make multi-connection tests misleading.
    Foreign keys are enabled because SQLite ignores them by default and
    PostgreSQL does not — without this, tests could pass here and fail in
    production.
    """
    database_file = tmp_path / "tenantmind_test.db"
    dsn = f"sqlite+pysqlite:///{database_file}"

    engine = engine_module.create_engine_from_url(dsn)
    enable_sqlite_foreign_keys(engine)

    yield engine

    engine.dispose()


@pytest.fixture
def sqlite_session_factory(
    sqlite_engine: Engine,
) -> sessionmaker[Session]:
    """A session factory bound to the isolated SQLite engine."""
    return build_session_factory(sqlite_engine)


@pytest.fixture
def model_session(
    sqlite_session_factory: sessionmaker[Session],
) -> Generator[Session, None, None]:
    """A session on a database whose schema was built from the models.

    Built with ``Base.metadata.create_all`` rather than by running the Alembic
    migration, because the tests using it are about model behaviour. The two are
    proven equivalent separately: ``test_alembic.py`` asserts that autogenerate
    detects no drift between the models and the migrated schema.

    Foreign keys are enforced, because SQLite ignores them by default and without
    that a tenant-isolation test could pass here and fail in production.
    """
    session = sqlite_session_factory()
    Base.metadata.create_all(session.get_bind())

    yield session

    session.close()


@pytest.fixture
def configured_database(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Repoint the ``app.core.config.settings`` singleton at a given DSN.

    ``alembic/env.py`` resolves its URL from the module-level ``settings``
    singleton, not from ``os.environ``, so an env var alone cannot redirect it
    inside a running process. Patching the attribute is what actually reaches
    the code, because ``from app.core.config import settings`` re-reads the
    attribute every time Alembic re-executes ``env.py``.

    Usage::

        configured_database(dsn)
    """

    def _configure(dsn: str) -> None:
        monkeypatch.setattr(
            config_module, "settings", make_settings(database_url=dsn)
        )

    return _configure


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """Make any real network access fail the test with a clear message.

    Guards the "no network calls during ordinary unit tests" rule. SQLite is
    in-process and does not go through ``socket``, so the isolated test
    database is unaffected; only genuine outbound network use trips this.

    Both egress points are blocked, because blocking only one leaves a gap that
    a test can silently pass through:

    * ``socket.getaddrinfo`` — name resolution. ``psycopg`` resolves the host
      before it ever opens a socket, so a fixture that blocked only
      ``connect`` would let a real DNS query escape and then fail with a
      driver's own error, which reads as an unrelated test failure rather than
      as the policy breach it is.
    * ``socket.socket.connect`` / ``connect_ex`` — the actual connection.
    """
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def _blocked_connect(self: socket.socket, address: object) -> None:
        raise AssertionError(
            f"unit test attempted a network connection to {address!r}"
        )

    def _blocked_connect_ex(self: socket.socket, address: object) -> int:
        raise AssertionError(
            f"unit test attempted a network connection to {address!r}"
        )

    def _blocked_getaddrinfo(
        host: object, port: object, *args: object, **kwargs: object
    ) -> None:
        raise AssertionError(f"unit test attempted a DNS lookup for {host!r}")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked_getaddrinfo)
    yield

    monkeypatch.setattr(socket.socket, "connect", original_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", original_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", original_getaddrinfo)


def table_of(model: type[DeclarativeBase]) -> Table:
    """Return a model's ``__table__``, narrowed from ``FromClause`` to ``Table``.

    ``DeclarativeBase.__table__`` is annotated as the abstract
    :class:`~sqlalchemy.FromClause`, which has none of the members these tests
    need — ``insert``, ``delete``, ``indexes``, ``constraints``. A concrete model
    always has a real :class:`~sqlalchemy.Table`, so the narrowing happens once
    here rather than at every call site.

    Used only where a test deliberately goes below the ORM to speak raw SQL; the
    ORM-facing tests use the model directly.
    """
    table = model.__table__
    assert isinstance(table, Table), (
        f"{model.__name__}.__table__ is a {type(table).__name__}, not a Table"
    )
    return table


def require(value: T | None) -> T:
    """Assert a lookup returned a row, and return it narrowed.

    ``Session.get`` is typed ``T | None``. A test that just fetched a row it
    created wants to fail loudly if the lookup missed, not to raise
    ``AttributeError`` several expressions later with an unrelated message.
    """
    assert value is not None, "expected a persisted row, but the lookup returned None"
    return value


def unique_constraints(table: Table) -> list[UniqueConstraint]:
    """Return a table's :class:`~sqlalchemy.UniqueConstraint` objects.

    ``Table.constraints`` is a heterogeneous collection typed as ``Constraint``,
    which has no ``columns``. Narrowing here keeps the constraint-shape tests
    readable, and filtering by type rather than by name keeps a constraint
    renamed by a future migration from silently dropping out of the assertions.
    """
    return [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]


def constraint_names(
    table: Table, constraint_type: type[Constraint]
) -> set[str]:
    """Return the names of every constraint of one type on a table.

    An unnamed constraint fails here rather than being skipped. That is the point:
    the shared naming convention exists so every constraint has a deterministic
    name, so a ``None`` would mean the convention did not apply — which must be a
    loud failure in a test named after constraint names, not a quietly absent
    element that makes an equality assertion pass by omission.
    """
    names: set[str] = set()
    for constraint in table.constraints:
        if not isinstance(constraint, constraint_type):
            continue
        # SQLAlchemy represents "deferred, ultimately None" as a private enum
        # sentinel rather than `None`, so an `is not None` check would not narrow
        # the type and would pass on a genuinely unnamed constraint. Asserting the
        # value is a `str` rejects both spellings.
        assert isinstance(constraint.name, str), (
            f"{type(constraint).__name__} on {table.name} has no name "
            f"({constraint.name!r}); the naming convention in app.db.base did not "
            "apply to it"
        )
        names.add(constraint.name)
    return names


def queue_pool(engine: Engine) -> QueuePool:
    """Return the engine's pool, asserting that it is a ``QueuePool``.

    ``Engine.pool`` is annotated with the abstract :class:`~sqlalchemy.pool.Pool`,
    which has neither ``checkedout()`` nor ``size()``. Both live on
    :class:`~sqlalchemy.pool.QueuePool`, the pool SQLAlchemy selects for every
    engine this project builds. Narrowing once here keeps the assertion honest
    and avoids repeating it, or reaching for private attributes, at each call
    site.
    """
    pool = engine.pool
    assert isinstance(pool, QueuePool), (
        f"expected a QueuePool for {engine.url}, got {type(pool).__name__}"
    )
    return pool
