"""Benchmark adapters that drive the **production** retrieval components.

Per the phase brief (§5, §25), evaluation reuses the existing retrieval
implementation rather than creating a second one. Each configuration below is a
thin orchestration over exactly the components the query service uses:

=============== ================================================================
Configuration   Production component(s)
=============== ================================================================
DENSE           ``store.search_points`` + PostgreSQL re-verification
BM25            :func:`app.rag.bm25.bm25_search` + re-verification
HYBRID          :func:`app.rag.hybrid.hybrid_retrieve` (dense + BM25 + RRF) +
                :func:`app.rag.hybrid.load_candidate_chunks` + re-verification
HYBRID_RERANK   the HYBRID chain plus
                :func:`app.rag.rerank.rerank_chunks` over a reranker built by
                :func:`app.rag.rerank.get_reranker`
=============== ================================================================

Every configuration keeps the two production security layers intact:

1. **Tenant scoping** — the collection is derived from ``identity.tenant_id``
   alone; no adapter takes a tenant id as an argument.
2. **Role filtering** — payload filters (dense), ``role_may_view`` (BM25), and
   the PostgreSQL re-verification layer (:func:`_authorize`, a mirror of the
   query service's second check) apply exactly as in production.

The evaluator never accepts a tenant id from a caller, so it cannot bypass
tenant isolation even by mistake — the same invariant the phase brief §8 and §19
require.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.identity import AuthenticatedIdentity
from app.models.document import Document
from app.rag.bm25 import bm25_search
from app.rag.embeddings import EmbeddingProvider
from app.rag.hybrid import hybrid_retrieve, load_candidate_chunks
from app.rag.rerank import get_reranker, rerank_chunks
from app.rag.retrieval import RetrievedChunk
from app.storage.qdrant import QdrantVectorStore


class RetrievalConfig(str, enum.Enum):
    """The four retrieval configurations under comparison."""

    DENSE = "dense"
    BM25 = "bm25"
    HYBRID = "hybrid"
    HYBRID_RERANK = "hybrid_rerank"


#: All configurations the benchmark compares (§14 experiment matrix).
CONFIGS: tuple[RetrievalConfig, ...] = (
    RetrievalConfig.DENSE,
    RetrievalConfig.BM25,
    RetrievalConfig.HYBRID,
    RetrievalConfig.HYBRID_RERANK,
)


@dataclass(frozen=True, slots=True)
class RetrievalOutcome:
    """One query's retrieval result for one configuration.

    ``doc_ranks`` and ``chunk_ranks`` capture the configuration's **full ranking**
    (up to ``candidate_limit``), which is what ranking metrics (Hit@K/MRR) are
    measured on. ``chunks`` is the authorized top-``top_k`` context that
    generation would be grounded on. Keeping the two separate is deliberate:
    ranking quality and answer context are different claims.
    """

    config: RetrievalConfig
    question_id: str
    question: str
    doc_ranks: tuple[str, ...]
    chunk_ranks: tuple[str, ...]
    chunks: tuple[RetrievedChunk, ...]
    latency_ms: float
    retrieved_count: int
    candidate_limit: int
    reranker_model: str | None = None


def _effective_reranker_name(reranker: Any, declared: str) -> str:
    """The name of the reranker that *actually* ran.

    A ``CrossEncoderReranker`` whose model failed to load (module or model
    missing) behaves exactly like the no-op fallback; this function returns a
    name that says so instead of the declared model name. A genuine
    :class:`~app.rag.rerank.NoOpReranker` (reranking disabled by configuration)
    is reported as plain ``"noop"``.
    """
    if type(reranker).__name__ == "NoOpReranker":
        return "noop"
    if getattr(reranker, "_model", None) is None:
        return "noop-fallback (cross-encoder missing)"
    return declared


def chunk_key(chunk: RetrievedChunk) -> str:
    """The chunk-level item id used by the metrics: ``"<doc_uuid>:<index>"``."""
    return f"{chunk.document_id}:{chunk.chunk_index}"


def _chunk_ranks(chunks: list[RetrievedChunk]) -> tuple[str, ...]:
    return tuple(chunk_key(chunk) for chunk in chunks)


def _doc_ranks(chunks: list[RetrievedChunk]) -> tuple[str, ...]:
    """Document ids in first-appearance order (document-level ranking)."""
    seen: list[str] = []
    present: set[uuid.UUID] = set()
    for chunk in chunks:
        if chunk.document_id not in present:
            present.add(chunk.document_id)
            seen.append(str(chunk.document_id))
    return tuple(seen)


def _authorize(
    session: Session,
    identity: AuthenticatedIdentity,
    chunks: list[RetrievedChunk],
) -> list[RetrievedChunk]:
    """Re-verify every candidate against the database (second auth layer).

    A mirror of the query service's check: a chunk survives only when its
    document row exists, belongs to ``identity.tenant_id``, and
    ``Document.role_may_view`` permits ``identity.role``. Keeping the benchmark
    inside this layer is what §8 of the phase brief requires — evaluation must
    not weaken the authorization model.
    """
    if not chunks:
        return []
    document_ids = {chunk.document_id for chunk in chunks}
    rows = session.scalars(
        select(Document).where(
            Document.id.in_(document_ids),
            Document.tenant_id == identity.tenant_id,
        )
    ).all()
    viewable = {
        document.id for document in rows if document.role_may_view(identity.role)
    }
    return [chunk for chunk in chunks if chunk.document_id in viewable]


def _point_chunk(point: Any) -> RetrievedChunk | None:
    """Map one scored point's payload to a :class:`RetrievedChunk`.

    Malformed payloads are skipped rather than raised on: the search already
    asserts payload presence, and a single corrupt point must not fail the whole
    benchmark run.
    """
    payload = getattr(point, "payload", None)
    if not isinstance(payload, dict):
        return None
    raw_doc = payload.get("document_id")
    chunk_index = payload.get("chunk_index")
    text = payload.get("text")
    filename = payload.get("filename")
    if not isinstance(raw_doc, str) or not isinstance(chunk_index, int):
        return None
    try:
        document_id = uuid.UUID(raw_doc)
    except ValueError:
        return None
    page = payload.get("page")
    return RetrievedChunk(
        document_id=document_id,
        filename=filename if isinstance(filename, str) else "",
        chunk_index=chunk_index,
        page=page if isinstance(page, int) else None,
        text=text if isinstance(text, str) else "",
        score=float(getattr(point, "score", 0.0) or 0.0),
    )


def _retrieve_dense(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    embeddings: EmbeddingProvider,
    question: str,
    limit: int,
) -> tuple[list[RetrievedChunk], int]:
    """Dense-only retrieval through the store's tenant-scoped search."""
    vectors = embeddings.embed_texts([question])
    points = store.search_points(
        identity.tenant_id,
        vector=vectors[0],
        limit=limit,
        role=identity.role,
    )
    chunks: list[RetrievedChunk] = []
    for point in points:
        chunk = _point_chunk(point)
        if chunk is not None:
            chunks.append(chunk)
    return chunks, len(points)


