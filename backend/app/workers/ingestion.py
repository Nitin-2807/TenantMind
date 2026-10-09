"""The Celery document-ingestion task: ``tenantmind.ingest_document``.

What this module is
-------------------
The thin boundary between a message on a broker and
:func:`app.services.ingestion.ingest_document`. It resolves the collaborators
the pipeline needs, derives a tenant identity from persisted data, runs the
existing pipeline unchanged, and returns a small JSON-serialisable summary.

It contains **no** parsing, cleaning, chunking, embedding or vector-write
logic. Every one of those lives in :mod:`app.rag` and :mod:`app.storage`, was
written and gated in Phase 2.5, and is called here rather than reimplemented —
:func:`ingest_document` is imported, not copied.

Why the argument list is one long name
-------------------------------------
The task takes a ``document_id`` and nothing else. There is no ``tenant_id``
argument, no ``collection`` argument, no ``identity`` argument and no ``**kwargs``.

That is not minimalism for its own sake; it is what makes the cross-tenant
attack **unrepresentable** rather than merely checked::

    (document_id=A, tenant_id=B)   # cannot be expressed in a message

A caller who can publish to this broker can name a document. They cannot name
the tenant it is processed within, because that value is read from the document
row itself and never from the message. The chain is::

    document_id
        -> persisted Document row          (SELECT by primary key, nothing else)
        -> document.tenant_id
        -> AuthenticatedIdentity.for_tenant_service(tenant_id=...)
        -> ingest_document(identity=..., document_id=...)   # re-reads, tenant-scoped

Note the double read, and that it is deliberate. :func:`ingest_document` looks
the document up **again**, through
:func:`~app.services.documents.get_document_for_ingestion`, with its ordinary
``WHERE id = ? AND tenant_id = ?`` predicate. The derivation here does not
replace that predicate or weaken it — it *satisfies* it, and if the derivation
were ever wrong the pipeline's own tenant-scoped lookup would raise
:class:`~app.services.documents.DocumentNotFoundError` rather than ingesting
into the wrong collection. The safety property is carried by the existing
predicate; this module's job is to supply an identity it will accept.

Why the service identity exists at all
---------------------------------------
:func:`ingest_document` takes an :class:`~app.core.identity.AuthenticatedIdentity`
because that is how every tenant-scoped service in this project is called. A
worker process holds no bearer token, and minting one would mean signing a token
in order to work as itself. So the identity is built by
:meth:`AuthenticatedIdentity.for_tenant_service` — the second and last
construction site on that type, named after what it trusts, and reached from
exactly one place in the shipped application: this module.

Nothing here constructs ``AuthenticatedIdentity(...)`` directly. That would
create the "second call site outside ``from_access_token``" that
``tests/test_tenant_identity.py`` exists to prevent, while leaving that test
green, because the guard reads ``identity.py``'s own source and would not see a
construction in another file.

Enqueueing, on the other side
-----------------------------
This module is also where a request process asks for a document to be ingested:
:func:`enqueue_ingestion` publishes a message naming this task and nothing else.
It is the one door from :mod:`app.api` into the worker package, and a source
guard asserts it is the only one: a handler that imported Celery itself could
name a task from a client-controlled value, and that is the privilege no request
code holds. The message carries the ``document_id`` only — the same
unrepresentability argument as the receive side, in the other direction: a
publisher cannot name a tenant because there is no field for one.

Status handling
---------------
The pipeline owns the happy path: :func:`ingest_document` marks the document
``processing`` before any work that can fail and ends at ``ready`` or ``failed``
through :func:`~app.services.documents.record_ingestion_outcome`, which is the
only writer of those three columns. This module records nothing itself on the
happy path. The one exception is retry exhaustion — the pipeline's last attempt
raised before it could record, so :func:`_record_retry_exhaustion` writes the
``failed`` status the worker has finished earning. A document that does not exist
is the one condition with no row to write, and it raises
:class:`IngestionTaskError` rather than reporting a status — see that class for
why a "missing" return value would be the wrong shape.

Retry policy
------------
:func:`ingest_document_task` is ``bind=True`` and retries itself explicitly —
there is no ``autoretry_for``. Only the conditions in
:data:`RETRYABLE_TASK_FAILURES` are retried:
:class:`~app.services.ingestion.TransientIngestionError` (what the pipeline
raises for an embedding-provider, vector-store or filesystem failure) and
:class:`sqlalchemy.exc.SQLAlchemyError` (a briefly-unreachable database). The
budget is :data:`MAX_RETRIES` retries, spaced by :func:`_retry_backoff_seconds`
— exponential, with jitter, so a burst of failures does not re-collide in
lockstep — and a retry re-publishes the task with its current message unchanged:
``self.retry`` is given no ``args`` or ``kwargs``, so the wire contract stays
``tenantmind.ingest_document(document_id)`` on every attempt. When the budget is
spent, :func:`_record_retry_exhaustion` records the document ``failed`` with the
message the pipeline would have written, and the task returns the same shape as
any other outcome. Everything outside the retried set — a missing document, a
parse failure, a configuration fault, an unexpected exception — is failed once
and stops.

Deliberately not here
---------------------
* **Blanket retry options.** ``autoretry_for`` would retry *every* exception,
  a programming bug included, and ``acks_late`` changes the delivery guarantee
  for every task, not just the transient ones. Retrying here is narrow and
  explicit — see above.
* **A failure state machine beyond the row's own ``failed``.** The worker
  records ``failed`` itself only after its retries are spent. It invents no
  counters and no statuses; the row remains the record of truth.
* **A queue name, a priority, or routing.** The task publishes to Celery's
  default queue. Deciding that ingestion is more or less urgent than anything
  else is a decision for the step that first has two kinds of work to weigh.
* **FastAPI.** This module imports neither ``fastapi`` nor ``app.api``, and a
  test asserts that from the source. Note the limit of that claim, which is
  recorded honestly rather than overclaimed: importing *this module* does drag
  the web framework in transitively, because every model imports
  ``app.db.base`` and ``app/db/__init__.py`` re-exports ``app.db.session``,
  which imports ``fastapi``. That predates Phase 5 and is a Phase 1 layout
  question; fixing it is not this step's work. What holds here is the property
  that can be stated without qualification: no line of this module names a web
  framework or a route module.
* **Beats and schedules.** Nothing periodic exists.
* **A queue name, a priority, or routing.** The task publishes to Celery's
  default queue. Deciding that ingestion is more or less urgent than anything
  else is a decision for the step that first has two kinds of work to weigh.
* **FastAPI.** This module imports neither ``fastapi`` nor ``app.api``, and a
  test asserts that from the source. Note the limit of that claim, which is
  recorded honestly rather than overclaimed: importing *this module* does drag
  the web framework in transitively, because every model imports
  ``app.db.base`` and ``app/db/__init__.py`` re-exports ``app.db.session``,
  which imports ``fastapi``. That predates Phase 5 and is a Phase 1 layout
  question; fixing it is not this step's work. What holds here is the property
  that can be stated without qualification: no line of this module names a web
  framework or a route module.
* **Beats and schedules.** Nothing periodic exists.

Database handling
-----------------
:func:`~app.db.session.session_scope` — the project's existing non-request
session pattern, documented for "scripts and background workers" — opens one
session, commits on success, rolls back on failure, and closes in a ``finally``.
No second database configuration exists, no engine is built at import time, and
no session outlives the task that created it. The engine comes from
:func:`~app.db.engine.get_engine`, the same process-wide cached engine the web
application uses.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from celery import Task  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, settings
from app.core.identity import AuthenticatedIdentity
from app.db.engine import get_engine
from app.db.session import build_session_factory, session_scope
from app.models.document import Document, DocumentStatus
from app.rag.embeddings import EmbeddingProvider, build_embedding_provider
from app.services.documents import record_ingestion_outcome
from app.services.ingestion import (
    IngestionResult,
    IngestionStage,
    TransientIngestionError,
    ingest_document,
)
from app.storage.local import LocalFileStore
from app.storage.qdrant import QdrantVectorStore, get_vector_store
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

#: The registered task name.
#:
#: Explicit rather than relying on Celery's ``<app>.<function>`` derivation, so the
#: name a broker operator sees is a constant a test can assert instead of a
#: convention that changes if the function is ever renamed.
INGEST_DOCUMENT_TASK_NAME: Final[str] = "tenantmind.ingest_document"

#: How many times a transient failure is retried, after the first attempt.
#:
#: "Three" means three retries, so a single message is attempted up to four times
#: in the worst case before :func:`_record_retry_exhaustion` records ``failed``.
#: The budget is deliberately small: the conditions retried are infrastructure
#: blips, and a condition that has not cleared in ~3.5 minutes of backoff is
#: unlikely to clear by waiting longer.
MAX_RETRIES: Final[int] = 3

#: Seconds before the first retry.
#:
#: Doubled per retry by :data:`RETRY_BACKOFF_MULTIPLIER`, so the waits before
#: retries one, two and three are approximately 30s, 60s and 120s — plus jitter.
RETRY_BACKOFF_BASE_SECONDS: Final[float] = 30.0

#: The exponential factor applied per retry: base * multiplier ** retries_so_far.
RETRY_BACKOFF_MULTIPLIER: Final[float] = 2.0

#: Jitter, as a fraction of the backoff, added to every wait.
#:
#: A fleet of workers that fail together would otherwise re-collide in lockstep at
#: each backoff rung, retrying in a synchronized stampede instead of apart.
RETRY_BACKOFF_JITTER_FRACTION: Final[float] = 0.5

#: The exceptions a task failure retries. Everything else fails the task once.
#:
#: Only two conditions are worth waiting for: :class:`TransientIngestionError` —
#: what the pipeline raises for an embedding-provider, vector-store or filesystem
#: (``OSError``) failure; see
#: :data:`~app.services.ingestion.RETRYABLE_INGESTION_FAILURES` — and a
#: :class:`sqlalchemy.exc.SQLAlchemyError`, for a database that was briefly
#: unreachable. Deliberately not included: :class:`IngestionTaskError` and
#: :class:`~app.services.documents.DocumentNotFoundError` (a missing row will not
#: come back), :class:`~app.services.ingestion.IngestionError` and parse/missing-
#: object failures (permanent conditions, already recorded ``failed`` by the
#: pipeline), and anything unexpected — a programming bug must surface as a task
#: failure, once, so an operator sees it.
RETRYABLE_TASK_FAILURES: Final[tuple[type[BaseException], ...]] = (
    TransientIngestionError,
    SQLAlchemyError,
)


class IngestionTaskError(RuntimeError):
    """A task could not be started at all, as distinct from an ingestion that failed.

    Raised only when there is no row to record anything on. Every other failure
    is :func:`ingest_document`'s to handle, and it handles it: a stage failure
    becomes a ``failed`` row and a returned result, which is PMP §20's "failures
    will be recorded in PostgreSQL" implemented as the default outcome.

    A missing document is different in kind. The upload was accepted, the
    message was published, and by the time the worker picked it up the row was
    gone — deleted, or never committed. Reporting a *result* for it would be
    wrong in a way that is hard to see later: a ``status`` of ``missing`` is not
    a :class:`~app.models.document.DocumentStatus`, so a caller comparing status
    strings would have to special-case it, and a caller that did not would read
    it as a status the pipeline had never produced.

    Raising makes it a task failure instead, which is what it is. Whether that is
    retryable was Steps 3 and 4's question; Step 4A's answer is **no** — this
    exception names a row that is gone, and waiting will not bring a row back, so
    a retry would add latency and noise without a chance of success.

    The message names the ``document_id`` only. No filename, no storage key and
    no tenant content: this string reaches the worker's logs and, once retry
    policy exists, whatever Celery does with a failure.
    """


class EnqueueError(RuntimeError):
    """A task could not be published, so nothing was queued for the document.

    Raised by :func:`enqueue_ingestion` when the broker refused the message,
    and the one condition that leaves a committed ``processing`` row with no
    task behind it. The row is deliberately left as it is: it records the
    truth — the document exists and has not been ingested — and deciding how a
    later step reconciles rows that no task ever picked up (the crash window
    "database commit succeeds, publish fails") is Phase 5 failure/retry work,
    not this step's.

    Distinct from :class:`IngestionTaskError` on purpose. That class means a
    task *ran* and found no row; this one means a task was never *started*.
    One is a receive-side condition, the other a publish-side condition, and
    the API maps only this one to a visible error.

    The message names the ``document_id`` and the fixed task name, and nothing
    else: no broker URL, no credential, no document content. It is logged at
    the publish site and can reach an operator's log file.
    """


def enqueue_ingestion(document_id: uuid.UUID) -> None:
    """Publish the ingestion task for one document row.

    The fixed task :data:`INGEST_DOCUMENT_TASK_NAME` is published by name with
    one argument — the ``document_id`` as its string form, which is the wire
    format the task parses. Nothing else is in the message: no ``tenant_id``,
    no identity, no collection, no content. A caller who can publish to this
    broker can name a document; they cannot name how or where it is processed,
    exactly as on the receive side.

    The caller must already have committed the row. Publishing before the
    commit lets the worker pick the message up and find no document; the API's
    ordering — commit, then enqueue — is asserted by a test.

    Args:
        document_id: the persisted document's id.

    Raises:
        EnqueueError: the broker refused the message. Raised so the caller can
            tell the requester honestly that nothing was queued, and so the
            document id is in the log for an operator reconciling the row.
    """
    try:
        celery_app.send_task(
            INGEST_DOCUMENT_TASK_NAME,
            args=[str(document_id)],
            kwargs={},
        )
    except Exception as exc:
        logger.error(
            "enqueue failed: document=%s task=%s: %s",
            document_id,
            INGEST_DOCUMENT_TASK_NAME,
            exc,
        )
        raise EnqueueError(
            f"could not publish {INGEST_DOCUMENT_TASK_NAME} for "
            f"document {document_id}"
        ) from exc


def reconcile_stale_documents(
    session_factory: sessionmaker[Session],
    *,
    threshold_seconds: int,
) -> int:
    """Find and re-enqueue documents stuck in ``processing``.

    A document is *stale* when:
    - its ``status`` is ``processing``, and
    - its ``updated_at`` is older than ``threshold_seconds``.

    The threshold exists because a document that has been ``processing`` for
    minutes without a worker picking it up is either:
    - the crash window from Step 3 (database commit succeeded, Celery publish
      failed), or
    - a worker that died mid-ingestion, leaving the row ``processing`` without
      ever reaching ``ready`` or ``failed``.

    Re-enqueueing publishes the same ``tenantmind.ingest_document(document_id)``
    message the original upload did — the tenant is derived from the row, and no
    tenant identifier is in the message.

    Args:
        session_factory: the session factory to use for database access.
        threshold_seconds: the stale threshold in seconds.

    Returns:
        The number of documents re-enqueued.

    Raises:
        EnqueueError: if the broker refuses any message. The function stops at
            the first publish failure so an operator sees the exact document
            that could not be re-queued. Documents already processed in this
            call remain re-queued.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=threshold_seconds)

    requeued = 0

    with session_scope(session_factory) as session:
        stale = session.scalars(
            select(Document.id).where(
                Document.status == DocumentStatus.PROCESSING,
                Document.updated_at < cutoff,
            )
        ).all()

    for doc_id in stale:
        enqueue_ingestion(doc_id)
        requeued += 1

    if requeued:
        logger.info(
            "reconciliation re-enqueued %d stale document(s) (threshold=%ss)",
            requeued,
            threshold_seconds,
        )
    else:
        logger.debug(
            "reconciliation found no stale documents (threshold=%ss)",
            threshold_seconds,
        )

    return requeued


