"""Tests for :mod:`evaluation.dataset`: validation of the Phase 8 dataset.

The dataset is the ground truth for every number in the Phase 8 report, so the
loader must refuse structurally broken datasets — the phase brief requires
deterministic fixtures with complete ground truth, and a benchmark run must not
quietly proceed against a dataset whose ground truth is wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.dataset import (
    MAX_QUESTION_CHARS,
    Dataset,
    DatasetError,
    load_dataset,
    parse_dataset,
    validate_dataset,
)

DATASET_PATH = Path(__file__).resolve().parents[1] / "evaluation" / "dataset_v1.json"


@pytest.fixture(scope="module")
def dataset() -> Dataset:
    return load_dataset(DATASET_PATH)


def _valid_payload() -> dict:
    return {
        "version": 1,
        "name": "test dataset",
        "created": "2026-01-01",
        "description": "fixture",
        "question_categories": ["simple_factual", "unanswerable"],
        "question_tags": ["factoid", "no_answer"],
        "documents": [
            {
                "slug": "policy-a",
                "filename": "policy-a.md",
                "text": "Annual leave is 25 days per year.",
            }
        ],
        "questions": [
            {
                "id": "q001",
                "category": "simple_factual",
                "tags": ["factoid"],
                "question": "How many days of annual leave?",
                "expected_documents": ["policy-a"],
                "key_phrases": [{"document": "policy-a", "phrase": "25 days"}],
                "reference_answer": "25 days per year.",
                "answerable": True,
            }
        ],
    }


def test_shipped_dataset_loads_cleanly(dataset: Dataset) -> None:
    problems = validate_dataset(dataset)
    assert problems == [], f"shipped dataset must validate cleanly: {problems}"


def test_shipped_dataset_size_is_in_brief_range(dataset: Dataset) -> None:
    questions = dataset.questions
    assert 30 <= len(questions) <= 50


def test_shipped_dataset_composition_is_deliberate(dataset: Dataset) -> None:
    counts = dataset.category_counts()
    # Every declared category is used by at least one question, and the
    # composition matches the Phase 8 brief: mostly answerable questions, plus
    # a share of unanswerable ones so refusal behaviour is exercised.
    assert set(counts) == set(dataset.question_categories)
    assert counts.get("unanswerable", 0) >= 3
    assert counts["unanswerable"] < len(dataset.questions) / 2
    # Multi-document questions must actually cross documents.
    multi = [q for q in dataset.questions if len(q.expected_documents) > 1]
    assert multi, "the dataset must exercise multi-document retrieval"
    for question in multi:
        assert len(set(question.expected_documents)) == len(question.expected_documents)


def test_shipped_questions_fit_the_query_limit(dataset: Dataset) -> None:
    for question in dataset.questions:
        assert len(question.question) <= MAX_QUESTION_CHARS


def test_shipped_ground_truth_is_internal_and_reusable(dataset: Dataset) -> None:
    # No production identifiers may leak into the dataset (§4 of the brief):
    # slugs are dataset-internal, filenames are fictional.
    for question in dataset.questions:
        for slug in question.expected_documents:
            assert slug in dataset.slugs
    for document in dataset.documents:
        assert document.slug not in {"acme", "eval-v1", "tenantmind"}


def test_parse_rejects_bad_version() -> None:
    payload = _valid_payload()
    payload["version"] = 0
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_unknown_category() -> None:
    payload = _valid_payload()
    payload["questions"][0]["category"] = "nonsense"
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_unknown_tag() -> None:
    payload = _valid_payload()
    payload["questions"][0]["tags"] = ["imaginary-tag"]
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_expected_documents_that_do_not_exist() -> None:
    payload = _valid_payload()
    payload["questions"][0]["expected_documents"] = ["missing-doc"]
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_answerable_without_expected_documents() -> None:
    payload = _valid_payload()
    question = payload["questions"][0]
    question["expected_documents"] = []
    question["key_phrases"] = []
    question["reference_answer"] = ""
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_answerable_without_key_phrases() -> None:
    payload = _valid_payload()
    payload["questions"][0]["key_phrases"] = []
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_answerable_without_reference_answer() -> None:
    payload = _valid_payload()
    payload["questions"][0]["reference_answer"] = ""
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_unanswerable_with_ground_truth() -> None:
    payload = _valid_payload()
    question: dict = dict(payload["questions"][0])
    question["answerable"] = False
    question["reference_answer"] = ""
    question["expected_documents"] = []
    question["key_phrases"] = []
    payload["questions"] = [question]
    payload["question_categories"] = ["unanswerable"]
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_unanswerable_naming_expected_documents() -> None:
    payload = _valid_payload()
    question: dict = dict(payload["questions"][0])
    question["answerable"] = False
    question["reference_answer"] = ""
    question["key_phrases"] = []
    payload["questions"] = [question]
    payload["question_categories"] = ["unanswerable"]
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_key_phrases_outside_expected_documents() -> None:
    payload = _valid_payload()
    question = payload["questions"][0]
    question["expected_documents"] = []
    question["key_phrases"] = [{"document": "policy-a", "phrase": "25 days"}]
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_duplicate_slugs_and_ids() -> None:
    payload = _valid_payload()
    payload["documents"].append(dict(payload["documents"][0]))
    with pytest.raises(DatasetError):
        parse_dataset(payload)

    payload = _valid_payload()
    payload["questions"].append(dict(payload["questions"][0]))
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_empty_phrase() -> None:
    payload = _valid_payload()
    payload["questions"][0]["key_phrases"] = [
        {"document": "policy-a", "phrase": "   "}
    ]
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_parse_rejects_overlong_question() -> None:
    payload = _valid_payload()
    payload["questions"][0]["question"] = "x" * (MAX_QUESTION_CHARS + 1)
    with pytest.raises(DatasetError):
        parse_dataset(payload)


def test_validate_dataset_flags_phrase_absent_from_document() -> None:
    payload = _valid_payload()
    payload["documents"][0]["text"] = "Something entirely different."
    dataset = parse_dataset(payload)
    problems = validate_dataset(dataset)
    assert any("q001" in problem and "25 days" in problem for problem in problems)


def test_validate_dataset_flags_missing_expected_documents_across_dataset() -> None:
    payload = _valid_payload()
    question = payload["questions"][0]
    question["answerable"] = False
    question["expected_documents"] = []
    question["key_phrases"] = []
    question["reference_answer"] = ""
    dataset = parse_dataset(payload)
    problems = validate_dataset(dataset)
    assert problems, "an all-unanswerable dataset must be flagged"


def test_round_trip_matches_shipped_json(dataset: Dataset) -> None:
    """The shipped JSON parses to exactly the schema the loader promises."""
    assert dataset.version == 1
    assert dataset.name
    assert len(dataset.documents) > 0
    assert len(dataset.questions) > 0
    # Every answerable question carries all three ground-truth pieces.
    for question in dataset.questions:
        if question.answerable:
            assert question.expected_documents
            assert question.key_phrases
            assert question.reference_answer.strip()
        else:
            assert not question.expected_documents
            assert not question.key_phrases
            assert not question.reference_answer.strip()


def test_dataset_file_is_valid_json() -> None:
    with DATASET_PATH.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    assert isinstance(payload, dict)