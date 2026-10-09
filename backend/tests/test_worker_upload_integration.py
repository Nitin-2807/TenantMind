"""Phase 5 Step 3 — upload queues, and a real worker ingests, over real servers.

What this file is for
----------------------
:mod:`tests.test_documents_api` proves the request side against a recorder and
:mod:`tests.test_worker_ingestion_integration` proves the task against a broker.
What neither proves is the *connection between them*: that ``POST /documents``
commits a row, publishes the task, and returns ``202`` while a real worker —
consuming the real broker — ingests that row and writes points into a real
Qdrant server. This file closes exactly that gap::

    POST /documents -> PostgreSQL row (committed)
                    -> Redis queue
                    -> Celery worker (in-process, real)
                    -> ingest_stored_document
                    -> Qdrant
                    -> row status `ready`

Running it
----------
The same three opt-in servers as the Step 2 gate::

    TENANTMIND_TEST_REDIS_URL='redis://:<password>@127.0.0.1:6379/4' \
    TENANTMIND_TEST_QDRANT_URL='http://127.0.0.1:6333' \
    TENANTMIND_TEST_PG_DSN='postgresql+psycopg://...:5432/tenantmind_test' \
        pytest tests/test_worker_upload_integration.py

Without any one of the three the affected tests skip.

What is real here
-----------------
* The application is the production one (``create_app``), with only the
  database dependency overridden to the test PostgreSQL and settings pointed at
  the three test servers. The handler, the service, the commit and the enqueue
  are all production code.
* The publish goes through the real :func:`~app.workers.ingestion.enqueue_ingestion`
  onto a real broker. The only substitution is *which application instance* it
  publishes with: the process-wide ``celery_app`` is configured from default
  settings, so in this interpreter the worker module's ``celery_app`` attribute
  is repointed at the test app built from ``live_settings`` — the same seam
  pattern the Step 2 suite uses for ``build_ingestion_resources``.
* The worker is a real Celery worker (``celery.contrib.testing.worker.start_worker``)
  consuming the real broker, running the real task body against the real
  PostgreSQL, the real storage and the real Qdrant store.

What is deliberately not here
-----------------------------
The retry *policy* is Step 4A's and is proved in the unit suite
(``test_worker_ingestion.py``), not here: this live file would otherwise sit
through a transient failure's full backoff. Retrying re-publishes the same
``(document_id,)`` message. ``acks_late`` and failure-tracking counters remain
later steps. The queue-publish failure path is asserted at the request layer in
``tests/test_documents_api.py``; this file proves the happy circle.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_db, session_scope
from app.main import create_app
from app.models.document import Document, DocumentStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.rag.embeddings import build_embedding_provider
from app.storage.local import LocalFileStore
from app.storage.qdrant import QdrantVectorStore, collection_name_for
from app.workers.celery_app import build_celery_app
from app.workers.ingestion import INGEST_DOCUMENT_TASK_NAME, IngestionResources
from tests.conftest import TEST_JWT_SECRET, make_settings
from tests.documents import build_txt
from tests.records import payload_key
from tests.test_documents_api import upload
from tests.test_worker_ingestion import BODY, STATIC_MODEL_NAME, WIDTH

REDIS_URL_ENV_VAR = "TENANTMIND_TEST_REDIS_URL"
QDRANT_URL_ENV_VAR = "TENANTMIND_TEST_QDRANT_URL"
PG_DSN_ENV_VAR = "TENANTMIND_TEST_PG_DSN"

#: Fragments indicating a disposable Redis, inherited from the Step 1/2 suites.
SAFE_HOST_MARKERS = ("localhost", "127.0.0.1", "test", "tmp", "ci", "redis", "dev")

#: A distinct app name and queue, so a run of this file is distinguishable from a
#: real worker (or Step 1/2 workers) in the same interpreter.
TEST_APP_NAME = "tenantmind-upload-worker"

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

    Restated here rather than imported, following the convention the Step 2
    suite records: an integration file's fixtures are private to it, and
    restating the three assertions is cheaper than importing internals.
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
def live_root(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One temporary directory that is *both* the API's storage and the worker's.

    The upload endpoint writes through ``LocalFileStore(settings.storage_root)``
    and the worker reads through ``resources.storage``; if the two roots
    differed, the worker would look in a directory the upload never touched.
    Sharing one root is what makes the circle real.
    """
    return tmp_path_factory.mktemp("upload-live-objects")


