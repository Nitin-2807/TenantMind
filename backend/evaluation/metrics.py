"""Deterministic retrieval and answer-quality metrics for PMP Phase 8.

Every function here is pure over values: a query is a ranked list of item ids
and a set of relevant ids, and everything below is arithmetic on those two
inputs. That is what lets ``tests/test_eval_metrics.py`` pin each formula to
hand-computed examples (§23 of the phase brief).

Definitions (documented; the same numbers the report uses)
----------------------------------------------------------
* **Hit@K** — 1 if at least one relevant item appears in the top K of the
  ranked list, else 0. Averaged over queries.
* **MRR** — mean reciprocal rank: 1 / (1-based rank of the first relevant item),
  or 0 for a query with no relevant item in the list. Averaged over queries.
* **Context Precision** — for one query, the fraction of retrieved items that
  are relevant: ``|retrieved ∩ relevant| / |retrieved|``. 0 when nothing was
  retrieved. Averaged only over queries where the metric is defined.
* **Context Recall** — for one query, the fraction of relevant items that were
  retrieved: ``|retrieved ∩ relevant| / |relevant|``. 0 when nothing was
  retrieved; undefined (excluded from the aggregate) when the query has no
  relevant items — an unanswerable question has ``relevant = ∅``, and "recall
  of nothing" is not a number.
* **Faithfulness** — the fraction of an answer's sentences whose content tokens
  all appear in the retrieved evidence (claim-level support). See
  :func:`faithfulness` for the exact heuristic and its limitations.
* **Answer Relevance** — token F1 between the generated answer and the
  reference answer.
* **Correctness** — the fraction of the reference answer's *pivotal* tokens
  (reference tokens that are not already in the question) that appear in the
  generated answer. Only meaningful for answerable questions with a ground
  truth that adds facts beyond the question.

Level choice
------------
These functions are level-agnostic: the caller decides whether each item id is
a document id or a chunk id, and states so in the report. The benchmark
computes document-level and chunk-level rankings with the same code.

None of these functions is a statistical significance test. With ~36 questions,
the phase report explicitly declines to claim significance (§18 of the brief).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Sequence

#: ``Ranked items`` and ``relevant sets`` are opaque ids. Document-level calls
#: pass document ids; chunk-level calls pass ``"<doc_uuid>:<chunk_index>"``.
RankedItems = Sequence[str]
RelevantItems = Collection[str]


def hit_at_k(ranked: RankedItems, relevant: RelevantItems, k: int) -> float:
    """1.0 if any relevant item appears in the top ``k``, else 0.0.

    ``k`` is clamped to the list length, so a query whose whole list is shorter
    than ``k`` is evaluated against the shorter window — the strict reading of
    "the top K" when fewer than K items exist.
    """
    window = ranked[:k]
    return 1.0 if any(item in relevant for item in window) else 0.0


def reciprocal_rank(ranked: RankedItems, relevant: RelevantItems) -> float:
    """``1 / rank`` of the first relevant item, or 0.0 when there is none.

    Ranks are 1-based: the item at index 0 has rank 1. Search stops at the
    first relevant item, which is what makes MRR reward *early* relevance.
    """
    for index, item in enumerate(ranked, start=1):
        if item in relevant:
            return 1.0 / index
    return 0.0


def hit_rate(ranked: Sequence[RankedItems], relevant: Sequence[RelevantItems], k: int) -> tuple[float, list[float]]:
    """Mean Hit@K over queries, plus the per-query values.

    Returns:
        ``(mean, values)``. ``mean`` is 0.0 over an empty query set; ``values``
        is empty then too. The caller reports the query count separately.
    """
    values = [hit_at_k(ranks, rel, k) for ranks, rel in zip(ranked, relevant, strict=True)]
    return (mean_or_zero(values), values)


def mean_reciprocal_rank(
    ranked: Sequence[RankedItems], relevant: Sequence[RelevantItems]
) -> tuple[float, list[float]]:
    """Mean MRR over queries, plus the per-query values (0.0 for an empty set)."""
    values = [reciprocal_rank(ranks, rel) for ranks, rel in zip(ranked, relevant, strict=True)]
    return (mean_or_zero(values), values)


def context_precision(ranked: RankedItems, relevant: RelevantItems) -> float:
    """Fraction of retrieved items that are relevant; 0.0 when nothing returned."""
    if not ranked:
        return 0.0
    hits = sum(1 for item in ranked if item in relevant)
    return hits / len(ranked)


def context_recall(ranked: RankedItems, relevant: RelevantItems) -> float:
    """Fraction of relevant items retrieved; 0.0 when nothing returned.

    Undefined when ``relevant`` is empty — the caller decides whether to include
    such a query in the aggregate.
    """
    if not relevant:
        return 0.0
    hits = sum(1 for item in relevant if item in ranked)
    return hits / len(relevant)


def mean_context_precision(
    ranked: Sequence[RankedItems], relevant: Sequence[RelevantItems]
) -> tuple[float, int, list[float]]:
    """Mean Context Precision over queries with a defined value.

    Returns:
        ``(mean, n, values)`` where ``n`` is the number of queries averaged.
        A query always has a defined precision (the retrieved set is nonempty
        here), but the count is returned so the report can state it.
    """
    values = [context_precision(ranks, rel) for ranks, rel in zip(ranked, relevant, strict=True)]
    return (mean_or_zero(values), len(values), values)


def mean_context_recall(
    ranked: Sequence[RankedItems], relevant: Sequence[RelevantItems]
) -> tuple[float, int, list[float]]:
    """Mean Context Recall over queries with a **defined** value.

    Queries with empty ``relevant`` are excluded (recall of an empty target is
    undefined), and ``n`` reports how many queries were averaged.
    """
    values: list[float] = []
    counted = 0
    for ranks, rel in zip(ranked, relevant, strict=True):
        if not rel:
            continue
        counted += 1
        values.append(context_recall(ranks, rel))
    return (mean_or_zero(values), counted, values)


def _unique_in_order(items: Iterable[str]) -> list[str]:
    """Deduplicate an ordered list, keeping first occurrences in order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def document_level(ranked: Sequence[Sequence[tuple[str, int]]]) -> list[list[str]]:
    """Project chunk-level rankings ``(doc_id, index)`` onto document ids.

    Order is preserved and duplicates removed, so the first chunk of a document
    determines that document's rank — the standard reading of "document-level
    ranking".
    """
    return [_unique_in_order(doc_id for doc_id, _ in chunk_row) for chunk_row in ranked]


