"""Answer a question from this tenant's indexed documents, with citations.

PMP §7 FR-09 ("basic semantic search"), FR-12 and FR-13 ("citations"), §18
"Retrieval", and §25 success criteria 6-8. This module is the orchestrator; the
stages it runs are in :mod:`app.rag.embeddings`, :mod:`app.storage.qdrant`,
:mod:`app.rag.retrieval` and :mod:`app.rag.generation`, and none of them knows about
any of the others.

Why the orchestrator exists at all
-----------------------------------
:mod:`app.services.ingestion` is the write-side counterpart, and the argument for
separating orchestration from stages is the one that module's docstring makes: a single
``answer_query`` containing embedding, search, filtering, prompting and parsing could only
be tested by running all five, which means a context-construction bug can only be found by
running a language model. Here, the security decisions — which are the expensive ones to
get right — are separable from the parts that merely talk to a vendor.

The order of operations, and why it is that order
------------------------------------------------

.. code-block:: text

    AuthenticatedIdentity
            |  identity.tenant_id, identity.role
            v
    embed the question              (app.rag.embeddings)
            v
    tenant-scoped search            (app.storage.qdrant)
      - collection comes from tenant_id alone
      - allowed_roles filtered inside the store
            v
    re-verify against PostgreSQL    (this module)
            v
    build bounded context           (app.rag.retrieval)
            v
    generate                        (app.rag.generation)
            v
    answer + citations

Two authorization layers, and the reason for the second
-------------------------------------------------------
:meth:`app.storage.qdrant.QdrantVectorStore.search_points` filters on the ``allowed_roles``
**payload**, so a chunk the caller may not read is never returned out of the store at all.
That is the right place for the primary check, and it is where PMP §25 criterion 7 is
actually enforced.

But the payload is a *copy* of the ``Document`` row, copied once at ingestion time. There
is currently no endpoint that edits a document — :mod:`app.api.documents` records why one
was not invented — so the copy cannot go stale today. "Cannot go stale today" is
sufficient by accident rather than by construction, and this module does not build on
accidents: :func:`_viewable_document_ids` re-checks every candidate against the row it was
copied from, using :meth:`~app.models.document.Document.role_may_view` itself rather than
a second expression of the same rule. One extra query per request, on the path where
being wrong is a confidentiality breach.

When the first ``allowed_roles`` edit endpoint is built, this function is what already
makes it safe, and that is the point of paying for it now rather than later.

Why the role filter is not applied once, centrally, in this module
-----------------------------------------------------------------
Because by the time a chunk is in this process its text has been read out of the vector
store. Filtering here would be correct but late: the unauthorized text would already have
existed in memory on its way to being discarded, one careless future log statement away
from being persisted. The primary filter belongs where the text has not yet been read.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TypeGuard

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.identity import AuthenticatedIdentity
from app.models.document import Document
from app.rag.embeddings import EmbeddingError, EmbeddingProvider
from app.rag.generation import GenerationError, GenerationProvider
from app.rag.hybrid import hybrid_retrieve, load_candidate_chunks
from app.rag.rerank import get_reranker, rerank_chunks
from app.rag.retrieval import Citation, RagContext, RetrievedChunk, build_context
from app.storage.qdrant import QdrantVectorStore, VectorStoreError

logger = logging.getLogger(__name__)

#: The answer returned when retrieval produced nothing the caller may read.
#:
#: A fixed sentence, and no model call. Two reasons, both deliberate:
#:
#: 1. There is no grounded answer to produce. PMP FR-12 requires an answer to cite its
#:    sources; with no sources there is nothing to ground one in, and asking a model to
#:    answer from an empty evidence block produces a fluent invention — the exact failure
#:    the risk table scores as "LLM hallucination / High" and answers with citations and
#:    grounded prompts.
#: 2. It makes the empty case deterministic and testable. The response is a pure function
#:    of the corpus, so no test of this path needs a provider, a network, or a timeout.
#:
#: It says plainly that the knowledge base did not contain the answer rather than implying
#: the tenant has no such document: retrieval found nothing, which is not the same claim,
#: and a message implying the latter would leak whether the tenant indexed anything.
NO_CONTEXT_ANSWER = (
    "I could not find anything in this knowledge base that answers that question."
)


class QueryError(RuntimeError):
    """The query could not be answered.

    A :class:`RuntimeError` because no caller-supplied value is at fault, exactly like
    :class:`~app.storage.qdrant.VectorStoreError`: an unreachable provider, an
    unreachable vector store, and an unparsable provider response are all operational
    conditions. The API layer maps these to statuses and the messages must never contain
    document content, prompts or credentials.
    """


class QueryRejectedError(QueryError):
    """The question itself is unacceptable.

    Distinct from :class:`QueryError` because it is the only one of these that is the
    caller's fault, and therefore the only one the caller is told about specifically.
    """


class RetrievalFailedError(QueryError):
    """Embedding the question, or searching the tenant's collection, failed.

    Split from generation failure so the API layer can report which half of the pipeline
    is unavailable. A caller cannot act on either, but an operator can act on exactly one.
    """


class GenerationFailedError(QueryError):
    """The generation provider could not produce an answer.

    Raised for a provider failure and for a provider response this project cannot parse.
    The two are deliberately the same type: a caller learns that no answer is available
    and nothing else, and an operator gets the precise reason from the log line this
    module writes before raising.
    """


@dataclass(frozen=True, slots=True)
class _Retrieval:
    """What one retrieval produced, and how much was asked for.

    Attributes:
        chunks: the authorized, well-formed chunks, in the order the store ranked them.
            A tuple because it is a fixed set of survivors rather than a growing
            collection.
        retrieved_count: how many points the store returned **before** authorization and
            payload validation. Kept only so the ungrounded-answer log line can say
            whether the store found nothing or everything it found was refused; it is not
            part of the answer, and no caller outside this module may read it.

    Frozen and slotted for the same reason as everything else crossing into a prompt: the
    chunks are untrusted-derived data about to be assembled into evidence.
    """

    chunks: tuple[RetrievedChunk, ...]
    retrieved_count: int


@dataclass(frozen=True, slots=True)
class QueryAnswer:
    """One answer to one question, and the sources it is grounded in.

    Attributes:
        question: the question as asked, after trimming.
        answer: the generated text, or :data:`NO_CONTEXT_ANSWER` when nothing was
            retrieved.
        citations: the chunks the answer is grounded in, numbered in the order the model
            saw them. Empty when ``grounded`` is ``False``.
        grounded: whether an answer was generated at all. ``False`` means retrieval found
            nothing the caller may read, so ``answer`` is the fixed
            :data:`NO_CONTEXT_ANSWER` and ``citations`` is empty. A client can branch on
            this without having to recognise the fixed sentence.
        context_truncated: whether evidence was dropped to fit the context budget. It does
            not mean the answer was wrong; it means some relevant chunks were not shown
            to the model, which is worth surfacing rather than hiding.
    """

    question: str
    answer: str
    citations: tuple[Citation, ...]
    grounded: bool
    context_truncated: bool


def answer_query(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    embedding_provider: EmbeddingProvider,
    generation_provider: GenerationProvider,
    settings: Settings,
    question: str,
) -> QueryAnswer:
    """Answer ``question`` from this tenant's indexed documents.

    Args:
        session: read-only here. The query path writes nothing — query history is PMP
            Phase 7 and this function does not pretend otherwise.
        identity: the authenticated caller. Supplies the tenant **and** the role, and
            both are used: the tenant selects the collection, the role selects which of
            its documents may be read.
        store: the tenant-scoped vector store.
        embedding_provider: used to embed the single question.
        generation_provider: used once, and only if there is something to ground on.
        settings: supplies ``query_top_k`` and ``query_max_context_chars``.
        question: the user's question.

    Returns:
        A :class:`QueryAnswer`. ``grounded`` is ``False`` when retrieval found nothing the
        caller may read, which is an ordinary outcome and not an error.

    Raises:
        QueryRejectedError: the question is empty, whitespace, or longer than
            ``query_max_question_chars``.
        RetrievalFailedError: embedding or search failed.
        GenerationFailedError: the provider failed or answered unusably.
    """
    asked = question.strip()
    if not asked:
        raise QueryRejectedError("the question is empty")
    if len(asked) > settings.query_max_question_chars:
        raise QueryRejectedError(
            f"the question is {len(asked)} characters, which exceeds the "
            f"{settings.query_max_question_chars}-character limit"
        )

    retrieval = _retrieve(
        session=session,
        identity=identity,
        store=store,
        embedding_provider=embedding_provider,
        top_k=settings.query_top_k,
        question=asked,
        settings=settings,
    )
    points = retrieval.chunks
    retrieved_count = retrieval.retrieved_count

    context = build_context(
        chunks=points,
        question=asked,
        max_chars=settings.query_max_context_chars,
    )
    if not context.citations:
        # Nothing survived authorization, or nothing was indexed. Either way there is no
        # grounded answer to be had, and the model is not called — see NO_CONTEXT_ANSWER.
        #
        # Both counts are logged, not just the surviving one, because the two causes are
        # otherwise indistinguishable here and only one of them is a configuration problem:
        # `retrieved=0` means this tenant has nothing indexed, while `retrieved=3
        # authorized=0` means the corpus is full of documents this caller may not read —
        # which is the report that tells an operator their ingestion succeeded and their
        # access model is wrong. Logging only the survivors makes both read as 0.
        logger.info(
            "query answered without context: user=%s tenant=%s retrieved=%d "
            "authorized=%d",
            identity.user_id,
            identity.tenant_id,
            retrieved_count,
            len(points),
        )
        return QueryAnswer(
            question=asked,
            answer=NO_CONTEXT_ANSWER,
            citations=(),
            grounded=False,
            context_truncated=context.truncated,
        )

    answer = _generate(
        generation_provider=generation_provider,
        context=context,
        question=asked,
        identity=identity,
    )

    logger.info(
        "query answered: user=%s tenant=%s citations=%d truncated=%s model=%s",
        identity.user_id,
        identity.tenant_id,
        len(context.citations),
        context.truncated,
        generation_provider.model_name,
    )
    return QueryAnswer(
        question=asked,
        answer=answer,
        citations=context.citations,
        grounded=True,
        context_truncated=context.truncated,
    )


def _retrieve(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    embedding_provider: EmbeddingProvider,
    top_k: int,
    question: str,
    settings: Settings,
) -> _Retrieval:
    """Hybrid retrieval: dense + BM25 fused with RRF, then reranked.

    Pipeline:
      1. Dense vector search (Qdrant)
      2. BM25 keyword search (PostgreSQL full-text)
      3. Reciprocal Rank Fusion (RRF)
      4. Cross-encoder reranking (optional)
      5. Load full chunk data from Qdrant
      6. Re-verify authorization against PostgreSQL

    Returns a :class:`_Retrieval` with the final authorized chunks.
    """
    # Step 1-4: Hybrid retrieval with RRF and reranking
    try:
        # Check if reranking is enabled in settings
        reranker_enabled = getattr(settings, "rerank_enabled", True)
        rerank_model = getattr(settings, "rerank_model", None)
        reranker = get_reranker(enabled=reranker_enabled, model_name=rerank_model)

        hybrid_candidates = hybrid_retrieve(
            session=session,
            identity=identity,
            store=store,
            embedding_provider=embedding_provider,
            question=question,
            top_k=top_k * 2,  # fetch more for reranking
        )

        # Load full chunk data from Qdrant for the fused candidates
        chunks = load_candidate_chunks(
            session=session,
            store=store,
            identity=identity,
            candidates=hybrid_candidates,
        )

        # Rerank the loaded chunks
        rerank_result = rerank_chunks(reranker, question, chunks, top_k=top_k)

        # Step 6: Re-verify authorization against PostgreSQL (second layer)
        viewable = _viewable_document_ids(
            session,
            identity=identity,
            document_ids={chunk.document_id for chunk in rerank_result.chunks},
        )
        survivors = tuple(
            chunk for chunk in rerank_result.chunks
            if chunk.document_id in viewable
        )

        retrieved_count = len(hybrid_candidates)
        logger.info(
            "hybrid retrieval: tenant=%s candidates=%d reranked=%d authorized=%d",
            identity.tenant_id,
            retrieved_count,
            len(rerank_result.chunks),
            len(survivors),
        )

        return _Retrieval(chunks=survivors, retrieved_count=retrieved_count)

    except EmbeddingError as exc:
        # Embedding failure is fatal - no retrieval possible without vectors
        logger.warning("query embedding failed: %s", type(exc).__name__)
        raise RetrievalFailedError(
            "the question could not be turned into a query vector"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - hybrid retrieval fallback
        # Other hybrid retrieval failures (BM25, reranking, etc.) fall back to dense-only
        logger.warning(
            "hybrid retrieval failed, falling back to dense-only: %s",
            type(exc).__name__,
        )
        return _retrieve_dense(
            session=session,
            identity=identity,
            store=store,
            embedding_provider=embedding_provider,
            top_k=top_k,
            question=question,
        )


def _retrieve_dense(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    embedding_provider: EmbeddingProvider,
    top_k: int,
    question: str,
) -> _Retrieval:
    """Fallback dense-only retrieval (original Phase 4 behavior)."""
    try:
        vectors = embedding_provider.embed_texts([question])
    except EmbeddingError as exc:
        logger.warning("query embedding failed: %s", type(exc).__name__)
        raise RetrievalFailedError(
            "the question could not be turned into a query vector"
        ) from exc

    try:
        found = store.search_points(
            identity.tenant_id,
            vector=vectors[0],
            limit=top_k,
            role=identity.role,
        )
    except VectorStoreError as exc:
        logger.warning("query search failed: %s", type(exc).__name__)
        raise RetrievalFailedError(
            "this tenant's indexed documents could not be searched"
        ) from exc

    viewable = _viewable_document_ids(
        session,
        identity=identity,
        document_ids={
            found_id
            for point in found
            if (found_id := _payload_uuid(point, "document_id")) is not None
        },
    )
    survivors = tuple(
        chunk
        for point in found
        if (chunk := _to_chunk(point)) is not None
        and chunk.document_id in viewable
    )
    return _Retrieval(chunks=survivors, retrieved_count=len(found))


def _viewable_document_ids(
    session: Session, *, identity: AuthenticatedIdentity, document_ids: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of ``document_ids`` the caller may read, according to the database.

    The second authorization layer described in the module docstring. Three properties
    make it worth a query:

    * It reads :meth:`~app.models.document.Document.role_may_view` — the same method
      :mod:`app.services.documents` uses for ``GET /documents`` and ``GET
      /documents/{id}``. There is no second expression of the restriction anywhere in the
      query path, so the document list and a generated answer cannot disagree about who
      may read what.
    * It carries the tenant predicate as well as the role check. The collection is
      already the tenant boundary, so this is redundant today; it is here because the
      redundancy is what makes the invariant hold if that ever changes, and because a
      document id that resolves in no tenant resolves in none here.
    * A candidate whose document row has since been **deleted** is not viewable. The
      vector points outlive the document row until a deletion removes them, and a chunk
      whose document no longer exists must not be answerable from.

    Args:
        session: the request's session.
        identity: supplies the tenant and the role.
        document_ids: the documents the retrieved points claim to belong to. Already
            parsed as UUIDs and already filtered to the well-formed ones, so a single
            malformed point cannot make this query fail — the parameter is
            ``set[uuid.UUID]``, not ``set[str]``, so a string can not reach ``in_()`` at
            all.

    Returns:
        The ids of the documents this caller may read. An id absent from the result is not
        necessarily absent from the tenant — it may exist and be restricted.
    """
    if not document_ids:
        return set()

    rows = session.scalars(
        select(Document).where(
            Document.id.in_(document_ids),
            Document.tenant_id == identity.tenant_id,
        )
    ).all()
    return {document.id for document in rows if document.role_may_view(identity.role)}


