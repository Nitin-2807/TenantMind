"""Tests for the session factory and transaction lifecycle.

These run against an isolated SQLite database, so they exercise real SQL,
real transactions, and real rollback without a PostgreSQL server. Commit and
rollback semantics are engine-level behaviour and identical across backends.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, ForeignKey, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

from app.db import engine as engine_module
from app.db.base import Base
from app.db.session import (
    DbSession,
    build_session_factory,
    enable_sqlite_foreign_keys,
    get_db,
    session_scope,
)
from tests.conftest import make_settings, queue_pool


class ScaffoldingBase(DeclarativeBase):
    """A declarative base owned by this test module only.

    It carries its own :class:`~sqlalchemy.MetaData`, which is the whole point:
    the tables below are registered in *this* registry and never in
    :data:`app.db.base.Base.metadata`. Subclassing the application's ``Base``
    would register test scaffolding into the metadata Alembic autogenerate
    compares against the live schema, so a real ``alembic revision
    --autogenerate`` would try to create ``test_widget`` in production — and,
    worse, would make "the application ships no models" untestable within a
    session, since the registry is process-wide.

    Named to avoid pytest's ``Test*`` collection prefix, which would otherwise
    try to collect this class as a test suite.
    """


class Widget(ScaffoldingBase):
    """A throwaway table used only to prove transactions really work."""

    __tablename__ = "test_widget"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()


class Parent(ScaffoldingBase):
    """Second table, to prove foreign-key enforcement in the test database."""

    __tablename__ = "test_parent"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column()


class Child(ScaffoldingBase):
    __tablename__ = "test_child"

    id: Mapped[int] = mapped_column(primary_key=True)
    # An explicit ForeignKey, not the `foreign_key=` string shorthand: with
    # `from __future__ import annotations` the string is never evaluated, and
    # SQLAlchemy then tries to resolve "foreign" as a dialect name.
    parent_id: Mapped[int] = mapped_column(ForeignKey("test_parent.id"), nullable=False)


@pytest.fixture
def schema(sqlite_engine: Engine) -> Iterator[None]:
    """Create the test tables, then drop them."""
    ScaffoldingBase.metadata.create_all(sqlite_engine)
    yield
    ScaffoldingBase.metadata.drop_all(sqlite_engine)


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------


def test_factory_creates_independent_sessions(
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    first = sqlite_session_factory()
    second = sqlite_session_factory()

    try:
        assert first is not second
        assert isinstance(first, Session)
    finally:
        first.close()
        second.close()


def test_sessions_do_not_share_transaction_state(
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    """Session identity must not be shared, which is what makes it request-scoped."""
    first = sqlite_session_factory()
    second = sqlite_session_factory()

    try:
        first.execute(text("SELECT 1"))

        assert first.in_transaction()
        # The second session has issued nothing, so it has no transaction.
        assert not second.in_transaction()
    finally:
        first.close()
        second.close()


def test_factory_disables_expire_on_commit(sqlite_session_factory: sessionmaker[Session]) -> None:
    """Attributes must survive a commit, or responses serialise stale objects.

    With the default ``expire_on_commit=True`` a loaded attribute is discarded
    on commit and any later access re-queries the database — which fails
    outright once the session is closed.
    """
    assert sqlite_session_factory.kw["expire_on_commit"] is False


def test_factory_keeps_autoflush_enabled(sqlite_session_factory: sessionmaker[Session]) -> None:
    assert sqlite_session_factory.kw["autoflush"] is True


def test_factory_binds_the_given_engine(
    sqlite_engine: Engine, sqlite_session_factory: sessionmaker[Session]
) -> None:
    assert sqlite_session_factory.kw["bind"] is sqlite_engine


def test_no_autocommit_is_configured(sqlite_session_factory: sessionmaker[Session]) -> None:
    """``autocommit`` was removed in SQLAlchemy 2.0; it must not reappear."""
    assert "autocommit" not in sqlite_session_factory.kw


def test_factory_is_rebuildable_from_the_same_engine(sqlite_engine: Engine) -> None:
    first = build_session_factory(sqlite_engine)
    second = build_session_factory(sqlite_engine)

    assert first is not second
    assert first.kw["bind"] is second.kw["bind"]


# ---------------------------------------------------------------------------
# Reads and writes against a real database
# ---------------------------------------------------------------------------


def test_session_executes_a_trivial_query(
    sqlite_session_factory: sessionmaker[Session],
) -> None:
    session = sqlite_session_factory()
    try:
        assert session.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        session.close()


def test_successful_transaction_commits(
    sqlite_session_factory: sessionmaker[Session], schema: None
) -> None:
    session = sqlite_session_factory()
    session.add(Widget(name="committed"))
    session.commit()
    session.close()

    verifier = sqlite_session_factory()
    try:
        names = verifier.execute(text("SELECT name FROM test_widget")).scalars().all()
        assert names == ["committed"]
    finally:
        verifier.close()


def test_failed_transaction_rolls_back(
    sqlite_session_factory: sessionmaker[Session], schema: None
) -> None:
    """An integrity violation must leave no partial write behind."""
    session = sqlite_session_factory()
    session.add(Widget(id=1, name="first"))
    session.commit()

    session.add(Widget(id=1, name="duplicate id"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()

    verifier = sqlite_session_factory()
    try:
        rows = verifier.execute(text("SELECT name FROM test_widget")).scalars().all()
        assert rows == ["first"]
    finally:
        verifier.close()


def test_rollback_discards_every_pending_write(
    sqlite_session_factory: sessionmaker[Session], schema: None
) -> None:
    session = sqlite_session_factory()
    session.add(Widget(name="never-committed"))
    session.rollback()
    session.close()

    verifier = sqlite_session_factory()
    try:
        count = verifier.execute(text("SELECT count(*) FROM test_widget")).scalar_one()
        assert count == 0
    finally:
        verifier.close()


def test_session_close_releases_the_connection(
    sqlite_engine: Engine, schema: None
) -> None:
    """A closed session must return its connection to the pool.

    Verified against the engine's checked-out count rather than by inspection,
    so it tests the actual contract that matters under load.
    """
    factory = build_session_factory(sqlite_engine)

    session = factory()
    session.execute(text("SELECT 1"))
    assert queue_pool(sqlite_engine).checkedout() == 1

    session.close()
    assert queue_pool(sqlite_engine).checkedout() == 0


def test_foreign_keys_are_enforced_in_the_test_database(
    sqlite_session_factory: sessionmaker[Session], schema: None
) -> None:
    """SQLite must reject orphans, or tests would not predict PostgreSQL."""
    session = sqlite_session_factory()
    session.add(Parent(label="p"))
    session.commit()

    session.add(Child(parent_id=9999))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.close()


def test_orm_insert_and_query_round_trip(
    sqlite_session_factory: sessionmaker[Session], schema: None
) -> None:
    session = sqlite_session_factory()
    session.add(Widget(name="orm"))
    session.commit()

    found = session.execute(
        text("SELECT name FROM test_widget WHERE name = :name"),
        {"name": "orm"},
    ).scalar_one()
    assert found == "orm"
    session.close()


# ---------------------------------------------------------------------------
# session_scope — the non-request transaction helper
# ---------------------------------------------------------------------------


def test_session_scope_commits_on_success(
    sqlite_session_factory: sessionmaker[Session], schema: None
) -> None:
    with session_scope(sqlite_session_factory) as session:
        session.add(Widget(name="scoped"))

    verifier = sqlite_session_factory()
    try:
        assert verifier.execute(
            text("SELECT name FROM test_widget")
        ).scalar_one() == "scoped"
    finally:
        verifier.close()


def test_session_scope_rolls_back_and_reraises(
    sqlite_session_factory: sessionmaker[Session], schema: None
) -> None:
    # The two contexts are combined deliberately: the raise happens inside
    # `session_scope`, and `pytest.raises` must observe the re-raised original.
    with (
        pytest.raises(RuntimeError, match="deliberate failure"),
        session_scope(sqlite_session_factory) as session,
    ):
        session.add(Widget(name="discarded"))
        raise RuntimeError("deliberate failure")

    verifier = sqlite_session_factory()
    try:
        count = verifier.execute(text("SELECT count(*) FROM test_widget")).scalar_one()
        assert count == 0
    finally:
        verifier.close()


def test_session_scope_closes_the_session_on_error(
    sqlite_engine: Engine, schema: None
) -> None:
    """A failure must not leak a checked-out connection."""
    factory = build_session_factory(sqlite_engine)

    with pytest.raises(RuntimeError), session_scope(factory) as session:
        session.execute(text("SELECT 1"))
        raise RuntimeError("boom")

    assert queue_pool(sqlite_engine).checkedout() == 0


# ---------------------------------------------------------------------------
# The FastAPI dependency
# ---------------------------------------------------------------------------


def test_get_db_is_usable_as_a_fastapi_dependency(sqlite_engine: Engine) -> None:
    """The dependency must actually be wired for FastAPI, not just a generator."""
    application = FastAPI()
    seen: list[Session] = []

    @application.get("/probe")
    def _probe(session: Session = DbSession) -> dict[str, int]:
        seen.append(session)
        return {"value": session.execute(text("SELECT 1")).scalar_one()}

    # get_db resolves the process-wide engine, so point that at the test DB.
    engine_module._engine = sqlite_engine
    try:
        with TestClient(application) as client:
            assert client.get("/probe").json() == {"value": 1}
    finally:
        engine_module.reset_engine()

    assert len(seen) == 1
    assert isinstance(seen[0], Session)


def test_get_db_commits_on_success(sqlite_engine: Engine, schema: None) -> None:
    application = FastAPI()

    @application.post("/write")
    def _write(session: Session = DbSession) -> dict[str, bool]:
        session.add(Widget(name="via-dependency"))
        return {"ok": True}

    engine_module._engine = sqlite_engine
    try:
        with TestClient(application) as client:
            assert client.post("/write").json() == {"ok": True}
    finally:
        engine_module.reset_engine()

    verifier = build_session_factory(sqlite_engine)()
    try:
        assert verifier.execute(
            text("SELECT name FROM test_widget")
        ).scalar_one() == "via-dependency"
    finally:
        verifier.close()


def test_get_db_rolls_back_when_the_handler_raises(sqlite_engine: Engine, schema: None) -> None:
    """A failing request must not leave writes behind."""
    application = FastAPI()

    @application.post("/explode")
    def _explode(session: Session = DbSession) -> dict[str, bool]:
        session.add(Widget(name="must-not-persist"))
        raise RuntimeError("handler failed")

    engine_module._engine = sqlite_engine
    try:
        with TestClient(application) as client, pytest.raises(RuntimeError):
            client.post("/explode")
    finally:
        engine_module.reset_engine()

    verifier = build_session_factory(sqlite_engine)()
    try:
        count = verifier.execute(text("SELECT count(*) FROM test_widget")).scalar_one()
        assert count == 0
    finally:
        verifier.close()


def test_get_db_closes_the_session_after_the_response(sqlite_engine: Engine) -> None:
    """The connection must be released, not held for the response's lifetime."""
    application = FastAPI()
    captured: list[Session] = []

    @application.get("/hold")
    def _hold(session: Session = DbSession) -> dict[str, bool]:
        session.execute(text("SELECT 1"))
        captured.append(session)
        return {"ok": True}

    engine_module._engine = sqlite_engine
    try:
        with TestClient(application) as client:
            assert client.get("/hold").json() == {"ok": True}
            # The request has completed, so the connection is back in the pool.
            assert queue_pool(sqlite_engine).checkedout() == 0
    finally:
        engine_module.reset_engine()

    assert captured[0].in_transaction() is False