@dataclass(frozen=True, slots=True)
class IngestionResources:
    """Every collaborator one task run needs, resolved from configuration.

    Grouped into one object so the task body takes a single parameter and the
    construction seam is a single function. That is what makes this module
    testable without a broker, a database or a network: a test builds one of
    these by hand and :func:`ingest_stored_document` cannot tell the difference.

    Attributes:
        app_settings: the configuration the pipeline reads chunk size, batch
            size and vector width from.
        session_factory: bound to an engine. Passed rather than an engine so the
            task body has no way to build a second engine.
        storage: where the document's bytes are.
        store: the Qdrant store, process-wide cached by
            :func:`~app.storage.qdrant.get_vector_store` because the
            :class:`~qdrant_client.QdrantClient` it owns holds a connection
            pool.
        provider: the embedding provider.
    """

    app_settings: Settings
    session_factory: sessionmaker[Session]
    storage: LocalFileStore
    store: QdrantVectorStore
    provider: EmbeddingProvider


def build_ingestion_resources(app_settings: Settings) -> IngestionResources:
    """Construct the production collaborators for a worker process.

    The one place configuration is read, so the task body has nothing to look
    up and a test can replace the whole set with
    :meth:`IngestionResources` built by hand.

    Nothing here opens a connection. :func:`~app.db.engine.get_engine` builds a
    pool without dialing, :class:`LocalFileStore` resolves a path, and
    :func:`~app.storage.qdrant.get_vector_store` builds a client lazily — so a
    worker can be started, and this function called, before PostgreSQL or Qdrant
    are reachable. The first connection happens inside the task, where a failure
    belongs to a task and not to worker startup.

    Args:
        app_settings: the configuration to build from.

    Returns:
        Collaborators ready to hand to :func:`ingest_stored_document`.

    Raises:
        app.db.engine.DatabaseNotConfiguredError: ``DATABASE_URL`` is unset.
            Surfaced at task time rather than at worker startup, so a worker
            starts and reports the misconfiguration per task with the document
            id that could not be processed.
        app.rag.embeddings.EmbeddingProviderUnavailable: the configured provider
            has no credential. Both are this project's own exception types and
            neither message contains a secret — :func:`build_embedding_provider`
            names the variable to set, not its value.
    """
    return IngestionResources(
        app_settings=app_settings,
        session_factory=build_session_factory(get_engine(app_settings)),
        storage=LocalFileStore(app_settings.storage_root),
        store=get_vector_store(app_settings),
        provider=build_embedding_provider(app_settings),
    )


