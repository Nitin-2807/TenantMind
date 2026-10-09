"""Ingestion orchestration: stored document → embedded chunks → Qdrant points.

What this module is
-------------------
The one place the RAG stages are run in order and told what happened. Every stage
lives in :mod:`app.rag`; every write to the vector store lives in
:mod:`app.storage.qdrant`; what is here is sequencing, the stage-tagged failure
record, and the tenant-scoped resolution of *which* document is being ingested.

Deliberately not a method on a router
-------------------------------------
It takes a :class:`~sqlalchemy.orm.Session`, an
:class:`~app.core.identity.AuthenticatedIdentity`, and plain configuration objects.
It imports nothing from :mod:`fastapi` and takes no request object, so it is callable
from a Celery task, a management command, a test, or a REPL with equal fidelity.
Phase 5's worker will call exactly this function. That is the reason for the
separation, and the reason to be strict about it: a pipeline that needs a request
object cannot be moved off the request path, and moving it is the whole point of
wanting ingestion to be asynchronous.

No endpoint calls this
----------------------
Not one. ``POST /documents`` returns ``202`` after storing the bytes, and nothing in
this phase moves a document on. PMP §10's "trigger the ingestion pipeline" is
satisfied by the pipeline existing and being reachable from the service layer, not by
exposing an endpoint that starts it — and a ``POST /documents/{id}/ingest`` would be
an API surface the PMP does not describe. See the Phase 2.5 entry in
``PROJECT_EXECUTION_LOG.md``.

The stages
----------
Most are module-level functions taking explicit arguments and returning plain values,
so each is testable on its own — a chunking bug can be found without a provider, and a
width mismatch without a document on disk.

=========================  ===============================================
:func:`stage_read`         bounded read of the stored bytes
:func:`stage_parse`        format-specific extraction into pages
:func:`stage_chunk`        per-page clean and split into bounded chunks
:func:`stage_embed`        batched embedding
:func:`stage_build`        chunk + vector → :class:`~app.rag.vectors.VectorPoint`
:func:`stage_upsert`       batched point writes
:func:`stage_prune`        delete points from a previous, longer ingest
=========================  ===============================================

Chunking is per page
--------------------
PMP §13 requires a ``page`` in every point's payload, and that field exists so a
retrieved chunk can be cited. A chunk cut from a whole document concatenated together
can straddle page 4 and page 5, and then it has no honest page number — it would have
to report the page it mostly came from, which is a guess dressed as a fact.

Recovering the page from the chunk's offset instead does not work here, because
cleaning is not length-preserving: folding unicode spaces, collapsing whitespace runs
and dropping invisible characters all shorten the string before the page boundaries
they were measured against, so an offset computed on cleaned text addresses the wrong
page. The error would be silent, plausible, and growing with document size. See
:class:`~app.rag.parsers.ParsedDocument`, which says the same thing from the other
side.

So each page is cleaned and chunked on its own, and the page number travels with the
chunk. **The cost, stated plainly:** a page holding less than
:attr:`Settings.chunk_size_chars` yields one short chunk rather than being padded up to
size. For a slide deck that means roughly one chunk per slide, which is the right
answer anyway; for a text file there is only one page, so nothing changes. The trade is
exact citations in exchange for variable chunk length, and PMP §20 lists citation as a
requirement while chunk length is an implementation detail — so it is the trade this
project should make.

Chunk indices are document-wide
-------------------------------
``chunk_index`` counts across the whole document, not within a page. This is not a
detail: :func:`~app.rag.vectors.point_id` derives a point's identity from
``(tenant_id, document_id, chunk_index)``, so a per-page index would give page 1's
first chunk and page 2's first chunk **the same id**, and the second one written would
silently overwrite the first. Document-wide numbering makes the id unique by
construction.

Failure handling
----------------
Every stage's exception is caught, and the document is recorded ``failed`` with a
message naming **the stage** and the exception's **class**. Three rules, each
load-bearing.

**Never ``ready`` on failure.** :attr:`IngestionResult.status` is derived from whether
the vector write completed, and :func:`_record_success` is the only code that assigns
``READY``. There is no path that records ``ready`` for a document whose points are
missing.

**Never tenant content in the error.** ``documents.error`` is returned verbatim by
``GET /admin/documents/{id}/status``. A parser message can quote the document's bytes —
:mod:`pypdf` does exactly that — and a provider's error can quote the request, which is
the chunk text. So the recorded message is assembled from stage names, exception class
names, and the messages of exception types this project constructs itself from
content-free material. See :func:`_failure_message` for exactly which is which, and
why the embedding provider's messages are excluded.

**Temporary infrastructure failures are the caller's to retry.** Three conditions —
an :class:`~app.rag.embeddings.EmbeddingError`, a
:class:`~app.storage.qdrant.VectorStoreError`, or an :class:`OSError` — are raised
again as :class:`TransientIngestionError` instead of being recorded ``failed`` on
the spot. A provider that is briefly down and a store that refused a write are
conditions that can clear by themselves, and only the caller — Phase 5's worker —
knows how many retries it has budgeted. The row is left ``processing``, so the
retry needs nothing here undone, and the worker records ``failed`` itself when its
budget runs out. The tuple that wins is :data:`RETRYABLE_INGESTION_FAILURES`,
checked **first** inside the ``except``. Deliberately still recorded immediately:
the permanent subclasses named in :data:`PERMANENT_RETRYABLE_SUBCLASSES` (a
collaborator answering at the wrong width — retrying cannot change a width), and
every non-retryable fault listed in :data:`PIPELINE_FAILURES`.

**Leave the vector store consistent where that is cheap.** Points are upserted
*before* stale ones are pruned, so a shrinking re-ingest is briefly over-complete
rather than momentarily absent: a query running concurrently sees a document with some
surplus chunks rather than a document that briefly appears deleted. What is **not**
claimed: PostgreSQL and Qdrant do not share a transaction, and no ordering of writes
makes them. If the process dies between the upsert and the status write, the document
is left ``processing`` with correct points. Re-running :func:`ingest_document`
converges it — which is what the deterministic point ids in :mod:`app.rag.vectors`
buy — and building a reconciliation sweep is Phase 5's work, not this phase's.

Idempotency, and its stated limit
---------------------------------
Calling :func:`ingest_document` twice with the same document and the same bytes writes
the same points twice, which is one write rather than two. That is the whole of the
guarantee, and it holds because point ids are derived from
``(tenant_id, document_id, chunk_index)``.

It is **not** document versioning. If the stored text changed, chunk *N* is overwritten
by the new text of chunk *N* even when the boundaries have moved, so a chunk from the
old text at index 5 is replaced by a different chunk's text at index 5. Both versions
cannot be held at once. PMP §28 lists automatic document versioning as a later
enhancement; this phase records the limitation rather than pre-building it.

A document with no extractable text
-----------------------------------
Recorded ``failed`` with :class:`NoExtractableTextError`, not ``ready`` with a chunk
count of zero. A scanned PDF with no text layer parses successfully and yields
nothing, and reporting that as success would mark a document searchable that no query
can retrieve anything from. ``status`` is what distinguishes the two situations a zero
count could otherwise confuse — ``processing`` with ``chunk_count = 0`` means the
pipeline has not finished, ``failed`` with ``chunk_count = 0`` means it ran and found
nothing — so no extra column or sentinel is needed for it.
"""

