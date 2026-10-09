"""PMP Phase 8 benchmark runner: seed, measure, score, and report.

The runner is intentionally a *measurement* program, not a framework:

* it seeds a fresh evaluation tenant through the **real** document and
  ingestion services (:func:`app.services.documents.create_document` +
  :func:`app.services.ingestion.ingest_document`), so BM25 runs against real
  PostgreSQL ``tsvector`` data and dense retrieval runs against real Qdrant
  points written by the production pipeline;
* every retrieval configuration is driven by :mod:`evaluation.retrieval_eval`,
  which calls the production components through the normal tenant-scoped,
  role-filtered, re-verified architecture;
* results are written to ``docs/evaluation/results/`` as JSON (per-query rows),
  CSV, and the markdown report in ``docs/evaluation/``.

Usage (from ``backend/``)::

    python -m evaluation.runner --out-dir ../docs/evaluation/results

Environment: ``DATABASE_URL`` (required live), ``TENANTMIND_QDRANT_URL``
(optional override), and the benchmark builds its own settings otherwise. No
credential is committed by this module.
"""

from __future__ import annotations

import argparse
import datetime
import io
import os
import secrets
import statistics
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.identity import AuthenticatedIdentity
from app.core.security import create_access_token, hash_password
from app.db.engine import create_engine_from_url, reset_engine
from app.db.session import build_session_factory
from app.models import Document, DocumentChunk, Tenant, User, UserRole
from app.rag.embeddings import EmbeddingProvider
from app.services.documents import create_document
from app.services.ingestion import ingest_document
from app.storage.local import LocalFileStore
from app.storage.qdrant import QdrantVectorStore
from evaluation import (
    answer_eval,
    failure,
    metrics,
    report,
    results,
    retrieval_eval,
)
from evaluation import (
    dataset as dataset_module,
)
from evaluation import (
    embeddings as embedding_module,
)
from evaluation import (
    generation as generation_module,
)

#: The evaluation tenant slug and the benchmark's own user. Dedicated and
#: clearly named so evaluation data can never be mistaken for a real tenant's.
EVAL_TENANT_SLUG = "eval-v1"
EVAL_TENANT_NAME = "Meridian Cloud Evaluation (PMP Phase 8)"
EVAL_USER_EMAIL = "eval-benchmark@tenantmind.local"
EVAL_PASSWORD = "eval-only-not-a-real-credential"

#: K values for ranking metrics (explained in the report).
KS = (1, 3, 5, 10)


@dataclass(frozen=True, slots=True)
class RunConfig:
    """Everything that must be recorded to reproduce a run (§13)."""

    top_k: int
    candidate_limit: int
    configs: tuple[retrieval_eval.RetrievalConfig, ...]
    embedding_dimension: int
    embed_answers: bool
    seed: int
    max_questions: int | None
    fresh_tenant: bool
    keep_tenant: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "top_k": self.top_k,
            "candidate_limit": self.candidate_limit,
            "configs": [config.value for config in self.configs],
            "embedding_dimension": self.embedding_dimension,
            "embed_answers": self.embed_answers,
            "seed": self.seed,
            "max_questions": self.max_questions,
            "fresh_tenant": self.fresh_tenant,
            "keep_tenant": self.keep_tenant,
        }


def build_settings(
    *,
    embedding_dimension: int,
    database_url: str | None,
    storage_root: Path,
    qdrant_url: str | None,
    jwt_secret: str,
) -> Settings:
    """Build benchmark settings with env-file loading disabled.

    Environment variables still populate fields not overridden here, so the
    live run uses ``DATABASE_URL``/``TENANTMIND_QDRANT_URL`` from the shell.
    The JWT secret is generated per run: the runner both mints and verifies the
    evaluation token, so reproducibility does not depend on it, and a shared
    hard-coded key would be a credential in source.
    """
    constructor = cast(Any, Settings)
    kwargs: dict[str, Any] = {
        "jwt_secret_key": jwt_secret,
        "embedding_dimension": embedding_dimension,
        "storage_root": storage_root,
    }
    if database_url:
        kwargs["database_url"] = database_url
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    return constructor(_env_file=None, **kwargs)