def ingest_stored_document(
    document_id: uuid.UUID, resources: IngestionResources
) -> dict[str, object]:
    """Ingest one persisted document and return a small, serialisable summary.

    The entire task body, with no Celery in it and no configuration lookup. That
    separation is what lets every property below be tested by calling this
    function directly, and it is why the registered task at the bottom of the
    module is as short as it is.

    The session is opened by :func:`~app.db.session.session_scope`, which
    commits on success, rolls back on a raised exception and closes in a
    ``finally``. One session spans the tenant derivation *and* the pipeline, so
    the document is read once for its tenant and once again by the pipeline's own
    tenant-scoped lookup — and both reads see the same uncommitted transaction.

    Args:
        document_id: which document to ingest. Already parsed from the wire
            format by the registered task.
        resources: the collaborators to run with.

    Returns:
        A JSON-serialisable :class:`dict` — no ORM object, no document content,
        no embedding, no credentials. See :func:`task_result` for what each field
        is and why it is there.

    Raises:
        IngestionTaskError: no document with that id exists, so there is no row
            to record an outcome on.
    """
    with session_scope(resources.session_factory) as session:
        tenant_id = _tenant_id_of(session, document_id)
        identity = AuthenticatedIdentity.for_tenant_service(tenant_id=tenant_id)

        logger.info(
            "worker task starting: document=%s tenant=%s task=%s",
            document_id,
            tenant_id,
            INGEST_DOCUMENT_TASK_NAME,
        )

        result = ingest_document(
            session,
            identity=identity,
            document_id=document_id,
            settings=resources.app_settings,
            storage=resources.storage,
            store=resources.store,
            provider=resources.provider,
        )

    return task_result(result, tenant_id=tenant_id)