def _generate(
    *,
    generation_provider: GenerationProvider,
    context: RagContext,
    question: str,
    identity: AuthenticatedIdentity,
) -> str:
    """Produce the answer text, or raise :class:`GenerationFailedError`.

    The provider is called with the three strings the context builder produced and nothing
    else. It is given no way to re-assemble the prompt: if it could rebuild the
    instruction block it could be persuaded that the instruction block is different from
    the one it was handed, so the layer that establishes the hierarchy is the only layer
    that can write it.
    """
    try:
        answer = generation_provider.generate(
            instructions=context.instructions,
            question=question,
            evidence=context.evidence,
        )
    except GenerationError as exc:
        # The provider's message can quote the prompt, and the prompt contains retrieved
        # tenant content, so the exception type is logged and the provider's own text is
        # not.
        logger.warning("query generation failed: %s", type(exc).__name__)
        raise GenerationFailedError(
            "an answer could not be generated for this question"
        ) from exc

    if not answer.strip():
        # A provider that returns whitespace has answered nothing. Treating it as an
        # answer would put an empty string in the response with citations attached, which
        # reads as "the answer is that nothing is known" while actually meaning the
        # provider malfunctioned.
        logger.warning("query generation returned no text: tenant=%s", identity.tenant_id)
        raise GenerationFailedError("the generation provider returned no answer text")
    return answer