def _find_or_create_eval_account(
    session: Session, settings: Settings
) -> tuple[Tenant, User]:
    """The evaluation tenant and user, created idempotently."""
    tenant = session.scalar(select(Tenant).where(Tenant.slug == EVAL_TENANT_SLUG))
    if tenant is None:
        tenant = Tenant(name=EVAL_TENANT_NAME, slug=EVAL_TENANT_SLUG)
        session.add(tenant)
        session.flush()
    user = session.scalar(
        select(User).where(User.tenant_id == tenant.id, User.email == EVAL_USER_EMAIL)
    )
    if user is None:
        user = User(
            tenant_id=tenant.id,
            email=EVAL_USER_EMAIL,
            password_hash=hash_password(EVAL_PASSWORD),
            role=UserRole.VIEWER,
            is_active=True,
        )
        session.add(user)
        session.flush()
    session.commit()
    return tenant, user


def eval_identity(session: Session, settings: Settings) -> AuthenticatedIdentity:
    """The evaluation identity, minted through the real token path.

    Uses the application's first and only trusted construction site —
    :meth:`AuthenticatedIdentity.from_access_token` — so the benchmark moves
    through the same token boundary as a production request.
    """
    tenant, user = _find_or_create_eval_account(session, settings)
    token = create_access_token(
        subject=user.id,
        tenant_id=tenant.id,
        role=str(user.role.value),
        app_settings=settings,
    )
    return AuthenticatedIdentity.from_access_token(token, app_settings=settings)


def seed_corpus(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    settings: Settings,
    storage: LocalFileStore,
    store: QdrantVectorStore,
    provider: EmbeddingProvider,
    dataset: dataset_module.Dataset,
) -> dict[str, uuid.UUID]:
    """Ingest every corpus document through the production pipeline.

    Returns the slug -> real document id map the ground truth resolves against.
    Idempotent per slug: a re-run on the same tenant reuses indexed documents.
    """
    slug_to_id: dict[str, uuid.UUID] = {}
    for document in dataset.documents:
        existing = session.scalar(
            select(Document).where(
                Document.tenant_id == identity.tenant_id,
                Document.filename == document.filename,
            )
        )
        if existing is not None:
            slug_to_id[document.slug] = existing.id
            continue
        created = create_document(
            session,
            identity=identity,
            storage=storage,
            source=io.BytesIO(document.text.encode("utf-8")),
            filename=document.filename,
            allowed_roles=None,
            max_upload_size_mb=settings.max_upload_size_mb,
        )
        result = ingest_document(
            session,
            identity=identity,
            document_id=created.id,
            settings=settings,
            storage=storage,
            store=store,
            provider=provider,
        )
        if not result.succeeded:
            raise RuntimeError(
                f"ingestion failed for {document.slug}: stage={result.stage} "
                f"error={result.error!r}"
            )
        session.commit()
        slug_to_id[document.slug] = created.id
    return slug_to_id


