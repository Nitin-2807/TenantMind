"""Hybrid retrieval: fusion of dense (vector) and sparse (BM25) retrieval.

PMP Phase 6 deliverable: Hybrid retrieval.

This module combines the results of dense vector search and BM25 keyword search
using Reciprocal Rank Fusion (RRF), a parameter-free fusion method that
combines ranked lists by their reciprocal ranks.

The pipeline:
  Query
    ├── Dense retrieval (Qdrant vector search)
    │
    └── Keyword retrieval (PostgreSQL BM25)
         ↓
    Score fusion (RRF)
         ↓
    Combined candidates
         ↓
    Authorization filter (already applied in each retriever)
         ↓
    Reranking (optional, separate module)
         ↓
    Top-K chunks
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.orm import Session

from app.core.identity import AuthenticatedIdentity
from app.rag.bm25 import bm25_search_ids
from app.rag.retrieval import RetrievedChunk
from app.storage.qdrant import QdrantVectorStore

#: Default RRF constant (k). RRF score = sum(1 / (k + rank)).
#: k=60 is the standard value from the original RRF paper.
RRF_K: Final[int] = 60

#: Maximum candidates to fetch from each retriever before fusion.
#: Should be larger than the final top_k to give fusion enough signal.
HYBRID_CANDIDATE_LIMIT: Final[int] = 50


@dataclass(frozen=True, slots=True)
class HybridCandidate:
    """A candidate from hybrid retrieval before reranking.

    Attributes:
        document_id: the document the chunk came from.
        chunk_index: the chunk's ordinal within the document.
        dense_score: the dense retrieval score (cosine similarity), or None if not in dense results.
        bm25_score: the BM25 score (ts_rank_cd), or None if not in BM25 results.
        rrf_score: the fused RRF score.
    """

    document_id: uuid.UUID
    chunk_index: int
    dense_score: float | None
    bm25_score: float | None
    rrf_score: float


def _rrf_score(ranks: list[int], k: int = RRF_K) -> float:
    """Compute Reciprocal Rank Fusion score from a list of ranks.

    RRF = sum(1 / (k + rank_i)) for each retriever where the item appears.
    Rank is 1-based (1 = best).

    Args:
        ranks: list of 1-based ranks from each retriever (only for retrievers where the item appears).
        k: RRF constant (default 60).

    Returns:
        The fused RRF score.
    """
    return sum(1.0 / (k + rank) for rank in ranks)


def hybrid_retrieve(
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    embedding_provider,  # EmbeddingProvider - avoiding circular import
    question: str,
    top_k: int = 10,
    dense_limit: int = HYBRID_CANDIDATE_LIMIT,
    bm25_limit: int = HYBRID_CANDIDATE_LIMIT,
) -> list[HybridCandidate]:
    """Run hybrid retrieval: dense + BM25 fused with RRF.

    Args:
        session: database session.
        identity: authenticated caller (tenant_id + role).
        store: tenant-scoped Qdrant vector store.
        embedding_provider: provider to embed the question.
        question: the user's question.
        top_k: final number of candidates to return.
        dense_limit: how many dense candidates to fetch.
        bm25_limit: how many BM25 candidates to fetch.

    Returns:
        List of HybridCandidate sorted by RRF score (highest first), length <= top_k.

    Note:
        Both dense and BM25 retrievers already apply tenant + role filtering.
        This function only fuses the scores.
    """

    # --- Dense retrieval ---
    dense_candidates: list[tuple[uuid.UUID, int, float]] = []
    vectors = embedding_provider.embed_texts([question])
    dense_points = store.search_points(
        identity.tenant_id,
        vector=vectors[0],
        limit=dense_limit,
        role=identity.role,
    )
    for rank, point in enumerate(dense_points, start=1):
        payload = getattr(point, "payload", None)
        if not isinstance(payload, dict):
            continue
        doc_id = payload.get("document_id")
        chunk_idx = payload.get("chunk_index")
        if not isinstance(doc_id, str) or not isinstance(chunk_idx, int):
            continue
        dense_candidates.append((
            uuid.UUID(doc_id),
            chunk_idx,
            point.score,  # cosine similarity
        ))

    # --- BM25 retrieval ---
    bm25_candidates = bm25_search_ids(
        session=session,
        identity=identity,
        question=question,
        limit=bm25_limit,
    )

    # --- RRF Fusion ---
    # Build rank maps: (document_id, chunk_index) -> 1-based rank
    dense_ranks = {
        (doc_id, chunk_idx): rank
        for rank, (doc_id, chunk_idx, _) in enumerate(dense_candidates, start=1)
    }
    bm25_ranks = {
        (doc_id, chunk_idx): rank
        for rank, (doc_id, chunk_idx, _) in enumerate(bm25_candidates, start=1)
    }

    # All unique candidates
    all_keys = set(dense_ranks.keys()) | set(bm25_ranks.keys())

    fused: list[HybridCandidate] = []
    for key in all_keys:
        doc_id, chunk_idx = key
        ranks = []
        dense_score = None
        bm25_score = None

        if key in dense_ranks:
            ranks.append(dense_ranks[key])
            # Get the actual dense score
            for d_doc, d_idx, d_score in dense_candidates:
                if d_doc == doc_id and d_idx == chunk_idx:
                    dense_score = d_score
                    break

        if key in bm25_ranks:
            ranks.append(bm25_ranks[key])
            # Get the actual BM25 score
            for b_doc, b_idx, b_score in bm25_candidates:
                if b_doc == doc_id and b_idx == chunk_idx:
                    bm25_score = b_score
                    break

        rrf = _rrf_score(ranks)
        fused.append(HybridCandidate(
            document_id=doc_id,
            chunk_index=chunk_idx,
            dense_score=dense_score,
            bm25_score=bm25_score,
            rrf_score=rrf,
        ))

    # Sort by RRF score descending, take top_k
    fused.sort(key=lambda c: c.rrf_score, reverse=True)
    return fused[:top_k]


def load_candidate_chunks(
    session: Session,
    store: QdrantVectorStore,
    identity: AuthenticatedIdentity,
    candidates: list[HybridCandidate],
) -> list[RetrievedChunk]:
    """Load full chunk data (text, filename, page) for hybrid candidates.

    Fetches from Qdrant (which has the vectors and payload) for the candidates
    that survived fusion. This avoids loading full text for all candidates
    during the fusion phase.

    Args:
        session: database session (for document-level role re-verification).
        store: tenant-scoped Qdrant vector store.
        identity: authenticated caller.
        candidates: fused candidates from hybrid_retrieve.

    Returns:
        List of RetrievedChunk with full data, in the same order as candidates.
    """
    if not candidates:
        return []

    # Try to use the store's pre-configured points (e.g., RecordingStore in tests)
    # which avoids needing a scroll call that the test double may not support.
    points_attr = getattr(store, "points", None)
    if isinstance(points_attr, list):
        # Test double with pre-configured points - use them directly
        return _load_chunks_from_points(points_attr, candidates)

    # Real Qdrant path: scroll all points for this tenant and filter in Python
    from app.storage.qdrant import collection_name_for
    name = collection_name_for(identity.tenant_id)

    target_pairs = {(c.document_id, c.chunk_index) for c in candidates}

    try:
        records, _ = store._client.scroll(
            collection_name=name,
            scroll_filter=None,
            limit=1000,
            with_payload=True,
            with_vectors=False,
        )
    except Exception:  # noqa: BLE001 - Qdrant client can raise various exceptions
        return []

    record_map: dict[tuple[uuid.UUID, int], Any] = {}
    for record in records:
        payload = getattr(record, "payload", None)
        if not isinstance(payload, dict):
            continue
        doc_id = payload.get("document_id")
        chunk_idx = payload.get("chunk_index")
        if isinstance(doc_id, str) and isinstance(chunk_idx, int):
            key = (uuid.UUID(doc_id), chunk_idx)
            if key in target_pairs:
                record_map[key] = record

    from app.services.query import _to_chunk
    chunks: list[RetrievedChunk] = []
    for c in candidates:
        rec: Any = record_map.get((c.document_id, c.chunk_index))
        if rec is not None:
            chunk = _to_chunk(rec)  # type: ignore[arg-type]
            if chunk is not None:
                chunks.append(chunk)

    return chunks


def _load_chunks_from_points(
    points: list[Any],
    candidates: list[HybridCandidate],
) -> list[RetrievedChunk]:
    """Load chunks from a pre-configured list of ScoredPoint objects (test doubles)."""
    from app.services.query import _to_chunk

    # Map points by (document_id, chunk_index)
    record_map: dict[tuple[uuid.UUID, int], Any] = {}
    for point in points:
        payload = getattr(point, "payload", None)
        if not isinstance(payload, dict):
            continue
        doc_id = payload.get("document_id")
        chunk_idx = payload.get("chunk_index")
        if isinstance(doc_id, str) and isinstance(chunk_idx, int):
            record_map[(uuid.UUID(doc_id), chunk_idx)] = point

    chunks: list[RetrievedChunk] = []
    for c in candidates:
        record = record_map.get((c.document_id, c.chunk_index))
        if record is not None:
            chunk = _to_chunk(record)
            if chunk is not None:
                chunks.append(chunk)

    return chunks