def _tenant_id_of(session: Session, document_id: uuid.UUID) -> uuid.UUID:
    """Read the owning tenant off the document row, and refuse a document that is gone.

    The one lookup in this module that is **not** tenant-scoped, and it cannot
    be: there is no tenant yet to scope it with. That is not a hole in the
    isolation model, it is where the tenant comes *from*.

    Deliberately a bare primary-key lookup with no ``tenant_id`` predicate,
    because adding one would require already knowing the answer. What makes it
    safe is everything downstream of it:

    * the value returned is used to build an identity, never to open a resource —
      no store, no path and no collection is named from it here;
    * :func:`ingest_document` immediately re-reads the document through
      :func:`~app.services.documents.get_document_for_ingestion`, which applies
      the tenant predicate, and raises
      :class:`~app.services.documents.DocumentNotFoundError` if the two
      disagree;
    * nothing the caller controls arrives alongside the id, so there is no
      second value for this to disagree *with*.

    A malicious or buggy enqueuer therefore learns the ``tenant_id`` of a
    document id they already possessed — a fact they could equally obtain from
    any legitimate document listing they were authorised to see — and gains
    nothing they did not have, because the id alone is sufficient to name the
    document and the tenant was never the secret.

    Args:
        session: an open session, committed by the caller.
        document_id: the document to look up.

    Returns:
        The row's ``tenant_id``.

    Raises:
        IngestionTaskError: no such row.
    """
    tenant_id = session.scalar(
        select(Document.tenant_id).where(Document.id == document_id)
    )
    if tenant_id is None:
        logger.error(
            "worker task cannot start: document=%s does not exist, so there is no "
            "row to record an outcome on",
            document_id,
        )
        raise IngestionTaskError(
            f"document {document_id} does not exist, so there is nothing to ingest"
        )
    return tenant_id