def test_get_db_does_not_introduce_tenant_identity(sqlite_engine: Engine) -> None:
    """No tenant scoping leaks in from a later phase.

    The dependency must expose a plain ``Session``. Tenant identity arrives in
    Phase 2, derived from the authenticated principal — never from request
    data (PMP §8).
    """
    generator = get_db()
    engine_module._engine = sqlite_engine
    try:
        session = next(generator)
    finally:
        engine_module.reset_engine()

    try:
        # No tenant attribute, and no request object to source one from.
        assert not hasattr(session, "tenant_id")
        assert not hasattr(session, "tenant")
        assert not hasattr(session, "user")
    finally:
        generator.close()


def test_db_session_alias_targets_get_db() -> None:
    """The router-facing alias must resolve to this dependency."""
    dependency = DbSession.dependency

    assert dependency is get_db


def test_get_db_raises_when_no_database_is_configured() -> None:
    """The failure must be a clear configuration error, not an AttributeError."""
    engine_module.reset_engine()

    generator = get_db()
    with pytest.raises(engine_module.DatabaseNotConfiguredError):
        next(generator)

    generator.close()


# ---------------------------------------------------------------------------
# SQLite test-support behaviour
# ---------------------------------------------------------------------------


def test_enable_sqlite_foreign_keys_is_a_noop_for_postgres() -> None:
    """Registering the pragma on a PostgreSQL engine must not break it."""
    engine = engine_module.create_engine_from_url(
        "postgresql+psycopg://u:p@localhost:5432/d"
    )
    try:
        enable_sqlite_foreign_keys(engine)
    finally:
        engine.dispose()


