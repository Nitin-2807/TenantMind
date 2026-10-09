"""Per-configuration answer generation and scoring for the Phase 8 benchmark.

The answer path mirrors production end to end: a configuration's retrieved
context goes through the *same* :func:`app.rag.retrieval.build_context` the query
service uses, and generation goes through the real
:class:`app.rag.generation.GenerationProvider` contract. With no LLM credential
present the benchmark drives :class:`evaluation.generation.
ExtractiveAnswerProvider` — a clearly-labelled deterministic stand-in whose
numbers measure the retrieval-to-answer grounding contract (see the evaluator
package docstring and §10–§11 of the phase report).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.identity import AuthenticatedIdentity
from app.rag.embeddings import EmbeddingProvider
from app.rag.generation import GenerationProvider
from app.rag.retrieval import build_context
from app.storage.qdrant import QdrantVectorStore
from evaluation.generation import NO_EVIDENCE_ANSWER
from evaluation.metrics import faithfulness
from evaluation.retrieval_eval import RetrievalConfig


@dataclass(frozen=True, slots=True)
class AnswerOutcome:
    """One generated answer for one configuration and question.

    ``faithfulness`` is computed at generation time against the exact evidence
    string the provider was grounded on. Relevance and correctness are computed
    by the runner against the dataset's reference answer (they need ground
    truth the generator must not see).
    """

    question_id: str
    config: RetrievalConfig
    answer: str
    refusal: bool
    grounded: bool
    faithfulness: float
    context_chunk_count: int
    retrieval_latency_ms: float
    generation_latency_ms: float
    total_latency_ms: float


def generate_and_score(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    embeddings: EmbeddingProvider,
    generation: GenerationProvider,
    settings: Settings,
    question_id: str,
    question: str,
    config: RetrievalConfig,
    top_k: int,
    candidate_limit: int,
) -> AnswerOutcome:
    """Retrieve with ``config``, build production context, generate, score.

    Mirrors the production flow: if nothing survives authorization there are no
    citations, generation is **not** called, and the outcome is a refusal —
    the same no-hallucination rule the query service enforces.
    """
    from evaluation.retrieval_eval import retrieve as do_retrieve

    started = time.perf_counter()
    outcome = do_retrieve(
        session=session,
        identity=identity,
        store=store,
        embeddings=embeddings,
        settings=settings,
        question=question,
        config=config,
        top_k=top_k,
        candidate_limit=candidate_limit,
    )
    retrieval_latency = outcome.latency_ms

    context = build_context(
        chunks=list(outcome.chunks),
        question=question,
        max_chars=settings.query_max_context_chars,
    )

    if not context.citations:
        return AnswerOutcome(
            question_id=question_id,
            config=config,
            answer="",
            refusal=True,
            grounded=False,
            faithfulness=faithfulness("", context.evidence),
            context_chunk_count=0,
            retrieval_latency_ms=retrieval_latency,
            generation_latency_ms=0.0,
            total_latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    generation_started = time.perf_counter()
    answer = generation.generate(
        instructions=context.instructions,
        question=question,
        evidence=context.evidence,
    )
    generation_latency = (time.perf_counter() - generation_started) * 1000.0

    refusal = answer == NO_EVIDENCE_ANSWER
    return AnswerOutcome(
        question_id=question_id,
        config=config,
        answer=answer,
        refusal=refusal,
        grounded=True,
        faithfulness=faithfulness(answer, context.evidence),
        context_chunk_count=len(context.citations),
        retrieval_latency_ms=retrieval_latency,
        generation_latency_ms=generation_latency,
        total_latency_ms=(time.perf_counter() - started) * 1000.0,
    )