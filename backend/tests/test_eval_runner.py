"""Runner wiring tests: seeding, relevance resolution, aggregation, emission.

``run_benchmark`` is deliberately exercised end to end against the SQLite
harness (real seeding through ``create_document`` + ``ingest_document``, real
Qdrant stand-in, real token-minted identity), which is what ties the
measurement pipeline together. BM25 is excluded there because SQLite has no
full-text functions — the honest constraint the adapter tests already pin.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.models import Tenant, User
from app.storage.local import LocalFileStore
from evaluation import results, runner
from evaluation.dataset import Dataset, parse_dataset
from evaluation.embeddings import LexicalHashEmbeddings
from evaluation.generation import ExtractiveAnswerProvider
from evaluation.retrieval_eval import RetrievalConfig
from tests.conftest import make_settings
from tests.eval_helpers import make_store


def _mini_dataset() -> Dataset:
    return parse_dataset(
        {
            "version": 1,
            "name": "runner wiring fixture",
            "created": "2026-01-01",
            "description": "two documents, two questions",
            "question_categories": ["simple_factual"],
            "question_tags": ["factoid"],
            "documents": [
                {
                    "slug": "leave",
                    "filename": "leave.md",
                    "text": "Employees accrue 25 days of annual leave each year.",
                },
                {
                    "slug": "safety",
                    "filename": "safety.md",
                    "text": "Fire drills happen every quarter at Meridian Cloud.",
                },
            ],
            "questions": [
                {
                    "id": "q001",
                    "category": "simple_factual",
                    "tags": ["factoid"],
                    "question": "How many days of annual leave do staff get?",
                    "expected_documents": ["leave"],
                    "key_phrases": [{"document": "leave", "phrase": "25 days"}],
                    "reference_answer": "25 days of annual leave each year.",
                    "answerable": True,
                },
                {
                    "id": "q002",
                    "category": "simple_factual",
                    "tags": ["factoid"],
                    "question": "How often do fire drills happen?",
                    "expected_documents": ["safety"],
                    "key_phrases": [{"document": "safety", "phrase": "every quarter"}],
                    "reference_answer": "Every quarter.",
                    "answerable": True,
                },
            ],
        }
    )


def _run_config(configs: tuple[RetrievalConfig, ...] = (RetrievalConfig.DENSE,)) -> runner.RunConfig:
    return runner.RunConfig(
        top_k=5,
        candidate_limit=20,
        configs=configs,
        embedding_dimension=8,
        embed_answers=True,
        seed=7,
        max_questions=None,
        fresh_tenant=False,
        keep_tenant=True,
    )


def _settings(tmp_path, auth_settings) -> Settings:
    return make_settings(
        jwt_secret_key=auth_settings.jwt_secret_key,
        access_token_expire_minutes=auth_settings.access_token_expire_minutes,
        refresh_token_expire_days=auth_settings.refresh_token_expire_days,
        storage_root=tmp_path / "storage",
        embedding_dimension=8,
    )


class TestRunBenchmarkEndToEnd:
    def test_seeds_corpus_and_measures_dense_retrieval_and_answers(
        self, model_session, sqlite_session_factory, auth_settings, tmp_path
    ) -> None:
        settings = _settings(tmp_path, auth_settings)
        dataset = _mini_dataset()
        store = make_store(settings)
        storage = LocalFileStore(tmp_path / "storage")
        retrieval_rows, answer_rows, slug_to_id = runner.run_benchmark(
            dataset=dataset,
            settings=settings,
            store=store,
            storage=storage,
            session_factory=sqlite_session_factory,
            provider=LexicalHashEmbeddings(dimensions=8),
            generation=ExtractiveAnswerProvider(),
            config=_run_config(),
        )
        # One retrieval + one answer row per (question, config).
        assert len(retrieval_rows) == 2
        assert len(answer_rows) == 2
        assert set(slug_to_id) == {"leave", "safety"}
        # Ground truth resolves against the seeded corpus.
        with sqlite_session_factory() as session:
            tenant = session.scalars(
                select(Tenant).where(Tenant.slug == runner.EVAL_TENANT_SLUG)
            ).one()
            tenant_id = tenant.id
        doc_relevant, chunk_relevant = runner.resolve_relevance(
            model_session,
            tenant_id=tenant_id,
            dataset=dataset,
            slug_to_id=slug_to_id,
        )
        leave_id = str(slug_to_id["leave"])
        assert doc_relevant["q001"] == {leave_id}
        assert chunk_relevant["q001"]  # the "25 days" chunk was found
        # Answers were generated and grounded on retrieved chunks.
        answers = {row.question_id: row for row in answer_rows}
        assert not answers["q001"].refusal
        assert answers["q001"].grounded
        assert answers["q001"].context_chunk_count >= 1
        assert answers["q001"].faithfulness > 0.0
        assert answers["q001"].relevance > 0.0
        assert answers["q001"].correctness is not None
        # The eval tenant and its user were created through the real path.
        with sqlite_session_factory() as session:
            tenant = session.scalars(
                select(Tenant).where(Tenant.slug == runner.EVAL_TENANT_SLUG)
            ).one()
            assert tenant.name == runner.EVAL_TENANT_NAME
            user_row = session.scalars(
                select(User).where(User.tenant_id == tenant.id)
            ).first()
            assert user_row is not None
            assert user_row.role.value == "viewer"


class TestResolveRelevance:
    def test_resolves_documents_and_chunks_by_key_phrase(
        self, model_session, make_account
    ) -> None:

        from app.models import DocumentChunk

        tenant, user = make_account(slug="eval-resolve")
        from tests.eval_helpers import seed_document

        document = seed_document(
            model_session,
            tenant=tenant,
            uploaded_by=user,
            filename="policy.md",
        )
        model_session.flush()
        chunk = DocumentChunk(
            document_id=document.id,
            tenant_id=tenant.id,
            chunk_index=0,
            page=1,
            text="Annual leave is 25 days per year.",
            search_vector="",
        )
        model_session.add(chunk)
        model_session.commit()
        slug_to_id = {"policy": document.id}
        resolve_dataset = parse_dataset(
            {
                "version": 1,
                "name": "resolve fixture",
                "created": "",
                "description": "",
                "question_categories": ["simple_factual"],
                "question_tags": ["factoid"],
                "documents": [
                    {
                        "slug": "policy",
                        "filename": "policy.md",
                        "text": "Annual leave is 25 days per year.",
                    }
                ],
                "questions": [
                    {
                        "id": "q001",
                        "category": "simple_factual",
                        "tags": ["factoid"],
                        "question": "How many days?",
                        "expected_documents": ["policy"],
                        "key_phrases": [{"document": "policy", "phrase": "25 days"}],
                        "reference_answer": "25 days.",
                        "answerable": True,
                    }
                ],
            }
        )
        doc_relevant, chunk_relevant = runner.resolve_relevance(
            model_session,
            tenant_id=tenant.id,
            dataset=resolve_dataset,
            slug_to_id=slug_to_id,
        )
        assert doc_relevant["q001"] == {str(document.id)}
        assert f"{document.id}:0" in chunk_relevant["q001"]


class TestSummarizeRetrieval:
    def test_hand_computed_aggregates(self) -> None:
        rows = [
            results.RetrievalRow(
                question_id="q1",
                config="dense",
                doc_ranks=["A"],
                chunk_ranks=["A:0", "A:1"],
                latency_ms=1.0,
                retrieved_count=2,
            ),
            results.RetrievalRow(
                question_id="q2",
                config="dense",
                doc_ranks=["B", "A"],
                chunk_ranks=["B:0"],
                latency_ms=2.0,
                retrieved_count=1,
            ),
        ]
        doc_relevant = {"q1": {"A"}, "q2": {"A"}}
        chunk_relevant = {"q1": {"A:0"}, "q2": {"A:0"}}
        lines = runner.summarize_retrieval(
            rows,
            doc_relevant=doc_relevant,
            chunk_relevant=chunk_relevant,
            configs=(RetrievalConfig.DENSE,),
        )
        values = {
            (line.level, line.metric): line.value
            for line in lines
            if line.config == "dense"
        }
        assert values[("document", "mrr")] == pytest.approx(0.75)
        assert values[("document", "hit@1")] == pytest.approx(0.5)
        assert values[("document", "hit@3")] == pytest.approx(1.0)
        assert values[("document", "context_precision")] == pytest.approx(0.75)
        assert values[("document", "context_recall")] == pytest.approx(1.0)
        assert values[("chunk", "mrr")] == pytest.approx(0.5)
        assert values[("chunk", "hit@1")] == pytest.approx(0.5)


class TestSummarizeLatency:
    def test_means_and_medians_per_stage(self) -> None:
        retrieval = [
            results.RetrievalRow("q1", "dense", ["A"], ["A:0"], 10.0, 1),
            results.RetrievalRow("q2", "dense", ["B"], ["B:0"], 30.0, 1),
        ]
        answers = [
            results.AnswerRow(
                question_id="q1",
                config="dense",
                answerable=True,
                refusal=False,
                grounded=True,
                faithfulness=1.0,
                relevance=0.5,
                correctness=0.5,
                context_chunk_count=1,
                retrieval_latency_ms=10.0,
                generation_latency_ms=5.0,
                total_latency_ms=15.0,
            ),
            results.AnswerRow(
                question_id="q2",
                config="dense",
                answerable=True,
                refusal=False,
                grounded=True,
                faithfulness=1.0,
                relevance=0.5,
                correctness=0.5,
                context_chunk_count=1,
                retrieval_latency_ms=30.0,
                generation_latency_ms=7.0,
                total_latency_ms=37.0,
            ),
        ]
        lines = runner.summarize_latency(
            retrieval, answers, (RetrievalConfig.DENSE,)
        )
        by_stage = {line.stage: line for line in lines if line.config == "dense"}
        assert by_stage["retrieval"].mean_ms == pytest.approx(20.0)
        assert by_stage["generation"].mean_ms == pytest.approx(6.0)
        assert by_stage["total"].mean_ms == pytest.approx(26.0)
        assert by_stage["total"].median_ms == pytest.approx(26.0)


class TestAnalyzeFailures:
    def test_counts_include_success_for_answerable_hits(self) -> None:
        dataset = _mini_dataset()
        retrieval = [
            results.RetrievalRow(
                question_id="q001",
                config="dense",
                doc_ranks=["leave-id"],
                chunk_ranks=["leave-id:0"],
                latency_ms=1.0,
                retrieved_count=1,
            )
        ]
        answers = [
            results.AnswerRow(
                question_id="q001",
                config="dense",
                answerable=True,
                refusal=False,
                grounded=True,
                faithfulness=1.0,
                relevance=0.5,
                correctness=0.5,
                context_chunk_count=1,
                retrieval_latency_ms=1.0,
                generation_latency_ms=1.0,
                total_latency_ms=2.0,
            )
        ]
        findings, counts = runner.analyze_failures(
            retrieval,
            answers,
            dataset=dataset,
            doc_relevant={"q001": {"leave-id"}},
        )
        assert counts["dense"]["success"] == 1
        assert not findings


class TestEmit:
    def test_writes_json_csv_and_markdown(self, tmp_path) -> None:
        dataset = _mini_dataset()
        config = _run_config()
        out_dir = tmp_path / "results"
        retrieval_rows = [
            results.RetrievalRow("q001", "dense", ["doc-1"], ["doc-1:0"], 1.0, 1)
        ]
        answer_rows = [
            results.AnswerRow(
                question_id="q001",
                config="dense",
                answerable=True,
                refusal=False,
                grounded=True,
                faithfulness=1.0,
                relevance=0.5,
                correctness=0.5,
                context_chunk_count=1,
                retrieval_latency_ms=1.0,
                generation_latency_ms=1.0,
                total_latency_ms=2.0,
            )
        ]
        metric_lines = runner.summarize_retrieval(
            retrieval_rows,
            doc_relevant={"q001": {"doc-1"}},
            chunk_relevant={"q001": {"doc-1:0"}},
            configs=(RetrievalConfig.DENSE,),
        )
        latency_lines = runner.summarize_latency(
            retrieval_rows, answer_rows, (RetrievalConfig.DENSE,)
        )
        components = {"Embedding model": "lexical-hash-eval-v1", "Reranker": "noop"}
        markdown = runner.emit(
            run_id="phase8-test",
            created_iso="2026-01-01T00:00:00+00:00",
            dataset=dataset,
            components=components,
            retrieval_rows=retrieval_rows,
            answer_rows=answer_rows,
            metric_lines=metric_lines,
            latency_lines=latency_lines,
            findings=[],
            failure_counts={"dense": {"success": 1}},
            config=config,
            out_dir=out_dir,
        )
        assert (out_dir / "results.json").exists()
        assert (out_dir / "retrieval_rows.csv").exists()
        assert (out_dir / "answer_rows.csv").exists()
        assert (out_dir / "metrics.csv").exists()
        assert (out_dir / "latency.csv").exists()
        payload = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
        assert payload["run_id"] == "phase8-test"
        assert payload["components"]["Embedding model"] == "lexical-hash-eval-v1"
        assert payload["retrieval"][0]["question_id"] == "q001"
        assert "Phase 8" in markdown
        assert "Retrieval Metrics" in markdown
        assert "Executive Summary" in markdown
        assert "lexical-hash-eval-v1" in markdown  # the stand-in is named, not hidden


class TestMainCli:
    def test_missing_database_url_returns_two(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("TENANTMIND_QDRANT_URL", raising=False)
        dataset = tmp_path / "ds.json"
        dataset.write_text(
            json.dumps(
                {
                    "version": 1,
                    "name": "x",
                    "created": "",
                    "description": "",
                    "question_categories": ["simple_factual"],
                    "question_tags": ["factoid"],
                    "documents": [
                        {
                            "slug": "a",
                            "filename": "a.md",
                            "text": "The answer is 42.",
                        }
                    ],
                    "questions": [
                        {
                            "id": "q001",
                            "category": "simple_factual",
                            "tags": ["factoid"],
                            "question": "What is the answer?",
                            "expected_documents": ["a"],
                            "key_phrases": [{"document": "a", "phrase": "42"}],
                            "reference_answer": "42",
                            "answerable": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert runner.main(["--dataset", str(dataset)]) == 2

    def test_unknown_configuration_returns_two(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert runner.main(["--dataset", "evaluation/dataset_v1.json", "--configs", "bogus"]) == 2

    def test_dataset_validation_failure_returns_two(self, tmp_path) -> None:
        # A dataset that parses but whose key phrase is absent from its document
        # must fail the cross-field validation before anything is measured.
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "version": 1,
                    "name": "x",
                    "created": "",
                    "description": "",
                    "question_categories": ["simple_factual"],
                    "question_tags": ["factoid"],
                    "documents": [
                        {"slug": "a", "filename": "a.md", "text": "Nothing here."}
                    ],
                    "questions": [
                        {
                            "id": "q001",
                            "category": "simple_factual",
                            "tags": ["factoid"],
                            "question": "What is the answer?",
                            "expected_documents": ["a"],
                            "key_phrases": [{"document": "a", "phrase": "42"}],
                            "reference_answer": "42",
                            "answerable": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        assert runner.main(["--dataset", str(bad)]) == 2


class TestReportText:
    def test_limitations_block_names_every_stand_in(self) -> None:
        text = "\n".join(runner.limitations_text({}))
        assert "lexical-hash-eval-v1" in text
        assert "extractive-eval-v1" in text
        assert "NoOpReranker" in text
        assert "no statistical significance" in text

    def test_conclusions_make_no_reranker_claim(self) -> None:
        lines = runner.conclusions_text([], {})
        assert any("Reranking could not be evaluated" in line for line in lines)

    def test_hybrid_vs_dense_classifier(self) -> None:
        rows = [
            results.RetrievalRow("q1", "dense", ["A"], ["A:0"], 1.0, 1),
            results.RetrievalRow("q1", "hybrid", ["A"], ["A:0"], 1.0, 1),
        ]
        assert runner._hybrid_vs_dense(rows) == "identical"
        rows.append(results.RetrievalRow("q2", "dense", ["B"], ["B:0"], 1.0, 1))
        rows.append(results.RetrievalRow("q2", "hybrid", ["A"], ["A:0"], 2.0, 1))
        assert runner._hybrid_vs_dense(rows) == "differs"
        assert runner._hybrid_vs_dense([]) is None
        only_dense = [results.RetrievalRow("q1", "dense", ["A"], ["A:0"], 1.0, 1)]
        assert runner._hybrid_vs_dense(only_dense) is None

    def test_conclusions_report_fusion_equalled_dense_when_identical(self) -> None:
        metrics = [
            results.MetricLine("dense", "document", "mrr", 0.787, 36),
            results.MetricLine("bm25", "document", "mrr", 0.028, 36),
            results.MetricLine("hybrid", "document", "mrr", 0.787, 36),
        ]
        lines = runner.conclusions_text(
            metrics, {}, hybrid_vs_dense="identical"
        )
        assert any("equalled dense" in line and "0.787" in line for line in lines)
        assert not any("improved" in line for line in lines)