"""Failure analysis for the Phase 8 benchmark (§16 of the phase brief).

After evaluation, each failed query is classified so the report can separate
"retrieval missed the answer" from "retrieval found it but ranked it wrong"
from "the answer path produced something unusable". Classification is pure and
deterministic: it reads a question's ground truth and one configuration's
outcome, and returns a category plus a likely cause and a recommendation.

The categories:

===================  ==========================================================
Category             Meaning
===================  ==========================================================
success              A relevant document ranked first (document level).
ranking_failure      A relevant document was retrieved but not at rank 1.
retrieval_failure    No relevant document was retrieved at all.
empty_retrieval      Retrieval returned nothing for an answerable question.
answer_failure       Retrieval succeeded (relevant doc at rank 1) but the
                     generated answer barely matches the reference — a
                     chunk-level miss or an extractive-stand-in limitation.
unanswerable_refused An unanswerable question; no context was retrieved, so
                     no answer was produced (correct behaviour).
unanswerable_context An unanswerable question; non-relevant context was
                     retrieved and an extractive answer was produced from it
                     (grounded, but not correct).
===================  ==========================================================

Only meaningful failures enter the report table (§30); the rest are summarised
by count.
"""

from __future__ import annotations

import enum
from collections.abc import Collection
from dataclasses import dataclass

#: Relevance threshold below which an otherwise-successful answer is counted an
#: answer failure. Deliberately low — the extractive stand-in answers with a
#: verbatim evidence sentence, so even a correct retrieval can produce a
#: sentence with modest lexical overlap with a reference answer.
ANSWER_RELEVANCE_THRESHOLD = 0.1


class FailureCategory(str, enum.Enum):
    SUCCESS = "success"
    RANKING_FAILURE = "ranking_failure"
    RETRIEVAL_FAILURE = "retrieval_failure"
    EMPTY_RETRIEVAL = "empty_retrieval"
    ANSWER_FAILURE = "answer_failure"
    UNANSWERABLE_REFUSED = "unanswerable_refused"
    UNANSWERABLE_CONTEXT = "unanswerable_context"


@dataclass(frozen=True, slots=True)
class Finding:
    """One classified query for one configuration, ready for the report table."""

    question_id: str
    config: str
    category: FailureCategory
    observed: str
    expected: str
    likely_cause: str
    recommendation: str


class _Entry:
    __slots__ = ("cause", "expected", "observed", "recommendation")

    def __init__(self, observed: str, expected: str, cause: str, recommendation: str) -> None:
        self.observed = observed
        self.expected = expected
        self.cause = cause
        self.recommendation = recommendation


_EXPLANATIONS: dict[FailureCategory, _Entry] = {
    FailureCategory.SUCCESS: _Entry(
        "a relevant document ranked first",
        "the relevant document at rank 1",
        "no failure; the configuration retrieved and ranked the answer correctly",
        "keep the configuration; it is the baseline other failures are compared with",
    ),
    FailureCategory.RANKING_FAILURE: _Entry(
        "a relevant document was retrieved but did not rank first",
        "the relevant document at rank 1",
        "the retriever's scoring placed a non-relevant document ahead of the relevant one",
        "investigate fusion weights and consider reranking where the reranker is available",
    ),
    FailureCategory.RETRIEVAL_FAILURE: _Entry(
        "no relevant document appeared in the retrieved set",
        "at least one expected document in the retrieved set",
        "the question's vocabulary and the answer's vocabulary overlap too little for this retriever",
        "combine with the complementary retriever (hybrid) or expand the query vocabulary",
    ),
    FailureCategory.EMPTY_RETRIEVAL: _Entry(
        "retrieval returned nothing for an answerable question",
        "at least one chunk retrieved",
        "the corpus has no chunk matching the question under this retriever's matching rules",
        "verify the document is indexed; otherwise this is a genuine absence in the corpus",
    ),
    FailureCategory.ANSWER_FAILURE: _Entry(
        "the relevant document ranked first but the generated answer barely matched the reference",
        "an answer overlapping the reference answer",
        "a chunk-level miss (right document, wrong chunk) or the extractive stand-in's lexical scoring",
        "re-rank at chunk level; with a real LLM, re-evaluate with a model judge",
    ),
    FailureCategory.UNANSWERABLE_REFUSED: _Entry(
        "no context was retrieved, so no answer was produced",
        "no fabricated answer",
        "retrieved nothing to ground on",
        "correct no-hallucination behaviour; keep",
    ),
    FailureCategory.UNANSWERABLE_CONTEXT: _Entry(
        "non-relevant context was retrieved and an extractive answer was produced from it",
        "an explicit cannot-answer response; no hallucination",
        "retrieval returned plausible-but-irrelevant chunks; the extractive stand-in cannot decline with non-empty context",
        "with a real LLM, the grounded instructions should decline; re-verify with a model judge",
    ),
}


def classify(
    *,
    answerable: bool,
    relevant_doc_ids: Collection[str],
    doc_ranks: tuple[str, ...],
    retrieved_count: int,
    answer_relevance: float | None,
    refusal: bool,
) -> FailureCategory:
    """Classify one query's outcome for one configuration.

    Args:
        answerable: the dataset's ground-truth flag.
        relevant_doc_ids: the dataset's expected document ids for this question
            (empty for unanswerable questions).
        doc_ranks: the configuration's document-level ranking.
        retrieved_count: how many candidates the primary retriever returned.
        answer_relevance: the answer-quality metric when an answer was generated
            for an answerable question; None otherwise.
        refusal: whether generation refused (no context).
    """
    if not answerable:
        if refusal:
            return FailureCategory.UNANSWERABLE_REFUSED
        return FailureCategory.UNANSWERABLE_CONTEXT
    if not doc_ranks:
        if retrieved_count == 0:
            return FailureCategory.EMPTY_RETRIEVAL
        return FailureCategory.RETRIEVAL_FAILURE
    if doc_ranks[0] in relevant_doc_ids:
        if answer_relevance is not None and answer_relevance < ANSWER_RELEVANCE_THRESHOLD:
            return FailureCategory.ANSWER_FAILURE
        return FailureCategory.SUCCESS
    if any(document in relevant_doc_ids for document in doc_ranks):
        return FailureCategory.RANKING_FAILURE
    return FailureCategory.RETRIEVAL_FAILURE


def describe(category: FailureCategory) -> tuple[str, str, str]:
    """The (observed, likely_cause, recommendation) text for a category."""
    entry = _EXPLANATIONS[category]
    return entry.observed, entry.cause, entry.recommendation


def findings_for(
    *,
    question_id: str,
    config: str,
    answerable: bool,
    relevant_doc_ids: Collection[str],
    doc_ranks: tuple[str, ...],
    retrieved_count: int,
    answer_relevance: float | None,
    refusal: bool,
) -> Finding:
    """Build a :class:`Finding` row for the report table."""
    category = classify(
        answerable=answerable,
        relevant_doc_ids=relevant_doc_ids,
        doc_ranks=doc_ranks,
        retrieved_count=retrieved_count,
        answer_relevance=answer_relevance,
        refusal=refusal,
    )
    entry = _EXPLANATIONS[category]
    return Finding(
        question_id=question_id,
        config=config,
        category=category,
        observed=entry.observed,
        expected=entry.expected,
        likely_cause=entry.cause,
        recommendation=entry.recommendation,
    )