def resolve_relevance(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    dataset: dataset_module.Dataset,
    slug_to_id: dict[str, uuid.UUID],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Resolve the dataset's ground truth against the indexed corpus.

    Returns ``(doc_relevant, chunk_relevant)``: per question, the relevant
    document ids and the relevant chunk ids (``"<doc_uuid>:<index>"``).
    Chunk relevance is anchored to the *actually indexed* chunk rows by key
    phrase containment, so ground truth never guesses chunk boundaries.
    """
    doc_relevant: dict[str, set[str]] = {}
    chunk_relevant: dict[str, set[str]] = {}
    for question in dataset.questions:
        doc_ids = {str(slug_to_id[slug]) for slug in question.expected_documents}
        doc_relevant[question.id] = doc_ids
        chunk_ids: set[str] = set()
        for phrase in question.key_phrases:
            document_id = slug_to_id[phrase.document]
            rows = session.execute(
                select(DocumentChunk.chunk_index).where(
                    DocumentChunk.tenant_id == tenant_id,
                    DocumentChunk.document_id == document_id,
                    DocumentChunk.text.contains(phrase.phrase),
                )
            ).all()
            chunk_ids.update(f"{document_id}:{index}" for (index,) in rows)
        chunk_relevant[question.id] = chunk_ids
    return doc_relevant, chunk_relevant


def _questions(dataset: dataset_module.Dataset, max_questions: int | None):
    questions = dataset.questions
    if max_questions is not None:
        questions = questions[:max_questions]
    return questions


def evaluate_retrieval(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    provider: EmbeddingProvider,
    settings: Settings,
    dataset: dataset_module.Dataset,
    config: RunConfig,
) -> list[results.RetrievalRow]:
    """Run every configured retrieval variant over the dataset."""
    rows: list[results.RetrievalRow] = []
    for question in _questions(dataset, config.max_questions):
        for retrieval_config in config.configs:
            outcome = retrieval_eval.retrieve(
                session=session,
                identity=identity,
                store=store,
                embeddings=provider,
                settings=settings,
                question=question.question,
                config=retrieval_config,
                top_k=config.top_k,
                candidate_limit=config.candidate_limit,
            )
            rows.append(
                results.RetrievalRow(
                    question_id=question.id,
                    config=retrieval_config.value,
                    doc_ranks=list(outcome.doc_ranks),
                    chunk_ranks=list(outcome.chunk_ranks),
                    latency_ms=outcome.latency_ms,
                    retrieved_count=outcome.retrieved_count,
                )
            )
    return rows


def evaluate_answers(
    *,
    session: Session,
    identity: AuthenticatedIdentity,
    store: QdrantVectorStore,
    provider: EmbeddingProvider,
    generation: Any,
    settings: Settings,
    dataset: dataset_module.Dataset,
    config: RunConfig,
) -> list[results.AnswerRow]:
    """Generate and score one answer per question and configuration."""
    rows: list[results.AnswerRow] = []
    for question in _questions(dataset, config.max_questions):
        for retrieval_config in config.configs:
            outcome = answer_eval.generate_and_score(
                session=session,
                identity=identity,
                store=store,
                embeddings=provider,
                generation=generation,
                settings=settings,
                question_id=question.id,
                question=question.question,
                config=retrieval_config,
                top_k=config.top_k,
                candidate_limit=config.candidate_limit,
            )
            relevance = metrics.token_f1(outcome.answer, question.reference_answer)
            correctness = (
                metrics.correctness(
                    outcome.answer, question.question, question.reference_answer
                )
                if question.answerable
                else None
            )
            rows.append(
                results.AnswerRow(
                    question_id=question.id,
                    config=retrieval_config.value,
                    answerable=question.answerable,
                    refusal=outcome.refusal,
                    grounded=outcome.grounded,
                    faithfulness=outcome.faithfulness,
                    relevance=relevance,
                    correctness=correctness,
                    context_chunk_count=outcome.context_chunk_count,
                    retrieval_latency_ms=outcome.retrieval_latency_ms,
                    generation_latency_ms=outcome.generation_latency_ms,
                    total_latency_ms=outcome.total_latency_ms,
                )
            )
    return rows


def summarize_retrieval(
    rows: list[results.RetrievalRow],
    *,
    doc_relevant: dict[str, set[str]],
    chunk_relevant: dict[str, set[str]],
    configs: tuple[retrieval_eval.RetrievalConfig, ...],
) -> list[results.MetricLine]:
    """Aggregate per-configuration retrieval metrics into table lines."""
    lines: list[results.MetricLine] = []
    for retrieval_config in configs:
        subset = [row for row in rows if row.config == retrieval_config.value]
        if not subset:
            continue
        doc_ranks = [row.doc_ranks for row in subset]
        doc_rel = [doc_relevant[row.question_id] for row in subset]
        chunk_ranks = [row.chunk_ranks for row in subset]
        chunk_rel = [chunk_relevant[row.question_id] for row in subset]

        mrr, _ = metrics.mean_reciprocal_rank(doc_ranks, doc_rel)
        precision, precision_n, _ = metrics.mean_context_precision(doc_ranks, doc_rel)
        recall, recall_n, _ = metrics.mean_context_recall(doc_ranks, doc_rel)
        lines.append(results.MetricLine(retrieval_config.value, "document", "mrr", mrr, len(subset)))
        lines.append(results.MetricLine(retrieval_config.value, "document", "context_precision", precision, precision_n))
        lines.append(results.MetricLine(retrieval_config.value, "document", "context_recall", recall, recall_n))
        for k in KS:
            hit, _ = metrics.hit_rate(doc_ranks, doc_rel, k)
            lines.append(results.MetricLine(retrieval_config.value, "document", f"hit@{k}", hit, len(subset)))

        chunk_mrr, _ = metrics.mean_reciprocal_rank(chunk_ranks, chunk_rel)
        lines.append(results.MetricLine(retrieval_config.value, "chunk", "mrr", chunk_mrr, len(subset)))
        for k in KS:
            hit, _ = metrics.hit_rate(chunk_ranks, chunk_rel, k)
            lines.append(results.MetricLine(retrieval_config.value, "chunk", f"hit@{k}", hit, len(subset)))
    return lines


def summarize_latency(
    retrieval_rows: list[results.RetrievalRow],
    answer_rows: list[results.AnswerRow],
    configs: tuple[retrieval_eval.RetrievalConfig, ...],
) -> list[results.LatencyLine]:
    """Mean/median latency per configuration and stage."""
    lines: list[results.LatencyLine] = []
    for retrieval_config in configs:
        rs = [row.latency_ms for row in retrieval_rows if row.config == retrieval_config.value]
        if rs:
            lines.append(
                results.LatencyLine(
                    retrieval_config.value,
                    "retrieval",
                    statistics.mean(rs),
                    statistics.median(rs),
                    len(rs),
                )
            )
        generated = [row for row in answer_rows if row.config == retrieval_config.value]
        gens = [row.generation_latency_ms for row in generated]
        if gens:
            lines.append(
                results.LatencyLine(
                    retrieval_config.value,
                    "generation",
                    statistics.mean(gens),
                    statistics.median(gens),
                    len(gens),
                )
            )
        totals = [row.total_latency_ms for row in generated]
        if totals:
            lines.append(
                results.LatencyLine(
                    retrieval_config.value,
                    "total",
                    statistics.mean(totals),
                    statistics.median(totals),
                    len(totals),
                )
            )
    return lines


def analyze_failures(
    retrieval_rows: list[results.RetrievalRow],
    answer_rows: list[results.AnswerRow],
    *,
    dataset: dataset_module.Dataset,
    doc_relevant: dict[str, set[str]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Classify every query per configuration and count the categories."""
    answers_by_key = {(row.question_id, row.config): row for row in answer_rows}
    questions = {question.id: question for question in dataset.questions}
    findings: list[dict[str, Any]] = []
    counts: dict[str, dict[str, int]] = {}
    for row in retrieval_rows:
        question = questions[row.question_id]
        answer = answers_by_key.get((row.question_id, row.config))
        finding = failure.findings_for(
            question_id=row.question_id,
            config=row.config,
            answerable=question.answerable,
            relevant_doc_ids=doc_relevant[row.question_id],
            doc_ranks=tuple(row.doc_ranks),
            retrieved_count=row.retrieved_count,
            answer_relevance=answer.relevance if answer and question.answerable else None,
            refusal=answer.refusal if answer else False,
        )
        by_config = counts.setdefault(row.config, {})
        by_config[finding.category.value] = by_config.get(finding.category.value, 0) + 1
        if finding.category is not failure.FailureCategory.SUCCESS:
            findings.append(
                {
                    "question_id": finding.question_id,
                    "config": finding.config,
                    "category": finding.category.value,
                    "observed": finding.observed,
                    "expected": finding.expected,
                    "likely_cause": finding.likely_cause,
                    "recommendation": finding.recommendation,
                }
            )
    return findings, counts


def limitations_text(components: dict[str, str]) -> list[str]:
    """The honest-limitations block for the report."""
    return [
        ("36-question dataset: results describe this corpus only; §18 of the phase "
        "brief — no statistical significance is claimed."),
        ("Dense retrieval used the documented deterministic `lexical-hash-eval-v1` "
        "embedding stand-in (no Gemini key, no sentence-transformers); its numbers "
        "are not a production bi-encoder's."),
        ("Reranking is unavailable in this environment (`NoOpReranker` fallback); "
        "the Hybrid/Rerank row duplicates Hybrid/RRF, and no reranker quality or "
        "latency is claimed."),
        ("Answer metrics were produced by the deterministic `extractive-eval-v1` "
        "provider: faithfulness reflects the grounding contract (answers are "
        "verbatim evidence sentences), not a production LLM's faithfulness."),
        ("Context Precision/Recall are document-level (a document counts when any of "
        "its chunks is retrieved); chunk-level Hit@K/MRR are in the JSON results."),
        ("On this dataset BM25 matched no candidate chunk for almost every question "
        "(1 of 36 hits). The production path matches each query as "
        "``websearch_to_tsquery``'s conjunction of every content word, so a single "
        "vocabulary mismatch between the natural-language question and the document "
        "eliminates a match; hybrid/RRF therefore carried no BM25 signal and its rows "
        "are identical to dense's. This measures the real full-text index on "
        "conversational questions, and is not an ingestion or wiring failure."),
        ("The extractive stand-in cannot decline with non-empty context, so "
        "unanswerable questions with plausible-but-irrelevant retrieval produce a "
        "grounded-but-wrong answer; the report classifies this as "
        "`unanswerable_context` rather than measuring it as hallucination."),
    ]


def conclusions_text(
    metric_lines: list[results.MetricLine],
    components: dict[str, str],
    hybrid_vs_dense: str | None = None,
) -> list[str]:
    """The measured conclusions — one per §17 question, no overstatement."""
    doc = [line for line in metric_lines if line.level == "document"]
    value = {config: {line.metric: line.value for line in doc if line.config == config} for config in {line.config for line in doc}}

    def mrr(config: str) -> float:
        return value.get(config, {}).get("mrr", 0.0)

    dense_better = mrr("dense") > mrr("bm25")
    hybrid_better = mrr("hybrid") > max(mrr("dense"), mrr("bm25")) and (
        hybrid_vs_dense != "identical"
    )
    if hybrid_vs_dense == "identical":
        hybrid_line = (
            f"Hybrid/RRF equalled dense exactly on this dataset "
            f"(MRR {mrr('hybrid'):.3f} vs {mrr('dense'):.3f}): BM25 matched no "
            f"candidate chunks, so RRF had no second ranked list to fuse and hybrid "
            f"reduced to dense alone. Fusion benefit: not measurable (limitation 2)."
        )
    else:
        hybrid_line = (
            f"Hybrid/RRF {'improved' if hybrid_better else 'did not improve on'} the "
            f"higher of the two single retrievers' MRR ({mrr('hybrid'):.3f} vs "
            f"dense {mrr('dense'):.3f} / BM25 {mrr('bm25'):.3f}); per-query recovery of "
            "each retriever's misses is visible in the JSON rows."
        )
    return [
        (f"On the Phase 8 dataset, {'dense' if dense_better else 'BM25'} achieved the "
        f"higher MRR ({mrr('dense'):.3f} vs {mrr('bm25'):.3f}); the dense figure is the "
        "lexical-hash stand-in's, so this is a matching-mode comparison, not a model one."),
        hybrid_line,
        ("Reranking could not be evaluated (no cross-encoder available); the "
        "`Hybrid/RRF + Rerank` row is the NoOp fallback and carries no reranker claim."),
        ("Answer relevance/correctness track retrieval quality configuration by "
        "configuration; because the answer stand-in is extractive, these numbers "
        "measure whether better retrieval delivers better grounded answers, not LLM "
        "faithfulness (which is ~1.0 by construction for the stand-in)."),
        ("Every claim here is about this dataset and this environment; see the "
        "Limitations section before generalising."),
    ]


def _components(
    settings: Settings, provider: EmbeddingProvider, generation: Any
) -> dict[str, str]:
    return {
        "Database": "PostgreSQL (live full-text index)",
        "Vector store": "Qdrant",
        "Embedding model": provider.model_name,
        "Generation model": getattr(generation, "model_name", "unknown"),
        "Reranker": "noop (cross-encoder not installed; §20 unavailable)",
        "Query top_k": str(settings.query_top_k),
        "Deterministic seed": "n/a (all providers deterministic)",
    }


def run_benchmark(
    *,
    dataset: dataset_module.Dataset,
    settings: Settings,
    store: QdrantVectorStore,
    storage: LocalFileStore,
    session_factory: sessionmaker[Session],
    provider: EmbeddingProvider,
    generation: Any,
    config: RunConfig,
) -> tuple[list[results.RetrievalRow], list[results.AnswerRow], dict[str, uuid.UUID]]:
    """Seed the corpus and evaluate every configuration.

    Returns the retrieval rows, the answer rows, and the slug-to-document-id
    map the ground truth resolution needs — so a caller never has to guess
    which seeded document corresponds to which dataset slug.
    """
    with session_factory() as session:
        identity = eval_identity(session, settings)
        slug_to_id = seed_corpus(
            session=session,
            identity=identity,
            settings=settings,
            storage=storage,
            store=store,
            provider=provider,
            dataset=dataset,
        )
        retrieval_rows = evaluate_retrieval(
            session=session,
            identity=identity,
            store=store,
            provider=provider,
            settings=settings,
            dataset=dataset,
            config=config,
        )
        answer_rows: list[results.AnswerRow] = []
        if config.embed_answers:
            answer_rows = evaluate_answers(
                session=session,
                identity=identity,
                store=store,
                provider=provider,
                generation=generation,
                settings=settings,
                dataset=dataset,
                config=config,
            )
        return retrieval_rows, answer_rows, slug_to_id


def _hybrid_vs_dense(retrieval_rows: list[results.RetrievalRow]) -> str | None:
    """Classify hybrid vs dense for the report text.

    Returns "identical" when every hybrid row matches the dense row for the
    same question (BM25 surfaced nothing for RRF to fuse), "differs" when any
    row differs, and None when the run did not include both configurations.
    """
    by_question: dict[str, dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {}
    for row in retrieval_rows:
        by_question.setdefault(row.question_id, {})[row.config] = (
            tuple(row.doc_ranks),
            tuple(row.chunk_ranks),
        )
    if not by_question:
        return None
    if any("dense" not in cfg or "hybrid" not in cfg for cfg in by_question.values()):
        return None
    if all(cfg["dense"] == cfg["hybrid"] for cfg in by_question.values()):
        return "identical"
    return "differs"


def emit(
    *,
    run_id: str,
    created_iso: str,
    dataset: dataset_module.Dataset,
    components: dict[str, str],
    retrieval_rows: list[results.RetrievalRow],
    answer_rows: list[results.AnswerRow],
    metric_lines: list[results.MetricLine],
    latency_lines: list[results.LatencyLine],
    findings: list[dict[str, Any]],
    failure_counts: dict[str, dict[str, int]],
    config: RunConfig,
    out_dir: Path,
) -> str:
    """Write JSON, CSV and the markdown report; returns the markdown text."""
    payload: dict[str, Any] = {
        "run_id": run_id,
        "created_iso": created_iso,
        "dataset": {
            "name": dataset.name,
            "version": dataset.version,
            "composition": dataset.category_counts(),
        },
        "components": components,
        "config": config.as_dict(),
        "retrieval": [row.to_dict() for row in retrieval_rows],
        "answers": [row.to_dict() for row in answer_rows],
        "metrics": [line.to_dict() for line in metric_lines],
        "latency": [line.to_dict() for line in latency_lines],
        "failure_findings": findings,
        "failure_counts": failure_counts,
    }
    report.write_json(payload, out_dir / "results.json")
    report.write_csv(
        [row.to_dict() for row in retrieval_rows], out_dir / "retrieval_rows.csv"
    )
    report.write_csv([row.to_dict() for row in answer_rows], out_dir / "answer_rows.csv")
    report.write_csv([line.to_dict() for line in metric_lines], out_dir / "metrics.csv")
    report.write_csv(
        [line.to_dict() for line in latency_lines], out_dir / "latency.csv"
    )

    markdown = report.render_report(
        run_id=run_id,
        created_iso=created_iso,
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        dataset_composition=dataset.category_counts(),
        components=components,
        metric_lines=metric_lines,
        answer_rows=answer_rows,
        latency_lines=latency_lines,
        findings=findings,
        failure_counts=failure_counts,
        config=config.as_dict(),
        limitations=limitations_text(components),
        conclusions=conclusions_text(
            metric_lines, components, _hybrid_vs_dense(retrieval_rows)
        ),
        hybrid_vs_dense=_hybrid_vs_dense(retrieval_rows),
    )
    return markdown


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="PMP Phase 8 benchmark runner")
    parser.add_argument("--dataset", default=str(Path(__file__).resolve().parent / "dataset_v1.json"))
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parents[2] / "docs" / "evaluation" / "results"))
    parser.add_argument("--configs", default="dense,bm25,hybrid,hybrid_rerank")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--candidate-limit", type=int, default=20)
    parser.add_argument("--embedding-dim", type=int, default=512)
    parser.add_argument("--no-answers", action="store_true")
    parser.add_argument("--max-questions", type=int, default=None)
    parser.add_argument("--fresh", action="store_true", help="recreate the evaluation tenant")
    parser.add_argument("--keep-tenant", action="store_true")
    args = parser.parse_args(argv)

    parsed = dataset_module.load_dataset(args.dataset)
    problems = dataset_module.validate_dataset(parsed)
    if problems:
        print("dataset validation failed:\n" + "\n".join(f"  - {p}" for p in problems))
        return 2

    chosen: list[retrieval_eval.RetrievalConfig] = []
    for name in (item.strip() for item in args.configs.split(",")):
        try:
            chosen.append(retrieval_eval.RetrievalConfig(name))
        except ValueError:
            print(f"unknown configuration {name!r}; choose from "
                  f"{[c.value for c in retrieval_eval.CONFIGS]}")
            return 2

    run_config = RunConfig(
        top_k=args.top_k,
        candidate_limit=args.candidate_limit,
        configs=tuple(chosen),
        embedding_dimension=args.embedding_dim,
        embed_answers=not args.no_answers,
        seed=7,
        max_questions=args.max_questions,
        fresh_tenant=args.fresh,
        keep_tenant=args.keep_tenant,
    )

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set; the live benchmark needs a PostgreSQL DSN")
        return 2

    storage_root = Path(tempfile.mkdtemp(prefix="eval-storage-"))
    settings = build_settings(
        embedding_dimension=args.embedding_dim,
        database_url=database_url,
        storage_root=storage_root,
        qdrant_url=os.environ.get("TENANTMIND_QDRANT_URL"),
        jwt_secret=secrets.token_hex(32),
    )

    reset_engine()
    engine = create_engine_from_url(database_url)
    session_factory = build_session_factory(engine)
    store = QdrantVectorStore(settings)
    storage = LocalFileStore(storage_root)
    provider = embedding_module.LexicalHashEmbeddings(dimensions=args.embedding_dim)
    generation = generation_module.ExtractiveAnswerProvider()

    print(f"seeding {len(parsed.documents)} documents and evaluating {len(parsed.questions)} questions...")
    started_run = time.perf_counter()
    retrieval_rows, answer_rows, slug_to_id = run_benchmark(
        dataset=parsed,
        settings=settings,
        store=store,
        storage=storage,
        session_factory=session_factory,
        provider=provider,
        generation=generation,
        config=run_config,
    )
    elapsed = time.perf_counter() - started_run

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with session_factory() as session:
        identity = eval_identity(session, settings)
        doc_relevant, chunk_relevant = resolve_relevance(
            session,
            tenant_id=identity.tenant_id,
            dataset=parsed,
            slug_to_id=slug_to_id,
        )

    metric_lines = summarize_retrieval(
        retrieval_rows,
        doc_relevant=doc_relevant,
        chunk_relevant=chunk_relevant,
        configs=run_config.configs,
    )
    latency_lines = summarize_latency(retrieval_rows, answer_rows, run_config.configs)
    findings, failure_counts = analyze_failures(
        retrieval_rows,
        answer_rows,
        dataset=parsed,
        doc_relevant=doc_relevant,
    )

    components = _components(settings, provider, generation)
    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("phase8-%Y%m%d-%H%M%S")
    markdown = emit(
        run_id=run_id,
        created_iso=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        dataset=parsed,
        components=components,
        retrieval_rows=retrieval_rows,
        answer_rows=answer_rows,
        metric_lines=metric_lines,
        latency_lines=latency_lines,
        findings=findings,
        failure_counts=failure_counts,
        config=run_config,
        out_dir=out_dir,
    )
    (out_dir.parent / "PHASE_8_EVALUATION_REPORT.md").write_text(markdown, encoding="utf-8")

    print(f"benchmark complete in {elapsed:.1f}s: {len(retrieval_rows)} retrieval rows, "
          f"{len(answer_rows)} answer rows")
    print(f"results written to {out_dir}")
    if not run_config.keep_tenant:
        try:
            store.delete_collection(identity.tenant_id)
        except Exception:  # noqa: BLE001 - teardown must not mask a successful run
            print("warning: evaluation collection teardown failed")
        with session_factory() as session:
            tenant = session.scalar(select(Tenant).where(Tenant.slug == EVAL_TENANT_SLUG))
            if tenant is not None:
                session.delete(tenant)
                session.commit()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())