def test_enable_sqlite_foreign_keys_is_idempotent(sqlite_engine: Engine) -> None:
    enable_sqlite_foreign_keys(sqlite_engine)
    enable_sqlite_foreign_keys(sqlite_engine)

    with sqlite_engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1


def test_test_models_never_reach_the_application_metadata() -> None:
    """Test scaffolding must stay out of the Alembic autogenerate registry.

    ``Base.metadata`` is what Alembic compares against the live schema. A test
    model registered there would become a table a real
    ``alembic revision --autogenerate`` tried to create in production, so
    :class:`ScaffoldingBase` exists precisely to keep the two registries separate.
    """
    # The test tables are registered, but in their own registry.
    assert "test_widget" in ScaffoldingBase.metadata.tables

    # And the application's registry contains only real application tables, so
    # the scaffolding table never becomes something a production autogenerate
    # would try to create.
    import app.models

    assert "test_widget" not in Base.metadata.tables
    assert set(Base.metadata.tables) == app.models.MODEL_TABLES


def test_importing_base_alone_registers_no_tables() -> None:
    """``app.db.base`` must not import the model package on its own.

    Still true in Phase 2 Step 1, and now more consequential: model discovery is
    an explicit act (``import app.models``), and this is what proves the base
    module does not quietly pull the models in behind the caller's back. Asserted
    in a subprocess so the property does not depend on which test modules have
    already run in this process.
    """
    import subprocess
    import sys
    from pathlib import Path

    backend_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.db.base import Base; print(len(Base.metadata.tables))",
        ],
        capture_output=True,
        text=True,
        cwd=backend_root,
        check=True,
    )

    assert result.stdout.strip() == "0"