def _to_chunk(point: object) -> RetrievedChunk | None:
    """Map one Qdrant scored point to a :class:`~app.rag.retrieval.RetrievedChunk`.

    Returns ``None`` for a point whose payload cannot supply the fields a citation needs.

    Skipping rather than raising is a deliberate choice about *which* failure this is. The
    four fields read here — ``document_id``, ``filename``, ``chunk_index``, ``text`` —
    were all written by the same ingestion code that produced this point, so a point
    missing one is not a malformed third-party record; it is a point this project wrote
    badly, and the honest response to that is a smaller result set plus a log line rather
    than a failed query for the user. :meth:`app.storage.qdrant.QdrantVectorStore.upsert_points`
    builds every point through one function for exactly this reason.

    A missing ``page`` is not a malformed point: ``page`` is genuinely optional, and
    ``None`` is its correct value for a format without pages.
    """
    payload = getattr(point, "payload", None)
    if not isinstance(payload, dict):
        logger.warning("retrieved point carried no payload and was skipped")
        return None

    filename = payload.get("filename")
    chunk_index = payload.get("chunk_index")
    text = payload.get("text")
    parsed_id = _payload_uuid(point, "document_id")
    if not _is_str(filename):
        logger.warning("retrieved point had no filename and was skipped")
        return None
    if not _is_int(chunk_index):
        logger.warning("retrieved point had no usable chunk_index and was skipped")
        return None
    if not _is_str(text):
        logger.warning("retrieved point had no text and was skipped")
        return None
    if parsed_id is None:
        logger.warning("retrieved point had no usable document_id and was skipped")
        return None

    page = payload.get("page")
    score = getattr(point, "score", None)
    return RetrievedChunk(
        document_id=parsed_id,
        filename=filename,
        chunk_index=chunk_index,
        page=page if _is_int(page) else None,
        text=text,
        score=float(score) if isinstance(score, int | float) else 0.0,
    )


