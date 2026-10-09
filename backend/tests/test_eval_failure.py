"""Table-driven tests for the failure classifier (:mod:`evaluation.failure`).

Classification is pure and deterministic: ground truth + one configuration's
outcome in, one category out. Every branch of :func:`classify` is pinned to a
hand-constructed case, and every category has a stable explanation the report
tables use.
"""

from __future__ import annotations

from evaluation.failure import (
    ANSWER_RELEVANCE_THRESHOLD,
    FailureCategory,
    classify,
    describe,
    findings_for,
)

DOC_A = "doc-a"
DOC_B = "doc-b"


def classify_case(
    *,
    answerable: bool = True,
    relevant=("doc-a",),
    ranks: tuple[str, ...] = (),
    retrieved_count: int = 0,
    answer_relevance: float | None = None,
    refusal: bool = False,
) -> FailureCategory:
    return classify(
        answerable=answerable,
        relevant_doc_ids=relevant,
        doc_ranks=ranks,
        retrieved_count=retrieved_count,
        answer_relevance=answer_relevance,
        refusal=refusal,
    )


class TestAnswerableQuestions:
    def test_relevant_document_first_with_decent_answer_is_success(self) -> None:
        assert (
            classify_case(
                ranks=(DOC_A,),
                retrieved_count=5,
                answer_relevance=0.5,
            )
            is FailureCategory.SUCCESS
        )

    def test_relevant_first_but_poor_answer_is_answer_failure(self) -> None:
        assert (
            classify_case(
                ranks=(DOC_A,),
                retrieved_count=5,
                answer_relevance=ANSWER_RELEVANCE_THRESHOLD / 2,
            )
            is FailureCategory.ANSWER_FAILURE
        )

    def test_relevant_retrieved_but_not_first_is_ranking_failure(self) -> None:
        assert (
            classify_case(ranks=(DOC_B, DOC_A), retrieved_count=5)
            is FailureCategory.RANKING_FAILURE
        )

    def test_no_relevant_document_retrieved_is_retrieval_failure(self) -> None:
        assert (
            classify_case(ranks=(DOC_B,), retrieved_count=5)
            is FailureCategory.RETRIEVAL_FAILURE
        )

    def test_nothing_retrieved_at_all_is_empty_retrieval(self) -> None:
        assert (
            classify_case(ranks=(), retrieved_count=0)
            is FailureCategory.EMPTY_RETRIEVAL
        )

    def test_retrieved_count_without_ranks_is_still_retrieval_failure(self) -> None:
        # The primary retriever returned candidates but every one was dropped by
        # authorization or payload parsing — a retrieval-shaped failure.
        assert (
            classify_case(ranks=(), retrieved_count=3)
            is FailureCategory.RETRIEVAL_FAILURE
        )


class TestUnanswerableQuestions:
    def test_refusal_with_no_context_is_correct_behaviour(self) -> None:
        assert (
            classify_case(
                answerable=False,
                ranks=(),
                retrieved_count=0,
                refusal=True,
            )
            is FailureCategory.UNANSWERABLE_REFUSED
        )

    def test_plausible_but_irrelevant_context_is_unanswerable_context(self) -> None:
        assert (
            classify_case(
                answerable=False,
                ranks=(DOC_B,),
                retrieved_count=3,
                refusal=False,
            )
            is FailureCategory.UNANSWERABLE_CONTEXT
        )


class TestBoundaryValues:
    def test_answer_relevance_at_exactly_the_threshold_is_success(self) -> None:
        assert (
            classify_case(
                ranks=(DOC_A,),
                retrieved_count=5,
                answer_relevance=ANSWER_RELEVANCE_THRESHOLD,
            )
            is FailureCategory.SUCCESS
        )

    def test_answer_relevance_just_below_the_threshold_is_answer_failure(self) -> None:
        below = ANSWER_RELEVANCE_THRESHOLD - 1e-9
        assert (
            classify_case(
                ranks=(DOC_A,),
                retrieved_count=5,
                answer_relevance=below,
            )
            is FailureCategory.ANSWER_FAILURE
        )


class TestFindingsAndDescriptions:
    def test_findings_for_carries_the_report_fields(self) -> None:
        finding = findings_for(
            question_id="q007",
            config="dense",
            answerable=True,
            relevant_doc_ids=(DOC_A,),
            doc_ranks=(DOC_B, DOC_A),  # retrieved, but not ranked first
            retrieved_count=5,
            answer_relevance=0.2,
            refusal=False,
        )
        assert finding.question_id == "q007"
        assert finding.config == "dense"
        assert finding.category is FailureCategory.RANKING_FAILURE
        assert finding.observed
        assert finding.expected
        assert finding.likely_cause
        assert finding.recommendation

    def test_every_category_has_an_explanation(self) -> None:
        for category in FailureCategory:
            observed, cause, recommendation = describe(category)
            assert observed and cause and recommendation, category

    def test_unanswerable_refused_explanation_names_the_good_behaviour(self) -> None:
        observed, cause, recommendation = describe(FailureCategory.UNANSWERABLE_REFUSED)
        assert "no answer" in observed
        assert "no-hallucination" in recommendation.lower() or "correct" in cause.lower()