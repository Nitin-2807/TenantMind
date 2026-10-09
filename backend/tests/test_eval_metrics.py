"""Deterministic tests for :mod:`evaluation.metrics`.

Every assertion here is a hand-computed value, not a re-derivation of the code
under test: the phase brief (§23) requires the metrics themselves to be
unit-tested on deterministic examples and edge cases, and this file is where the
arithmetic is pinned down.
"""

from __future__ import annotations

import pytest

from evaluation.metrics import (
    context_precision,
    context_recall,
    correctness,
    document_level,
    faithfulness,
    hit_at_k,
    hit_rate,
    mean_context_precision,
    mean_context_recall,
    mean_or_zero,
    mean_reciprocal_rank,
    pivotal_tokens,
    reciprocal_rank,
    token_f1,
    tokenize,
)


class TestHitAtK:
    def test_relevant_at_rank_one(self) -> None:
        assert hit_at_k(["a", "b", "c"], {"a"}, 1) == 1.0

    def test_relevant_beyond_k_counts_as_miss(self) -> None:
        assert hit_at_k(["a", "b", "c"], {"c"}, 2) == 0.0

    def test_relevant_at_last_position_hits_when_k_is_large_enough(self) -> None:
        assert hit_at_k(["a", "b", "c"], {"c"}, 3) == 1.0

    def test_no_relevant_item_is_a_miss(self) -> None:
        assert hit_at_k(["a", "b"], {"z"}, 2) == 0.0

    def test_k_greater_than_list_length_is_clamped(self) -> None:
        # The window is the whole (short) list; still a hit if relevant is there.
        assert hit_at_k(["x"], {"x"}, 10) == 1.0
        assert hit_at_k(["x"], {"y"}, 10) == 0.0

    def test_empty_ranking_is_never_a_hit(self) -> None:
        assert hit_at_k([], {"a"}, 5) == 0.0

    def test_non_positive_k_returns_zero(self) -> None:
        assert hit_at_k(["a"], {"a"}, 0) == 0.0

    def test_empty_relevant_set_is_never_a_hit(self) -> None:
        assert hit_at_k(["a", "b"], set(), 5) == 0.0


class TestMRR:
    def test_first_relevant_at_rank_one_gives_one(self) -> None:
        assert reciprocal_rank(["a", "b", "c"], {"a"}) == 1.0

    def test_first_relevant_at_rank_three_gives_one_third(self) -> None:
        assert reciprocal_rank(["a", "b", "c"], {"c"}) == pytest.approx(1.0 / 3.0)

    def test_later_relevant_items_do_not_improve_the_rank(self) -> None:
        # The first relevant item decides; a second one at rank 2 changes nothing.
        assert reciprocal_rank(["a", "b", "c"], {"c", "b"}) == pytest.approx(1.0 / 2.0)

    def test_no_relevant_item_gives_zero(self) -> None:
        assert reciprocal_rank(["a", "b"], {"z"}) == 0.0

    def test_empty_ranking_gives_zero(self) -> None:
        assert reciprocal_rank([], {"a"}) == 0.0

    def test_mean_over_queries(self) -> None:
        ranked = [["a", "b"], ["x", "y", "z"], ["p"]]
        relevant = [{"a"}, {"z"}, {"q"}]
        mean, values = mean_reciprocal_rank(ranked, relevant)
        assert values == [1.0, pytest.approx(1.0 / 3.0), 0.0]
        assert mean == pytest.approx((1.0 + 1.0 / 3.0 + 0.0) / 3.0)

    def test_empty_query_set_mean_is_zero(self) -> None:
        mean, values = mean_reciprocal_rank([], [])
        assert mean == 0.0
        assert values == []


class TestHitRate:
    def test_mixed_queries(self) -> None:
        ranked = [["a", "b"], ["c", "d"]]
        relevant = [{"a"}, {"z"}]
        mean, values = hit_rate(ranked, relevant, 2)
        assert values == [1.0, 0.0]
        assert mean == 0.5


class TestContextPrecision:
    def test_all_retrieved_relevant(self) -> None:
        assert context_precision(["a", "b", "c"], {"a", "b", "c"}) == 1.0

    def test_one_of_three(self) -> None:
        assert context_precision(["a", "x", "y"], {"a"}) == pytest.approx(1.0 / 3.0)

    def test_empty_retrieval_is_zero_not_undefined(self) -> None:
        assert context_precision([], {"a"}) == 0.0

    def test_duplicates_in_retrieval_are_counted(self) -> None:
        # Two copies of the same chunk both count as retrieved items.
        assert context_precision(["a", "a"], {"a"}) == 1.0
        assert context_precision(["a", "a", "b"], {"a"}) == pytest.approx(2.0 / 3.0)


class TestContextRecall:
    def test_all_relevant_retrieved(self) -> None:
        assert context_recall(["a", "b"], {"a", "b"}) == 1.0

    def test_half_the_relevant_retrieved(self) -> None:
        assert context_recall(["a"], {"a", "b"}) == 0.5

    def test_empty_retrieval_is_zero(self) -> None:
        assert context_recall([], {"a", "b"}) == 0.0

    def test_empty_relevant_is_zero_and_excluded_from_the_aggregate(self) -> None:
        ranked = [["a"], ["x", "y"], []]
        relevant = [{"a"}, set(), {"b"}]
        mean, counted, values = mean_context_recall(ranked, relevant)
        assert counted == 2  # query 2 (empty relevant) is excluded
        assert values[0] == 1.0
        assert values[1] == 0.0
        assert mean == 0.5

    def test_precision_aggregate_counts_every_query(self) -> None:
        ranked = [["a"], ["x", "y"], []]
        relevant = [{"a"}, set(), {"b"}]
        mean, counted, _values = mean_context_precision(ranked, relevant)
        assert counted == 3
        assert mean == pytest.approx((1.0 + 0.0 + 0.0) / 3.0)


