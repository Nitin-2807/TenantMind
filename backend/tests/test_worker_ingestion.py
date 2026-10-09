"""The Celery ingestion task, from the outside (PMP §20, §22 Phase 5 Step 2).

What is proved here
-------------------
* :func:`~app.workers.ingestion.ingest_document_task` is registered on the project
  Celery app under a stable name, and accepts **one** argument.
* That argument is a ``document_id``. The signature admits no tenant, no role, no
  collection name, no identity object and no ``**kwargs`` — so
  ``(document_id=A, tenant_id=B)`` is not rejected but *unrepresentable*.
* The tenant is derived from the persisted document row, so a document can only
  ever be processed within the tenant that owns it.
* A document that does not exist fails loudly rather than reporting a status that
  is not a :class:`~app.models.document.DocumentStatus`.
* The result is JSON-serialisable and holds no document content, no embedding, no
  ORM object and no credential.
* :func:`~app.workers.ingestion.enqueue_ingestion` — the Step 3 door the upload
  endpoint calls — publishes the fixed task name with exactly the ``document_id``,
  and a publish failure is an :class:`~app.workers.ingestion.EnqueueError` that
  names neither the broker nor its credentials.
* The worker layer names no web framework and no route module.
* Retry policy (Step 4A): transient failures — an embedding provider, the vector
  store, the filesystem, a briefly-unreachable database — are retried explicitly
  with ``self.retry``, a bounded budget and exponential backoff; retry
  exhaustion records ``failed`` with the same stage-tagged message the pipeline
  would have written; permanent and unexpected failures are not retried and no
  retry reshapes the ``(document_id,)`` message.

How the tests reach the task
----------------------------
The body of the task lives in
:func:`~app.workers.ingestion.ingest_stored_document`, which takes its
collaborators as parameters. Most of this file calls that function directly, with
no broker, no worker and no network — because that is where the security
properties are, and because a test that needs a broker proves less about the thing
than a test that proves the thing.

:class:`TestTheRegisteredTask` covers the thin layer above it by replacing
:func:`~app.workers.ingestion.build_ingestion_resources` — the single seam where
configuration is read — and calling ``.run()`` in process. That is the wiring a
worker actually executes.

Three structural guards read source rather than call code, for the same reason the
identity tests do: the properties are about *which modules are named*, and only the
parsed source knows that.

Scoping note
------------
The pipeline itself is gated by ``tests/test_ingestion.py``. Nothing here re-tests
parsing, chunking, embedding or vector writes. The store is the same
:class:`FakeQdrant`-backed double that file uses, so a failure in this file points
at the task wrapper rather than at a stage that was already tested.
"""

from __future__ import annotations

import ast
import inspect
import json
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from celery.app.task import Context  # type: ignore[import-untyped]
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import app.workers.ingestion as worker_module
from app.core.config import Settings
from app.core.identity import (
    SYSTEM_ACTOR_ID,
    AuthenticatedIdentity,
    IdentitySource,
)
from app.models.document import Document, DocumentStatus
from app.models.user import UserRole
from app.rag.embeddings import EmbeddingError, EmbeddingProvider
from app.services.ingestion import (
    IngestionResult,
    IngestionStage,
    TransientIngestionError,
)
from app.storage.local import LocalFileStore
from app.storage.qdrant import QdrantVectorStore, collection_name_for
from app.workers.celery_app import celery_app
from app.workers.ingestion import (
    INGEST_DOCUMENT_TASK_NAME,
    EnqueueError,
    IngestionResources,
    IngestionTaskError,
    enqueue_ingestion,
    ingest_document_task,
    ingest_stored_document,
    task_result,
)
from tests.conftest import make_settings
from tests.documents import build_txt
from tests.records import store_over
from tests.test_qdrant_store import FakeQdrant

#: Width used throughout. Matches ``tests/test_ingestion.py`` so the same number
#: satisfies both the fake client and a real server.
WIDTH = 8

#: The provider's ``model_name``. Named rather than inlined into the provider so a
#: test in the integration suite can assert that the *shipped* factory built the
#: provider this file expects.
STATIC_MODEL_NAME = "static-test-vectors"

#: A tenant id for the tests that never touch the database.
#:
#: Only :func:`~app.workers.ingestion.task_result` and the identity constructors
#: accept one, and neither reads a row, so a plain UUID is the honest input here.
#: Everywhere a row is involved the test uses the world's real tenant ids instead.
ARBITRARY_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000000a1")

#: Content whose presence in a task result would be a leak. Distinctive enough
#: that a substring search cannot find it by accident, and planted in every
#: document body this file writes.
CANARY = "ZEBRAFISH-CANARY-CONTENT-MUST-NOT-REACH-THE-BROKER"

#: A body long enough to produce several chunks at the chunk size below, with real
#: paragraph boundaries to split on.
BODY = (
    "Section 1. Definitions.\n\n"
    "Staff means any person employed by the Company under a written contract.\n\n"
    "Section 2. Annual Leave.\n\n"
    "Each member of Staff accrues two days of paid leave per completed month of "
    f"service. {CANARY}\n\n"
    "Section 3. Notice.\n\n"
    "Either party may terminate employment by giving ninety days written notice.\n\n"
    "Section 4. Provisions.\n\n"
    "Nothing in this agreement reduces any statutory entitlement.\n"
)


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


class StaticProvider(EmbeddingProvider):
    """A free, deterministic provider.

    The task wrapper is what is under test, so the provider must cost nothing and
    vary nothing. The vector is a function of the text rather than a constant for
    the reason ``tests/test_ingestion.py`` gives: a constant vector would hide a
    chunk paired with the wrong vector behind every comparison passing.
    """

    def __init__(self, *, dimensions: int = WIDTH) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return STATIC_MODEL_NAME

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        assert texts, "embed_texts requires at least one text"
        self.calls.append(list(texts))
        return [[float(len(text) % 7)] * self._dimensions for text in texts]


class _FlakyProvider(StaticProvider):
    """Fails the first ``failures`` embedding calls, then succeeds.

    The retry story tested end to end: the first attempt raises an
    :class:`EmbeddingError` (the pipeline lifts it into a
    :class:`TransientIngestionError`), and a redelivered attempt — the provider
    having recovered — ingests normally.
    """

    def __init__(self, *, failures: int = 1) -> None:
        super().__init__()
        self._failures_left = failures

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if self._failures_left > 0:
            self._failures_left -= 1
            raise EmbeddingError("the embedding provider is briefly down")
        return super().embed_texts(texts)


class _AlwaysDownProvider(StaticProvider):
    """Fails every embedding call, for the retry-exhaustion path."""

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        raise EmbeddingError("the embedding provider is down")


class _RetriedSignal(Exception):
    """What the fake ``self.retry`` raises to stop an in-process attempt.

    Celery's real ``retry`` aborts the attempt by raising
    :class:`celery.exceptions.Retry`; the double stands in for that, so a test
    can capture the retry request and then make the next attempt itself.
    """


