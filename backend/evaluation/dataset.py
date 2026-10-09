"""The PMP Phase 8 evaluation dataset: deterministic corpus and ground truth.

Designed around the requirements in the phase brief (§3–§4): 30–50 questions
covering simple facts, multi-document questions, specific details, context
questions, similar terminology, BM25/semantic/hybrid-favoured questions, and
unanswerable questions — each with ground truth for retrieval and answer
evaluation.

Structure (``dataset_v1.json``)
-------------------------------
Two halves, both fully deterministic and fictional:

* ``documents`` — corpus documents as ``(slug, filename, text)``. Slugs are
  dataset-internal ids; the benchmark seed step resolves them to real UUIDs
  when the corpus is ingested into a tenant. No production tenant, user or
  document id is ever hard-coded.
* ``questions`` — each question carries:
    * ``expected_documents`` — the dataset slugs whose content contains the
      answer (empty for unanswerable questions). This anchors the
      **document-level** retrieval metrics.
    * ``key_phrases`` — distinctive phrases that identify the answering
      *chunks* inside those documents. Resolved against the actually-indexed
      ``document_chunks`` rows at run time, which anchors the **chunk-level**
      retrieval metrics without guessing chunk boundaries up front.
    * ``reference_answer`` — an ideal answer, used by the answer-quality
      heuristics.

Validation (:func:`validate_dataset`)
-------------------------------------
Loading refuses structurally broken datasets (unknown slugs, empty phrases,
answerable questions without ground truth, unanswerable questions *with* ground
truth) so that a benchmark run cannot quietly proceed on a dataset whose
ground truth is wrong — the one way this phase could produce numbers that look
real but are not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Longest question the benchmark will accept. Bound to the application limit
#: (:attr:`app.core.config.Settings.query_max_question_chars`) so every question
#: in the dataset is a question the real query path would accept as-is.
MAX_QUESTION_CHARS = 500


class DatasetError(ValueError):
    """The dataset failed structural validation."""


@dataclass(frozen=True, slots=True)
class DatasetDocument:
    """One corpus document, identified by a dataset-internal slug."""

    slug: str
    filename: str
    text: str


@dataclass(frozen=True, slots=True)
class KeyPhrase:
    """A distinctive phrase locating the answer inside one expected document."""

    document: str
    phrase: str


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    """One benchmark question with its ground truth."""

    id: str
    category: str
    tags: tuple[str, ...]
    question: str
    expected_documents: tuple[str, ...]
    key_phrases: tuple[KeyPhrase, ...]
    reference_answer: str
    answerable: bool


@dataclass(frozen=True, slots=True)
class Dataset:
    """The full evaluation dataset: corpus plus questions."""

    version: int
    name: str
    created: str
    description: str
    documents: tuple[DatasetDocument, ...]
    questions: tuple[EvalQuestion, ...]
    question_categories: tuple[str, ...]
    question_tags: tuple[str, ...]

    def document(self, slug: str) -> DatasetDocument:
        """The document with ``slug``, or raise :class:`DatasetError`."""
        for document in self.documents:
            if document.slug == slug:
                return document
        raise DatasetError(f"unknown document slug {slug!r}")

    def question(self, question_id: str) -> EvalQuestion:
        """The question with ``id``, or raise :class:`DatasetError`."""
        for question in self.questions:
            if question.id == question_id:
                return question
        raise DatasetError(f"unknown question id {question_id!r}")

    @property
    def slugs(self) -> tuple[str, ...]:
        return tuple(document.slug for document in self.documents)

    def category_counts(self) -> dict[str, int]:
        """Question counts per category, for the dataset-composition report."""
        counts: dict[str, int] = {}
        for question in self.questions:
            counts[question.category] = counts.get(question.category, 0) + 1
        return counts


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DatasetError(message)


def _as_list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise DatasetError(f"{where}: expected a list, got {type(value).__name__}")
    return value


def _as_str(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise DatasetError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _document(raw: dict[str, Any], index: int) -> DatasetDocument:
    where = f"documents[{index}]"
    slug = _as_str(raw.get("slug"), where).strip()
    filename = _as_str(raw.get("filename"), where)
    text = _as_str(raw.get("text"), where)
    _require(bool(slug), f"{where}: slug must be non-empty")
    _require(bool(filename), f"{where}: filename must be non-empty")
    _require(bool(text), f"{where}: text must be non-empty")
    return DatasetDocument(slug=slug, filename=filename, text=text)


def _question(
    raw: dict[str, Any],
    index: int,
    *,
    categories: set[str],
    tags: set[str],
    slugs: set[str],
) -> EvalQuestion:
    where = f"questions[{index}]"
    question_id = _as_str(raw.get("id"), where).strip()
    category = _as_str(raw.get("category"), where).strip()
    question = _as_str(raw.get("question"), where).strip()
    reference = _as_str(raw.get("reference_answer"), where)
    answerable_raw = raw.get("answerable")
    if not isinstance(answerable_raw, bool):
        raise DatasetError(f"{where}: answerable must be a bool")

    raw_tags = _as_list(raw.get("tags", []), where)
    unknown_tags = [t for t in raw_tags if not isinstance(t, str) or t not in tags]
    _require(not unknown_tags, f"{where}: unknown tags {unknown_tags}")

    raw_expected = _as_list(raw.get("expected_documents", []), where)
    expected = tuple(_as_str(item, f"{where}.expected_documents") for item in raw_expected)
    unknown_docs = [slug for slug in expected if slug not in slugs]
    _require(not unknown_docs, f"{where}: unknown expected documents {unknown_docs}")

    raw_phrases = _as_list(raw.get("key_phrases", []), where)
    phrases: list[KeyPhrase] = []
    for phrase_index, item in enumerate(raw_phrases):
        if not isinstance(item, dict):
            raise DatasetError(f"{where}.key_phrases[{phrase_index}]: expected an object")
        doc = _as_str(item.get("document"), where).strip()
        phrase = _as_str(item.get("phrase"), where).strip()
        _require(bool(phrase), f"{where}.key_phrases[{phrase_index}]: phrase is empty")
        phrases.append(KeyPhrase(document=doc, phrase=phrase))

    _require(bool(question_id), f"{where}: id must be non-empty")
    _require(bool(question), f"{where}: question must be non-empty")
    _require(
        len(question) <= MAX_QUESTION_CHARS,
        f"{where}: question is {len(question)} chars, over the {MAX_QUESTION_CHARS} limit",
    )
    _require(category in categories, f"{where}: unknown category {category!r}")
    _require(
        not (answerable_raw and not expected),
        f"{where}: answerable questions must name expected documents",
    )
    _require(
        not (answerable_raw and not phrases),
        f"{where}: answerable questions must name key phrases",
    )
    _require(
        not (answerable_raw and not reference),
        f"{where}: answerable questions must carry a reference answer",
    )
    _require(
        not (not answerable_raw and expected),
        f"{where}: unanswerable questions must not name expected documents",
    )
    _require(
        not (not answerable_raw and phrases),
        f"{where}: unanswerable questions must not name key phrases",
    )
    _require(
        not (not answerable_raw and reference),
        f"{where}: unanswerable questions must not carry a reference answer",
    )
    phrase_docs = {phrase.document for phrase in phrases}
    _require(
        phrase_docs <= set(expected),
        f"{where}: key phrases must reference expected documents, got {phrase_docs}",
    )

    return EvalQuestion(
        id=question_id,
        category=category,
        tags=tuple(t for t in raw_tags if isinstance(t, str)),
        question=question,
        expected_documents=expected,
        key_phrases=tuple(phrases),
        reference_answer=reference,
        answerable=answerable_raw,
    )


def parse_dataset(payload: dict[str, Any]) -> Dataset:
    """Build a :class:`Dataset` from parsed JSON, validating its structure."""
    version = payload.get("version")
    if not isinstance(version, int) or version < 1:
        raise DatasetError("version must be a positive integer")
    name = _as_str(payload.get("name"), "name").strip()
    created = _as_str(payload.get("created", ""), "created")
    description = _as_str(payload.get("description", ""), "description")
    _require(bool(name), "name must be non-empty")

    categories = tuple(_as_str(c, "question_categories") for c in _as_list(payload.get("question_categories", []), "question_categories"))
    tags = tuple(_as_str(t, "question_tags") for t in _as_list(payload.get("question_tags", []), "question_tags"))
    categories_set = set(categories)
    tags_set = set(tags)

    documents = tuple(
        _document(raw, index)
        for index, raw in enumerate(_as_list(payload.get("documents", []), "documents"))
        if isinstance(raw, dict)
    )
    _require(len(documents) > 0, "the dataset must contain at least one document")
    slugs = [document.slug for document in documents]
    _require(len(slugs) == len(set(slugs)), "document slugs must be unique")

    questions = tuple(
        _question(
            raw,
            index,
            categories=categories_set,
            tags=tags_set,
            slugs=set(slugs),
        )
        for index, raw in enumerate(_as_list(payload.get("questions", []), "questions"))
        if isinstance(raw, dict)
    )
    _require(len(questions) > 0, "the dataset must contain at least one question")
    ids = [question.id for question in questions]
    _require(len(ids) == len(set(ids)), "question ids must be unique")

    return Dataset(
        version=version,
        name=name,
        created=created,
        description=description,
        documents=documents,
        questions=questions,
        question_categories=categories,
        question_tags=tags,
    )


def load_dataset(path: str | Path) -> Dataset:
    """Load and validate a dataset from a JSON file."""
    with Path(path).open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise DatasetError("dataset file must contain a JSON object")
    return parse_dataset(payload)


def validate_dataset(dataset: Dataset) -> list[str]:
    """Cross-field validation beyond :func:`parse_dataset`.

    Returns a list of problems (empty when the dataset is healthy). Called by
    the benchmark runner before any measurement so a dataset-quality defect is
    never silently measured around.
    """
    problems: list[str] = []
    for question in dataset.questions:
        for phrase in question.key_phrases:
            document = dataset.document(phrase.document)
            if phrase.phrase not in document.text:
                problems.append(
                    f"{question.id}: key phrase {phrase.phrase!r} is absent from "
                    f"document {phrase.document!r}"
                )
    if not any(question.expected_documents for question in dataset.questions):
        problems.append(
            "no question in the dataset names any expected document; retrieval "
            "metrics would be undefined"
        )
    return problems