@pytest.fixture(scope="module")
def live_settings(
    redis_url: str, qdrant_url: str, pg_dsn: str, live_root: Any
) -> Settings:
    """Configuration pointing every dependency at the three test servers.

    Built through :func:`tests.conftest.make_settings` so no ``.env`` file is
    read: a developer's local configuration must not be able to decide where
    this suite connects.
    """
    return make_settings(
        database_url=pg_dsn,
        qdrant_url=qdrant_url,
        redis_url=redis_url,
        jwt_secret_key=TEST_JWT_SECRET,
        storage_root=str(live_root),
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

    The same alembic-swap pattern ``tests/test_postgres_integration.py``
    established and the Step 2 suite reused.
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
def live_storage(live_root: Any) -> LocalFileStore:
    """The store over the shared temporary root — the one directory both sides use."""
    return LocalFileStore(live_root)


@pytest.fixture(scope="module")
def live_store(
    live_settings: Settings, qdrant_url: str
) -> Iterator[QdrantVectorStore]:
    """The real store, pointed at the real server. Not a double."""
    from qdrant_client import QdrantClient

    provider = build_embedding_provider(live_settings)
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
    """The collaborators the worker module will build, production classes all."""
    return IngestionResources(
        app_settings=live_settings,
        session_factory=live_session_factory,
        storage=live_storage,
        store=live_store,
        provider=build_embedding_provider(live_settings),
    )


@pytest.fixture(scope="module")
def worker_app(live_settings: Settings, resources: IngestionResources) -> Any:
    """A Celery app built by the shipped factory, carrying the shipped task.

    ``task_always_eager`` is deliberately **not** set, exactly as in Step 2:
    the message must cross the broker for this file to mean anything.

    The production task is ``bind=True``, so ``run`` is a bound method; the raw
    ``run.__func__`` is registered with ``bind=True`` instead, which keeps the
    signature checker (and the broker contract) the same as in production.
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
    worker_app: Any,
    worker_module: Any,
    resources: IngestionResources,
) -> Iterator[Any]:
    """A real worker consuming the real broker, for the module's duration."""
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

    All this redirects is where collaborators come from; the task body itself is
    the shipped one, running against the real servers named by ``resources``.
    """
    import app.workers.ingestion as module

    def _seam(app_settings: Settings) -> IngestionResources:
        return resources

    original = module.build_ingestion_resources
    module.build_ingestion_resources = _seam
    yield module
    module.build_ingestion_resources = original


@pytest.fixture
def live_api(
    live_settings: Settings,
    live_session_factory: sessionmaker[Session],
    worker_app: Any,
) -> Iterator[TestClient]:
    """The production application, wired to the real database and the test broker.

    Two substitutions, both seams the application provides:

    * ``get_db`` runs requests against the migrated test PostgreSQL via
      :func:`~app.db.session.session_scope`, keeping the real transaction
      contract — commit when the handler returns, roll back when it raises.
    * The worker module's ``celery_app`` attribute is repointed at
      ``worker_app`` while a test runs, because the process-wide ``celery_app``
      is configured from default settings and would publish to the wrong Redis.
      :func:`~app.workers.ingestion.enqueue_ingestion` still runs for real —
      same function, real broker, real message.

    The upload family of dependencies (storage root, settings) resolve from
    ``live_settings`` through the ordinary ``get_settings`` override.
    """
    import app.workers.ingestion as ingestion_module

    original_app = ingestion_module.celery_app
    ingestion_module.celery_app = worker_app

    application = create_app(live_settings)

    def _override_get_db() -> Iterator[Session]:
        with session_scope(live_session_factory) as session:
            yield session

    application.dependency_overrides[get_settings] = lambda: live_settings
    application.dependency_overrides[get_db] = _override_get_db

    try:
        yield TestClient(application)
    finally:
        ingestion_module.celery_app = original_app


@pytest.fixture
def world(
    live_session_factory: sessionmaker[Session],
    live_storage: LocalFileStore,
    live_store: QdrantVectorStore,
    live_settings: Settings,
) -> Iterator[Any]:
    """Two real tenants in real PostgreSQL, fresh per test.

    Each test's upload is associated with a committed tenant and an
    administrator, so the request authenticates exactly as production would.
    Cleanup drops the collections this suite created, then all rows.
    """
    from sqlalchemy import delete as sql_delete
    from sqlalchemy import select as sql_select

    @dataclass
    class Live:
        session: Session
        store: QdrantVectorStore
        tenant_a: uuid.UUID
        tenant_b: uuid.UUID
        user_a: uuid.UUID
        make_token: Callable[[uuid.UUID, uuid.UUID], str]

        def reloaded(self, document_id: uuid.UUID) -> Document:
            self.session.expire_all()
            row = self.session.scalar(
                sql_select(Document).where(Document.id == document_id)
            )
            assert row is not None, "the document row vanished"
            return row

        def cleanup(self) -> None:
            self.session.rollback()
            for tenant in (self.tenant_a, self.tenant_b):
                try:
                    self.store._client.delete_collection(
                        collection_name_for(tenant)
                    )
                except Exception:  # noqa: BLE001, S110 - teardown only
                    pass
            self.session.execute(sql_delete(Document))
            self.session.execute(sql_delete(User))
            self.session.execute(sql_delete(Tenant))
            self.session.commit()

    session = live_session_factory()
    suffix = uuid.uuid4().hex[:8]

    tenant_a = Tenant(name="Upload A", slug=f"upload-a-{suffix}")
    tenant_b = Tenant(name="Upload B", slug=f"upload-b-{suffix}")
    session.add_all([tenant_a, tenant_b])
    session.flush()

    user_a = User(
        tenant_id=tenant_a.id,
        email=f"upload-a-{suffix}@example.com",
        password_hash=hash_password("correct horse battery staple"),
        role="admin",  # type: ignore[arg-type]
    )
    session.add(user_a)
    session.commit()

    def _token(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
        return create_access_token(
            subject=user_id,
            tenant_id=tenant_id,
            role="admin",
            app_settings=live_settings,
        )

    built = Live(
        session=session,
        store=live_store,
        tenant_a=tenant_a.id,
        tenant_b=tenant_b.id,
        user_a=user_a.id,
        make_token=_token,
    )

    try:
        yield built
    finally:
        built.cleanup()
        session.close()


# ---------------------------------------------------------------------------
# The circle: upload -> queue -> worker -> ingestion -> Qdrant
# ---------------------------------------------------------------------------


def _wait_until_ready(
    world: Any, document_id: uuid.UUID, timeout: float = RESULT_TIMEOUT_SECONDS
) -> Document:
    """Poll the row until the worker's pipeline records ``ready``.

    The test never touches the task result: the durable record is the
    PostgreSQL row, which is also what the brief's chain ends in.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = world.reloaded(document_id)
        if row.status is DocumentStatus.READY:
            return row
        time.sleep(0.2)
    raise AssertionError(
        f"document {document_id} never reached {DocumentStatus.READY!r} "
        f"(last status {world.reloaded(document_id).status!r})"
    )


class TestUploadFlowsThroughTheBroker:
    """``POST /documents`` -> broker -> real worker -> real Qdrant."""

    @requires_all
    def test_upload_is_ingested_by_the_worker_and_lands_in_qdrant(
        self, worker: Any, live_api: TestClient, world: Any, live_store: QdrantVectorStore
    ) -> None:
        """The full Step 3 circle, with every link real.

        ``worker`` is requested first so the worker is consuming *before* the
        upload; otherwise the message waits in the queue until the timeout.
        """
        assert worker is not None
        token = world.make_token(world.tenant_a, world.user_a)

        response = upload(
            live_api,
            token,
            filename="policy.txt",
            content=build_txt(BODY),
            content_type="text/plain",
        )

        assert response.status_code == 202, response.text
        document_id = uuid.UUID(response.json()["id"])

        row = _wait_until_ready(world, document_id)
        assert row.chunk_count and row.chunk_count > 0

        stored = live_store.scroll_document_points(world.tenant_a, document_id)
        assert stored, "the worker wrote nothing to the real index"
        assert len(stored) == row.chunk_count

    @requires_all
    def test_a_tenants_upload_stays_in_that_tenants_collection(
        self, worker: Any, live_api: TestClient, world: Any, live_store: QdrantVectorStore
    ) -> None:
        """The tenant chain survives the whole circle.

        Upload into tenant B: the response names the document, the worker
        derives tenant B from the row, and the points the server actually holds
        carry tenant B's id in their payloads. No ``tenant_id`` ever crossed
        the broker — only the API's authenticated identity and the row supplied
        it, in that order.
        """
        assert worker is not None
        token = world.make_token(world.tenant_b, world.user_a)

        response = upload(
            live_api,
            token,
            filename="policy.txt",
            content=build_txt(BODY),
            content_type="text/plain",
        )

        assert response.status_code == 202, response.text
        document_id = uuid.UUID(response.json()["id"])

        row = _wait_until_ready(world, document_id)
        assert row.tenant_id == world.tenant_b

        stored = live_store.scroll_document_points(world.tenant_b, document_id)
        assert stored
        for record in stored:
            assert payload_key(record, "tenant_id") == str(world.tenant_b)
            assert payload_key(record, "document_id") == str(document_id)
        assert live_store.scroll_document_points(world.tenant_a, document_id) == []