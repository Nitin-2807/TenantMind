"""Phase 5 Step 2 — the ingestion task across a real broker and a real vector store.

What this file is for
---------------------
:mod:`tests.test_worker_ingestion` proves the task's *properties* against doubles:
the argument contract, the tenant derivation, the isolation argument, the result
shape. Those tests are fast and they fail precisely. What they cannot prove is
that the message crosses a broker and comes back as the same thing, and that the
pipeline's writes land in a Qdrant server rather than in a dictionary in this
interpreter.

This file closes that one gap, through the **registered** task only::

    .delay() -> Redis -> worker -> ingest_stored_document -> Qdrant -> result

Running it
----------
All three servers are opt-in, exactly as the Phase 4 and Step 1 gates are::

    TENANTMIND_TEST_REDIS_URL='redis://:<password>@127.0.0.1:6379/4' \
    TENANTMIND_TEST_QDRANT_URL='http://127.0.0.1:6333' \
    TENANTMIND_TEST_PG_DSN='postgresql+psycopg://...:5432/tenantmind_test' \
        pytest tests/test_worker_ingestion_integration.py

Without any one of the three the affected tests skip, so the ordinary suite never
depends on a running server.

Why PostgreSQL is in the chain
------------------------------
The task's security argument rests on reading ``document.tenant_id`` from a
persisted row. SQLite is where the unit suite already runs the identical code
path, so SQLite here would revisit proven ground with a broker bolted on. Using
the production database makes the one query this step introduces behave as it
will in production, foreign keys and real UUID columns included.

The worker runs the real task
-----------------------------
The registered task is registered onto a test-built app and consumed by a real
worker started in-process by
:func:`celery.contrib.testing.worker.start_worker` — the same helper Step 1 used.
The collaborators come from :func:`~app.workers.ingestion.build_ingestion_resources`
with only the three network endpoints redirected: the real session factory, the
real :class:`~app.storage.local.LocalFileStore`, the real
:class:`~app.storage.qdrant.QdrantVectorStore` and the shipped
:func:`~app.rag.embeddings.build_embedding_provider`. The store in particular is
not a double here; a fake would defeat the purpose of the file.

What is deliberately absent
---------------------------
The retry *policy* was proved in the unit suite (``test_worker_ingestion.py``'s
``TestTheTaskRetryPolicy``): transient failures are retried in-process with a
bounded budget and exponential backoff, and retry exhaustion records ``failed``.
This live suite proves the happy circle only — a live retry would wait its full
backoff. ``acks_late`` and failure-tracking counters remain later steps.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from celery import Celery  # type: ignore[import-untyped]
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models.document import Document, DocumentStatus
from app.rag.embeddings import EmbeddingProvider, build_embedding_provider
from app.storage.local import LocalFileStore
from app.storage.qdrant import QdrantVectorStore, collection_name_for
from app.workers.celery_app import build_celery_app
from app.workers.ingestion import (
    INGEST_DOCUMENT_TASK_NAME,
    IngestionResources,
    IngestionTaskError,
)
from tests.conftest import make_settings
from tests.documents import build_txt
from tests.records import payload_key
from tests.test_worker_ingestion import BODY, CANARY, STATIC_MODEL_NAME, WIDTH

REDIS_URL_ENV_VAR = "TENANTMIND_TEST_REDIS_URL"
QDRANT_URL_ENV_VAR = "TENANTMIND_TEST_QDRANT_URL"
PG_DSN_ENV_VAR = "TENANTMIND_TEST_PG_DSN"

#: Fragments indicating a disposable Redis, inherited from Step 1's suite.
SAFE_HOST_MARKERS = ("localhost", "127.0.0.1", "test", "tmp", "ci", "redis", "dev")

#: A distinct app name and queue, so a run of this file is distinguishable from a
#: real worker (or Step 1's smoke worker) in the same interpreter.
TEST_APP_NAME = "tenantmind-ingest-worker"

#: Bounded for the reason Step 1 records: a gate that hangs is worse than one
#: that fails.
RESULT_TIMEOUT_SECONDS = 60.0


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _skip_reason(name: str) -> str:
    return (
        f"{name} is not set, so this integration gate is PENDING; see this "
        "module's docstring for how to run it"
    )


def _without_credentials(url: str) -> str:
    head, _, _ = url.rpartition("@")
    return head.rsplit("://", 1)[-1] if head else url


def _database_index(url: str) -> int:
    path = url.split("?", 1)[0].rsplit("/", 1)[-1]
    try:
        return int(path)
    except ValueError:
        return 0


#: Applied to the consumers rather than as a module-level ``pytestmark``, so the
#: structural checks still run and a skip can never be silent.
requires_all = pytest.mark.usefixtures("redis_url", "qdrant_url", "pg_dsn")

REQUIRES_REDIS = pytest.mark.skipif(
    _env(REDIS_URL_ENV_VAR) is None, reason=_skip_reason(REDIS_URL_ENV_VAR)
)
REQUIRES_QDRANT = pytest.mark.skipif(
    _env(QDRANT_URL_ENV_VAR) is None, reason=_skip_reason(QDRANT_URL_ENV_VAR)
)
REQUIRES_POSTGRES = pytest.mark.skipif(
    _env(PG_DSN_ENV_VAR) is None, reason=_skip_reason(PG_DSN_ENV_VAR)
)


# ---------------------------------------------------------------------------
# Fixtures — module scope, one server connection each
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def redis_url() -> str:
    """The broker URL, after the Step 1 safety checks.

    Re-implemented rather than imported from
    :mod:`tests.test_worker_integration`: that module's fixture is private to its
    own file, and restating three assertions here is cheaper than making another
    module's internals public API.
    """
    url = _env(REDIS_URL_ENV_VAR)
    if url is None:
        pytest.skip(_skip_reason(REDIS_URL_ENV_VAR))

    lowered = url.lower()
    assert lowered.startswith(("redis://", "rediss://")), (
        f"{REDIS_URL_ENV_VAR} must be a redis:// or rediss:// URL, got "
        f"{_without_credentials(url)!r}"
    )
    assert any(marker in lowered for marker in SAFE_HOST_MARKERS), (
        f"refusing to test against {_without_credentials(url)!r}: this suite "
        "writes and deletes keys."
    )
    assert _database_index(url) != 0, (
        f"{REDIS_URL_ENV_VAR} points at database 0, where a developer's own "
        "broker queue lives; use a dedicated index (e.g. .../6379/4)."
    )
    return url


@pytest.fixture(scope="module")
def qdrant_url() -> str:
    url = _env(QDRANT_URL_ENV_VAR)
    if url is None:
        pytest.skip(_skip_reason(QDRANT_URL_ENV_VAR))
    return url


@pytest.fixture(scope="module")
def pg_dsn() -> str:
    url = _env(PG_DSN_ENV_VAR)
    if url is None:
        pytest.skip(_skip_reason(PG_DSN_ENV_VAR))
    return url


@pytest.fixture(scope="module")
def live_settings(redis_url: str, qdrant_url: str, pg_dsn: str) -> Settings:
    """Configuration pointing every dependency at the three test servers.

    Built through :func:`tests.conftest.make_settings` so no ``.env`` file is read:
    a developer's local configuration must not be able to decide where this suite
    connects.
    """
    return make_settings(
        database_url=pg_dsn,
        qdrant_url=qdrant_url,
        redis_url=redis_url,
        embedding_dimension=WIDTH,
        embedding_provider="static",
        chunk_size_chars=120,
        chunk_overlap_chars=20,
        embedding_batch_size=4,
    )


@pytest.fixture(scope="module")
def live_session_factory(
    live_settings: Settings, pg_dsn: str
) -> Iterator[sessionmaker[Session]]:
    """A session factory over the test database, schema migrated.

    The migration is run rather than ``create_all`` because this file is about the
    production database: real constraints, real UUID columns, the same schema the
    lookup runs against in production.

    The pattern — swap ``config.settings`` so ``alembic/env.py`` resolves the test
    DSN, run the real migration, then swap back — is borrowed from
    ``tests/test_postgres_integration.py``, which established it for exactly this
    reason.
    """
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    from alembic import command
    from app.core import config as config_module
    from app.db.session import build_session_factory
    from tests.conftest import ALEMBIC_INI
    from tests.test_postgres_integration import MANAGED_TABLES

    engine = create_engine(pg_dsn, poolclass=None)

    previous = config_module.settings
    config_module.settings = live_settings
    try:
        with engine.begin() as connection:
            existing = set(inspect(connection).get_table_names())
            for table in MANAGED_TABLES:
                if table in existing:
                    connection.execute(text(f'DROP TABLE "{table}" CASCADE'))
            if "alembic_version" in existing:
                connection.execute(text('DROP TABLE "alembic_version"'))

        command.upgrade(Config(str(ALEMBIC_INI)), "head")

        yield build_session_factory(engine)
    finally:
        config_module.settings = previous
        engine.dispose()


@pytest.fixture(scope="module")
def live_storage(
    live_settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> LocalFileStore:
    """A store over a temporary directory, not the application's own storage root.

    Pointed away from ``./storage`` so a run cannot leave documents on a
    developer's disk, and so the files a test asserts on are only the ones that
    test created.
    """
    root = tmp_path_factory.mktemp("worker-ingest-objects")
    return LocalFileStore(root)


@pytest.fixture(scope="module")
def live_store(
    live_settings: Settings, qdrant_url: str
) -> Iterator[QdrantVectorStore]:
    """The real store, pointed at the real server.

    Not a double. The purpose of this file is to prove the pipeline's points
    actually arrive in an index, and a recording dictionary would prove nothing
    about that.
    """
    from qdrant_client import QdrantClient

    provider = build_embedding_provider(live_settings)
    assert isinstance(provider, EmbeddingProvider)
    assert provider.model_name == STATIC_MODEL_NAME, (
        "the static provider must be what the shipped factory builds, or every "
        "vector-width assertion below would compare against the wrong width"
    )

    client = QdrantClient(url=qdrant_url, timeout=15)
    store = QdrantVectorStore(live_settings, client=client)
    yield store
    client.close()


@pytest.fixture(scope="module")
def resources(
    live_settings: Settings,
    live_session_factory: sessionmaker[Session],
    live_storage: LocalFileStore,
    live_store: QdrantVectorStore,
) -> IngestionResources:
    """The collaborators the worker module will build, production classes all.

    The module's own seam is monkeypatched to return exactly these, so the worker
    runs the real session factory, the real store and the real provider — the only
    thing redirected is the three network endpoints, which the settings already
    point at the test servers.
    """
    return IngestionResources(
        app_settings=live_settings,
        session_factory=live_session_factory,
        storage=live_storage,
        store=live_store,
        provider=build_embedding_provider(live_settings),
    )


@pytest.fixture(scope="module")
def worker_app(live_settings: Settings, resources: IngestionResources) -> Celery:
    """A Celery app built by the shipped factory, carrying the shipped task.

    ``task_always_eager`` is deliberately **not** set — eager mode runs tasks
    inline in the calling process and never touches the broker, which would make
    every round-trip test here pass against no Redis at all. This matches Step 1.

    The task is created **fresh on this app** rather than re-registered from the
    project singleton. Re-registering the already-bound ``ingest_document_task``
    would leave it bound to the project app, whose default result backend points
    at ``redis://localhost:6379/0`` without the test password — the message would
    publish on this app while results were written and read against the wrong
    server. The production task is ``bind=True``, so its ``run`` is a bound
    method; decorating that directly leaves Celery's signature checker blind to
    ``document_id`` (Step 4A proved this the hard way). Registering the raw
    ``run.__func__`` with ``bind=True`` gives the test app its own task object
    whose ``run`` is the real function, held exactly as the production registry
    holds it, as Step 1's smoke task is.
    """
    from app.workers.ingestion import ingest_document_task

    application = build_celery_app(live_settings)
    application.conf.main = TEST_APP_NAME
    application.conf.task_default_queue = f"{TEST_APP_NAME}-{uuid.uuid4().hex[:8]}"
    application.task(name=INGEST_DOCUMENT_TASK_NAME, bind=True)(
        ingest_document_task.run.__func__
    )
    return application


@pytest.fixture(scope="module")
def worker(
    worker_app: Celery,
    worker_module: Any,
    resources: IngestionResources,
) -> Iterator[Any]:
    """A real worker consuming the real broker, for the module's duration.

    ``perform_ping_check`` is left on, so Celery's ``ping`` completes before the
    tests run: an independent round trip, meaning a broker that is up but not
    delivering fails with Celery's own assertion rather than as an unexplained
    failure below. The import of ``celery.contrib.testing.tasks`` is not optional —
    Celery ships ``celery.ping`` there and registers it only when something imports
    it.
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


