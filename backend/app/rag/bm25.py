"""BM25 keyword retrieval using PostgreSQL full-text search on document_chunks.

PMP Phase 6 deliverable: BM25 / keyword retrieval.

PostgreSQL's built-in full-text search provides BM25-ranked keyword search
via tsvector/tsquery. We store per-chunk text in the document_chunks table
with a precomputed search_vector column (populated at ingestion time).

This module is pure over values where possible — the search function itself
needs a database session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.identity import AuthenticatedIdentity
from app.models.document import Document

#: Maximum number of candidates BM25 will return before hybrid fusion/reranking.
#: Kept separate from the dense retrieval's top_k so the two can be tuned
#: independently. A larger pool gives the fusion/reranker more signal.
BM25_DEFAULT_LIMIT: Final[int] = 20

#: The PostgreSQL full-text search configuration to use.
#: 'english' provides stemming, stop words, etc. for English text.
FTS_CONFIG: Final[str] = "english"


@dataclass(frozen=True, slots=True)
class BM25Candidate:
    """One chunk candidate from BM25 keyword search.

    Attributes:
        document_id: the document the chunk came from.
        filename: the document's display filename.
        chunk_index: the chunk's ordinal within the document.
        page: the 1-based page, or None for formats without pages.
        text: the chunk's content (untrusted tenant data).
        score: the BM25 rank (ts_rank_cd), higher = better match.
    """

    document_id: uuid.UUID
    filename: str
    chunk_index: int
    page: int | None
    text: str
    score: float


def _tsquery_for_question(question: str) -> str:
    """Convert a user question to a PostgreSQL tsquery string.

    Uses websearch_tsquery syntax which supports:
    - phrase search with quotes: "exact phrase"
    - negation: -term
    - OR: term1 | term2
    - prefix matching: term:*

    This is more forgiving than plainto_tsquery for user questions.
    """
    # Escape single quotes for SQL string literal
    escaped = question.replace("'", "''")
    return f"websearch_to_tsquery('{FTS_CONFIG}', '{escaped}')"


def bm25_search(
    session: Session,
    identity: AuthenticatedIdentity,
    question: str,
    limit: int = BM25_DEFAULT_LIMIT,
) -> list[BM25Candidate]:
    """Search the tenant's documents using PostgreSQL BM25 (full-text search).

    Args:
        session: database session.
        identity: the authenticated caller (supplies tenant_id and role).
        question: the user's question.
        limit: maximum number of candidates to return.

    Returns:
        List of BM25Candidate, ranked by BM25 score (highest first).

    Note:
        Only chunks from documents with status=READY are searched.
        Role filtering is applied via the document's allowed_roles column.
    """
    if not question.strip():
        return []

    tsquery = _tsquery_for_question(question)

    # Query: join document_chunks with documents to get filename and apply
    # tenant/role/document status filters.
    # Uses ts_rank_cd (cover density ranking) for BM25-like scoring.
    query = text(
        f"""
        SELECT
            dc.document_id,
            d.filename,
            dc.chunk_index,
            dc.page,
            dc.text,
            ts_rank_cd(to_tsvector('{FTS_CONFIG}', dc.search_vector), {tsquery}) AS rank
        FROM document_chunks dc
        JOIN documents d ON d.id = dc.document_id
        WHERE dc.tenant_id = :tenant_id
          AND d.status = 'ready'
          AND to_tsvector('{FTS_CONFIG}', dc.search_vector) @@ {tsquery}
        ORDER BY rank DESC
        LIMIT :limit
        """
    )

    rows = session.execute(
        query,
        {"tenant_id": str(identity.tenant_id), "limit": limit},
    ).fetchall()

    candidates: list[BM25Candidate] = []
    for row in rows:
        # Role filtering - check the document's allowed_roles
        # We need to load the document to check role_may_view
        # For efficiency, we could join allowed_roles, but the JSON column
        # makes it verbose. Since limit is small, we'll filter in Python.
        doc = session.get(Document, row.document_id)
        if doc is None:
            continue
        if not doc.role_may_view(identity.role):
            continue

        candidates.append(
            BM25Candidate(
                document_id=row.document_id,
                filename=row.filename,
                chunk_index=row.chunk_index,
                page=row.page,
                text=row.text,
                score=float(row.rank),
            )
        )

    return candidates


def bm25_search_ids(
    session: Session,
    identity: AuthenticatedIdentity,
    question: str,
    limit: int = BM25_DEFAULT_LIMIT,
) -> list[tuple[uuid.UUID, int, float]]:
    """Lightweight BM25 search returning only (document_id, chunk_index, score).

    Used by hybrid retrieval to avoid loading full text when we just need
    to merge scores. The full text is loaded later from Qdrant or the DB.

    Returns empty list if the database doesn't support full-text search
    (e.g., SQLite in tests) or if any error occurs.
    """
    if not question.strip():
        return []

    tsquery = _tsquery_for_question(question)

    query = text(
        f"""
        SELECT
            dc.document_id,
            dc.chunk_index,
            ts_rank_cd(to_tsvector('{FTS_CONFIG}', dc.search_vector), {tsquery}) AS rank
        FROM document_chunks dc
        JOIN documents d ON d.id = dc.document_id
        WHERE dc.tenant_id = :tenant_id
          AND d.status = 'ready'
          AND to_tsvector('{FTS_CONFIG}', dc.search_vector) @@ {tsquery}
        ORDER BY rank DESC
        LIMIT :limit
        """
    )

    try:
        rows = session.execute(
            query,
            {"tenant_id": str(identity.tenant_id), "limit": limit},
        ).fetchall()
    except SQLAlchemyError:
        # Database doesn't support the required functions (e.g., SQLite)
        # or any other error - return empty to fall back to dense-only
        return []

    results: list[tuple[uuid.UUID, int, float]] = []
    for row in rows:
        doc = session.get(Document, row.document_id)
        if doc is None or not doc.role_may_view(identity.role):
            continue
        results.append((row.document_id, row.chunk_index, float(row.rank)))

    return results