def task_result(
    result: IngestionResult, *, tenant_id: uuid.UUID
) -> dict[str, object]:
    """Convert a pipeline outcome into the task's wire format.

    A plain :class:`dict` of JSON-serialisable scalars, and deliberately not the
    :class:`~app.services.ingestion.IngestionResult` dataclass: that object is a
    Python value with a nested ``enum.StrEnum``, and handing it back as a Celery
    result would either need a serialiser this project refuses to enable (see
    :mod:`app.workers.celery_app` on pickle) or silently depend on Celery's
    encoder. A ``dict`` of strings, ints and ``None`` is what the worker's
    JSON-only policy can actually carry.

    What is deliberately absent: document text, extracted chunks, embeddings,
    vectors, the storage key, the filename, and any credential. A result sits in
    the broker's result backend keyed by task id, and "everything the pipeline
    produced" is not a thing that should be recoverable from there.

    ``error`` **is** included, and the reason is that it is already public:
    :func:`~app.services.ingestion._failure_message` builds it from stage names,
    exception class names and content-free detail, and it is returned verbatim by
    ``GET /admin/documents/{id}/status`` to administrators. Returning it here
    tells an operator why a task failed without a second query, and publishes
    nothing an administrator could not already read.

    Args:
        result: what :func:`ingest_document` returned.
        tenant_id: the tenant derived from the document row before the run. Passed
            in rather than recovered from ``result`` because
            :class:`IngestionResult` does not carry one — and reporting it *here*,
            where it was derived, is what lets a test compare the tenant the work
            ran in against the tenant on the document it was given, instead of
            trusting that the two match.

    Returns:
        A JSON-serialisable mapping. See the module docstring for what is absent.
    """
    return {
        "document_id": str(result.document_id),
        "tenant_id": str(tenant_id),
        "status": str(result.status.value),
        "stage": str(result.stage.value),
        "chunk_count": result.chunk_count,
        "collection": result.collection,
        "error": result.error,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
    }


