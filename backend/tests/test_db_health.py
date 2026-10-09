"""Tests for the database connectivity helper.

The property that matters most here is negative: a failed probe must produce a
*value*, never an exception carrying a DSN. Driver error messages routinely
embed the connection string, so propagating one into an API response or a log
line would leak the database password.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.db import engine as engine_module
from app.db.health import (
    CONNECTIVITY_PROBE,
    DatabaseHealth,
    DatabaseStatus,
    check_database,
    check_database_or_raise,
)
from tests.conftest import make_settings, queue_pool

# ---------------------------------------------------------------------------
# Successful connectivity
# ---------------------------------------------------------------------------


def test_successful_probe_returns_ok(sqlite_engine: Engine) -> None:
    health = check_database(sqlite_engine)

    assert health.status is DatabaseStatus.OK
    assert health.is_healthy is True
    assert health.error_type is None


def test_probe_executes_select_one(sqlite_engine: Engine) -> None:
    """The probe must be a real statement, not a fabricated result."""
    assert CONNECTIVITY_PROBE == "SELECT 1"

    health = check_database(sqlite_engine)

    assert health.is_healthy


def test_probe_closes_its_connection(sqlite_engine: Engine) -> None:
    """A probe that leaked its connection would exhaust the pool over time."""
    check_database(sqlite_engine)

    assert queue_pool(sqlite_engine).checkedout() == 0


def test_probe_releases_the_connection_even_when_the_query_fails() -> None:
    engine = engine_module.create_engine_from_url(
        "postgresql+psycopg://user:pw@127.0.0.1:59999/tenantmind"
    )
    try:
        check_database(engine)

        assert queue_pool(engine).checkedout() == 0
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Failed connectivity
# ---------------------------------------------------------------------------


def test_unreachable_postgres_reports_unavailable() -> None:
    """An unreachable server must be reported, never raised."""
    engine = engine_module.create_engine_from_url(
        "postgresql+psycopg://user:pw@127.0.0.1:59999/tenantmind"
    )
    try:
        health = check_database(engine)

        assert health.status is DatabaseStatus.UNAVAILABLE
        assert health.is_healthy is False
    finally:
        engine.dispose()


def test_failed_probe_does_not_raise() -> None:
    """A probe that raises cannot be used in a health endpoint."""
    engine = engine_module.create_engine_from_url(
        "postgresql+psycopg://user:pw@127.0.0.1:59999/tenantmind"
    )
    try:
        health = check_database(engine)
    finally:
        engine.dispose()

    assert isinstance(health, DatabaseHealth)


def test_unconfigured_database_reports_not_configured() -> None:
    """Absent configuration and an unreachable server are different problems."""
    engine_module.reset_engine()

    health = check_database()

    assert health.status is DatabaseStatus.NOT_CONFIGURED
    assert health.is_healthy is False
    assert health.error_type is None


def test_not_configured_and_unavailable_are_distinct() -> None:
    """An operator must be able to tell deployment from availability faults."""
    assert DatabaseStatus.NOT_CONFIGURED is not DatabaseStatus.UNAVAILABLE
    assert DatabaseStatus.NOT_CONFIGURED.value == "not_configured"
    assert DatabaseStatus.UNAVAILABLE.value == "unavailable"


def test_error_type_is_reported_without_the_message() -> None:
    engine = engine_module.create_engine_from_url(
        "postgresql+psycopg://user:pw@127.0.0.1:59999/tenantmind"
    )
    try:
        health = check_database(engine)
    finally:
        engine.dispose()

    # The class name is useful and fixed; the message can contain the DSN.
    assert health.error_type == "OperationalError"
    assert "127.0.0.1" not in (health.error_type or "")


# ---------------------------------------------------------------------------
# No credential disclosure
# ---------------------------------------------------------------------------


def test_health_result_never_contains_credentials() -> None:
    """Neither the dataclass nor its dict form may carry a password."""
    sentinel = "sup3rs3cret-db-password"
    engine = engine_module.create_engine_from_url(
        f"postgresql+psycopg://app_user:{sentinel}@127.0.0.1:59999/tenantmind"
    )
    try:
        health = check_database(engine)
    finally:
        engine.dispose()

    rendered = repr(health) + repr(health.as_dict()) + str(health.as_dict())

    assert sentinel not in rendered
    assert "app_user" not in rendered
    assert health.as_dict()["error_type"] == "OperationalError"


def test_health_result_has_no_dsn_field() -> None:
    """The dataclass must not grow a field that could hold a DSN."""
    field_names = {field.name for field in dataclasses.fields(DatabaseHealth)}

    assert field_names == {"status", "error_type"}
    assert not any(
        name in field_names for name in ("url", "dsn", "message", "detail", "error")
    )


def test_health_result_is_immutable() -> None:
    """A reported result must not be alterable by a later caller."""
    result = DatabaseHealth(status=DatabaseStatus.OK)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = DatabaseStatus.UNAVAILABLE  # type: ignore[misc]


def test_as_dict_is_json_ready() -> None:
    import json

    result = DatabaseHealth(status=DatabaseStatus.UNAVAILABLE, error_type="OperationalError")

    assert result.as_dict() == {
        "database": "unavailable",
        "error_type": "OperationalError",
    }
    # Must serialise without a custom encoder.
    assert json.loads(json.dumps(result.as_dict()))["database"] == "unavailable"


def test_status_is_a_string_enum() -> None:
    """So it drops into a JSON response directly."""
    assert isinstance(DatabaseStatus.OK, str)
    assert DatabaseStatus.OK == "ok"
    assert DatabaseStatus.OK.value == "ok"


# ---------------------------------------------------------------------------
# The raising variant
# ---------------------------------------------------------------------------


def test_check_database_or_raise_passes_on_success(sqlite_engine: Engine) -> None:
    """Success is signalled by the absence of an exception, not a return value."""
    check_database_or_raise(sqlite_engine)


def test_check_database_or_raise_propagates_the_driver_error() -> None:
    """CLI callers genuinely want the original exception."""
    engine = engine_module.create_engine_from_url(
        "postgresql+psycopg://user:pw@127.0.0.1:59999/tenantmind"
    )
    try:
        with pytest.raises(SQLAlchemyError):
            check_database_or_raise(engine)
    finally:
        engine.dispose()


def test_check_database_or_raise_raises_when_unconfigured() -> None:
    engine_module.reset_engine()

    with pytest.raises(engine_module.DatabaseNotConfiguredError):
        check_database_or_raise()


def test_operational_error_is_what_a_down_server_raises() -> None:
    """Pins the expected exception class for the unavailable-path tests."""
    engine = engine_module.create_engine_from_url(
        "postgresql+psycopg://user:pw@127.0.0.1:59999/tenantmind"
    )
    try:
        with pytest.raises(OperationalError), engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# /health is unaffected
# ---------------------------------------------------------------------------


def test_health_endpoints_do_not_call_the_database_probe() -> None:
    """Liveness and readiness must not touch the database in Step 4.

    Phase 1 has no PostgreSQL to probe, and requiring one would fail on every
    machine without a database. This test is the guard that keeps the probe
    available for a future readiness step without it leaking in early.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    # No engine configured at all, so any database call would raise.
    engine_module.reset_engine()

    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}


def test_health_router_does_not_import_the_database_layer() -> None:
    """Keeps the dependency direction one-way: the router must not need the probe.

    Checked against the parsed import statements rather than the raw text, so a
    docstring that merely mentions ``check_database`` cannot satisfy or break
    the assertion. The path is derived from the package location, so the test
    does not depend on the working directory pytest was started from.
    """
    import ast
    import pathlib

    import app.api.health as health_module

    assert health_module.__file__ is not None
    source = pathlib.Path(health_module.__file__).read_text(encoding="utf-8")

    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module or ''}.{a.name}" for a in node.names)

    assert not any(name.startswith("app.db") for name in imported), (
        f"the health router must not import the database layer: {sorted(imported)}"
    )


def test_database_status_is_not_faked_as_healthy() -> None:
    """The probe must never report OK when it did not reach a server."""
    engine_module.reset_engine()
    unconfigured = check_database()

    assert unconfigured.status is not DatabaseStatus.OK
    assert unconfigured.is_healthy is False


def test_check_database_does_not_mutate_settings() -> None:
    settings = make_settings(database_url=None)

    check_database()

    assert settings.database_url is None