def _payload_uuid(point: object, key: str) -> uuid.UUID | None:
    """Read a UUID-shaped payload field, or ``None`` if it is absent or malformed.

    Kept separate from :func:`_to_chunk` because the document-id set is needed *before*
    the per-point validation runs — the second authorization layer needs to know which
    documents to ask PostgreSQL about, and asking about a malformed id would make the
    query itself fail on a value a single bad point introduced.

    Returns a :class:`uuid.UUID` rather than the raw string so that the tenant-scoped
    lookup below is type-checked against a ``UUID`` column and a string can never reach
    it.
    """
    payload = getattr(point, "payload", None)
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _is_str(value: object) -> TypeGuard[str]:
    """Whether ``value`` is a non-empty ``str``.

    A :class:`~typing.TypeGuard` rather than a plain ``bool`` so that the checks below
    narrow the payload's ``object``-typed values without a cast. That matters more than
    usual here: the alternative to a guard is ``cast``, and a cast in this function would
    be asserting a fact about a value read out of Qdrant's JSON — a claim about a third
    party's data that the code itself had just declined to verify.

    ``bool`` is not a :class:`str` but an ``int`` is accepted by :func:`_is_int`, and the
    two are checked separately, so a payload carrying ``"chunk_index": true`` is rejected
    by the wrong one of them rather than silently becoming chunk 1.
    """
    return isinstance(value, str) and bool(value.strip())


def _is_int(value: object) -> TypeGuard[int]:
    """Whether ``value`` is an ``int`` and not a ``bool``.

    ``isinstance(True, int)`` is ``True`` in Python, so a boolean sneaking into a payload
    would otherwise become chunk index 1 or page 1 — a plausible-looking citation that
    points somewhere real but wrong.
    """
    return isinstance(value, int) and not isinstance(value, bool)