def _retrieve_bm25(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    question: str,
    limit: int,
) -> tuple[list[RetrievedChunk], int]:
    """BM25-only retrieval through the production keyword search."""
    candidates = bm25_search(
        session=session, identity=identity, question=question, limit=limit
    )
    return (
        [
            RetrievedChunk(
                document_id=candidate.document_id,
                filename=candidate.filename,
                chunk_index=candidate.chunk_index,
                page=candidate.page,
                text=candidate.text,
                score=candidate.score,
            )
            for candidate in candidates
        ],
        len(candidates),
    )


def _retrieve_hybrid(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    embeddings: EmbeddingProvider,
    question: str,
    limit: int,
) -> tuple[list[RetrievedChunk], int]:
    """Dense + BM25 fused with RRF, loaded through the production path."""
    fused = hybrid_retrieve(
        session=session,
        identity=identity,
        store=store,
        embedding_provider=embeddings,
        question=question,
        top_k=limit,
        dense_limit=limit,
        bm25_limit=limit,
    )
    chunks = load_candidate_chunks(
        session=session,
        store=store,
        identity=identity,
        candidates=fused,
    )
    return list(chunks), len(fused)


def retrieve(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    embeddings: EmbeddingProvider,
    settings: Settings,
    question: str,
    config: RetrievalConfig,
    top_k: int,
    candidate_limit: int,
) -> RetrievalOutcome:
    """Run one retrieval configuration for ``question`` and return the outcome.

    Args:
        session: read-only database session (used by BM25 and the second
            authorization layer).
        identity: the evaluated caller; the tenant and role come from here and
            nowhere else.
        store: the tenant-scoped vector store.
        embeddings: the embedding provider (the documented stand-in in the
            measured run).
        settings: supplies reranker configuration.
        question: the question text.
        config: which configuration to run.
        top_k: how many chunks form the answer context.
        candidate_limit: how many candidates each retriever returns; ranking
            metrics are computed over this list.

    Returns:
        :class:`RetrievalOutcome` with the config's ranking and answer context.
        Reranker model name is recorded when the config uses one.
    """
    started = time.perf_counter()
    if config is RetrievalConfig.DENSE:
        chunks, retrieved_count = _retrieve_dense(
            session=session,
            identity=identity,
            store=store,
            embeddings=embeddings,
            question=question,
            limit=candidate_limit,
        )
        reranker_model: str | None = None
    elif config is RetrievalConfig.BM25:
        chunks, retrieved_count = _retrieve_bm25(
            session=session,
            identity=identity,
            question=question,
            limit=candidate_limit,
        )
        reranker_model = None
    elif config is RetrievalConfig.HYBRID:
        chunks, retrieved_count = _retrieve_hybrid(
            session=session,
            identity=identity,
            store=store,
            embeddings=embeddings,
            question=question,
            limit=candidate_limit,
        )
        reranker_model = None
    elif config is RetrievalConfig.HYBRID_RERANK:
        chunks, retrieved_count = _retrieve_hybrid(
            session=session,
            identity=identity,
            store=store,
            embeddings=embeddings,
            question=question,
            limit=candidate_limit,
        )
        reranker = get_reranker(
            enabled=getattr(settings, "rerank_enabled", True),
            model_name=getattr(settings, "rerank_model", None),
        )
        reranked = rerank_chunks(reranker, question, chunks, top_k=None)
        chunks = reranked.chunks
        # The production seam degrades to no-op internally when the model is
        # missing; the *effective* model is what the benchmark records, so a
        # degraded rerank is never reported under the name of a model that did
        # not run (§20 of the phase brief).
        reranker_model = _effective_reranker_name(reranker, reranked.model_name)
    else:  # pragma: no cover - exhaustive over the enum
        raise ValueError(f"unknown retrieval configuration {config!r}")

    authorized = _authorize(session, identity, chunks)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return RetrievalOutcome(
        config=config,
        question_id="",
        question=question,
        doc_ranks=_doc_ranks(authorized),
        chunk_ranks=_chunk_ranks(authorized),
        chunks=tuple(authorized[:top_k]),
        latency_ms=elapsed_ms,
        retrieved_count=retrieved_count,
        candidate_limit=candidate_limit,
        reranker_model=reranker_model,
    )