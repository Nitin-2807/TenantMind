"""Database connectivity probe for future readiness checks.

Exists so that Phase 5 (or whichever step wires up real readiness) has a
single, tested definition of "is the database reachable?" without re-deriving
it inline in a router.

Security posture
----------------
:func:`check_database` returns a :class:`DatabaseHealth` **value**, never an
exception. Callers cannot accidentally leak a driver error — which routinely
embeds the DSN, and therefore the password — into an API response or a log
line. The only diagnostic carried is the exception *class name*, which is a
fixed set of driver identifiers and carries no user data.

``/health`` does not call this, and ``/health/ready`` does not call it either
in Step 4. Phase 1 has no PostgreSQL to probe, and wiring it into readiness
now would make the endpoint fail on every developer machine that has no
database running. See the module note in ``app/api/health.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.db.engine import DatabaseNotConfiguredError, get_engine

#: The cheapest possible statement that still proves the server parsed and
#: executed a query. Deliberately not ``SELECT version()`` or a table probe:
#: this answers "is the connection usable", not "is the schema correct".
CONNECTIVITY_PROBE = "SELECT 1"


class DatabaseStatus(str, Enum):
    """Outcome of a connectivity probe.

    Inherits from ``str`` so the value serialises directly to JSON without a
    custom encoder.

    ``NOT_CONFIGURED`` is distinct from ``UNAVAILABLE`` on purpose: the first
    means "this deployment never configured a database" (a deployment
    problem), the second means "configured, but unreachable right now" (an
    availability problem). Collapsing them would make an operator debug the
    wrong thing.
    """

    OK = "ok"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class DatabaseHealth:
    """Result of a connectivity probe.

    Frozen so a result cannot be mutated after being reported. Carries no DSN,
    no credentials, and no raw exception message.
    """

    status: DatabaseStatus
    #: Exception class name, e.g. ``"OperationalError"``. ``None`` on success.
    error_type: str | None = None

    @property
    def is_healthy(self) -> bool:
        """True only when the probe actually reached the server."""
        return self.status is DatabaseStatus.OK

    def as_dict(self) -> dict[str, str | None]:
        """JSON-ready form for a future readiness response."""
        return {"database": self.status.value, "error_type": self.error_type}


def check_database(engine: Engine | None = None) -> DatabaseHealth:
    """Probe the database with ``SELECT 1`` and report the outcome.

    Args:
        engine: Engine to probe. Defaults to the process-wide engine. Passing
            one explicitly is how tests point the probe at an isolated
            database.

    Returns:
        A :class:`DatabaseHealth`. This function does not raise: a probe that
        raises is a probe that cannot be used in a health endpoint.
    """
    try:
        target = get_engine() if engine is None else engine
    except DatabaseNotConfiguredError:
        return DatabaseHealth(status=DatabaseStatus.NOT_CONFIGURED)

    try:
        with target.connect() as connection:
            connection.execute(text(CONNECTIVITY_PROBE))
    except SQLAlchemyError as exc:
        # Only the class name is reported. The message can contain the DSN,
        # which contains the password.
        return DatabaseHealth(
            status=DatabaseStatus.UNAVAILABLE,
            error_type=type(exc).__name__,
        )

    return DatabaseHealth(status=DatabaseStatus.OK)


def check_database_or_raise(engine: Engine | None = None) -> None:
    """Probe, returning ``None`` on success and re-raising on failure.

    For non-endpoint callers — startup scripts, CLI checks — that genuinely
    want the original driver exception. Endpoint code must use
    :func:`check_database` and never this, because the exception message can
    embed the DSN.

    Raises:
        DatabaseNotConfiguredError: if no engine is configured.
        SQLAlchemyError: if the probe fails.
    """
    target = get_engine() if engine is None else engine

    with target.connect() as connection:
        connection.execute(text(CONNECTIVITY_PROBE))