from __future__ import annotations

import enum
import logging
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final, TypeVar

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.identity import AuthenticatedIdentity
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.rag.chunking import Chunk, chunk_text
from app.rag.cleaning import clean_text, cleaned_text_is_empty
from app.rag.embeddings import (
    MAX_BATCH_SIZE,
    EmbeddingDimensionMismatch,
    EmbeddingError,
    EmbeddingProvider,
)
from app.rag.parsers import DocumentParseError, ParsedDocument, resolve_parser
from app.rag.vectors import VectorPoint, payload_for, point_id
from app.services.documents import (
    DocumentNotFoundError,
    get_document_for_ingestion,
    record_ingestion_outcome,
)
from app.storage.local import (
    FileTooLargeError,
    LocalFileStore,
    StorageContainmentError,
    StorageKeyError,
    StoredObjectMissingError,
)
from app.storage.qdrant import (
    QdrantVectorStore,
    VectorDimensionMismatch,
    VectorStoreError,
)

logger = logging.getLogger(__name__)

#: Element type of the sequences :func:`_batched` slices.
_T = TypeVar("_T")

#: How many points one Qdrant upsert carries.
#:
#: A payload-shape decision. A 10 MB PDF at :attr:`Settings.chunk_size_chars` yields on
#: the order of ten thousand chunks, and sending them in one request is one very large
#: JSON body that either succeeds or times out having achieved nothing visible.
#: Batching bounds how much work a failed write has to repeat. Kept well under
#: :data:`~app.rag.embeddings.MAX_BATCH_SIZE` so both bounds express the same intent
#: from different ends: no single request may scale with the size of an upload.
VECTOR_WRITE_BATCH_SIZE: Final[int] = 256

#: The longest error message this module will record.
#:
#: Bounds the string handed to :func:`~app.services.documents.record_ingestion_outcome`
#: before the column's own 2000-character limit applies. Every message here is built
#: from stage and exception names, so this ceiling is not reached in practice; it exists
#: so that a future stage which *does* interpolate something unbounded is truncated here
#: rather than rejected at flush time, where the failure reads as a server fault.
MAX_ERROR_CHARS: Final[int] = 500


class IngestionStage(enum.StrEnum):
    """Every point at which the pipeline can stop.

    An :class:`enum.StrEnum` for the reason :class:`app.models.user.UserRole` is, and
    recorded in the error message as its plain string. Two consumers depend on these
    spellings: the message an operator reads, and the tests asserting *which* stage
    failed. A :class:`ValueError` from the chunker and a :class:`ValueError` from the
    payload builder would otherwise produce the same recorded string, and the stage is
    what tells a reader which module to open.

    ``RECORD`` is both the final stage and the value reported on success, so a caller
    that did not inspect the stages can still tell a completed run from a failed one by
    comparing ``status`` alone.
    """

    RESOLVE = "resolve"
    READ = "read"
    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"
    BUILD = "build"
    COLLECTION = "collection"
    UPSERT = "upsert"
    PRUNE = "prune"
    RECORD = "record"


class IngestionError(RuntimeError):
    """A condition that no amount of retrying will fix.

    Raised rather than recorded on a document row, and only for contradictions in the
    *configuration* — principally a provider whose declared width disagrees with
    :attr:`Settings.embedding_dimension`. Every other failure is absorbed into a
    ``failed`` row, because that is what PMP §20 asks for and because a caller has
    nothing useful to do with "the provider is unreachable" beyond reading the row.
    """


class NoExtractableTextError(IngestionError):
    """The document parsed successfully and yielded nothing indexable.

    A distinct type rather than a message, because this is a **permanent** condition —
    re-running the pipeline cannot change it and neither a retry nor a fallback will
    help — while every other stage failure is at least conceivably transient. A caller
    building a retry affordance can therefore branch on the type instead of matching
    on prose.

    Declared here rather than in :mod:`app.rag.parsers` because the decision needs the
    *cleaner* as well as the parser: a file containing nothing but whitespace parses
    perfectly and is faithfully extracted. "Nothing indexable" is a pipeline-level
    judgement; putting it in the parser layer would mean either raising from seven
    parsers or checking only raw emptiness and missing the whitespace case.
    """


