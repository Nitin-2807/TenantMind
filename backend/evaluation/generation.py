"""Deterministic extractive answer provider for the Phase 8 benchmark.

Why it exists
-------------
No LLM credential is available in this environment (no Gemini key), so
:class:`app.rag.generation.GeminiGenerationProvider` cannot be measured. The
phase brief (§21) permits deterministic evaluation where credentials are absent
as long as its limitations are documented, and forbids fabricating results
(§34).

What this stands in for, and how it is labelled
-----------------------------------------------
:class:`ExtractiveAnswerProvider` is a genuine
:class:`app.rag.generation.GenerationProvider` that returns the retrieved
evidence sentence with the highest token overlap with the question — a
deterministic extractive-QA stand-in with ``model_name``
``extractive-eval-v1``. Its properties define what the benchmark can and cannot
claim:

* because the answer is **always a verbatim sentence from the retrieved
  context**, Faithfulness ≈ 1 by construction. Reported figures therefore
  verify the *grounding contract* (answers built from evidence, never from
  nowhere), not the faithfulness of a production LLM;
* Answer Relevance / Correctness remain meaningful: if retrieval returns the
  wrong context, the extracted sentence cannot match the reference answer, so
  these scores measure whether better retrieval produces better answers down
  the whole pipeline;
* with **empty retrieved context** the provider refuses — the same
  no-hallucination behaviour as production's ``NO_CONTEXT_ANSWER`` path.

The refusal text is a constant, never derived from tenant content.
"""

from __future__ import annotations

from app.rag.generation import GenerationProvider
from app.rag.retrieval import build_context
from evaluation.metrics import sentences, tokenize

#: Distinctive model name (never mistaken for a production model).
MODEL_NAME: str = "extractive-eval-v1"

#: Refusal used when there is no retrieved context to ground on. Authored here,
#: constant, and deliberately parallel to the production NO_CONTEXT_ANSWER.
NO_EVIDENCE_ANSWER: str = (
    "I could not answer this question: no retrieved context was available."
)


def best_evidence_sentence(question: str, evidence: str) -> str:
    """The evidence sentence with the most question-token overlap (ties: first).

    Deterministic and pure: sentence order decides ties, so the same evidence
    always yields the same answer.

    The production context builder wraps document excerpts in an
    instructions-like envelope (``<<<BEGIN/END UNTRUSTED DOCUMENT
    EXCERPTS>>>`` plus per-chunk citation lines). Those lines are not answer
    material — a grounded model is exactly supposed to ignore them — so they
    are stripped before sentence selection. Without the strip, the first
    "sentence" would be the envelope line, and the stand-in would answer with
    the wrapper itself.
    """
    cleaned = " ".join(
        line.strip()
        for line in evidence.splitlines()
        if line.strip()
        and "<<<" not in line
        and not line.lstrip().startswith("[")
    )
    if not cleaned.strip():
        return NO_EVIDENCE_ANSWER
    question_tokens = set(tokenize(question))
    best: str | None = None
    best_score = -1
    for sentence in sentences(cleaned):
        score = sum(1 for token in tokenize(sentence) if token in question_tokens)
        if score > best_score:
            best = sentence
            best_score = score
    return best if best is not None else NO_EVIDENCE_ANSWER


class ExtractiveAnswerProvider(GenerationProvider):
    """A GenerationProvider that answers with the best evidence sentence.

    Implements the real ABC (:class:`~app.rag.generation.GenerationProvider`),
    so the benchmark drives the exact ``generate`` contract production drives —
    including the instruction string, which it honours by construction: the
    answer always comes from the evidence, never from the question alone.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    @property
    def model_name(self) -> str:
        return MODEL_NAME

    def generate(self, *, instructions: str, question: str, evidence: str) -> str:
        self.calls.append(
            {"instructions": instructions, "question": question, "evidence": evidence}
        )
        if not evidence.strip():
            return NO_EVIDENCE_ANSWER
        return best_evidence_sentence(question, evidence)


def context_for(chunks: list[object], question: str, max_chars: int) -> str:
    """The production context builder's evidence string for ``chunks``.

    Reuses :func:`app.rag.retrieval.build_context` — the *same* function the
    query service uses — so the benchmark measures the production grounding
    boundary, not a copy.
    """
    from app.rag.retrieval import (
        RetrievedChunk,
    )

    typed = [chunk for chunk in chunks if isinstance(chunk, RetrievedChunk)]
    context = build_context(chunks=typed, question=question, max_chars=max_chars)
    return context.evidence