class TestDocumentLevelProjection:
    def test_deduplicates_keeping_first_rank(self) -> None:
        rows = [[("d1", 0), ("d2", 0), ("d1", 1), ("d3", 0)]]
        assert document_level(rows) == [["d1", "d2", "d3"]]

    def test_empty_rows_project_to_empty_lists(self) -> None:
        assert document_level([[], []]) == [[], []]


class TestTokenizationAndF1:
    def test_tokenize_lowercases_and_drops_stopwords(self) -> None:
        assert tokenize("The Quick Fox and the Lazy Dog") == ["quick", "fox", "lazy", "dog"]

    def test_tokenize_handles_punctuation_and_numbers(self) -> None:
        # Single-character tokens (e.g. the digits "2" or "m") are dropped by
        # the content-token rule; 2+ char tokens survive.
        assert tokenize("UPLOAD-413 exceeded: $2.4m!") == ["upload", "413", "exceeded", "4m"]

    def test_token_f1_identical_is_one(self) -> None:
        assert token_f1("annual leave is 25 days", "annual leave is 25 days") == pytest.approx(1.0)

    def test_token_f1_disjoint_is_zero(self) -> None:
        assert token_f1("alpha bravo", "charlie delta") == 0.0

    def test_token_f1_partial_overlap(self) -> None:
        left = "retained for 90 days"
        right = "system logs retained for 90 days"
        # left tokens {retained,90,days}; right {system,logs,retained,90,days}
        # precision 3/3, recall 3/5 -> F1 = 2*(1*0.6)/(1+0.6) = 0.75
        assert token_f1(left, right) == pytest.approx(0.75)

    def test_token_f1_two_empty_texts_is_zero(self) -> None:
        assert token_f1("", "") == 0.0


class TestFaithfulness:
    def test_answer_sentences_all_in_evidence_is_one(self) -> None:
        evidence = "Password rotation happens every 90 days. MFA is mandatory."
        answer = "Password rotation happens every 90 days."
        assert faithfulness(answer, evidence) == 1.0

    def test_one_unsupported_sentence_lowers_the_score(self) -> None:
        evidence = "Password rotation happens every 90 days."
        answer = "Password rotation happens every 90 days. The moon is blue cheese."
        assert faithfulness(answer, evidence) == pytest.approx(0.5)

    def test_empty_answer_over_empty_evidence_is_one(self) -> None:
        # Nothing claimed, nothing to support: the refusal path (answer_eval
        # records refusals with an empty answer text).
        assert faithfulness("", "") == 1.0

    def test_refusal_with_content_tokens_is_unsupported_against_empty_evidence(self) -> None:
        # A sentence with content tokens claims something and is *not* supported
        # by empty evidence; answer_eval therefore records refusals as empty
        # answers (see the test above), never as a sentence.
        assert faithfulness("I could not answer this question!", "") == 0.0

    def test_partial_token_support_lowers_a_sentence(self) -> None:
        evidence = "Leave accrues monthly."
        answer = "Leave accrues weekly."
        # "weekly" is not in the evidence bag, so the sentence is unsupported.
        assert faithfulness(answer, evidence) == 0.0


class TestCorrectness:
    def test_all_pivotal_tokens_present(self) -> None:
        question = "How many days of leave?"
        reference = "25 days of leave."
        answer = "Employees receive 25 days of leave."
        # pivotal = reference minus question = {25}
        assert correctness(answer, question, reference) == 1.0

    def test_missing_pivotal_token(self) -> None:
        question = "How many days of leave?"
        reference = "25 days of leave."
        answer = "Employees receive leave."
        assert correctness(answer, question, reference) == 0.0

    def test_question_tokens_do_not_count(self) -> None:
        question = "How many days of leave?"
        reference = "25 days of leave in Europe."
        answer = "In Europe."
        # pivotal = reference minus question = {25, europe}
        assert correctness(answer, question, reference) == 0.5

    def test_reference_restating_the_question_scores_zero(self) -> None:
        question = "annual leave days"
        reference = "days annual leave"
        assert correctness("anything", question, reference) == 0.0

    def test_pivotal_tokens_are_the_reference_minus_question(self) -> None:
        assert set(pivotal_tokens("How long retained?", "retained for 7 years")) == {
            "years",
        }
        assert set(pivotal_tokens("How long retained?", "retained for 25 years")) == {
            "25",
            "years",
        }


class TestMiscellany:
    def test_mean_or_zero(self) -> None:
        assert mean_or_zero([1.0, 0.0, 0.5]) == pytest.approx(0.5)
        assert mean_or_zero([]) == 0.0

    def test_metrics_are_deterministic(self) -> None:
        ranked = [["a", "b", "c"], ["x", "y"]]
        relevant = [{"b"}, {"z"}]
        first = (reciprocal_rank(ranked[0], relevant[0]), token_f1("a b", "a b"))
        second = (reciprocal_rank(ranked[0], relevant[0]), token_f1("a b", "a b"))
        assert first == second

    def test_zip_strictness_rejects_mismatched_inputs(self) -> None:
        with pytest.raises(ValueError):
            mean_reciprocal_rank([["a"]], [{"a"}, {"b"}])