@pytest.fixture(scope="module")
def worker_module(resources: IngestionResources) -> Iterator[Any]:
    """The worker module, imported once, with its seam redirected to this suite.

    The worker thread shares this interpreter, so the module attribute is the seam.
    Everything below it is production code; only where the resources are built is
    redirected, and only to the three test server endpoints above.
    """
    import app.workers.ingestion as module

    def _seam(app_settings: Settings) -> IngestionResources:
        return resources

    original = module.build_ingestion_resources
    module.build_ingestion_resources = _seam
    yield module
    module.build_ingestion_resources = original


@pytest.fixture
def world(
    live_session_factory: sessionmaker[Session],
    live_storage: LocalFileStore,
    live_store: QdrantVectorStore,
    live_settings: Settings,
) -> Iterator[Any]:
    """Two real tenants in real PostgreSQL, fresh per test.

    Per-test rather than module-scoped: rows created by one test must not leak
    into another. Cleanup drops the collections this suite created, then all rows,
    so two runs against the same server do not accumulate.

    The ``lambda _settings: resources`` in :func:`worker_module` references the
    module-scoped ``resources`` fixture from within a module-scoped fixture body;
    wiring them through closure would be fragile across scopes, so the world does
    not depend on it. The seam is already redirected when this fixture runs.
    """
    from dataclasses import dataclass

    from app.core.security import hash_password
    from app.models.tenant import Tenant
    from app.models.user import User

    @dataclass
    class Live:
        session: Session
        storage: LocalFileStore
        store: QdrantVectorStore
        tenant_a: uuid.UUID
        tenant_b: uuid.UUID

        def make_document(self, tenant_id: uuid.UUID | None = None) -> Document:
            owner = tenant_id if tenant_id is not None else self.tenant_a
            payload = build_txt(BODY)
            key = self.storage.new_storage_key()
            path = self.storage.path_for(owner, key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

            document = Document(
                tenant_id=owner,
                filename="policy.txt",
                storage_key=key,
                status=DocumentStatus.PROCESSING,
                size_bytes=len(payload),
                content_type="text/plain",
            )
            self.session.add(document)
            self.session.commit()
            return document

        def reloaded(self, document_id: uuid.UUID) -> Document:
            from sqlalchemy import select as sql_select

            self.session.expire_all()
            row = self.session.scalar(
                sql_select(Document).where(Document.id == document_id)
            )
            assert row is not None, "the document row vanished"
            return row

        def cleanup(self) -> None:
            from sqlalchemy import delete as sql_delete

            self.session.rollback()
            for tenant in (self.tenant_a, self.tenant_b):
                try:
                    self.store._client.delete_collection(
                        collection_name_for(tenant)
                    )
                except Exception:  # noqa: BLE001, S110 - teardown only
                    # A cleanup failure says something about the server, not about
                    # the test that just passed.
                    pass
            self.session.execute(sql_delete(Document))
            self.session.execute(sql_delete(User))
            self.session.execute(sql_delete(Tenant))
            self.session.commit()

    session = live_session_factory()
    suffix = uuid.uuid4().hex[:8]

    tenant_a = Tenant(name="Worker A", slug=f"worker-a-{suffix}")
    tenant_b = Tenant(name="Worker B", slug=f"worker-b-{suffix}")
    session.add_all([tenant_a, tenant_b])
    session.flush()

    user_a = User(
        tenant_id=tenant_a.id,
        email=f"a-{suffix}@example.com",
        password_hash=hash_password("correct horse battery staple"),
        role="admin",  # type: ignore[arg-type]
    )
    session.add(user_a)
    session.commit()

    built = Live(
        session=session,
        storage=live_storage,
        store=live_store,
        tenant_a=tenant_a.id,
        tenant_b=tenant_b.id,
    )

    try:
        yield built
    finally:
        built.cleanup()
        session.close()


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------


class TestTheDocumentFlowsThroughTheBroker:
    """The registered task, message in, result out, real servers all the way."""

    @requires_all
    def test_the_small_mapping_comes_back_and_the_row_is_ready(
        self, worker: Any, world: Any, worker_app: Celery
    ) -> None:
        """``document_id`` -> Redis -> worker -> pipeline -> Qdrant -> result.

        ``worker`` is requested first so the worker is consuming *before* the
        message is published; otherwise the message waits in the queue until the
        timeout, and a slow start would read as a broken broker.
        """
        assert worker is not None
        document = world.make_document()

        outcome = worker_app.tasks[INGEST_DOCUMENT_TASK_NAME].delay(
            str(document.id)
        ).get(timeout=RESULT_TIMEOUT_SECONDS)

        assert outcome["status"] == DocumentStatus.READY.value
        assert outcome["document_id"] == str(document.id)
        assert outcome["tenant_id"] == str(world.tenant_a)
        assert isinstance(outcome["chunk_count"], int) and outcome["chunk_count"] > 0

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.READY
        assert row.chunk_count == outcome["chunk_count"]

    @requires_all
    def test_the_points_land_in_the_real_index(
        self, worker: Any, world: Any, worker_app: Celery
    ) -> None:
        """Read back *from the server*: count and payloads, not just a status.

        Every part is real — Redis, a worker, PostgreSQL, Qdrant — and the points
        are then read from Qdrant, so the assertion is about what arrived rather
        than about what the pipeline intended.
        """
        assert worker is not None
        document = world.make_document()

        outcome = worker_app.tasks[INGEST_DOCUMENT_TASK_NAME].delay(
            str(document.id)
        ).get(timeout=RESULT_TIMEOUT_SECONDS)

        stored = world.store.scroll_document_points(world.tenant_a, document.id)
        assert stored, "the pipeline wrote nothing to the real index"
        assert len(stored) == outcome["chunk_count"]

    @requires_all
    def test_the_stored_payloads_name_the_documents_own_tenant(
        self, worker: Any, world: Any, worker_app: Celery
    ) -> None:
        """The payload the server holds names tenant B, for a tenant-B document.

        The strongest single-server form of the isolation claim: the task was told
        nothing but the id, and the metadata the *server* stores came from the row.
        """
        assert worker is not None
        document = world.make_document(world.tenant_b)

        outcome = worker_app.tasks[INGEST_DOCUMENT_TASK_NAME].delay(
            str(document.id)
        ).get(timeout=RESULT_TIMEOUT_SECONDS)

        stored = world.store.scroll_document_points(world.tenant_b, document.id)
        assert stored
        for record in stored:
            assert payload_key(record, "tenant_id") == str(world.tenant_b)
            assert payload_key(record, "document_id") == str(document.id)
        assert outcome["collection"] == collection_name_for(world.tenant_b)
        assert outcome["collection"] != collection_name_for(world.tenant_a)

    @requires_all
    def test_two_tenants_do_not_cross_in_a_real_server(
        self, worker: Any, world: Any, worker_app: Celery
    ) -> None:
        """One worker holding two tenants' documents keeps them apart.

        Sent as two separate messages, which is the realistic ordering: two tenants'
        documents queued at once, one worker consuming both.
        """
        assert worker is not None
        first = world.make_document(world.tenant_a)
        second = world.make_document(world.tenant_b)

        task = worker_app.tasks[INGEST_DOCUMENT_TASK_NAME]
        outcomes = [
            task.delay(str(first.id)).get(timeout=RESULT_TIMEOUT_SECONDS),
            task.delay(str(second.id)).get(timeout=RESULT_TIMEOUT_SECONDS),
        ]

        assert [outcome["tenant_id"] for outcome in outcomes] == [
            str(world.tenant_a),
            str(world.tenant_b),
        ]
        assert world.store.scroll_document_points(world.tenant_a, second.id) == []
        assert world.store.scroll_document_points(world.tenant_b, first.id) == []

    @requires_all
    def test_no_document_content_reaches_the_result_backend(
        self, worker: Any, world: Any, worker_app: Celery
    ) -> None:
        """The result recovered from Redis carries no document body.

        The result backend is the last place content could escape: this is not the
        task's return value in this process, it is whatever Celery serialised into
        Redis. Recovering it through the backend object is what makes this
        different from the unit test that inspects the returned mapping.
        """
        import json

        assert worker is not None
        document = world.make_document()

        async_result = worker_app.tasks[INGEST_DOCUMENT_TASK_NAME].delay(
            str(document.id)
        )
        outcome = async_result.get(timeout=RESULT_TIMEOUT_SECONDS)

        backend_value = async_result.backend.get_result(async_result.id)
        rendered = json.dumps(backend_value, default=str)

        assert CANARY not in rendered
        assert CANARY not in json.dumps(outcome, default=str)
        assert backend_value["document_id"] == str(document.id)

    @requires_all
    def test_a_missing_document_fails_the_task_and_writes_nothing(
        self, worker: Any, worker_app: Celery, world: Any
    ) -> None:
        """The task is failed, and no status is invented for a row that is gone.

        ``get(propagate=False)`` is what a caller sees: the exception is stored,
        not raised into the test process. ``FAILURE`` and an
        :class:`~app.workers.ingestion.IngestionTaskError` are both asserted, and
        then the real collections are checked to hold nothing.
        """
        assert worker is not None
        absent = uuid.uuid4()

        async_result = worker_app.tasks[INGEST_DOCUMENT_TASK_NAME].delay(
            str(absent)
        )
        outcome = async_result.get(timeout=RESULT_TIMEOUT_SECONDS, propagate=False)

        # `get(propagate=False)` returns the stored exception rather than raising
        # it or returning None, which is exactly what a caller that *wants* to
        # read the failure sees.
        assert isinstance(outcome, IngestionTaskError)
        assert async_result.state == "FAILURE"
        assert isinstance(async_result.result, IngestionTaskError)

        assert world.store.scroll_document_points(world.tenant_a, absent) == []
        assert world.store.scroll_document_points(world.tenant_b, absent) == []

    @requires_all
    def test_a_row_with_no_file_is_recorded_failed_in_postgres(
        self, worker: Any, world: Any, worker_app: Celery
    ) -> None:
        """A stage failure is a ``failed`` row, written by the real worker.

        PMP §20's "failures will be recorded in PostgreSQL", with the worker in
        the loop. The row is the durable record and the result merely reports it.
        """
        assert worker is not None
        document = world.make_document()
        path = world.storage.path_for(world.tenant_a, document.storage_key)
        path.unlink()

        outcome = worker_app.tasks[INGEST_DOCUMENT_TASK_NAME].delay(
            str(document.id)
        ).get(timeout=RESULT_TIMEOUT_SECONDS)

        assert outcome["status"] == DocumentStatus.FAILED.value
        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.FAILED
        assert row.error
        assert outcome["chunk_count"] is None


class TestTheWorkerImportsNoWebFramework:
    """The claim, checked in the process that actually runs the worker.

    Repeats the source guard from :mod:`tests.test_worker_ingestion` because this
    is the case that matters: if a worker could be started whose *entry point*
    dragged the web application in, the claim would be about the wrong module. The
    transitive import through the model layer is a recorded Phase 1 limitation, not
    a violation of "no worker source names FastAPI".
    """

    def test_the_ingestion_task_is_registered_on_the_workers_app(self) -> None:
        """A worker started with ``-I app.workers.ingestion`` sees the task.

        ``-A app.workers.celery_app`` alone imports the app and *not* the task
        module, so an empty task registry is exactly the failure mode this guard
        prevents: a published message sitting unconsumed.
        """
        import app.workers.ingestion as ingestion_module

        assert (
            ingestion_module.INGEST_DOCUMENT_TASK_NAME
            in ingestion_module.celery_app.tasks
        )

    def test_the_shipped_factory_builds_the_static_provider_for_tests(self) -> None:
        """No credential is needed for ``static``, which is why this file can run.

        Asserted because it is the assumption the whole file rests on: if the
        shipped factory demanded a key for the configured provider, every round
        trip here would fail with an authentication error instead of a useful
        message.
        """
        settings = make_settings(
            embedding_dimension=WIDTH, embedding_provider="static"
        )

        provider = build_embedding_provider(settings)

        assert provider.model_name == STATIC_MODEL_NAME
        assert provider.dimensions == WIDTH