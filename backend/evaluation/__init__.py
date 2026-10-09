"""PMP Phase 8 — RAG evaluation, benchmarking and quality measurement.

This package measures how well TenantMind's retrieval pipeline works and whether
each stage (dense, BM25, hybrid/RRF, reranking) improves the final result.

Scope and honesty boundary
--------------------------
The package drives the **production retrieval components** — ``store.search_points``,
:func:`app.rag.bm25.bm25_search`, :func:`app.rag.hybrid.hybrid_retrieve`,
:func:`app.rag.hybrid.load_candidate_chunks`, :func:`app.rag.rerank.rerank_chunks`,
and the PostgreSQL re-verification layer — through thin adapters in
:mod:`evaluation.retrieval_eval`. It does **not** reimplement retrieval.

Where the environment lacks a production capability, the missing component is
either reported as *unavailable* (the reranker, §20 of the phase brief) or
replaced by a clearly-labelled deterministic stand-in:

* **Embeddings** — :mod:`evaluation.embeddings` provides a deterministic
  lexical-hash provider. No Gemini key and no ``sentence-transformers`` are
  present in this environment, so a production bi-encoder cannot be measured.
  The stand-in exercises the real dense path (embedding, Qdrant cosine search,
  RRF fusion) under controlled, reproducible conditions; its numbers are real
  for that stand-in and are labelled as such in the report.
* **Generation** — :mod:`evaluation.generation` provides a deterministic
  extractive provider. No LLM credential is present, so faithfulness and
  relevance figures measure the *retrieval-to-answer grounding contract*, not a
  production language model.

No metric in the report is fabricated, and none is claimed for a component that
did not run. See ``docs/evaluation/PHASE_8_EVALUATION_REPORT.md``.
"""

from __future__ import annotations