# ---------------------------------------------------------------------------
# Answer-quality heuristics
# ---------------------------------------------------------------------------

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")

#: Small, fixed English stopword set. Deterministic by construction; used only to
#: keep the heuristics' signal on content words, never to pretend a model judged
#: anything.
_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "i", "in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were", "will", "with", "would", "can", "could", "do", "does", "did", "not", "no", "yes", "what", "which", "who", "whom", "how", "when", "where", "why"]
)


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords removed, order preserved."""
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in _STOPWORDS and len(token) > 1
    ]
    return tokens


def _counter(tokens: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    return counts


def token_f1(left: str, right: str) -> float:
    """Term-frequency F1 between two texts (the Answer Relevance heuristic).

    F1 = 2 * precision * recall / (precision + recall) over token *counts*,
    with 0.0 for two tokenless texts. Deterministic; its limitations (lexical
    only, no synonymy, no word order) are documented in the report.
    """
    a = _counter(tokenize(left))
    b = _counter(tokenize(right))
    if not a and not b:
        return 0.0
    overlap = sum(min(a.get(t, 0), b.get(t, 0)) for t in set(a) | set(b))
    if not a or not b:
        return 0.0
    precision = overlap / sum(a.values())
    recall = overlap / sum(b.values())
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def sentences(text: str) -> list[str]:
    """Split ``text`` on sentence boundaries. Deterministic; crude by design."""
    parts = [part.strip() for part in _SENTENCE_BOUNDARY.split(text) if part.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def faithfulness(answer: str, evidence: str) -> float:
    """Fraction of the answer's sentences supported by the evidence tokens.

    For each sentence, every content token must appear in the evidence token
    bag for the sentence to count as supported; the metric is the supported
    fraction. ``evidence`` is the concatenated retrieved chunk text — the same
    text the generation provider was grounded on.

    Limitations (documented in the report): lexical containment is necessary
    but not sufficient for faithful generation, and this heuristic does not
    test entailment, paraphrasing or negation. It is the deterministic,
    credential-free check the phase brief's §21 permits, not a model judge.
    """
    evidence_tokens = _counter(tokenize(evidence))
    answer_sentences = sentences(answer)
    if not answer_sentences:
        return 1.0 if not answer.strip() else 0.0
    supported = 0
    for sentence in answer_sentences:
        needed = _counter(tokenize(sentence))
        if not needed:
            supported += 1
            continue
        if all(evidence_tokens.get(token, 0) >= count for token, count in needed.items()):
            supported += 1
    return supported / len(answer_sentences)


def pivotal_tokens(question: str, reference: str) -> list[str]:
    """Reference tokens that add information beyond the question.

    Correctness is measured on these tokens, so a reference that merely
    restates the question scores nothing — the metric is about the answer, not
    the echo.
    """
    question_tokens = set(tokenize(question))
    return [token for token in tokenize(reference) if token not in question_tokens]


def correctness(answer: str, question: str, reference: str) -> float:
    """Fraction of the reference's pivotal tokens present in the answer.

    0.0 when the reference adds no tokens beyond the question (nothing to be
    correct about); 1.0 when the answer contains every pivotal fact token.
    """
    pivotal = pivotal_tokens(question, reference)
    if not pivotal:
        return 0.0
    answer_tokens = set(tokenize(answer))
    present = sum(1 for token in pivotal if token in answer_tokens)
    return present / len(pivotal)


def mean_or_zero(values: Sequence[float]) -> float:
    """Arithmetic mean, or 0.0 when ``values`` is empty."""
    if not values:
        return 0.0
    return sum(values) / len(values)