def _retry_backoff_seconds(retries_so_far: int) -> float:
    """Seconds to wait before the next attempt, from the retry constants.

    Exponential: approximately 30s, 60s and 120s before retries one, two and
    three, each plus a random jitter of up to
    :data:`RETRY_BACKOFF_JITTER_FRACTION` of the rung — so a burst of failures
    is retried apart rather than in lockstep.

    Args:
        retries_so_far: ``self.request.retries`` at failure time — how many
            retries have already happened. The next attempt is therefore the next
            rung of the exponent.
    """
    base = RETRY_BACKOFF_BASE_SECONDS * (
        RETRY_BACKOFF_MULTIPLIER ** retries_so_far
    )
    return base + random.uniform(0.0, RETRY_BACKOFF_JITTER_FRACTION * base)


def _record_retry_exhaustion(
    document_id: uuid.UUID, cause: BaseException, resources: IngestionResources
) -> dict[str, object]:
    """Record the document ``failed`` after every retry was spent, and report it.

    Called in place of a further retry, so it is the last step of the retry
    policy. The row is the record of truth, and the pipeline's last attempt left
    it ``processing`` (its session rolled back) because it raised before it could
    record anything itself.

    The message is the same text the pipeline would have recorded — for a
    :class:`~app.services.ingestion.TransientIngestionError` it was built by
    :func:`~app.services.ingestion._failure_message` when the exception was
    raised; for a :class:`sqlalchemy.exc.SQLAlchemyError` it is the class name
    alone — so ``documents.error`` stays stage-tagged and content-free no matter
    who wrote it. A failure here (the database is unhappy *as well*) is left to
    propagate: the task fails loudly, the row stays ``processing``, and
    reconciling it is the deferred sweep's work.

    Args:
        document_id: which document.
        cause: the exception that used up the last retry.
        resources: the same collaborators the pipeline ran with.

    Returns:
        A :func:`task_result`-shaped mapping with ``status`` = ``failed``.

    Raises:
        IngestionTaskError: the row is gone, so there is nothing to record.
    """
    if isinstance(cause, TransientIngestionError):
        stage = cause.stage
        message = cause.message
    else:
        stage = IngestionStage.RESOLVE
        message = (
            f"ingestion failed at the {stage} stage ({type(cause).__name__})"
        )

    with session_scope(resources.session_factory) as session:
        tenant_id = _tenant_id_of(session, document_id)
        identity = AuthenticatedIdentity.for_tenant_service(tenant_id=tenant_id)
        record_ingestion_outcome(
            session,
            identity=identity,
            document_id=document_id,
            status=DocumentStatus.FAILED,
            error=message,
        )

    return task_result(
        IngestionResult(
            document_id=document_id,
            status=DocumentStatus.FAILED,
            chunk_count=None,
            stage=stage,
            error=message,
            collection=None,
            elapsed_seconds=0.0,
        ),
        tenant_id=tenant_id,
    )


