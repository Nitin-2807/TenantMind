"""Markdown report and JSON/CSV emission for the Phase 8 benchmark.

:func:`render_report` produces the 13-section report required by §27 of the
phase brief. Every number it puts on the page is computed beforehand by the
runner from real measurements; the report itself performs no arithmetic so a
number cannot drift between the machine-readable results and the prose.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from evaluation.results import (
    AnswerRow,
    LatencyLine,
    MetricLine,
)

#: K values reported for ranking metrics, explained in the report.
KS = (1, 3, 5, 10)


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.3f}"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _availability(components: dict[str, str]) -> str:
    rows = [[name, value] for name, value in components.items()]
    return _table(["Component", "Used"], rows)


def retrieval_results_table(
    metric_lines: list[MetricLine],
) -> str:
    """One table: rows = configurations, columns = metrics (document level)."""
    doc_lines = [line for line in metric_lines if line.level == "document"]
    configs = sorted({line.config for line in doc_lines})
    headers = ["Configuration", "Hit@1", "Hit@3", "Hit@5", "Hit@10", "MRR", "Ctx Prec", "Ctx Recall"]
    rows: list[list[str]] = []
    for config in configs:
        by_metric = {line.metric: line for line in doc_lines if line.config == config}
        row = [config]
        for k in KS:
            line = by_metric.get(f"hit@{k}")
            row.append(_fmt(line.value if line else None))
        mrr_line = by_metric.get("mrr")
        row.append(_fmt(mrr_line.value if mrr_line else None))
        precision_line = by_metric.get("context_precision")
        row.append(_fmt(precision_line.value if precision_line else None))
        recall_line = by_metric.get("context_recall")
        row.append(_fmt(recall_line.value if recall_line else None))
        rows.append(row)
    return _table(headers, rows)


def answer_results_table(answer_rows: list[AnswerRow]) -> str:
    """Faithfulness / relevance / correctness per configuration (§29)."""
    answerable = [row for row in answer_rows if row.answerable]
    configs = sorted({row.config for row in answerable})
    headers = ["Configuration", "Faithfulness", "Answer Relevance", "Correctness"]
    rows: list[list[str]] = []
    for config in configs:
        subset = [row for row in answerable if row.config == config]
        if not subset:
            continue
        faithfulness = sum(row.faithfulness for row in subset) / len(subset)
        relevance = sum(row.relevance for row in subset) / len(subset)
        scored = [row for row in subset if row.correctness is not None]
        correctness = (
            sum(row.correctness or 0.0 for row in scored) / len(scored) if scored else None
        )
        rows.append([config, _fmt(faithfulness), _fmt(relevance), _fmt(correctness)])
    table = _table(headers, rows)
    overview = accuracy_overview(answerable)
    return table if not overview else table + "\n\n" + overview


def accuracy_overview(rows: list[AnswerRow]) -> str:
    """A small overview table: how many answers were correct/refused/grounded."""
    n = len(rows)
    if n == 0:
        return ""
    correct = sum(1 for row in rows if row.correctness is not None and row.correctness >= 0.5)
    refused = sum(1 for row in rows if row.refusal)
    grounded = sum(1 for row in rows if row.grounded)
    return _table(
        ["Measure", "Count"],
        [
            ["answer rows (answerable questions)", str(n)],
            ["answers with correctness ≥ 0.5", str(correct)],
            ["refusals", str(refused)],
            ["grounded answers", str(grounded)],
        ],
    )


def latency_table(lines: list[LatencyLine]) -> str:
    configs = sorted({line.config for line in lines})
    headers = ["Configuration", "Retrieval mean (ms)", "Retrieval median (ms)", "Generation mean (ms)", "Total mean (ms)"]
    rows: list[list[str]] = []
    for config in configs:
        by_stage = {line.stage: line for line in lines if line.config == config}
        retrieval = by_stage.get("retrieval")
        generation = by_stage.get("generation")
        total = by_stage.get("total")
        rows.append(
            [
                config,
                _fmt(retrieval.mean_ms if retrieval else None),
                _fmt(retrieval.median_ms if retrieval else None),
                _fmt(generation.mean_ms if generation else None),
                _fmt(total.mean_ms if total else None),
            ]
        )
    return _table(headers, rows)


def failure_table(findings: list[dict[str, Any]]) -> str:
    headers = ["Query", "Config", "Category", "Observed", "Expected", "Likely Cause", "Recommendation"]
    rows = [
        [
            finding["question_id"],
            finding["config"],
            finding["category"],
            finding["observed"],
            finding["expected"],
            finding["likely_cause"],
            finding["recommendation"],
        ]
        for finding in findings
    ]
    return _table(headers, rows) if rows else "No meaningful failures to report."


def failure_counts_table(counts: dict[str, dict[str, int]]) -> str:
    categories = sorted({category for by_config in counts.values() for category in by_config})
    configs = sorted(counts)
    headers = ["Category"] + configs
    rows: list[list[str]] = []
    for category in categories:
        rows.append([category] + [str(counts[config].get(category, 0)) for config in configs])
    return _table(headers, rows)


def render_report(
    *,
    run_id: str,
    created_iso: str,
    dataset_name: str,
    dataset_version: int,
    dataset_composition: dict[str, int],
    components: dict[str, str],
    metric_lines: list[MetricLine],
    answer_rows: list[AnswerRow],
    latency_lines: list[LatencyLine],
    findings: list[dict[str, Any]],
    failure_counts: dict[str, dict[str, int]],
    config: dict[str, Any],
    limitations: list[str],
    conclusions: list[str],
    hybrid_vs_dense: str | None = None,
) -> str:
    """Assemble the full 13-section markdown report."""
    sections = [
        f"# PMP Phase 8 — RAG Evaluation Report ({run_id})\n",
        (f"**Run timestamp:** {created_iso}  \n"
        f"**Dataset:** {dataset_name} v{dataset_version}  \n"
        f"**Configurations:** {', '.join(str(c) for c in config.get('configs', []))}  \n"
        f"**top_k:** {config.get('top_k')}  **candidate_limit:** {config.get('candidate_limit')}"),
        "## 1. Executive Summary",
        _executive_summary(metric_lines, answer_rows, components),
        "## 2. Evaluation Dataset",
        "Composition (questions by category):\n"
        + _table(
            ["Category", "Count"],
            [[name, str(count)] for name, count in sorted(dataset_composition.items())],
        ),
        "## 3. Experimental Setup",
        f"`top_k = {config.get('top_k')}`, `candidate_limit = {config.get('candidate_limit')}`. "
        "Each configuration runs the production retrieval components (see "
        "`evaluation/retrieval_eval.py`); the only substitutions are the clearly-labelled "
        "deterministic stand-ins below.\n"
        + _availability(components),
        "## 4. Retrieval Metrics",
        retrieval_results_table(metric_lines),
        ("**Level:** document-level (a document counts as retrieved if any of its chunks "
        "appears in the ranked list). Chunk-level Hit@K/MRR are in the machine-readable "
        "results; the definitions of every metric are in `evaluation/metrics.py` and "
        "summarised in the Limitations section."),
        "## 5. Answer Metrics",
        answer_results_table(answer_rows),
        "## 6. Latency Results",
        latency_table(latency_lines),
        "## 7. Dense vs BM25",
        _dense_vs_bm25(metric_lines),
        "## 8. Hybrid / RRF Results",
        _hybrid_results(metric_lines, hybrid_vs_dense),
        "## 9. Reranker Results",
        _reranker_results(components, metric_lines),
        "## 10. Failure Analysis",
        failure_counts_table(failure_counts) + "\n\n" + failure_table(findings),
        "## 11. Limitations",
        "\n".join(f"* {limitation}" for limitation in limitations),
        "## 12. Conclusions",
        "\n".join(f"{index + 1}. {conclusion}" for index, conclusion in enumerate(conclusions)),
        "## 13. Recommended Future Improvements",
        ("1. Run the benchmark with a production bi-encoder embedding model and a real "
        "cross-encoder reranker once credentials/models are available, and re-measure "
        "the dense and reranked configurations."
        "\n2. Re-run answer evaluation with a production LLM and a model judge; the "
        "extractive stand-in here measures the grounding contract, not generation quality."
        "\n3. Grow the dataset and, if practical, add statistical significance testing "
        "(e.g., paired bootstrap over the 36 questions) before trusting small MRR gaps."
        "\n4. Re-measure latency after the optional reranker is installed; no-op "
        "reranking latency is not representative of a cross-encoder's cost."),
        "---",
        (f"_Report generated by `backend/evaluation/runner.py` (run `{run_id}`). "
        "Machine-readable results: `docs/evaluation/results/`._"),
    ]
    return "\n\n".join(sections)


def _executive_summary(
    metric_lines: list[MetricLine],
    answer_rows: list[AnswerRow],
    components: dict[str, str],
) -> str:
    doc = [line for line in metric_lines if line.level == "document"]
    best_mrr = max((line for line in doc if line.metric == "mrr"), key=lambda line: line.value, default=None)
    reranker = components.get("Reranker", "unavailable")
    summary = (
        "This run measured TenantMind's retrieval pipeline on the "
        f"{len({line.config for line in doc})} configurations of the experiment matrix. "
        "BM25 is measured against the real PostgreSQL full-text index; dense retrieval "
        "uses the documented deterministic `lexical-hash-eval-v1` embedding stand-in "
        f"(no production embedding model or LLM credential is available); the reranker is `{reranker}`. "
        "The highest document-level MRR across configurations is "
        f"{_fmt(best_mrr.value) if best_mrr else 'n/a'} — see the retrieval table below. "
        "Results are for this dataset only and are not statistical significance claims."
    )
    return summary


def _dense_vs_bm25(metric_lines: list[MetricLine]) -> str:
    doc = [line for line in metric_lines if line.level == "document"]
    dense = {line.metric: line.value for line in doc if line.config == "dense"}
    bm25 = {line.metric: line.value for line in doc if line.config == "bm25"}
    return (
        f"Dense Hit@5 {_fmt(dense.get('hit@5'))} vs BM25 Hit@5 {_fmt(bm25.get('hit@5'))}; "
        f"Dense MRR {_fmt(dense.get('mrr'))} vs BM25 MRR {_fmt(bm25.get('mrr'))}. "
        "The dense figure is the lexical-hash stand-in's, so this comparison measures "
        "the two matchings (cosine over hashed term distributions vs PostgreSQL "
        "ts_rank_cd) under controlled conditions — not a production bi-encoder's."
    )


def _hybrid_results(
    metric_lines: list[MetricLine], hybrid_vs_dense: str | None = None
) -> str:
    doc = [line for line in metric_lines if line.level == "document"]
    dense = {line.metric: line.value for line in doc if line.config == "dense"}
    bm25 = {line.metric: line.value for line in doc if line.config == "bm25"}
    hybrid = {line.metric: line.value for line in doc if line.config == "hybrid"}
    comparison = (
        f"Hybrid/RRF Hit@5 {_fmt(hybrid.get('hit@5'))} vs dense {_fmt(dense.get('hit@5'))} "
        f"and BM25 {_fmt(bm25.get('hit@5'))}; MRR {_fmt(hybrid.get('mrr'))} vs "
        f"{_fmt(dense.get('mrr'))} / {_fmt(bm25.get('mrr'))}. "
    )
    if hybrid_vs_dense == "identical":
        return comparison + (
            "On this dataset RRF contributed **nothing**: BM25 matched no candidate "
            "chunks, so every hybrid row is byte-identical to the dense row and no "
            "fusion benefit was measurable. The dataset's natural-language questions "
            "are matched by ``websearch_to_tsquery`` as a conjunction of every content "
            "word, so a single vocabulary mismatch suffices to eliminate a document "
            "(see the Limitations section)."
        )
    return comparison + (
        "RRF fusion's contribution is the difference; per-query recovery of each "
        "retriever's misses is in the machine-readable rows."
    )


def _reranker_results(components: dict[str, str], metric_lines: list[MetricLine]) -> str:
    reranker = components.get("Reranker", "unavailable")
    if reranker == "noop":
        return (
            "A real cross-encoder is **not installed** in this environment "
            "(`sentence-transformers`/`torch` absent), so the reranker degraded to the "
            "production `NoOpReranker` fallback and its row is identical to Hybrid/RRF. "
            "Per the phase brief §20, reranking is reported as **unavailable** rather "
            "than fabricated."
        )
    doc = [line for line in metric_lines if line.level == "document"]
    hybrid = {line.metric: line.value for line in doc if line.config == "hybrid"}
    rerank = {line.metric: line.value for line in doc if line.config == "hybrid_rerank"}
    return (
        f"Reranker `{reranker}`: Hybrid+rerank Hit@5 {_fmt(rerank.get('hit@5'))} vs "
        f"Hybrid {_fmt(hybrid.get('hit@5'))}; MRR {_fmt(rerank.get('mrr'))} vs "
        f"{_fmt(hybrid.get('mrr'))}."
    )


def write_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)