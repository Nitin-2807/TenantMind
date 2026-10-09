"""Result row types for the Phase 8 benchmark, exportable to JSON/CSV.

These are plain, serialisable records. Everything sensitive the phase brief
forbids from analytics/audit logs (§19) is likewise absent here: no password,
token, API key, document content, retrieved chunk text, prompt or model
response is stored — only question ids, rankings, and numeric scores.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class RetrievalRow:
    """One query's ranking for one configuration."""

    question_id: str
    config: str
    doc_ranks: list[str]
    chunk_ranks: list[str]
    latency_ms: float
    retrieved_count: int

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["n_docs"] = len(self.doc_ranks)
        payload["n_chunks"] = len(self.chunk_ranks)
        return payload


@dataclass(frozen=True, slots=True)
class AnswerRow:
    """One generated answer for one configuration, with its numeric scores."""

    question_id: str
    config: str
    answerable: bool
    refusal: bool
    grounded: bool
    faithfulness: float
    relevance: float
    correctness: float | None
    context_chunk_count: int
    retrieval_latency_ms: float
    generation_latency_ms: float
    total_latency_ms: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MetricLine:
    """One row of a results table: a metric at one K for one configuration."""

    config: str
    level: str
    metric: str
    value: float
    n: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class LatencyLine:
    """One latency statistic for one configuration."""

    config: str
    stage: str
    mean_ms: float
    median_ms: float
    n: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)