class TransientIngestionError(RuntimeError):
    """A stage failed from a condition that is probably temporary.

    Raised — in place of a ``failed`` record — when a stage fails with an
    :class:`~app.rag.embeddings.EmbeddingError`, a
    :class:`~app.storage.qdrant.VectorStoreError` or an :class:`OSError`, the
    :data:`RETRYABLE_INGESTION_FAILURES` set. Those are infrastructure
    conditions — a provider that is briefly down, a store that refused a write, a
    filesystem that hiccuped — and whether to try again is the caller's decision,
    made with information this module does not have (how many retries it has
    budgeted, how long it is willing to wait). The document's row is left
    ``processing``: nothing here has recorded a state that a retry would need to
    undo.

    The failing stage and the exact message this module would have recorded travel
    with the exception, so a caller that eventually gives up writes the same
    stage-tagged, content-free text to ``documents.error`` that an immediate
    failure would have written — see :func:`_failure_message` for why that text
    is safe to store. ``__cause__`` holds the original exception, and
    ``str(exception)`` is the original message for the logs.

    Deliberately **not** raised for permanent or programming conditions — a parse
    failure, a missing stored object, a configuration contradiction, a collaborator
    answering at the wrong width, or anything unanticipated. Those still become
    immediate ``failed`` records or propagate as bugs, exactly as before.

    Attributes:
        stage: where the pipeline stopped.
        message: what :func:`_failure_message` would have recorded for this
            failure. The text a retry-exhausted caller writes to the row.
        cause: the original exception.
    """

    def __init__(self, stage: IngestionStage, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.stage = stage
        self.message = _failure_message(stage, cause)
        self.cause = cause


#: Every exception type an ingestion stage can legitimately raise.
#:
#: :func:`ingest_document` catches exactly this tuple — **not** :class:`Exception` —
#: and the choice is the point rather than an inconvenience.
#:
#: Written out, it is the union of the ``Raises:`` sections of the seven stage
#: functions below, and it can be checked against them: every stage documents what it
#: raises, and this is that list in one place. Adding a new failure mode to a stage
#: means adding its type here, which is a line of work.
#:
#: What it buys is that a **bug in this module** — an :class:`AttributeError`, a
#: :class:`TypeError` from a changed signature — propagates instead of being written
#: into a tenant's document row as though the tenant's file were at fault. A wrong
#: ``Document`` attribute becomes a loud traceback in the pipeline's own logs; caught
#: broadly it would become a permanent ``failed`` row on a document nobody can fix,
#: discovered by a user who did nothing wrong and has no way to clear it.
#:
#: The entry that catches everything is :class:`ValueError`, which is what the chunker,
#: the payload builder and the point-id builder all raise for invalid arguments. Its
#: own subclasses :class:`~app.rag.parsers.DocumentParseError` and
#: :class:`~app.storage.local.StorageKeyError` are therefore listed only for the reader,
#: and the order below groups by *cause* rather than alphabetically.
PIPELINE_FAILURES: Final[tuple[type[BaseException], ...]] = (
    # A file that could not be parsed or read.
    DocumentParseError,
    ValueError,
    OSError,
    StorageKeyError,
    StoredObjectMissingError,
    StorageContainmentError,
    # A provider, a vector store, or this module's own verdict.
    EmbeddingError,
    VectorStoreError,
    IngestionError,
)

#: The subset of :data:`PIPELINE_FAILURES` worth trying again.
#:
#: A provider's refusal, a store's refusal and a filesystem error are conditions that
#: can clear by themselves. Every other pipeline failure names something retrying
#: cannot change: a file that does not parse, a configuration contradiction, a
#: missing stored object. The ``except`` in :func:`ingest_document` checks this
#: tuple **before** recording and raises the condition as
#: :class:`TransientIngestionError`, leaving the row ``processing`` so Phase 5's
#: worker — the only caller with a retry budget — decides how many times "try
#: again" means.
RETRYABLE_INGESTION_FAILURES: Final[tuple[type[BaseException], ...]] = (
    EmbeddingError,
    VectorStoreError,
    OSError,
)

#: Subclasses of :data:`RETRYABLE_INGESTION_FAILURES` that are **not** temporary.
#:
#: The two mismatch types are configuration-class faults: the collaborator is
#: answering at the wrong width, and retrying cannot change a width. They stay
#: immediate ``failed`` records despite inheriting from a retryable base, which is
#: why the ``except`` checks :func:`isinstance` for them rather than matching the
#: retryable tuple alone.
PERMANENT_RETRYABLE_SUBCLASSES: Final[tuple[type[BaseException], ...]] = (
    EmbeddingDimensionMismatch,
    VectorDimensionMismatch,
)

#: What :func:`~app.services.documents.record_ingestion_outcome` can raise.
#:
#: Narrower than :data:`PIPELINE_FAILURES` because its failure modes are known and few:
#: the two argument validations it performs (both :class:`ValueError`), and whatever
#: the session raises at flush time. A
#: :class:`~app.services.documents.DocumentNotFoundError` is deliberately **not**
#: caught, so a document deleted mid-ingestion propagates and the caller is told it
#: is gone — which is the truth — rather than receiving a failure report about a row
#: that no longer exists.
RECORDING_FAILURES: Final[tuple[type[BaseException], ...]] = (
    SQLAlchemyError,
    ValueError,
)


@dataclass(frozen=True, slots=True)
class PreparedChunk:
    """One chunk, cleaned, located, and numbered — everything but its vector.

    Attributes:
        chunk: the chunk from :mod:`app.rag.chunking`. Its own ``index`` is its
            position *within its page* and is never used as a point identity; see the
            module docstring on why that would collide.
        page: the 1-based page, or ``None`` for an unpaged format.
        chunk_index: the document-wide ordinal, which **is** the point identity input.
    """

    chunk: Chunk
    page: int | None
    chunk_index: int

    @property
    def text(self) -> str:
        """The chunk's text: what gets embedded and what goes in the payload."""
        return self.chunk.text


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """What one call to :func:`ingest_document` did.

    Returned rather than raised on failure, because the document's row is the durable
    record and the result mirrors it. A caller that wants to know what happened can read
    one value instead of re-querying the row, and a caller that does not care can ignore
    the return entirely.

    Attributes:
        document_id: the document that was ingested.
        status: what was recorded. ``READY`` only when every point was written *and*
            the pruning converged.
        chunk_count: how many chunks were indexed, or ``None`` on failure. ``None``
            means "this run indexed nothing", which the column cannot express: it is
            ``NOT NULL DEFAULT 0``, so zero is indistinguishable from "never ran". The
            result keeps the distinction the column loses.
        stage: where the pipeline stopped. :attr:`IngestionStage.RECORD` on success.
        error: the recorded message, or ``None``. Stage-tagged, and free of document
            content except for a validated filename.
        collection: the Qdrant collection written to, or ``None`` if the run stopped
            before that stage.
        elapsed_seconds: wall-clock duration, for the caller's own log line. Never
            written to the database.
    """

    document_id: uuid.UUID
    status: DocumentStatus
    chunk_count: int | None
    stage: IngestionStage
    error: str | None
    collection: str | None
    elapsed_seconds: float

    @property
    def succeeded(self) -> bool:
        """Whether the document ended up ``ready``."""
        return self.status is DocumentStatus.READY


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def stage_read(
    storage: LocalFileStore, document: Document, *, max_bytes: int
) -> bytes:
    """Read the stored bytes of ``document``, refusing to exceed ``max_bytes``.

    The path comes from ``document.storage_key``, which the storage layer minted and
    the database holds. It is never taken from a caller, which is what makes "never
    fetch a client-supplied path" structurally true rather than a rule somebody has to
    remember: there is no parameter here for a path to arrive in, and
    ``storage_key`` is a :data:`~app.storage.local.STORAGE_KEY_PATTERN`-validated token
    that cannot contain a separator.

    Args:
        max_bytes: the configured upload limit. **Not** the document's own size, and
            that is deliberate. Reading with the document's size as the bound would mean
            a file that had somehow grown is read at whatever size it now is — the
            bound would follow the thing it is supposed to constrain. Reading with the
            configured limit means a document stored under a now-lowered limit fails
            loudly and visibly instead of quietly allocating whatever it happens to be.

    Raises:
        StoredObjectMissingError: the row references a file that is not there.
        FileTooLargeError: the stored file is larger than ``max_bytes``. Effectively
            unreachable, since nothing in the application writes over an existing key —
            kept so that an externally modified file is refused rather than indexed.
    """
    return storage.read_object(
        document.tenant_id, document.storage_key, max_bytes=max_bytes
    )


def stage_parse(document: Document, data: bytes) -> ParsedDocument:
    """Extract ``data`` into pages, using the parser its stored filename selects.

    Resolved from the document's **stored** filename, which is the same value the
    upload validator checked the bytes against — so the format used to parse a document
    is the format it was accepted as. Resolving from anything caller-supplied would be
    the client-controlled parser selection this module must not have.

    Raises:
        DocumentParseError: the bytes are not readable as this format. Every message
            this project constructs for that case names a format, a limit or an
            exception class, never the content — see :mod:`app.rag.parsers`, which is
            where that promise is made and why :mod:`pypdf` required it.
    """
    return resolve_parser(document.filename).parse(data)


def stage_chunk(parsed: ParsedDocument, settings: Settings) -> list[PreparedChunk]:
    """Clean and chunk every page, keeping each chunk's page and document-wide index.

    Cleans **per page**, not on :attr:`~app.rag.parsers.ParsedDocument.full_text`, for
    the reason in the module docstring: the page then travels with the chunk instead of
    being computed from an offset that cleaning has invalidated.

    A page blank after cleaning contributes nothing rather than an empty chunk. Blank
    pages are ordinary — the last page of a PDF is often whitespace, and a slide may
    hold only a picture — and an empty chunk would put a point in the collection that no
    query can usefully match and no citation can quote.

    Returns:
        Chunks in document order: page by page, and within a page in offset order. The
        positions in this list are the document-wide ``chunk_index`` values, so for a
        given document and given bytes they are stable — which is what makes
        re-ingestion an overwrite rather than a second copy.

    Raises:
        Nothing. Chunk sizes come from validated settings, and
        :func:`~app.rag.chunking.chunk_text` refuses an inconsistent pair, which
        :class:`~app.core.config.Settings` cannot be constructed with.
    """
    prepared: list[PreparedChunk] = []
    for page in parsed.pages:
        cleaned = clean_text(page.text)
        if cleaned_text_is_empty(cleaned):
            continue
        for chunk in chunk_text(
            cleaned,
            chunk_size=settings.chunk_size_chars,
            chunk_overlap=settings.chunk_overlap_chars,
        ):
            prepared.append(
                PreparedChunk(
                    chunk=chunk,
                    page=page.number,
                    chunk_index=len(prepared),
                )
            )
    return prepared


def stage_embed(
    provider: EmbeddingProvider, prepared: Sequence[PreparedChunk], *, batch_size: int
) -> list[list[float]]:
    """Embed every chunk, in batches, and return one vector per chunk in order.

    Batched because a single request carries a bounded number of texts
    (:data:`~app.rag.embeddings.MAX_BATCH_SIZE`) and a ten-thousand-chunk document
    cannot be embedded in one call. ``batch_size`` is clamped to the provider's limit
    so that raising :attr:`Settings.embedding_batch_size` cannot produce a request the
    provider refuses for a reason that reads like an outage.

    Raises:
        ValueError: ``prepared`` is empty. An empty batch would be a call that cannot
            succeed, and the caller has already established it is non-empty — so this
            is a bug guard rather than a supported case.
        EmbeddingError: the provider could not embed some batch. Propagated rather than
            retried: retry policy belongs to the caller (Phase 5), and a silent retry
            here would end in a ``failed`` row with no indication that retrying is what
            would have helped.
    """
    if not prepared:
        raise ValueError("stage_embed requires at least one chunk")

    texts = [item.text for item in prepared]
    batch_size = max(1, min(batch_size, MAX_BATCH_SIZE))

    vectors: list[list[float]] = []
    for batch in _batched(texts, batch_size):
        vectors.extend(provider.embed_texts(list(batch)))
    return vectors


def stage_build(
    document: Document,
    prepared: Sequence[PreparedChunk],
    vectors: Sequence[Sequence[float]],
) -> list[VectorPoint]:
    """Pair each chunk with its vector and build the point that represents it.

    Both the id and the payload are pure functions of the document row, the chunk index
    and the vector, so building the same document twice yields the same points. That is
    the whole of this phase's idempotency, and it is why no clock, counter or random
    source appears in this function.

    Args:
        vectors: one vector per chunk, in the same order.

    Returns:
        One point per chunk, in the same order.

    Raises:
        ValueError: ``vectors`` and ``prepared`` differ in length, or a chunk's page is
            not a positive 1-based number or ``None``. Both are checked because the
            alternative is a payload holding something that cannot be true — a chunk
            whose page is ``0``, or a vector silently paired with the wrong chunk
            because one of the two lists ran out first.
    """
    if len(vectors) != len(prepared):
        raise ValueError(
            f"prepared {len(prepared)} chunk(s) but received {len(vectors)} vector(s); "
            "the two must correspond one to one"
        )

    return [
        VectorPoint(
            id=point_id(
                tenant_id=document.tenant_id,
                document_id=document.id,
                chunk_index=item.chunk_index,
            ),
            vector=list(vector),
            payload=payload_for(
                tenant_id=document.tenant_id,
                document_id=document.id,
                chunk_index=item.chunk_index,
                page=item.page,
                text=item.text,
                allowed_roles=document.allowed_roles,
                filename=document.filename,
            ),
        )
        for item, vector in zip(prepared, vectors, strict=True)
    ]


def stage_upsert(
    store: QdrantVectorStore,
    document: Document,
    points: Sequence[VectorPoint],
    *,
    batch_size: int = VECTOR_WRITE_BATCH_SIZE,
) -> int:
    """Write every point, in batches, and return how many were written.

    Raises:
        ValueError: ``batch_size`` is not positive.
        VectorStoreError: a batch could not be written. The message names the batch
            position and the underlying exception's class, never the points — which are
            tenant content.
    """
    if batch_size < 1:
        # Checked here rather than left to :func:`_batched`, which does check it. The
        # ceiling arithmetic below runs first and would raise ``ZeroDivisionError`` —
        # an exception this function does not document and a caller cannot meaningfully
        # handle, replacing the one clear message naming the offending argument.
        raise ValueError("batch size must be at least 1")
    total_batches = -(-len(points) // batch_size)
    written = 0
    for number, points_in_batch in enumerate(_batched(points, batch_size), start=1):
        try:
            written += store.upsert_points(document.tenant_id, list(points_in_batch))
        except VectorStoreError as exc:
            raise VectorStoreError(
                f"vector write failed on batch {number} of {total_batches} "
                f"({type(exc).__name__})"
            ) from exc
    return written


def stage_prune(
    store: QdrantVectorStore, document: Document, *, keep_below_index: int
) -> int:
    """Remove this document's points left over from a previous, longer ingest.

    Called on every ingest, including a first one, where ``keep_below_index`` equals
    the number of chunks just written and the filter therefore matches nothing. That
    costs one indexed range query on a collection that is nearly empty, and it buys the
    absence of a second source of truth recording "has this document been ingested
    before" — which would have to be kept consistent with Qdrant by hand.

    Pruning runs *after* the upsert, never before. Deleting first would leave a window
    in which the document has no points at all, so a query running concurrently sees it
    as absent rather than as stale — and "absent" is a much worse thing to show a user
    than "slightly out of date".

    Returns:
        How many points the document holds afterwards, which should be exactly
        ``keep_below_index``. The caller checks that rather than assuming it, because a
        mismatch means Qdrant holds points this run did not write.
    """
    return store.delete_stale_points(
        document.tenant_id, document.id, keep_below_index=keep_below_index
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def ingest_document(
    session: Session,
    *,
    identity: AuthenticatedIdentity,
    document_id: uuid.UUID,
    settings: Settings,
    storage: LocalFileStore,
    store: QdrantVectorStore,
    provider: EmbeddingProvider,
) -> IngestionResult:
    """Ingest one document end to end and record the outcome on its row.

    Args:
        session: used only for the tenant-scoped lookup and the outcome write. The
            pipeline touches no other table, so nothing here can modify a document other
            than the one named.
        identity: the authenticated caller, which supplies the tenant. **No tenant id
            is accepted as an argument.** That is the single most important signature
            decision in this module: a caller cannot name a tenant, so it cannot ingest
            into another tenant's collection even by mistake, and the tenant used for
            the database lookup, the storage path and the collection name is the same
            value throughout.
        document_id: which document, within that tenant.
        settings: application configuration — the only source of chunk size, batch size
            and vector width. Nothing here reads the environment.
        storage: where the bytes are.
        store: the Qdrant store.
        provider: the embedding provider. A parameter rather than constructed from
            ``settings`` so the pipeline can be driven end to end without a network, and
            so Phase 5's worker can pass a provider configured with a longer timeout.

    Returns:
        The outcome, mirroring what was recorded on the row.

    Raises:
        DocumentNotFoundError: no such document **in this tenant** — which is also what
            a caller gets for a document in another tenant, so the cross-tenant probe
            returns 404 rather than 403 and the two are indistinguishable (PMP §7
            FR-04, §12).
        IngestionError: the provider's declared width disagrees with
            :attr:`Settings.embedding_dimension`. Checked before any work, because the
            alternative is embedding every chunk of a large document and only then
            discovering the vectors cannot be stored.
        TransientIngestionError: a stage failed from a condition
            :data:`RETRYABLE_INGESTION_FAILURES` classifies as temporary — an
            embedding provider, a vector store, or the filesystem. The row is left
            ``processing`` and the exception carries the stage and the message this
            run would have recorded; the caller owns how many times to try again.
            The permanent width-mismatch subclasses in
            :data:`PERMANENT_RETRYABLE_SUBCLASSES` are excluded and are recorded
            ``failed`` like every other non-retryable fault.

    Every other failure becomes a ``failed`` row and a returned
    :class:`IngestionResult`. That is PMP §20's "failures will be recorded in
    PostgreSQL" implemented as the *default* outcome rather than as something a stage
    has to remember to do.
    """
    check_provider_matches_configuration(settings, provider)

    document = get_document_for_ingestion(
        session, identity=identity, document_id=document_id
    )
    # The filename is deliberately not logged. It is tenant-supplied, and while
    # `filename` has passed the hygiene rules that strip control characters,
    # a log line is not the place a tenant's strings belong when the document id and
    # tenant id identify the job completely.
    logger.info(
        "ingestion starting: document=%s tenant=%s", document.id, document.tenant_id
    )

    started = time.monotonic()
    collection: str | None = None
    stage = IngestionStage.RESOLVE
    written = 0

    # Mark the document in flight before doing anything that can fail, so a crash cannot
    # leave a `ready` document whose indexed content no longer matches its bytes.
    # `record_ingestion_outcome` is the only writer of these three columns — it was
    # built for exactly this and is reachable from no route.
    record_ingestion_outcome(
        session,
        identity=identity,
        document_id=document_id,
        status=DocumentStatus.PROCESSING,
    )

    try:
        stage = IngestionStage.READ
        data = stage_read(
            storage,
            document,
            max_bytes=settings.max_upload_size_mb * 1024 * 1024,
        )

        stage = IngestionStage.PARSE
        parsed = stage_parse(document, data)
        data = b""  # release the bytes before the much larger embedding stage

        stage = IngestionStage.CHUNK
        prepared = stage_chunk(parsed, settings)
        parsed = ParsedDocument(pages=())
        if not prepared:
            raise NoExtractableTextError(
                "the document yielded no indexable text: it is empty, or contains only "
                "whitespace, images, or formulas. Indexing a scanned PDF needs OCR, "
                "which this phase does not perform"
            )

        # Build the search vector text for BM25 (PMP Phase 6) from the concatenated
        # chunk text before we clear the prepared list.
        search_vector_text = " ".join(chunk.text for chunk in prepared)

        stage = IngestionStage.EMBED
        vectors = stage_embed(provider, prepared, batch_size=settings.embedding_batch_size)

        stage = IngestionStage.BUILD
        points = stage_build(document, prepared, vectors)
        prepared, vectors = [], []

        stage = IngestionStage.COLLECTION
        collection = store.ensure_collection(document.tenant_id)

        stage = IngestionStage.UPSERT
        written = stage_upsert(store, document, points)

        # Store document chunks for BM25 search (PMP Phase 6).
        # Done after UPSERT so we only index chunks that were successfully written.
        _store_document_chunks(session, document, points=points)

        points = []

        stage = IngestionStage.PRUNE
        remaining = stage_prune(store, document, keep_below_index=written)
    except DocumentNotFoundError:
        # The document was deleted between the lookup above and the outcome write.
        # There is no row left to record anything on, and telling the caller the
        # pipeline failed on a document that no longer exists would be misleading.
        raise
    except PIPELINE_FAILURES as exc:
        # The tuple, not `Exception` — see :data:`PIPELINE_FAILURES`. A failure a stage
        # documented is recorded against the document; a failure nobody anticipated is
        # a bug in this module and propagates, so it cannot become a permanent `failed`
        # row on a tenant's file.
        if isinstance(exc, PERMANENT_RETRYABLE_SUBCLASSES):
            # A collaborator answering at the wrong width is a configuration-class
            # fault: retrying cannot change it, so it is recorded like any other
            # permanent failure even though its type inherits a retryable base.
            return _record_failure(
                session,
                identity=identity,
                document=document,
                stage=stage,
                cause=exc,
                collection=collection,
                elapsed=elapsed_since(started),
            )
        if isinstance(exc, RETRYABLE_INGESTION_FAILURES):
            # A provider, the vector store, or the filesystem reported something
            # worth trying again. Recording it `failed` on the spot would turn a
            # blip into a permanent state, so the row is left `processing` and the
            # condition is handed to the caller as :class:`TransientIngestionError`
            # — stage and recorded-message attached — and is the worker's retry
            # policy's to spend its budget on.
            raise TransientIngestionError(stage, exc) from exc
        return _record_failure(
            session,
            identity=identity,
            document=document,
            stage=stage,
            cause=exc,
            collection=collection,
            elapsed=elapsed_since(started),
        )

    return _record_success(
        session,
        identity=identity,
        document=document,
        written=written,
        remaining=remaining,
        collection=collection,
        elapsed=elapsed_since(started),
        search_vector_text=search_vector_text,
    )


def check_provider_matches_configuration(
    settings: Settings, provider: EmbeddingProvider
) -> None:
    """Refuse a provider whose declared width is not the configured vector width.

    Run before any work at all, and it is the reason the provider's
    :attr:`~app.rag.embeddings.EmbeddingProvider.dimensions` is declared rather than
    discovered. Qdrant collections cannot be resized, so a provider that answers at a
    different width than :attr:`Settings.embedding_dimension` means one of two things:
    the collection will be created at the wrong width, or the write will be rejected
    after every chunk has been embedded and billed.

    Neither is worth discovering at that point, and neither is recoverable by retrying —
    so it is a configuration error, raised as :class:`IngestionError` rather than
    recorded against a document that is perfectly fine.

    Raises:
        IngestionError: the two widths disagree.
    """
    if provider.dimensions != settings.embedding_dimension:
        raise IngestionError(
            f"the embedding provider declares {provider.dimensions}-dimensional "
            f"vectors but embedding_dimension is {settings.embedding_dimension}. "
            "Qdrant collections cannot be resized, so this must be fixed before any "
            "document is ingested rather than discovered when a write is rejected."
        )


# ---------------------------------------------------------------------------
# Outcome recording
# ---------------------------------------------------------------------------


def _record_success(
    session: Session,
    *,
    identity: AuthenticatedIdentity,
    document: Document,
    written: int,
    remaining: int,
    collection: str | None,
    elapsed: float,
    search_vector_text: str,
) -> IngestionResult:
    """Record ``ready`` with the chunk count and search vector.

    The only code in the project that assigns ``DocumentStatus.READY``. Deliberately
    outside the ``try`` that catches stage failures: a failure to write the success row
    is a database fault rather than a pipeline fault, and recording it as ``failed``
    would replace a clear "could not write" with a misleading "could not ingest" while
    the points — which *are* written — sit in Qdrant perfectly well.

    ``remaining`` is verified against ``written`` rather than assumed equal. They match
    when pruning worked; a mismatch means Qdrant holds points this ingestion did not
    write, which is a real condition an operator needs to see, not a number to paper
    over by writing the row anyway.
    """
    if remaining != written:
        raise IngestionError(
            f"the vector store holds {remaining} point(s) for this document but this "
            f"ingestion wrote {written}; pruning did not converge. Re-run the "
            "ingestion, and investigate before treating this document as indexed."
        )

    record_ingestion_outcome(
        session,
        identity=identity,
        document_id=document.id,
        status=DocumentStatus.READY,
        chunk_count=written,
        error=None,
        search_vector=search_vector_text,
    )
    logger.info(
        "ingestion complete: document=%s chunks=%d collection=%s in %.2fs",
        document.id,
        written,
        collection,
        elapsed,
    )
    return IngestionResult(
        document_id=document.id,
        status=DocumentStatus.READY,
        chunk_count=written,
        stage=IngestionStage.RECORD,
        error=None,
        collection=collection,
        elapsed_seconds=elapsed,
    )


def _record_failure(
    session: Session,
    *,
    identity: AuthenticatedIdentity,
    document: Document,
    stage: IngestionStage,
    cause: BaseException,
    collection: str | None,
    elapsed: float,
) -> IngestionResult:
    """Record ``failed`` with a stage-tagged message, and return the outcome.

    Two guarantees.

    It writes ``failed`` and never ``ready``, so PMP §4.1's "track ingestion errors" is
    satisfied by construction rather than by remembering. ``chunk_count`` is
    deliberately **not** touched: the column is ``NOT NULL DEFAULT 0``, so zero means
    "not indexed yet", and overwriting a document's previous count with zero on a failed
    re-ingest would erase the only record of what is still sitting in the collection.
    Leaving it alone means the count keeps describing the points that are actually
    there — a stale count is the *accurate* count during a failed re-ingest, and
    ``status = failed`` is what says it cannot be trusted.

    And it refuses to let a *second* failure hide the first. If the failure write itself
    raises, that is logged with the original exception still in the traceback chain, and
    the original outcome is returned regardless — losing the ingestion diagnosis because
    the database was also unhappy would be the worst available outcome, and the caller
    still gets the truth about what actually failed.
    """
    message = _failure_message(stage, cause)
    logger.error(
        "ingestion failed: document=%s stage=%s after %.2fs: %s",
        document.id,
        stage,
        elapsed,
        message,
        exc_info=cause,
    )

    try:
        record_ingestion_outcome(
            session,
            identity=identity,
            document_id=document.id,
            status=DocumentStatus.FAILED,
            error=message,
        )
    except RECORDING_FAILURES:
        # The caller is already unwinding from a real failure, and this one must not
        # replace a more informative diagnosis with a less useful one. The original
        # outcome is returned below either way, so the caller still learns what
        # actually went wrong — it is only the *record* that is lost, and it is lost
        # loudly. `logger.exception` rather than `logger.error(exc_info=True)`: same
        # traceback, one fewer thing to get wrong at the call site.
        logger.exception(
            "the failed status for document=%s could not be recorded either; the "
            "database is unhappy as well as the pipeline",
            document.id,
        )

    return IngestionResult(
        document_id=document.id,
        status=DocumentStatus.FAILED,
        chunk_count=None,
        stage=stage,
        error=message,
        collection=collection,
        elapsed_seconds=elapsed,
    )


def _store_document_chunks(
    session: Session,
    document: Document,
    *,
    points: list[VectorPoint],
) -> None:
    """Persist document chunks for BM25 full-text search (PMP Phase 6).

    Called after successful Qdrant upsert. The points contain the payload
    with all the chunk data we need. We insert one row per chunk into the
    document_chunks table with the precomputed search_vector text.

    The search_vector is the raw chunk text — PostgreSQL's to_tsvector()
    will convert it at query time. Storing the raw text here keeps the
    schema simple and SQLite-compatible for tests.

    Args:
        session: the ingestion session.
        document: the document being ingested.
        points: the VectorPoints that were just upserted to Qdrant.
    """
    for point in points:
        payload = point.payload
        # Extract chunk data from the payload (same fields as Qdrant payload)
        chunk_index = payload.get("chunk_index")
        if not isinstance(chunk_index, int):
            logger.warning(
                "skipping BM25 chunk insert: missing chunk_index for document=%s",
                document.id,
            )
            continue
        text = payload.get("text")
        if not isinstance(text, str):
            logger.warning(
                "skipping BM25 chunk insert: missing text for document=%s chunk=%s",
                document.id,
                chunk_index,
            )
            continue
        page = payload.get("page")
        if page is not None and not isinstance(page, int):
            page = None

        chunk = DocumentChunk(
            document_id=document.id,
            tenant_id=document.tenant_id,
            chunk_index=chunk_index,
            page=page,
            text=text,
            search_vector=text,  # Raw text; to_tsvector() applied at query time
        )
        session.add(chunk)
    session.flush()


def _failure_message(stage: IngestionStage, cause: BaseException) -> str:
    """Build the recorded error message for a failed stage.

    Stage first, because that is what tells a reader which module to open. Then the
    exception's class, because the class is a closed vocabulary that separates
    conditions needing opposite responses — a provider that is *down* from a provider
    that is *answering at the wrong width* — which are both ``RuntimeError`` subclasses
    raised from different modules.

    The detail that follows is included only for exception types **this project
    constructs from content-free material**: format names, byte and page limits,
    configured values, counts, and exception class names.

    Notably excluded is :class:`~app.rag.embeddings.EmbeddingError`. Its messages are
    built from a provider's *own* error text, and a provider is free to echo what it was
    sent — which is a chunk of a tenant's document. Nothing here can bound or predict
    what a third party puts in an error body, so the class name is used alone. Losing
    the provider's wording costs an operator one extra step of triage; putting tenant
    content in an admin-visible column costs considerably more.

    Truncated to :data:`MAX_ERROR_CHARS` so a future stage interpolating something
    unbounded degrades here rather than being rejected by the column at flush time. The
    one piece of tenant-supplied text that does reach here is a validated filename, from
    ``resolve_parser``'s "no parser is registered for 'x' format" — bounded by
    :data:`~app.schemas.documents.DOCUMENT_FILENAME_MAX_LENGTH`, stripped of control
    characters by the upload validator, and already returned by ``GET /documents/{id}``
    to the same administrators who would read ``documents.error``.
    """
    if isinstance(
        cause,
        (
            DocumentParseError,
            VectorStoreError,
            NoExtractableTextError,
            StoredObjectMissingError,
            FileTooLargeError,
        ),
    ):
        detail = f": {cause}"
    else:
        detail = ""

    message = f"ingestion failed at the {stage} stage ({type(cause).__name__}){detail}"
    return message[:MAX_ERROR_CHARS]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _batched(items: Sequence[_T], size: int) -> Iterator[Sequence[_T]]:
    """Yield consecutive slices of ``items``, each of at most ``size``.

    Slices rather than iterators-of-iterators because both callers pass a batch to
    something that wants a real sequence — ``upsert_points`` and ``embed_texts`` both
    index and re-iterate — and a generator would force each to materialise it anyway,
    one layer down and without saying so.

    Refuses a non-positive ``size`` rather than looping forever, which is what the
    naive ``range(0, len(items), 0)`` does.

    Generic over the element type rather than over ``Sequence[object]``: both callers
    immediately wrap the batch in :func:`list` and hand it to something specific — one
    to ``embed_texts``, which takes strings, one to ``upsert_points``, which takes
    points. A ``Sequence[object]`` would make mypy reject both of those calls, and the
    obvious ways out of that — a cast, or ``list(batch)`` with an annotation — would
    discard the very type information that makes the batching worth checking.
    """
    if size < 1:
        raise ValueError("batch size must be at least 1")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def elapsed_since(started: float) -> float:
    """Seconds since ``started``, never negative.

    :func:`time.monotonic` rather than :func:`time.time`, because an elapsed duration
    must not change when the system clock does. Clamped at zero because a monotonic
    clock cannot go backwards and so a negative value would mean the arithmetic here is
    wrong; this is a log field rather than a measurement, and hiding the bug from a log
    line costs less than reporting a nonsense one.
    """
    return max(0.0, time.monotonic() - started)
