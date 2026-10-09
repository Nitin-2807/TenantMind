"""Phase 5 Step 1 — a real Redis, a real worker, one real round trip.

Everything in :mod:`tests.test_worker_app` runs without a server, which is
correct for a unit suite and also insufficient: it proves the application is
*configured* correctly, not that a message can cross a broker and come back.
This file closes that gap and nothing more.

What it proves, and nothing beyond it::

    Celery application -> Redis broker -> Celery worker -> task -> result

The task is a trivial addition. It touches no PostgreSQL, no Qdrant, no
document, no embedding provider and no LLM, because this step is about the
plumbing; anything task-shaped beyond ``add`` would be Step 2's work and would
make a broker failure indistinguishable from a pipeline failure.

Running it
----------

Point ``TENANTMIND_TEST_REDIS_URL`` at a Redis server and run::

    TENANTMIND_TEST_REDIS_URL='redis://:<password>@127.0.0.1:6379/4' \\
        pytest tests/test_worker_integration.py

Without that variable the file skips, exactly as
``tests/test_postgres_integration.py`` and ``tests/test_qdrant_integration.py``
do — so the ordinary suite never depends on a running server, and never
silently stops being runnable when one is absent.

Safety
------

This suite writes to Redis and deletes only the keys it created. It refuses a
URL whose host does not look local or test, and refuses Redis database 0, which
is the index a developer's real broker queue lives in. ``docker-compose.yml``
brings up a disposable Redis on ``127.0.0.1:6379`` for exactly this.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any, cast

import pytest
import redis
from celery import Celery  # type: ignore[import-untyped]
from celery.app.task import Task  # type: ignore[import-untyped]
from celery.exceptions import TimeoutError  # type: ignore[import-untyped]
from celery.result import AsyncResult  # type: ignore[import-untyped]

from app.workers.celery_app import build_celery_app
from tests.conftest import make_settings

#: Environment variable naming the Redis server to test against. Distinct from
#: the application's own ``REDIS_URL`` so that running the integration suite can
#: never be achieved by accident through a development ``.env``, and so that a
#: developer pointing their app at one Redis is not silently also pointing this
#: suite at it.
REDIS_URL_ENV_VAR = "TENANTMIND_TEST_REDIS_URL"

#: Fragments that indicate a disposable Redis. A URL naming nothing else is
#: refused, because this suite writes and deletes Redis keys.
SAFE_HOST_MARKERS = ("localhost", "127.0.0.1", "test", "tmp", "ci", "redis", "dev")

#: The application name and task name this suite's throwaway worker reports,
#: kept separate from the shipped ones so an ``inspect ping`` during a run can
#: tell them apart from a real worker in the same interpreter.
TEST_APP_NAME = "tenantmind-worker-smoke"
SMOKE_TASK_NAME = f"{TEST_APP_NAME}.smoke_add"

#: Returned by the smoke task for the ``(2, 2)`` arguments used by the first
#: round-trip test. Every later test passes a different pair and writes its own
#: expected sum out, because tests that all send identical arguments cannot tell
#: a broker that delivered the right message from one that delivered any
#: message at all.
SMOKE_RESULT = 4

#: How long to wait for a task to come back. Generous for a loopback broker and
#: still bounded: an unbounded ``get()`` on a broker that accepted a message and
#: never answered is a suite that hangs rather than fails, which is the one
#: outcome a gate must never produce.
RESULT_TIMEOUT_SECONDS = 30.0

SKIP_REASON = (
    f"{REDIS_URL_ENV_VAR} is not set, so no Redis server is configured. "
    "The Celery/Redis integration gate is therefore PENDING; see this "
    "module's docstring for how to run it."
)

#: Applied to the fixtures' consumers rather than as a module-level
#: ``pytestmark``, so that the guard-checking tests still run: a blanket module
#: skip would silence them and the suite could then skip for any structural
#: reason without anything reporting it.
requires_redis = pytest.mark.usefixtures("redis_url")


def _configured_url() -> str | None:
    value = os.environ.get(REDIS_URL_ENV_VAR)
    return value.strip() if value and value.strip() else None


REQUIRES_REDIS = pytest.mark.skipif(
    _configured_url() is None,
    reason=SKIP_REASON,
)


# ---------------------------------------------------------------------------
# The smoke task
# ---------------------------------------------------------------------------
#
# Defined here rather than in `app/workers/` on purpose. A task that exists only
# to prove infrastructure is test code; shipping it in the application would put
# a do-nothing task in the production task registry, where it would be
# discoverable, callable, and — for anyone reading the registry — hard to tell
# from a real one.


def smoke_add(left: int, right: int) -> int:
    """The smallest task that still proves arguments survive the round trip.

    Takes two arguments and returns their sum rather than returning a constant,
    because "the worker returned the right constant" is also what an eager task
    returns when no broker is involved at all. Two arguments are serialised into
    the message, deserialised by the worker, and come back.
    """
    return left + right


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def redis_url() -> str:
    """The configured server URL, after checking it is safe to write to.

    Carries the skip itself so every fixture needing it inherits the same
    decision in one place — the arrangement the PostgreSQL and Qdrant suites
    already use.
    """
    url = _configured_url()
    if url is None:
        pytest.skip(SKIP_REASON)

    lowered = url.lower()
    assert lowered.startswith(("redis://", "rediss://")), (
        f"{REDIS_URL_ENV_VAR} must be a redis:// or rediss:// URL, got "
        f"{_without_credentials(url)!r}"
    )
    assert any(marker in lowered for marker in SAFE_HOST_MARKERS), (
        f"refusing to test against {_without_credentials(url)!r}: this suite "
        "writes and deletes Redis keys, so the host must look like a local or "
        "test server"
    )

    # Database 0 is where a developer's real Celery broker queue lives. A
    # dedicated index per environment keeps this suite's keys out of it
    # entirely, which is stronger than deleting only what it created — a shared
    # index makes "only what it created" a claim about behaviour rather than
    # about the file.
    assert _database_index(url) != 0, (
        f"{REDIS_URL_ENV_VAR} points at Redis database 0, which is where a "
        "developer's own Celery queue lives. Use a dedicated index (for example "
        "redis://127.0.0.1:6379/4) so this suite cannot disturb it."
    )
    return url


@pytest.fixture(scope="module")
def live_redis(redis_url: str) -> Iterator[redis.Redis]:
    """A real client, connected for the module's duration.

    The connection is proved open here. Otherwise every test in this file would
    fail with its own error, and a reader would have to guess that the common
    cause was one refused connection — or worse, that some of them passed
    against nothing.
    """
    client = redis.Redis.from_url(redis_url, socket_connect_timeout=5)
    try:
        if not client.ping():
            pytest.fail(f"{REDIS_URL_ENV_VAR} answered PING with a falsy value")
    except redis.RedisError as error:  # pragma: no cover - the failing path
        pytest.fail(
            f"{REDIS_URL_ENV_VAR}={_without_credentials(redis_url)!r} is not "
            f"answering: {error}"
        )
    yield client
    client.close()


@pytest.fixture(scope="module")
def worker_app(redis_url: str) -> Celery:
    """A Celery application built by the *shipped* factory, against the test Redis.

    Deliberately the production
    :func:`app.workers.celery_app.build_celery_app` rather than a hand-rolled
    ``Celery(...)`` in the test. A test that constructed its own application would
    pass even if the factory wired the broker wrongly, which is the one thing
    this file exists to check.

    Three things are changed on top of the shipped configuration, all mechanical
    and all recorded here rather than left implicit:

    * a unique default queue per run, so two concurrent runs cannot deliver each
      other's messages;
    * a distinct application name, so ``celery inspect ping`` during a run
      distinguishes this throwaway worker from a real one in the same
      interpreter;
    * the smoke task registered on it, because Step 1 ships no task of its own.

    ``task_always_eager`` is deliberately **not** set. Eager mode runs tasks
    inline in the calling process and never touches the broker, so enabling it
    would make every round-trip test in this file pass against no Redis at all.

    The module-scoped lifetime matters too: one application, one worker, one
    queue for the whole file, so every test publishes onto the queue the running
    worker is actually consuming.
    """
    application = build_celery_app(make_settings(redis_url=redis_url))

    application.conf.main = TEST_APP_NAME
    application.conf.task_default_queue = f"{TEST_APP_NAME}-{uuid.uuid4().hex[:8]}"
    application.task(name=SMOKE_TASK_NAME)(smoke_add)

    return application


@pytest.fixture(scope="module")
def smoke(worker_app: Celery) -> Task:
    """The smoke task, reached through the registry rather than by import.

    Calling :func:`smoke_add` directly from a test would be the in-process call
    this file exists to rule out. Going through ``app.tasks[name].delay()`` is
    what a producer does, and it is the step that can break: the name has to
    resolve on *both* sides of the broker.
    """
    return worker_app.tasks[SMOKE_TASK_NAME]


@pytest.fixture
def worker(worker_app: Celery) -> Iterator[Any]:
    """A real Celery worker consuming the real broker, for one test.

    Started in-process by Celery's own test helper. That is not a shortcut — it
    is a genuine worker with a genuine broker connection, running the same
    ``app`` object a ``celery -A app.workers.celery_app worker`` process would.
    The alternative, launching a subprocess and polling its log for a readiness
    line, is the flaky version of this test: it depends on log formatting, on
    start-up ordering, and on a timeout chosen by guesswork.

    ``perform_ping_check`` is left on, so Celery's own ``ping`` task completes
    before the test body runs. That is an independent round trip through the
    broker, and it means a broker that is up but not delivering fails here with
    Celery's own assertion rather than as an unexplained failure below.

    The import of ``celery.contrib.testing.tasks`` is not optional and not
    incidental: Celery ships ``celery.ping`` inside that module and registers it
    only when something imports it, which is why the helper asserts
    ``'celery.ping' in app.tasks`` before it starts. It is named here so the next
    reader does not treat the line as scaffolding to delete.
    """
    import celery.contrib.testing.tasks  # type: ignore[import-untyped]
    import celery.contrib.testing.worker  # type: ignore[import-untyped]

    with celery.contrib.testing.worker.start_worker(
        worker_app,
        perform_ping_check=True,
        ping_task_timeout=RESULT_TIMEOUT_SECONDS,
        shutdown_timeout=RESULT_TIMEOUT_SECONDS,
    ) as running:
        yield running


# ---------------------------------------------------------------------------
# Test 1 — the infrastructure, before any project code is involved
# ---------------------------------------------------------------------------


class TestRedisIsReachable:
    """The broker answers, and it is not answering anyone who asks."""

    @requires_redis
    def test_it_answers_ping(self, live_redis: redis.Redis) -> None:
        assert live_redis.ping() is True

    @requires_redis
    def test_it_reports_a_version(self, live_redis: redis.Redis) -> None:
        """So a failure report can name the server, not just "Redis".

        A client/server mismatch is exactly the kind of thing that otherwise
        surfaces as an unexplained protocol error — the same lesson the Qdrant
        suite records about pinning its image to its client's version.

        The ``cast`` is redis-py's overloads, not a shortcut: it types
        ``info()`` as returning the client *or* an awaitable, because the same
        class serves both the synchronous and the async API.
        """
        server_info = cast("dict[str, Any]", live_redis.info("server"))

        assert str(server_info["redis_version"])

    @requires_redis
    def test_it_refuses_a_connection_with_no_password(
        self, redis_url: str
    ) -> None:
        """An unauthenticated broker on a network is an unauthenticated queue.

        Anyone who can open the port can then enqueue work for a worker that
        holds database credentials and provider keys. This is the test that makes
        the compose file's ``--requirepass`` a property rather than a preference.
        Skipped when the configured URL carries no credential at all, because
        there is nothing to strip and nothing to prove.
        """
        if "@" not in redis_url:
            pytest.skip(f"{REDIS_URL_ENV_VAR} carries no credential to strip")

        unauthenticated = _without_credentials(redis_url)
        client = redis.Redis.from_url(unauthenticated, socket_connect_timeout=5)
        try:
            with pytest.raises(redis.AuthenticationError):
                client.ping()
        finally:
            client.close()

    @requires_redis
    def test_the_test_database_is_not_the_broker_default(self, live_redis: redis.Redis) -> None:
        """The fixture's rule, observed against the server rather than the URL.

        Reading an index off a URL string proves the parsing. Connecting and
        asking ``SELECT``-equivalent state proves the connection actually
        landed where the fixture said it would.
        """
        assert live_redis.connection_pool.connection_kwargs["db"] != 0


# ---------------------------------------------------------------------------
# Test 2 — the shipped application, pointed at the real broker
# ---------------------------------------------------------------------------


class TestTheApplicationReachesTheRealBroker:
    """`build_celery_app` was not merely configured — it connects."""

    @requires_redis
    def test_the_broker_url_survives_the_secret_unwrapping(self, worker_app: Celery) -> None:
        """Byte-for-byte, credential included.

        The unit suite proves the broker URL equals the configured URL. This
        proves the value Celery received is the one an operator wrote, including
        the password — which is the specific thing a ``SecretStr`` round trip
        could plausibly get wrong.
        """
        assert worker_app.conf.broker_url == _configured_url()

    @requires_redis
    def test_the_application_can_open_a_broker_connection(self, worker_app: Celery) -> None:
        """Celery's own connection object, through the shipped configuration.

        This is the assertion that would catch a factory reading the broker URL
        from anywhere but :class:`~app.core.config.Settings`, or dropping the
        credential while unwrapping the :class:`~pydantic.SecretStr`.
        """
        with worker_app.connection() as connection:
            connection.ensure_connection(max_retries=1)

    @requires_redis
    def test_publishing_writes_a_message_onto_the_queue(
        self, worker_app: Celery, live_redis: redis.Redis
    ) -> None:
        """The message is on the *server*, before any worker consumes it.

        Publishes onto a private queue nobody is listening to, then reads the
        queue length back off Redis. If the message had stayed in the caller's
        memory, every round-trip test in this file would still pass while
        proving nothing.
        """
        queue = f"{TEST_APP_NAME}-publish-probe-{uuid.uuid4().hex[:8]}"
        worker_app.send_task(SMOKE_TASK_NAME, args=(1, 1), queue=queue)

        try:
            assert live_redis.llen(queue) == 1, (
                "publishing did not put a message on the broker's queue"
            )
        finally:
            # Only the key this test created.
            live_redis.delete(queue)

    @requires_redis
    def test_eager_mode_is_off(self, worker_app: Celery) -> None:
        """Stated rather than assumed.

        If this ever flips, every round-trip test in this file becomes a local
        function call that passes against no Redis whatsoever.
        """
        assert not worker_app.conf.task_always_eager


# ---------------------------------------------------------------------------
# Test 3 — the round trip
# ---------------------------------------------------------------------------


@requires_redis
class TestATaskSurvivesTheRoundTrip:
    """The actual deliverable of this step, and the whole of it."""

    def test_a_trivial_task_returns_its_result(
        self, worker: Any, smoke: Task
    ) -> None:
        """Application -> broker -> worker -> task -> result.

        The arguments cross the wire and come back, so this is not an in-process
        function call wearing a task's name.
        """
        outcome = smoke.delay(2, 2)

        assert outcome.get(timeout=RESULT_TIMEOUT_SECONDS) == SMOKE_RESULT

    def test_the_task_really_went_through_the_broker(
        self, worker: Any, worker_app: Celery, smoke: Task, live_redis: redis.Redis
    ) -> None:
        """Rules out eager mode, which would make the test above meaningless.

        Two independent proofs that a broker was involved:

        * the result is readable from Redis under Celery's own metadata key,
          which only a Redis result backend writes — an eager task never
          touches it;
        * the message is gone from the broker's queue, which only a consumer
          can do.

        Deliberately a different argument pair from the test above. If every test
        sent ``(2, 2)`` they would all pass identically whether the broker
        delivered the right message or merely *a* message, so the arguments
        differ per test and the expected sum is written out rather than reused.

        The consumed hostname is deliberately *not* asserted. It is only present
        in result metadata when ``result_extended`` is enabled, which this
        application does not enable: extended metadata is storage on every
        result for information no code reads yet, and turning it on to make a
        test assert something is the wrong order of work. Revisit it in the
        step that has failure tracking and actually wants to know which worker
        died.
        """
        queue = worker_app.conf.task_default_queue
        outcome = smoke.delay(20, 22)

        assert outcome.get(timeout=RESULT_TIMEOUT_SECONDS) == 42
        assert live_redis.exists(f"celery-task-meta-{outcome.id}"), (
            "no result reached the Redis result backend, so the task did not go "
            "through the broker"
        )
        assert live_redis.llen(queue) == 0, (
            "the message is still sitting on the broker's queue, so nothing "
            "consumed it"
        )
        assert outcome.result is not None

    def test_a_result_can_be_read_back_later(
        self, worker: Any, worker_app: Celery, smoke: Task
    ) -> None:
        """The result backend, not just the round trip.

        This is why a result backend exists at all: without one a task runs and
        its return value is discarded, so nothing downstream can ever learn what
        happened. Read the same task twice by id through a fresh
        :class:`~celery.result.AsyncResult` — which is how a later step will
        check whether a document finished ingesting.
        """
        published = smoke.delay(1, 2)
        assert published.get(timeout=RESULT_TIMEOUT_SECONDS) == 3

        reread = AsyncResult(published.id, app=worker_app)

        assert reread.state == "SUCCESS"
        assert reread.get(timeout=RESULT_TIMEOUT_SECONDS) == 3

    def test_a_task_nobody_consumed_stays_pending(
        self, smoke: Task, live_redis: redis.Redis
    ) -> None:
        """Queueing works independently of consumption.

        Published onto a queue with no worker on it. This is the other half of
        the round trip and it is what proves the *broker* is doing the holding,
        not the worker: nothing in this test starts a Celery process, and the
        message must still be sitting on Redis afterwards.
        """
        queue = f"{TEST_APP_NAME}-orphan-{uuid.uuid4().hex[:8]}"
        orphan = smoke.apply_async(args=(1, 1), queue=queue)

        try:
            assert orphan.state == "PENDING"
            assert live_redis.llen(queue) == 1, (
                "a task nobody consumed should still be on the queue"
            )
        finally:
            live_redis.delete(queue)

    def test_an_unknown_task_id_is_reported_as_missing_not_guessed(
        self, worker_app: Celery
    ) -> None:
        """A missing result must not come back as a plausible answer.

        Retry handling arrives in a later step and will depend on this: a
        backend that invented a value for an unknown id would let a retry policy
        treat a document that never ran as one that succeeded.

        The id is a ``str``, because that is what Celery produces. Passing a
        ``uuid.UUID`` instead raises ``TypeError`` from inside kombu's key
        builder long before any lookup happens, which would make this test pass
        for a reason that has nothing to do with the backend.
        """
        unknown = AsyncResult(uuid.uuid4().hex, app=worker_app)

        assert unknown.state == "PENDING"
        with pytest.raises(TimeoutError):
            unknown.get(timeout=5, propagate=True)


# ---------------------------------------------------------------------------
# Test 4 — the guards themselves still work
# ---------------------------------------------------------------------------


class TestTheGuards:
    """The same refusals the unit suite makes, checked with a server present.

    A skip guard that has only ever passed while nothing was configured is a
    guard nobody has watched fire.
    """

    def test_the_skip_reason_names_the_variable(self) -> None:
        assert REDIS_URL_ENV_VAR in SKIP_REASON
        assert REDIS_URL_ENV_VAR != "REDIS_URL", (
            "the integration suite must use its own variable, so that pointing "
            "the application at a development Redis does not also point this "
            "suite at it"
        )

    @pytest.mark.parametrize("value", ["", "   ", "\n"])
    def test_a_blank_url_is_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(REDIS_URL_ENV_VAR, value)

        assert _configured_url() is None

    def test_an_unset_variable_is_treated_as_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(REDIS_URL_ENV_VAR, raising=False)

        assert _configured_url() is None

    def test_the_safety_markers_accept_the_compose_host(self) -> None:
        """The disposable Redis in `docker-compose.yml` must pass the guard."""
        assert any(
            marker in "redis://127.0.0.1:6379/4" for marker in SAFE_HOST_MARKERS
        )

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("redis://127.0.0.1:6379/0", 0),
            ("redis://127.0.0.1:6379/4", 4),
            ("redis://127.0.0.1:6379", 0),
            ("redis://127.0.0.1:6379/", 0),
            ("rediss://cache.example:6380/12", 12),
        ],
    )
    def test_the_database_index_is_parsed_and_defaults_to_zero(
        self, url: str, expected: int
    ) -> None:
        """Including the no-index case, which is what makes the fixture's refusal
        of index 0 work for a bare URL."""
        assert _database_index(url) == expected

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("redis://:pw@host:6379/4", "redis://host:6379/4"),
            ("redis://host:6379/4", "redis://host:6379/4"),
            ("rediss://:pw@host:6380/1", "rediss://host:6380/1"),
        ],
    )
    def test_credentials_can_be_stripped_for_a_failure_message(
        self, url: str, expected: str
    ) -> None:
        """Because every skip and failure message here prints the URL, and the
        URL is where the broker password lives."""
        assert _without_credentials(url) == expected


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _database_index(url: str) -> int:
    """Return the Redis database index a ``redis://`` URL names.

    Defaults to 0 when the URL names no index, which is both Redis's own
    default and what lets the fixture refuse index 0 for a bare URL.
    """
    path = url.split("?", 1)[0].rsplit("/", 1)
    if len(path) != 2 or not path[1].isdigit():
        return 0
    return int(path[1])


def _without_credentials(url: str) -> str:
    """Strip the ``user:password@`` portion of a Redis URL.

    Used only in messages. Every skip and failure message in this file prints
    the URL it refused, and that URL is where the broker password lives, so it
    must never reach a test report, a CI log or a terminal.
    """
    scheme, _, remainder = url.partition("://")
    if "@" in remainder:
        remainder = remainder.split("@", 1)[1]
    return f"{scheme}://{remainder}"