@celery_app.task(name=INGEST_DOCUMENT_TASK_NAME, bind=True)
def ingest_document_task(self: Task, document_id: str) -> dict[str, object]:
    """Ingest one document by id. The worker's entry point.

    Named with a ``_task`` suffix rather than shadowing the imported
    :func:`~app.services.ingestion.ingest_document`. The two are different things —
    one is the pipeline, one is a message handler that calls it — and the name a
    reader sees in a traceback should not depend on which module it came from.
    The *registered* name is :data:`INGEST_DOCUMENT_TASK_NAME` either way, which is
    the only spelling that appears on the wire.

    Registered as :data:`INGEST_DOCUMENT_TASK_NAME` on the project Celery app, so
    the module has to be imported for the task to exist — which is why a worker is
    started with ``-I app.workers.ingestion``, and why :func:`enqueue_ingestion`,
    the function the upload endpoint calls, lives in the same module: importing
    it registers the task.

    All this function is: parse the wire format, build the collaborators, hand
    off, and apply the retry policy. Every property worth testing — tenant
    derivation, the missing-document case, the isolation argument, the shape of
    the result — lives in :func:`ingest_stored_document`, which takes its
    collaborators as parameters and can therefore be tested with no broker and no
    network. The retry decision is layered on top of it here, not inside it.

    Retrying is explicit and bounded. The task is ``bind=True`` so it can retry
    itself; only :data:`RETRYABLE_TASK_FAILURES` is retried — the transient
    conditions the pipeline lifts out of its own failure record — and only up to
    :data:`MAX_RETRIES` times, spaced by :func:`_retry_backoff_seconds`
    (exponential, with jitter). Every retry re-publishes the task with its
    **current message unchanged**: ``self.retry`` is given no ``args`` or
    ``kwargs``, so the contract stays ``tenantmind.ingest_document(document_id)``
    — no tenant, no identity, no content — on every attempt. When the budget is
    spent, :func:`_record_retry_exhaustion` records the document ``failed`` and
    this function returns the same shape as any other outcome.

    What is **not** retried — on purpose: :class:`IngestionTaskError` (a missing
    row will not come back), parse and configuration failures (permanent,
    already recorded ``failed`` by the pipeline), and any exception outside the
    tuple, which is a programming bug and must surface as a task failure, once,
    rather than being retried as if it were a blip. ``autoretry_for`` and
    ``acks_late`` are absent for the same reason: a blanket "retry everything"
    switch cannot tell a provider blip from a bug, and a delivery-guarantee
    change alters every task, not just the transient ones.

    Args:
        document_id: the document's UUID as a string, which is how it arrives on
            the wire — JSON has no UUID type. The **only** argument. There is no
            ``tenant_id`` parameter for a caller to fill in with a tenant of their
            own choosing, and no ``**kwargs`` for one to arrive under a different
            name, which is what makes ``(document_id=A, tenant_id=B)``
            unrepresentable rather than merely rejected.

    Returns:
        The mapping :func:`task_result` builds: JSON-serialisable scalars, with no
        document content, no embeddings and no ORM object.

    Raises:
        ValueError: ``document_id`` is not a UUID. Raised before anything is
            opened, so a malformed message costs no database round trip.
        IngestionTaskError: no such document.
    """
    parsed = uuid.UUID(document_id)
    resources = build_ingestion_resources(settings)
    try:
        return ingest_stored_document(parsed, resources)
    except RETRYABLE_TASK_FAILURES as exc:
        if self.request.retries < MAX_RETRIES:
            raise self.retry(
                exc=exc, countdown=_retry_backoff_seconds(self.request.retries)
            )
        return _record_retry_exhaustion(parsed, exc, resources)