@dataclass
class World:
    """Everything the task needs, over doubles.

    A dataclass for the reason ``tests/test_ingestion.py`` uses one: nearly every
    test needs all of it, and eight parameters per signature would bury the one
    argument the test is about.

    The two tenants are **real rows** created through the shared ``make_account``
    factory, so the ``documents`` foreign key is satisfied by a tenant that exists
    rather than by a hand-written UUID. Their ids are therefore whatever the
    factory minted, which is why every test reaches them as ``world.tenant_a``
    rather than as a module constant: a constant would have to be either a
    fabricated id the foreign key rejects, or a value the fixture silently ignored.
    """

    session: Session
    storage: LocalFileStore
    store: QdrantVectorStore
    provider: StaticProvider
    settings: Settings
    resources: IngestionResources
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    user_a: uuid.UUID

    @property
    def client(self) -> FakeQdrant:
        """The fake client, asserted rather than cast.

        Asserted because the world constructs the store with a
        :class:`FakeQdrant` and nothing else could be installed — so a failure here
        would mean the fixture changed, which is worth learning.
        """
        client = self.store._client
        assert isinstance(client, FakeQdrant), (
            f"the world was built with a {type(client).__name__}, not a FakeQdrant"
        )
        return client

    def make_document(
        self, *, tenant_id: uuid.UUID | None = None, stored: bool = True
    ) -> Document:
        """A committed ``documents`` row, with its bytes on disk by default.

        Written straight through the model rather than through the upload endpoint:
        Phase 2.4 proved that endpoint, and this file needs a row in the state the
        pipeline expects plus a file at the key the row names. ``stored=False``
        produces the row-without-a-file case, which is how the failure path is
        reached without monkeypatching a stage.

        ``tenant_id`` defaults to tenant A so that the common case — "a document" —
        reads without noise; the isolation tests pass it explicitly.
        """
        owner = tenant_id if tenant_id is not None else self.tenant_a
        payload = build_txt(BODY)
        key = self.storage.new_storage_key()
        if stored:
            path = self.storage.path_for(owner, key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        document = Document(
            tenant_id=owner,
            uploaded_by=self.user_a if owner == self.tenant_a else None,
            filename="policy.txt",
            storage_key=key,
            status=DocumentStatus.PROCESSING,
            size_bytes=len(payload),
            content_type="text/plain",
        )
        self.session.add(document)
        self.session.commit()
        return document

    def run(self, document_id: uuid.UUID) -> dict[str, object]:
        """Run the task body against this world's collaborators."""
        return ingest_stored_document(document_id, self.resources)

    def reloaded(self, document_id: uuid.UUID) -> Document:
        """The row as the database now holds it.

        A fresh query rather than the in-memory object: the pipeline writes through
        a different session, and an identity-mapped object would report whatever it
        already held — making every recording assertion pass regardless of what was
        written.
        """
        self.session.expire_all()
        row = self.session.scalar(select(Document).where(Document.id == document_id))
        assert row is not None, "the document row vanished"
        return row

    def collections_with_points(self) -> dict[str, set[str]]:
        """Which collections exist, and the point ids each holds.

        The isolation assertion reads this rather than a list of recorded calls:
        what a tenant's collection *contains* is the property under test, and a
        call log would only prove the store was called correctly.
        """
        return {
            name: set(collection.points)
            for name, collection in self.client.collections.items()
        }


@pytest.fixture
def worker_settings() -> Settings:
    """Chunking and vector settings the same shape ``tests/test_ingestion.py`` uses.

    A small ``chunk_size_chars`` so :data:`BODY` produces several chunks from a
    short string; ``static`` so :func:`~app.workers.ingestion.build_embedding_provider`
    has something to build without a credential, which matters for the tests that
    exercise that seam.
    """
    return make_settings(
        embedding_dimension=WIDTH,
        embedding_provider="static",
        chunk_size_chars=120,
        chunk_overlap_chars=20,
        embedding_batch_size=4,
    )


@pytest.fixture
def world(
    model_session: Session,
    sqlite_session_factory: sessionmaker[Session],
    make_account: Any,
    tmp_path: Path,
    worker_settings: Settings,
) -> Iterator[World]:
    """Two real tenants, a fake store, a static provider, production-shaped resources.

    Both tenants come from the shared ``make_account`` factory, so the rows, the
    foreign keys and the ``users.role`` values are real — a hand-built
    ``tenant_id`` would not exist in ``tenants`` and the ``documents`` foreign key
    would reject it.

    The session factory is the one over the same SQLite engine as ``model_session``
    but is used **through** :func:`~app.db.session.session_scope`, so the task
    opens its own session rather than borrowing the test's. That is the property
    worth exercising: the task must be able to obtain a session by itself.
    """
    acme, acme_user = make_account(
        slug="acme", email="a@example.com", role=UserRole.ADMIN
    )
    globex, _ = make_account(slug="globex", email="b@example.com", role=UserRole.ADMIN)
    model_session.commit()

    storage = LocalFileStore(tmp_path / "objects")
    store = store_over(worker_settings, FakeQdrant())

    built = World(
        session=model_session,
        storage=storage,
        store=store,
        provider=StaticProvider(),
        settings=worker_settings,
        resources=IngestionResources(
            app_settings=worker_settings,
            session_factory=sqlite_session_factory,
            storage=storage,
            store=store,
            provider=StaticProvider(),
        ),
        tenant_a=acme.id,
        tenant_b=globex.id,
        user_a=acme_user.id,
    )

    yield built

    store.close()


def result_of(
    document_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    status: DocumentStatus = DocumentStatus.READY,
    chunk_count: int | None = 3,
) -> IngestionResult:
    """An :class:`~app.services.ingestion.IngestionResult` built by hand.

    Constructed rather than produced by a pipeline run so that
    :func:`~app.workers.ingestion.task_result` can be tested on its own — including
    on the ``None`` chunk count a failure carries, which no happy-path run produces.
    """
    succeeded = status is DocumentStatus.READY
    return IngestionResult(
        document_id=document_id,
        status=status,
        chunk_count=chunk_count,
        stage=IngestionStage.RECORD if succeeded else IngestionStage.READ,
        error=None if succeeded else "read: stored object missing",
        collection=collection_name_for(tenant_id) if succeeded else None,
        elapsed_seconds=1.23456,
    )


# ---------------------------------------------------------------------------
# The service identity
# ---------------------------------------------------------------------------


class TestTheServiceIdentity:
    """``for_tenant_service`` is the only way the worker obtains an identity.

    Three assertions, one per field the worker does not control. ``tenant_id`` is
    excluded here precisely because it *is* controlled — by the row, which
    :class:`TestTheTenantComesFromThePersistedRow` covers.
    """

    def test_it_carries_the_trusted_persisted_row_source(self) -> None:
        identity = AuthenticatedIdentity.for_tenant_service(
            tenant_id=ARBITRARY_TENANT
        )
        assert identity.source is IdentitySource.PERSISTED_ROW
        assert identity.tenant_id is ARBITRARY_TENANT

    def test_it_is_the_least_privileged_role_the_enum_offers(self) -> None:
        """A service identity that could be an admin would be a latent privilege.

        Chosen so that if a later step adds a role filter to any service this
        touches, the worker is *refused* rather than silently granted.
        """
        assert (
            AuthenticatedIdentity.for_tenant_service(
                tenant_id=ARBITRARY_TENANT
            ).role
            is UserRole.VIEWER
        )

    def test_it_names_a_fixed_system_actor_rather_than_a_person(self) -> None:
        """The same constant every time, so two runs cannot be attributed to two users.

        A worker that attributed its writes to whoever uploaded the document would
        make an audit trail record a person for work that person did not do.
        """
        first = AuthenticatedIdentity.for_tenant_service(
            tenant_id=ARBITRARY_TENANT
        )
        second = AuthenticatedIdentity.for_tenant_service(tenant_id=uuid.uuid4())
        assert first.user_id == second.user_id == SYSTEM_ACTOR_ID
        assert first.user_id.int == 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestTheTaskIsRegistered:
    def test_it_is_registered_on_the_project_app_under_a_stable_name(self) -> None:
        """A worker started against this app resolves the name.

        The name is asserted as a literal *and* as the constant, so renaming either
        the function or the constant fails here. A broker is not needed for this:
        the registry is what a worker resolves a name against.
        """
        assert INGEST_DOCUMENT_TASK_NAME == "tenantmind.ingest_document"
        assert INGEST_DOCUMENT_TASK_NAME in celery_app.tasks

        # Compared by equality of the callable a worker actually invokes rather than
        # by object identity: Celery's registry holds its own proxy for the task, so
        # `is` on the objects fails even when the registration is correct. With
        # `bind=True`, `run` is a bound method recreated on every attribute access,
        # so `is` fails there too — but both sides bind the same task instance and
        # the same function, which is exactly what `==` on a bound method compares.
        registered = celery_app.tasks[INGEST_DOCUMENT_TASK_NAME]
        assert registered.run == ingest_document_task.run
        assert registered.name == INGEST_DOCUMENT_TASK_NAME

    def test_it_runs_on_the_project_app_rather_than_a_private_one(self) -> None:
        """A task bound to a second app would be registered where the worker looks nowhere.

        The symptom would be a message sitting in the queue forever, which reads as
        a broker problem rather than a wiring one.
        """
        assert ingest_document_task.app is celery_app

    def test_importing_the_module_opens_no_database_connection(self) -> None:
        """Import happens at worker start-up, before configuration is known to work.

        If the module built an engine or a session at import time, a worker could
        not boot far enough to report a missing ``DATABASE_URL``.
        """
        assert not any(
            isinstance(value, Session) for value in vars(worker_module).values()
        ), "the worker module must hold no Session at import time"

    def test_the_retry_policy_is_manual_and_bounded_not_blanket(self) -> None:
        """Retry is explicit ``self.retry`` with a budget, never Celery's blanket switch.

        ``autoretry_for`` would retry *every* exception — a programming bug
        included — and ``acks_late`` changes the delivery guarantee for every
        task, not just the transient ones. Step 4A retries the narrow set in
        :data:`~app.workers.ingestion.RETRYABLE_TASK_FAILURES` by hand, and the
        budget is :data:`~app.workers.ingestion.MAX_RETRIES`.
        """
        options = ingest_document_task.__dict__
        assert "max_retries" not in options
        assert "autoretry_for" not in options
        assert "acks_late" not in options
        assert worker_module.MAX_RETRIES == 3


# ---------------------------------------------------------------------------
# The argument contract
# ---------------------------------------------------------------------------


class TestTheTaskArgumentContract:
    """One argument, naming a document rather than a tenant.

    This is what makes the cross-tenant pairing *unrepresentable*. If a
    ``tenant_id`` parameter were ever added, this fails — which is why the
    assertion covers the whole signature rather than the parameters it happens to
    know about today.
    """

    def test_it_takes_exactly_one_parameter_named_document_id(self) -> None:
        parameters = inspect.signature(ingest_document_task.run).parameters
        assert list(parameters) == ["document_id"]
        assert (
            parameters["document_id"].kind
            is inspect.Parameter.POSITIONAL_OR_KEYWORD
        )

    def test_the_parameter_is_a_str_because_json_has_no_uuid_type(self) -> None:
        annotation = inspect.signature(ingest_document_task.run).parameters[
            "document_id"
        ].annotation
        assert annotation is str or annotation == "str", (
            f"expected a str annotation, got {annotation!r}"
        )

    def test_it_accepts_no_varargs_or_kwargs(self) -> None:
        """A variadic parameter reopens the surface this design closes.

        ``**kwargs`` is a way for a tenant id to arrive in a message without
        appearing in the signature, which is the one failure a signature assertion
        would otherwise miss.
        """
        kinds = {
            parameter.kind
            for parameter in inspect.signature(ingest_document_task.run).parameters.values()
        }
        assert inspect.Parameter.VAR_KEYWORD not in kinds
        assert inspect.Parameter.VAR_POSITIONAL not in kinds

    def test_no_entry_point_names_a_tenant_role_identity_or_collection(self) -> None:
        """Neither layer has anything to fill a tenant in with.

        The body is included even though it takes a resources object: the resources
        carry collaborators, not identity, and that distinction is worth pinning.
        """
        for call in (ingest_document_task.run, ingest_stored_document):
            for name in inspect.signature(call).parameters:
                for forbidden in ("tenant", "role", "identity", "collection"):
                    assert forbidden not in name, (
                        f"{call.__name__} has a {name!r} parameter"
                    )

    def test_the_resources_object_carries_no_identity_field(self) -> None:
        """``IngestionResources`` holds collaborators, not a caller.

        Checked by field name so that adding ``identity`` to it — the natural way to
        "make the task more flexible" — fails here rather than silently letting a
        task message name a tenant.
        """
        fields = {field_.name for field_ in IngestionResources.__dataclass_fields__.values()}
        assert fields == {
            "app_settings",
            "session_factory",
            "storage",
            "store",
            "provider",
        }
        for name in fields:
            assert "tenant" not in name
            assert "identity" not in name

    def test_it_rejects_a_malformed_id_before_reading_anything(self) -> None:
        """A bad message costs no database round trip.

        The parse happens in the registered task, before
        :func:`~app.workers.ingestion.build_ingestion_resources` is reached — so a
        typo cannot be turned into a connection attempt.
        """
        with pytest.raises(ValueError):
            ingest_document_task.run("not-a-uuid")

    def test_a_malformed_id_never_reaches_the_resource_seam(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Proved by making the seam fail if it is called at all.

        A ``ValueError`` from ``uuid.UUID`` and a ``ValueError`` from a settings
        lookup look the same to a caller, so the test that only asserts the type
        would pass even if the ordering were wrong.
        """
        def explode(_: Settings) -> IngestionResources:
            raise AssertionError("the resource seam must not be reached")

        monkeypatch.setattr(worker_module, "build_ingestion_resources", explode)

        with pytest.raises(ValueError):
            ingest_document_task.run("not-a-uuid")


# ---------------------------------------------------------------------------
# Tenant derivation
# ---------------------------------------------------------------------------


class TestTheTenantComesFromThePersistedRow:
    def test_the_identity_tenant_is_the_documents_own_tenant(self, world: World) -> None:
        """The document belonging to tenant B is processed as tenant B.

        B is the interesting one. A default of "the first tenant", or a leftover
        from a previous task in the same worker process, would show up here and
        nowhere else.
        """
        document = world.make_document(tenant_id=world.tenant_b)

        result = world.run(document.id)

        assert result["tenant_id"] == str(world.tenant_b)
        assert result["tenant_id"] == str(document.tenant_id)

    def test_the_row_reaches_ready_and_the_result_mirrors_it(self, world: World) -> None:
        """The worker adds no status logic of its own.

        ``ready`` was written by
        :func:`~app.services.documents.record_ingestion_outcome`, and the returned
        ``status`` is read out of the pipeline's own result rather than decided
        here — so a worker that skipped recording would fail on the row assertion
        even if the result still said ``ready``.
        """
        document = world.make_document()

        result = world.run(document.id)

        assert world.reloaded(document.id).status is DocumentStatus.READY
        assert result["status"] == DocumentStatus.READY.value
        assert result["stage"] == IngestionStage.RECORD.value

    def test_the_query_filters_on_the_primary_key_and_nothing_else(
        self, world: World
    ) -> None:
        """No query in the worker may carry a ``tenant_id`` predicate.

        A behavioural test cannot distinguish "read the row's tenant" from "read the
        tenant the caller supplied and happened to be right about" — there is no
        caller-supplied tenant in the signature for it to be right about, so what
        remains to pin is a *second* predicate. Filtering here would need a tenant
        the caller would also have to supply.
        """
        tree = ast.parse(Path(worker_module.__file__).read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_tenant_id_of"
        )
        for comparison in (n for n in ast.walk(function) if isinstance(n, ast.Compare)):
            rendered = ast.dump(comparison)
            assert "tenant_id" not in ast.dump(comparison.comparators[0]), (
                "_tenant_id_of must select the tenant, never filter on it"
            )
            assert "attr='id'" in rendered or "id" in rendered

    def test_the_selected_column_is_the_tenant(self) -> None:
        """The statement selects ``Document.tenant_id``, not a whole row.

        Selecting one column rather than the row is what keeps document content out
        of this lookup's memory entirely — the row is a large blob this code has no
        reason to hold.
        """
        source = Path(worker_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        selected = [
            ast.dump(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "select"
        ]
        assert any("tenant_id" in dump for dump in selected), (
            "the lookup must select the tenant column"
        )
        assert not any("filename" in dump for dump in selected)
        assert not any("storage_key" in dump for dump in selected)

    def test_the_pipeline_runs_under_a_persisted_row_identity(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> None:
        """The identity handed to the pipeline is the service one, not a token one.

        Monkeypatching the name the module imported is the only way to observe the
        call without reimplementing the pipeline around a recorder.
        """
        document = world.make_document()
        seen: dict[str, Any] = {}
        real = worker_module.ingest_document

        def spy(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(worker_module, "ingest_document", spy)

        world.run(document.id)

        identity = seen["identity"]
        assert isinstance(identity, AuthenticatedIdentity)
        assert identity.source is IdentitySource.PERSISTED_ROW
        assert identity.user_id == SYSTEM_ACTOR_ID
        assert identity.tenant_id == world.tenant_a


# ---------------------------------------------------------------------------
# Missing document
# ---------------------------------------------------------------------------


class TestAMissingDocument:
    def test_it_raises_rather_than_returning_a_status(self, world: World) -> None:
        """No row means no place to record anything, and that is not a status.

        ``missing`` is not a :class:`DocumentStatus`. A caller comparing status
        strings would have to special-case it, and one that did not would read it
        as a value the pipeline had produced. Raising keeps the condition visible
        as a task failure, which is what it is.
        """
        with pytest.raises(IngestionTaskError):
            world.run(uuid.uuid4())

    def test_the_message_names_the_id_and_nothing_else(self, world: World) -> None:
        """The message reaches the worker log and, later, whatever Celery does.

        Useful to an operator and useless to an attacker: the id is something the
        caller already sent, and a tenant id would be something they did not.
        """
        absent = uuid.uuid4()
        with pytest.raises(IngestionTaskError) as caught:
            world.run(absent)

        message = str(caught.value)
        assert str(absent) in message
        assert CANARY not in message
        assert str(world.tenant_a) not in message
        assert str(world.tenant_b) not in message

    def test_it_fails_before_the_pipeline_is_reached(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> None:
        """No vector write, no storage read, no stage call.

        Asserted through the store's contents rather than by counting calls: an
        empty collection is the observable proof, and it would hold even if the
        pipeline were reached and happened to write nothing.
        """
        def unreachable(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the pipeline must not run for a missing document")

        monkeypatch.setattr(worker_module, "ingest_document", unreachable)

        with pytest.raises(IngestionTaskError):
            world.run(uuid.uuid4())

        assert world.collections_with_points() == {}

    def test_no_row_is_created_for_a_missing_document(self, world: World) -> None:
        """The rollback leaves nothing behind.

        :func:`~app.db.session.session_scope` rolls back on a raise, so a partially
        applied derivation cannot survive — worth asserting because the derivation
        is the one thing that ran.
        """
        with pytest.raises(IngestionTaskError):
            world.run(uuid.uuid4())

        count = world.session.scalar(select(func.count()).select_from(Document))
        assert count == 0


# ---------------------------------------------------------------------------
# Invoking the existing pipeline
# ---------------------------------------------------------------------------


class TestThePipelineIsInvoked:
    def test_it_calls_the_existing_service_with_the_resources(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> None:
        """The wrapper delegates; it does not reimplement the stages.

        The recorded kwargs also prove the collaborators are the ones the resources
        carry — a worker that rebuilt its own store would pass a happy-path test
        and fail this one.
        """
        document = world.make_document()
        captured: dict[str, Any] = {}
        real = worker_module.ingest_document

        def spy(*args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(worker_module, "ingest_document", spy)

        world.run(document.id)

        assert captured["document_id"] == document.id
        assert captured["storage"] is world.resources.storage
        assert captured["store"] is world.resources.store
        assert captured["provider"] is world.resources.provider
        assert captured["settings"] is world.resources.app_settings

    def test_a_stage_failure_is_recorded_on_the_row_and_returned(
        self, world: World
    ) -> None:
        """A failed ingest is a ``failed`` row — PMP §20's default path.

        Reached by writing a row whose file is absent, so ``stage_read`` fails for a
        real reason. The worker must not convert that into an exception: the durable
        record is the row, and a caller has no other way to learn what happened.
        """
        document = world.make_document(stored=False)

        result = world.run(document.id)

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.FAILED
        assert result["status"] == DocumentStatus.FAILED.value
        assert result["error"] == row.error
        assert result["chunk_count"] is None

    def test_the_recorded_failure_message_carries_no_document_content(
        self, world: World
    ) -> None:
        """The error is the one place a stage failure could leak the body.

        Asserted on the row *and* on the result, since the result mirrors the row
        and both are readable by an operator.
        """
        document = world.make_document(stored=False)

        result = world.run(document.id)

        assert CANARY not in json.dumps(result)
        assert CANARY not in (world.reloaded(document.id).error or "")


# ---------------------------------------------------------------------------
# Cross-tenant isolation
# ---------------------------------------------------------------------------


class TestCrossTenantIsolation:
    def test_each_document_lands_in_its_own_tenants_collection(
        self, world: World
    ) -> None:
        """Two tenants, two documents, two collections, no crossing.

        The central security assertion. Only a ``document_id`` was ever supplied, so
        the tenant that ends up owning each write was decided by the row — and this
        reads what each collection *holds*, not what the store was told to write.
        """
        first = world.make_document()
        second = world.make_document(tenant_id=world.tenant_b)

        world.run(first.id)
        world.run(second.id)

        held = world.collections_with_points()
        acme_name = collection_name_for(world.tenant_a)
        globex_name = collection_name_for(world.tenant_b)
        assert acme_name in held and globex_name in held
        assert held[acme_name] and held[globex_name], "each document produced points"
        assert held[acme_name] != held[globex_name]

    def test_no_point_carries_the_other_tenants_payload(
        self, world: World
    ) -> None:
        """The payload's ``tenant_id`` is the document's, on every point.

        Read from the stored payloads rather than inferred from the collection name,
        so a bug that wrote the right collection with the wrong metadata would still
        be caught.
        """
        from tests.records import payload_key

        document = world.make_document(tenant_id=world.tenant_b)

        world.run(document.id)

        stored = world.store.scroll_document_points(world.tenant_b, document.id)
        assert stored, "the pipeline produced no points to check"
        for record in stored:
            assert payload_key(record, "tenant_id") == str(world.tenant_b)
            assert payload_key(record, "document_id") == str(document.id)

    def test_naming_one_tenants_document_cannot_reach_the_other_collection(
        self, world: World
    ) -> None:
        """There is no argument with which to express the attack.

        The attempt is written as a call with a ``tenant_id`` keyword, because that
        is how an attacker would try. :exc:`TypeError` is the outcome, raised by
        Python's own signature check — before any row is read, before any collection
        is named, and without this project writing a single ``if`` that could later
        be refactored into the wrong branch.
        """
        target = world.make_document(tenant_id=world.tenant_b)

        with pytest.raises(TypeError):
            ingest_stored_document(  # type: ignore[call-arg]
                target.id, world.resources, tenant_id=world.tenant_a
            )

        assert world.collections_with_points() == {}, (
            "the refused call must have written nothing at all"
        )

    def test_the_task_signature_also_refuses_a_tenant_keyword(
        self, world: World
    ) -> None:
        """The same refusal at the entry point a caller actually reaches.

        The attack as it would arrive on the wire: a message with two arguments.
        """
        target = world.make_document(tenant_id=world.tenant_b)

        with pytest.raises(TypeError):
            ingest_document_task(  # type: ignore[call-arg]
                str(target.id), tenant_id=str(world.tenant_a)
            )

    def test_the_pipeline_refuses_a_tenant_that_disagrees_with_the_row(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> None:
        """Defence in depth: the derivation is not the only guard.

        The worker's derivation could be wrong — a bug, a careless refactor — and
        the pipeline's own tenant-scoped lookup would still refuse to write. This
        replaces the derivation with a deliberately wrong tenant and asserts that
        nothing reaches any collection, which is what turns "the safety property is
        carried by the existing predicate" into a claim rather than an aspiration.

        ``DocumentNotFoundError`` is the expected outcome specifically: the pipeline
        looks the document up with ``WHERE id = ? AND tenant_id = ?`` and the
        derived tenant does not match, so the row is *not found*. An implementation
        that had silently dropped the tenant predicate would instead ingest into the
        wrong collection and fail nothing here — which is why the assertion is on
        the exception and on the empty collections together.
        """
        from app.services.documents import DocumentNotFoundError

        document = world.make_document()
        monkeypatch.setattr(
            worker_module, "_tenant_id_of", lambda *_a, **_k: world.tenant_b
        )

        with pytest.raises(DocumentNotFoundError):
            ingest_stored_document(document.id, world.resources)

        assert world.collections_with_points() == {}, (
            "a mismatched tenant must not reach a collection at all"
        )

    def test_the_collection_named_is_the_one_the_row_implies(
        self, world: World
    ) -> None:
        """Read back from the pipeline's own result, not from configuration.

        The collection name is what the pipeline asked for, so asserting it against
        ``collection_name_for(document.tenant_id)`` proves the derived tenant drove
        the write rather than coinciding with it.
        """
        document = world.make_document(tenant_id=world.tenant_b)

        result = world.run(document.id)

        assert result["collection"] == collection_name_for(world.tenant_b)
        assert result["collection"] != collection_name_for(world.tenant_a)

    def test_the_result_reports_the_derived_tenant_not_a_supplied_one(
        self, world: World
    ) -> None:
        """``tenant_id`` in the result exists to be checked against the document.

        Included so an operator can compare the tenant the work ran in against the
        tenant on the document — not because it is secret, and not because a caller
        supplied it.
        """
        document = world.make_document(tenant_id=world.tenant_b)

        result = world.run(document.id)

        assert result["tenant_id"] == str(world.tenant_b)
        assert result["tenant_id"] == str(document.tenant_id)


# ---------------------------------------------------------------------------
# The returned result
# ---------------------------------------------------------------------------


class TestTheResultIsSmallAndSerialisable:
    def test_it_is_json_round_trippable(self, world: World) -> None:
        """It survives the worker's declared JSON-only policy.

        Asserted by actually round-tripping, because a mapping holding a
        :class:`uuid.UUID` or an ``enum.StrEnum`` looks serialisable by inspection
        and is not.
        """
        document = world.make_document()

        result = world.run(document.id)

        assert json.loads(json.dumps(result)) == result

    def test_every_leaf_is_a_json_scalar(self, world: World) -> None:
        """No UUID, no enum, no ORM object.

        Checked by type rather than by the round trip above, because the round trip
        passes for a dataclass whose encoder coerces — which is the dependency on
        Celery's own serialiser that this project refuses. The values are plain
        here, so nothing has to coerce them.
        """
        document = world.make_document()

        result = world.run(document.id)

        for key, value in result.items():
            assert isinstance(key, str)
            assert value is None or isinstance(value, (str, int, float, bool)), (
                f"{key} is a {type(value).__name__}, not a JSON scalar"
            )
            assert not isinstance(value, (Document, uuid.UUID))

    def test_it_carries_no_document_content(self, world: World) -> None:
        """The canary never appears in the result.

        The result sits in the broker's result backend keyed by task id, and the
        body of a document is the one thing that must not be recoverable from there.
        """
        document = world.make_document()

        result = world.run(document.id)

        assert CANARY not in json.dumps(result)
        for value in result.values():
            assert not isinstance(value, list), "no chunk list may be returned"
            assert not isinstance(value, dict), "no nested payload may be returned"

    def test_it_names_exactly_the_documented_fields(self) -> None:
        """The field set is pinned, so a new key cannot appear unnoticed.

        A result that grows a field is a change to what the broker holds, and it
        should be a deliberate edit here rather than a side effect of something else.
        """
        document_id = uuid.uuid4()
        result = task_result(result_of(document_id, tenant_id=ARBITRARY_TENANT), tenant_id=ARBITRARY_TENANT)

        assert set(result) == {
            "document_id",
            "tenant_id",
            "status",
            "stage",
            "chunk_count",
            "collection",
            "error",
            "elapsed_seconds",
        }

    def test_it_reports_a_none_chunk_count_distinctly_from_zero(self) -> None:
        """``None`` means "this run indexed nothing"; ``0`` means "zero chunks".

        The column cannot express the difference — ``chunk_count`` is ``NOT NULL
        DEFAULT 0`` — so the result has to, or a caller would conclude a failed run
        produced no chunks rather than that it never got as far as counting.
        """
        document_id = uuid.uuid4()

        failed = task_result(
            result_of(
                document_id,
                tenant_id=ARBITRARY_TENANT,
                status=DocumentStatus.FAILED,
                chunk_count=None,
            ),
            tenant_id=ARBITRARY_TENANT,
        )
        empty = task_result(
            result_of(
                document_id,
                tenant_id=ARBITRARY_TENANT,
                status=DocumentStatus.READY,
                chunk_count=0,
            ),
            tenant_id=ARBITRARY_TENANT,
        )

        assert failed["chunk_count"] is None
        assert empty["chunk_count"] == 0

    def test_it_rounds_the_duration_so_the_result_is_stable(self) -> None:
        """A raw ``float`` of a wall-clock duration is noise in a stored result.

        Rounded to milliseconds: enough to see a regression, not enough to differ
        between two reads of the same record.
        """
        document_id = uuid.uuid4()

        result = task_result(result_of(document_id, tenant_id=ARBITRARY_TENANT), tenant_id=ARBITRARY_TENANT)

        assert result["elapsed_seconds"] == 1.235

    def test_task_result_reads_no_configuration(self) -> None:
        """No secret can appear, because no configuration value is read.

        Asserted on the code rather than on a particular secret: nothing in
        :func:`~app.workers.ingestion.task_result` touches
        :class:`~app.core.config.Settings`, so there is no key under which a
        password could be included. Stated this way because asserting "the result
        contains no ``redis_url``" would pass for a task never configured with one.
        """
        tree = ast.parse(Path(worker_module.__file__).read_text(encoding="utf-8"))
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "task_result"
        )
        parameters = {argument.arg for argument in function.args.args}
        parameters |= {argument.arg for argument in function.args.kwonlyargs}

        assert parameters == {"result", "tenant_id"}, (
            "task_result must take the outcome and the derived tenant, nothing else"
        )


# ---------------------------------------------------------------------------
# The registered task, in process
# ---------------------------------------------------------------------------


class TestTheRegisteredTask:
    """The thin layer above the body, with the configuration seam replaced.

    :func:`~app.workers.ingestion.build_ingestion_resources` is the one place
    configuration is read, so replacing it is enough to run the *registered* task
    against doubles — including the string parse a real message would need.
    """

    @pytest.fixture(autouse=True)
    def _seam(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> Iterator[None]:
        monkeypatch.setattr(
            worker_module, "build_ingestion_resources", lambda _: world.resources
        )
        yield

    def test_it_runs_the_body_and_returns_the_same_shape(self, world: World) -> None:
        document = world.make_document()

        result = ingest_document_task.run(str(document.id))

        assert result["document_id"] == str(document.id)
        assert result["tenant_id"] == str(world.tenant_a)
        assert result["status"] == DocumentStatus.READY.value
        assert json.loads(json.dumps(result)) == result

    def test_it_parses_the_string_id_and_passes_nothing_else(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> None:
        """The only transformation between the wire and the body is ``str -> UUID``.

        Anything else — a default tenant, a lookup of the caller's identity — would
        have to appear in this call, so the body is called with the parsed id and
        nothing beside it.
        """
        document = world.make_document()
        seen: list[Any] = []

        def record(parsed: uuid.UUID, resources: IngestionResources) -> dict[str, object]:
            seen.append((parsed, resources))
            return {}

        monkeypatch.setattr(worker_module, "ingest_stored_document", record)

        ingest_document_task.run(str(document.id))

        assert seen == [(document.id, world.resources)]

    def test_a_missing_document_fails_the_task(self, world: World) -> None:
        """The same exception reaches a worker, unwrapped.

        Celery needs to see it to mark the task failed; Steps 3 and 4 own what
        happens next.
        """
        with pytest.raises(IngestionTaskError):
            ingest_document_task.run(str(uuid.uuid4()))

    def test_the_seam_is_the_only_configuration_lookup(self) -> None:
        """One place reads settings, so the production wiring is one edit.

        Asserted structurally: if a second ``settings`` lookup appeared in the task
        body, a test that replaced only the seam would miss it.
        """
        source = Path(worker_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        calls: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.append(node.func.attr)

        # Each collaborator is built once, in the seam. Two calls to any of them
        # would mean a second construction path the tests above do not replace.
        for builder in (
            "build_embedding_provider",
            "get_vector_store",
            "get_engine",
            "build_session_factory",
            "LocalFileStore",
        ):
            assert calls.count(builder) == 1, (
                f"{builder} must be called exactly once, from "
                "build_ingestion_resources"
            )

    def test_it_uses_the_projects_session_helper(self) -> None:
        """``session_scope`` is the only session helper this module calls.

        A second one — a bare ``Session()``, or ``engine.begin()`` — would mean a
        second commit policy, and this project has exactly one by design.
        """
        tree = ast.parse(Path(worker_module.__file__).read_text(encoding="utf-8"))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)

        assert "session_scope" in called
        for forbidden in ("commit", "rollback", "begin", "close"):
            assert forbidden not in called, (
                f"the worker must not call {forbidden}() directly; session_scope "
                "owns the transaction"
            )


# ---------------------------------------------------------------------------
# The retry policy (Step 4A)
# ---------------------------------------------------------------------------


class TestTheTaskRetryPolicy:
    """The registered task's retry branches, in process.

    The pipeline is driven for real — the world's sqlite, storage and fake store
    — so "retry then succeed" and "retry exhausted" produce real rows. The only
    doubles are the ones the policy is about: ``self.request.retries`` (which
    attempt this is) and ``self.retry`` (which would re-dispatch instead of
    running in-process). ``self.retry`` is replaced by a double whose
    keyword-only signature is itself an assertion: the task retries by
    re-publishing its current message and passes nothing beside the exception
    and the wait.
    """

    @pytest.fixture(autouse=True)
    def _seam(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> Iterator[None]:
        monkeypatch.setattr(
            worker_module, "build_ingestion_resources", lambda _: world.resources
        )
        yield

    @pytest.fixture
    def retry_recorder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[list[tuple[BaseException, float]]]:
        """Point ``self.request.retries`` at 0 and capture retry requests.

        ``request`` is a read-only property whose value falls back to the task's
        ``_default_request`` when no worker dispatcher has pushed a request, so the
        patch targets that attribute with a :class:`~celery.app.task.Context`. The
        ``retry`` double is installed on the task *instance*; because it is a plain
        function it receives only the keywords the task passes — ``exc`` and
        ``countdown`` — so any attempt to slip ``args=`` or ``kwargs=`` into a
        redispatch fails here with a ``TypeError`` before an assertion is even
        needed.
        """
        recorded: list[tuple[BaseException, float]] = []
        monkeypatch.setattr(
            ingest_document_task, "_default_request", Context(retries=0)
        )

        def fake_retry(*, exc: BaseException, countdown: float) -> None:
            recorded.append((exc, countdown))
            raise _RetriedSignal

        monkeypatch.setattr(ingest_document_task, "retry", fake_retry)
        yield recorded

    def test_a_transient_failure_asks_for_a_retry_with_backoff(
        self, monkeypatch: pytest.MonkeyPatch, world: World, retry_recorder
    ) -> None:
        """An embedding refusal is retried — and nothing is recorded yet.

        The pipeline lifts the :class:`EmbeddingError` into a
        :class:`TransientIngestionError` carrying the embed stage; the task asks
        for a retry after the first backoff rung (30s, plus jitter); the row stays
        ``processing`` with no error, and the collection was never created.
        """
        document = world.make_document()
        monkeypatch.setattr(
            worker_module,
            "build_ingestion_resources",
            lambda _: replace(world.resources, provider=_AlwaysDownProvider()),
        )

        with pytest.raises(_RetriedSignal):
            ingest_document_task.run(str(document.id))

        [(exc, countdown)] = retry_recorder
        assert isinstance(exc, TransientIngestionError)
        assert exc.stage is IngestionStage.EMBED
        assert exc.__cause__, "the original exception is not chained"
        assert 30.0 <= countdown < 45.0, "the first retry must wait ~30s plus jitter"

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.PROCESSING, (
            "a transient failure must leave the row processing, not failed"
        )
        assert row.error is None
        assert world.store.collection_exists(world.tenant_a) is False

    def test_a_retry_that_then_succeeds_records_ready(
        self, monkeypatch: pytest.MonkeyPatch, world: World, retry_recorder
    ) -> None:
        """The recovery story Step 4A exists for: one blip, then the row is ready.

        Attempt one fails transiently and is retried; the redelivered attempt —
        the provider having recovered — ingests normally and records ``ready``
        with the points in the collection. Nothing about the first attempt is
        left behind on the row.
        """
        document = world.make_document()
        flaky = _FlakyProvider(failures=1)
        monkeypatch.setattr(
            worker_module,
            "build_ingestion_resources",
            lambda _: replace(world.resources, provider=flaky),
        )

        with pytest.raises(_RetriedSignal):
            ingest_document_task.run(str(document.id))

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.PROCESSING
        assert row.error is None

        result = ingest_document_task.run(str(document.id))

        assert result["status"] == DocumentStatus.READY.value
        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.READY
        assert row.error is None
        assert world.store.scroll_document_points(world.tenant_a, document.id)
        assert len(retry_recorder) == 1, "the recovered run retried again"

    def test_retry_exhaustion_records_failed_with_the_stage_tagged_error(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> None:
        """When the budget is spent the worker writes the ``failed`` the pipeline could not.

        ``documents.error`` carries the same stage-tagged text the pipeline would
        have recorded — class name alone for an embedding failure, content-free —
        and the returned result mirrors the row.
        """
        document = world.make_document()
        monkeypatch.setattr(
            worker_module,
            "build_ingestion_resources",
            lambda _: replace(world.resources, provider=_AlwaysDownProvider()),
        )
        monkeypatch.setattr(
            ingest_document_task,
            "_default_request",
            Context(retries=worker_module.MAX_RETRIES),
        )

        def must_not_retry(*, exc: BaseException, countdown: float) -> None:
            pytest.fail("a retry was asked after the budget was spent")

        monkeypatch.setattr(ingest_document_task, "retry", must_not_retry)

        result = ingest_document_task.run(str(document.id))

        assert result["status"] == DocumentStatus.FAILED.value
        assert result["document_id"] == str(document.id)
        assert result["tenant_id"] == str(world.tenant_a)
        error = result["error"]
        assert error is not None
        assert "embed" in error
        assert "EmbeddingError" in error
        assert CANARY not in error
        assert "the embedding provider is down" not in error, (
            "an embedding provider's own words must not reach documents.error"
        )

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.FAILED
        assert row.error == error
        assert world.store.collection_exists(world.tenant_a) is False

    def test_exhaustion_records_under_the_documents_own_tenant(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> None:
        """The tenant in the exhausted record is the row's, never the caller's.

        The retry policy introduced a second writer of ``status``; it must
        re-derive the tenant exactly as the happy path does, from the persisted
        row, so the Step 2 isolation argument holds for failures too.
        """
        document = world.make_document(tenant_id=world.tenant_b)
        monkeypatch.setattr(
            worker_module,
            "build_ingestion_resources",
            lambda _: replace(world.resources, provider=_AlwaysDownProvider()),
        )
        monkeypatch.setattr(
            ingest_document_task,
            "_default_request",
            Context(retries=worker_module.MAX_RETRIES),
        )
        monkeypatch.setattr(
            ingest_document_task,
            "retry",
            lambda *args, **kwargs: pytest.fail("a retry was asked after exhaustion"),
        )

        result = ingest_document_task.run(str(document.id))

        assert result["tenant_id"] == str(world.tenant_b)
        assert world.reloaded(document.id).status is DocumentStatus.FAILED
        assert world.store.collection_exists(world.tenant_a) is False
        assert world.store.collection_exists(world.tenant_b) is False

    def test_a_database_failure_is_retried(
        self, monkeypatch: pytest.MonkeyPatch, world: World, retry_recorder
    ) -> None:
        """A briefly-unreachable database is in the retryable set."""
        db_hiccup = OperationalError(
            "SELECT documents.tenant_id FROM documents WHERE documents.id = $1",
            (),
            Exception("database is restarting"),
        )

        def refuse(*args: Any, **kwargs: Any) -> dict[str, object]:
            raise db_hiccup

        monkeypatch.setattr(worker_module, "ingest_stored_document", refuse)

        with pytest.raises(_RetriedSignal):
            ingest_document_task.run(str(world.make_document().id))

        [(exc, _)] = retry_recorder
        assert exc is db_hiccup

    def test_database_exhaustion_records_a_class_name_only_error(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> None:
        """Without a pipeline stage there is no stage-tagged message; the class name stands in.

        A :class:`sqlalchemy.exc.SQLAlchemyError` carries no stage, so the worker
        records the resolve-stage shape with the class name alone — the same
        content-free branch :func:`~app.services.ingestion._failure_message`
        would take for a type this project does not construct.
        """
        document = world.make_document()
        monkeypatch.setattr(
            worker_module,
            "ingest_stored_document",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OperationalError("SELECT ...", (), Exception("restarting"))
            ),
        )
        monkeypatch.setattr(
            ingest_document_task,
            "_default_request",
            Context(retries=worker_module.MAX_RETRIES),
        )
        monkeypatch.setattr(
            ingest_document_task,
            "retry",
            lambda *args, **kwargs: pytest.fail("a retry was asked after exhaustion"),
        )

        result = ingest_document_task.run(str(document.id))

        assert result["status"] == DocumentStatus.FAILED.value
        assert result["error"] == (
            "ingestion failed at the resolve stage (OperationalError)"
        )
        assert world.reloaded(document.id).status is DocumentStatus.FAILED

    def test_a_missing_document_is_not_retried(
        self, world: World, retry_recorder
    ) -> None:
        """A row that is gone will not come back; waiting would only add noise."""
        with pytest.raises(IngestionTaskError):
            ingest_document_task.run(str(uuid.uuid4()))

        assert retry_recorder == []

    def test_a_permanent_pipeline_failure_is_recorded_not_retried(
        self, world: World, retry_recorder
    ) -> None:
        """A missing stored object is permanent; the pipeline already recorded it."""
        document = world.make_document(stored=False)

        result = ingest_document_task.run(str(document.id))

        assert result["status"] == DocumentStatus.FAILED.value
        assert "StoredObjectMissingError" in (result["error"] or "")
        assert retry_recorder == []
        assert world.reloaded(document.id).status is DocumentStatus.FAILED

    def test_an_unexpected_exception_is_not_swallowed_or_retried(
        self, monkeypatch: pytest.MonkeyPatch, world: World, retry_recorder
    ) -> None:
        """A bug surfaces as a task failure once, never retried as a blip."""
        def explode(*args: Any, **kwargs: Any) -> dict[str, object]:
            raise RuntimeError("programming bug: unexpected keyword")

        monkeypatch.setattr(worker_module, "ingest_stored_document", explode)

        with pytest.raises(RuntimeError, match="programming bug"):
            ingest_document_task.run(str(world.make_document().id))

        assert retry_recorder == []

    def test_every_retry_redispatch_keeps_the_original_message(self) -> None:
        """``self.retry(...)`` must never be asked to reshape the message.

        Celery re-publishes the task's current message when ``retry`` is given no
        ``args`` or ``kwargs``, so a redelivery is *exactly* the original
        ``(document_id,)`` — the same one-argument contract as the original
        publish, with no slot in which a tenant, a role or content could ride.
        The moment a retry passed ``args=`` it could add anything, so every retry
        call site is pinned to the two keywords policy is allowed to set.
        """
        source = Path(worker_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        retry_keywords: list[list[str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "retry":
                continue
            if not (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
            ):
                continue
            retry_keywords.append(
                sorted((keyword.arg or "**kwargs") for keyword in node.keywords)
            )

        assert retry_keywords, "no self.retry(...) call remains in the task"
        assert all(
            keywords == ["countdown", "exc"] for keywords in retry_keywords
        ), f"a retry reshaped the message: {retry_keywords}"


# ---------------------------------------------------------------------------
# Enqueueing (Step 3)
# ---------------------------------------------------------------------------


class TestEnqueueing:
    """The publish side: fixed task name, one argument, honest failure.

    :func:`~app.workers.ingestion.enqueue_ingestion` is the single door the
    request path uses to reach the worker package — asserted from the source in
    ``tests/test_worker_app.py``. These tests pin what goes *onto the wire*:
    the same one-argument contract as the receive side, in the other direction.
    """

    def test_it_publishes_the_fixed_task_with_only_the_document_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``send_task`` is reached with the constant name and one argument.

        The name is the constant, never a caller-supplied string, and the
        argument is the document id as its string form — the wire format the
        registered task parses back.
        """
        published: list[tuple[Any, ...]] = []
        monkeypatch.setattr(
            worker_module.celery_app,
            "send_task",
            lambda *args, **kwargs: published.append((args, kwargs)),
        )

        document_id = uuid.uuid4()
        enqueue_ingestion(document_id)

        assert published == [
            ((INGEST_DOCUMENT_TASK_NAME,), {"args": [str(document_id)], "kwargs": {}})
        ]

    def test_the_published_message_carries_no_tenant_and_no_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pinned by shape, not by a search for today's secret strings.

        ``args`` is exactly one item and ``kwargs`` is empty, so there is no
        slot in which a tenant, a collection or document content could ride —
        the same unrepresentability argument as the receive side.
        """
        captured: dict[str, Any] = {}
        monkeypatch.setattr(
            worker_module.celery_app,
            "send_task",
            lambda *args, **kwargs: captured.update(kwargs),
        )
        document_id = uuid.uuid4()
        tenant_id = uuid.uuid4()

        enqueue_ingestion(document_id)

        assert captured["args"] == [str(document_id)]
        assert captured["kwargs"] == {}
        assert len(captured["args"]) == 1
        assert str(tenant_id) not in json.dumps(captured)
        assert CANARY not in json.dumps(captured)

    def test_it_takes_one_parameter_named_document_id_and_nothing_else(
        self,
    ) -> None:
        """A tenant, ``**kwargs`` or a resources object would reopen the surface.

        Checked over the whole signature so an added parameter fails here even
        if it is not one of the forbidden names a search would notice.
        """
        parameters = inspect.signature(enqueue_ingestion).parameters
        assert list(parameters) == ["document_id"]
        assert (
            parameters["document_id"].kind
            is inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        for parameter in parameters.values():
            assert parameter.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            )
        for forbidden in ("tenant", "role", "identity", "collection", "settings"):
            assert all(forbidden not in name for name in parameters), (
                f"enqueue_ingestion must not accept a {forbidden!r} parameter"
            )

    def test_a_publish_failure_is_an_enqueue_error_not_a_lie(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller must be able to tell "not queued" from "queued".

        The API maps :class:`~app.workers.ingestion.EnqueueError` to a ``503``;
        anything but this exception escaping would either crash the request with
        a ``500`` or, worse, let a refusal look like a success.
        """
        refusal = RuntimeError("Connection to Redis lost")
        monkeypatch.setattr(
            worker_module.celery_app,
            "send_task",
            lambda *args, **kwargs: (_ for _ in ()).throw(refusal),
        )

        with pytest.raises(EnqueueError) as caught:
            enqueue_ingestion(uuid.uuid4())

        assert caught.value.__cause__ is refusal

    def test_the_enqueue_error_names_the_document_and_nothing_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The message reaches the request log, so it must stay content-free.

        The document id is the identifier the request was *about*; the task name
        is fixed and public. Neither the broker password nor the document body
        belong in a log line that publishes a refusal.
        """
        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise OSError("broker refused the connection")

        monkeypatch.setattr(worker_module.celery_app, "send_task", refuse)
        document_id = uuid.uuid4()

        with pytest.raises(EnqueueError) as caught:
            enqueue_ingestion(document_id)

        message = str(caught.value)
        assert str(document_id) in message
        assert INGEST_DOCUMENT_TASK_NAME in message
        assert "redis" not in message.lower()
        assert "password" not in message.lower()
        assert CANARY not in message


# ---------------------------------------------------------------------------
# The worker source
# ---------------------------------------------------------------------------


class TestTheWorkerSource:
    """What the worker *names*, asserted on the parsed source.

    The web-framework claim needs two tests rather than one, because only the
    second is honest: the first says no line of ``app/workers/`` imports FastAPI,
    and the second records that importing the worker still drags it in anyway
    through Phase 1's model layer.
    """

    def test_no_worker_module_imports_a_web_framework_or_a_route(
        self,
    ) -> None:
        workers_root = Path(worker_module.__file__).parent
        files = sorted(workers_root.glob("*.py"))
        assert files, "no worker modules found; the guard would pass vacuously"

        offenders: list[str] = []
        for path in files:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    names = [base, *[f"{base}.{a.name}" for a in node.names]]
                for name in names:
                    if name.startswith(("fastapi", "starlette", "app.api")):
                        # `node.lineno` is on stmt nodes; `ast.walk` also yields
                        # expressions, so read it defensively rather than assuming.
                        line = getattr(node, "lineno", 0)
                        offenders.append(f"{path.name}:{line} {name}")

        assert not offenders, (
            f"a Celery worker must not depend on the web layer; found {offenders}"
        )

    def test_the_worker_does_not_import_routing_or_retrieval(
        self,
    ) -> None:
        """A worker ingests. It does not serve, and it does not search.

        Naming the forbidden roots rather than re-listing prefixes, so the intent
        is readable rather than mechanical.
        """
        workers_root = Path(worker_module.__file__).parent
        forbidden = ("app.api", "app.rag.query", "app.services.query", "app.main")

        for path in sorted(workers_root.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith(forbidden), (
                        f"{path.name} imports {node.module}"
                    )

    def test_the_worker_reuses_the_service_rather_than_reimplementing_it(
        self,
    ) -> None:
        """No stage logic is duplicated in the worker layer.

        Asserted by import: the pipeline is reached through
        :func:`~app.services.ingestion.ingest_document`, and the parsing, chunking
        and embedding modules are not imported at all. A worker that re-derived
        chunks would have to import them, so the absence is the proof.
        """
        source = Path(worker_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert "app.services.ingestion" in imported
        for forbidden in ("app.rag.chunking", "app.rag.parsers", "app.rag.vectors"):
            assert forbidden not in imported, (
                f"{forbidden} must stay behind the ingestion service"
            )

    def test_the_import_chain_into_fastapi_is_preexisting_and_recorded(
        self,
    ) -> None:
        """The limitation, pinned so it cannot be forgotten or rediscovered.

        ``app.db.base`` is imported by every model, ``app/db/__init__.py``
        re-exports ``app.db.session``, and that module imports ``fastapi`` for a
        ``Depends`` annotation. So importing the worker loads the web framework even
        though no line of the worker names it.

        Asserting the chain *exists* is deliberate: if a later step fixes it, this
        test fails and asks whether the recorded limitation should be removed — which
        is the outcome wanted, because the limitation is documented in this file and
        in the module docstring rather than nowhere.
        """
        import sys

        import app.models.document  # noqa: F401  (the model under discussion)

        assert "fastapi" in sys.modules, (
            "the transitive import is expected and documented. If this now fails, "
            "app/db/__init__.py stopped pulling in app.db.session and the recorded "
            "limitation should be removed from the worker module docstring."
        )