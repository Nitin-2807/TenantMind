"""The retrieval-augmented-generation pipeline: document in, vectors out.

PMP §7 FR-04 through FR-08, and §18's "Embedding Model". This package holds the
stages that turn a stored document into embedded chunks; the orchestration that runs
them in order and records the outcome lives in :mod:`app.services.ingestion`, and the
Qdrant writes themselves in :mod:`app.storage.qdrant`.

Layer map
---------

============================  ==================================================
:mod:`app.rag.parsers`        stored bytes → :class:`~app.rag.parsers.Page` objects,
                              one per page or slide
:mod:`app.rag.cleaning`       deterministic whitespace and invisible-character
                              normalisation
:mod:`app.rag.chunking`       text → bounded, overlapping :class:`~app.rag.chunking.Chunk`
                              objects at structural boundaries
:mod:`app.rag.embeddings`     chunks → vectors, behind :class:`~app.rag.embeddings.EmbeddingProvider`
:mod:`app.rag.vectors`        (chunk, vector) → an identified, payload-bearing
                              :class:`~app.rag.vectors.VectorPoint`
============================  ==================================================

Why the stages are separate modules and not one pipeline function
-----------------------------------------------------------------
Each is independently testable, independently replaceable, and individually
auditable — which is the property that matters most for the layer that reads
untrusted tenant documents. A single :func:`ingest` containing all five steps could
only be tested by running all five, which means a chunking bug can only be found by
running an embedding provider, and a dimension-mismatch bug can only be found by
running a vector store. Here, ``tests/test_chunking.py`` needs neither, and the
property tests in that module run thousands of configurations in milliseconds.

What is deliberately absent
---------------------------
**Retrieval.** Nothing in this package searches. PMP §13's "semantic similarity
search" and PMP §7 FR-09 are Phase 2.6, and a query path added now would have no
tests, no caller, and no way to be shown correct.

**Hybrid search, BM25, reranking.** Listed in PMP §28 as later enhancements and out
of scope here.

**LLM calls of any kind.** Cleaning is deterministic and chunking is deterministic,
and both stay that way. Document text is untrusted data (PMP §17); a stage that sent
it to a model would be creating the injection surface this project is built to avoid,
in exchange for chunk quality that no requirement asks for. Semantic (LLM) chunking is
specifically declined, and the reasoning is recorded in :mod:`app.rag.chunking`.

**Batched or concurrent execution.** Ingestion of one document is sequential and
synchronous in this phase. Parallelism is a property of the orchestrator, not of these
stages, so nothing here assumes it — see ``PROJECT_EXECUTION_LOG.md`` for the
Phase 5 plan.
"""

from __future__ import annotations

from app.rag.chunking import Chunk, chunk_text, reassemble
from app.rag.cleaning import clean_text, cleaned_text_is_empty
from app.rag.embeddings import (
    EmbeddingDimensionMismatch,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingProviderUnavailable,
    build_embedding_provider,
)
from app.rag.parsers import (
    DocumentParseError,
    DocumentParser,
    ParsedDocument,
    resolve_parser,
)
from app.rag.vectors import VectorPoint, payload_for, point_id

#: One flat alphabetical list, with no per-stage grouping.
#:
#: A grouped list would read better, but grouping means a second ordering that can
#: disagree with this one — and ``RUF022`` enforces the alphabetical one, so a comment
#: sitting beside the wrong entry is the result of trying. The grouping belongs in the
#: module docstring's layer map, which can be prose and cannot drift.
__all__ = [
    "Chunk",
    "DocumentParseError",
    "DocumentParser",
    "EmbeddingDimensionMismatch",
    "EmbeddingError",
    "EmbeddingProvider",
    "EmbeddingProviderUnavailable",
    "ParsedDocument",
    "VectorPoint",
    "build_embedding_provider",
    "chunk_text",
    "clean_text",
    "cleaned_text_is_empty",
    "payload_for",
    "point_id",
    "reassemble",
    "resolve_parser",
]