def test_no_sqlalchemy_error_is_swallowed(
    sqlite_session_factory: sessionmaker[Session], schema: None
) -> None:
    """Database errors must propagate; silent failure is how data loss hides."""
    session = sqlite_session_factory()

    with pytest.raises(SQLAlchemyError):
        session.execute(text("SELECT * FROM table_that_does_not_exist"))

    session.rollback()
    session.close()


def test_db_session_alias_is_a_depends_marker_for_get_db() -> None:
    """The router-facing alias must stay bound to this exact dependency.

    :data:`DbSession` is what every route signature will use. If it were ever
    rebound to a different provider, routes would silently stop receiving the
    session this module defines — a failure that would otherwise only show up
    as a confusing error inside an unrelated endpoint.
    """
    marker = DbSession

    assert isinstance(marker, type(Depends(get_db)))
    assert marker.dependency is get_db
    assert marker.use_cache is True


def test_settings_are_not_mutated_by_the_db_layer() -> None:
    """Building a session must not alter shared configuration."""
    settings = make_settings(database_url=engine_module.sqlite_dsn("/tmp/x.db"))
    before = settings.model_dump()

    factory = build_session_factory(engine_module.create_engine_from_settings(settings))
    factory().close()

    assert settings.model_dump() == before


def test_pool_settings_have_the_types_sqlalchemy_requires() -> None:
    """A pool value that is a string or a float would fail only at connect time.

    These are plain constants, so a regression here means someone typed one
    wrongly; catching it at the boundary beats a confusing runtime error from
    deep inside the pool.
    """
    kwargs = engine_module.build_engine_kwargs("postgresql+psycopg://u:p@h:5432/d")

    for key in ("pool_size", "max_overflow", "pool_timeout", "pool_recycle"):
        assert isinstance(kwargs[key], int), f"{key} must be an int"
    assert isinstance(kwargs["pool_pre_ping"], bool)


def test_configured_database_url_is_used_by_the_factory(tmp_path: Path) -> None:
    """End-to-end: a ``Settings``-supplied DSN reaches a working engine.

    Closes the loop the whole module depends on: configuration read through
    ``Settings``, never ``os.environ``, produces an engine on which a session
    can actually run a statement.
    """
    dsn = engine_module.sqlite_dsn(str(tmp_path / "from_settings.db"))
    settings = make_settings(database_url=dsn)

    engine = engine_module.create_engine_from_settings(settings)
    try:
        session = build_session_factory(engine)()
        try:
            assert session.execute(text("SELECT 1")).scalar_one() == 1
        finally:
            session.close()
    finally:
        engine.dispose()
