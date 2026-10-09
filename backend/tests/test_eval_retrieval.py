"""Adapter tests: the evaluation drives the *production* retrieval components.

Each configuration in :mod:`evaluation.retrieval_eval` is a thin orchestration
over the same components the query service uses (Qdrant ``search_points``,
PostgreSQL BM25, RRF hybrid + `load_candidate_chunks`, and the rerank seam).
These tests pin down that wiring against a SQLite session and the suite's
in-memory Qdrant double, and pin down the honest degradation the environment
forces (BM25 unavailable on SQLite, reranker unavailable everywhere).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.models import UserRole
from app.rag.retrieval import RetrievedChunk
from evaluation.retrieval_eval import (
    CONFIGS,
    RetrievalConfig,
    RetrievalOutcome,
    chunk_key,
    retrieve,
)
from tests.eval_helpers import (
    EMBEDDINGS,
    corpus_text,
    make_identity,
    make_store,
    point_for,
    question_text,
    seed_document,
    seed_points,
)


@pytest.fixture
def world(auth_settings, model_session, make_account) -> dict[str, Any]:
    """Tenant A (viewer), a document row, a vector store, and matching settings.

    ``auth_settings`` defaults to 768-dimensional embeddings, but the suite's
    in-memory Qdrant stands up 8-wide collections; the fixture re-derives
    settings with the same JWT secret at the fake's width so the store and the
    points agree on a dimension.
    """
    from tests.conftest import make_settings

    settings = make_settings(
        jwt_secret_key=auth_settings.jwt_secret_key,
        access_token_expire_minutes=auth_settings.access_token_expire_minutes,
        refresh_token_expire_days=auth_settings.refresh_token_expire_days,
        storage_root=auth_settings.storage_root,
        embedding_dimension=8,
    )
    tenant, user = make_account(slug="eval-retrieval", role=UserRole.VIEWER)
    document = seed_document(
        model_session,
        tenant=tenant,
        uploaded_by=user,
        filename="leave-policy.md",
    )
    model_session.commit()
    store = make_store(settings)
    identity = make_identity(settings, tenant, user)
    return {
        "settings": settings,
        "session": model_session,
        "tenant": tenant,
        "user": user,
        "identity": identity,
        "document": document,
        "store": store,
    }


def run_retrieval(
    world: dict[str, Any], *, config: RetrievalConfig, question: str
) -> RetrievalOutcome:
    return retrieve(
        session=world["session"],
        identity=world["identity"],
        store=world["store"],
        embeddings=EMBEDDINGS,
        settings=world["settings"],
        question=question,
        config=config,
        top_k=5,
        candidate_limit=20,
    )


class TestDenseWiring:
    def test_dense_returns_authorized_chunks_ranked_by_cosine(self, world: dict) -> None:
        store = world["store"]
        tenant = world["tenant"]
        document = world["document"]
        seed_points(
            store,
            tenant,
            [
                point_for(
                    tenant=tenant,
                    document=document,
                    chunk_index=0,
                    text=corpus_text(),
                    vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                ),
                point_for(
                    tenant=tenant,
                    document=document,
                    chunk_index=1,
                    text="Unrelated maintenance windows.",
                    vector=[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
                ),
            ],
        )
        # Query sits closest to the [1,0,...] chunk (unit vectors below).
        outcome = retrieve(
            session=world["session"],
            identity=world["identity"],
            store=store,
            embeddings=EMBEDDINGS,
            settings=world["settings"],
            question=question_text(),
            config=RetrievalConfig.DENSE,
            top_k=5,
            candidate_limit=20,
        )
        assert outcome.config is RetrievalConfig.DENSE
        assert outcome.retrieved_count == 2
        assert len(outcome.doc_ranks) == 1  # both chunks belong to one document
        assert outcome.doc_ranks[0] == str(document.id)
        assert outcome.chunk_ranks[0] == chunk_key(
            RetrievedChunk(
                document_id=document.id,
                filename="leave-policy.md",
                chunk_index=0,
                page=1,
                text=corpus_text(),
                score=0.9,
            )
        )
        # The answer context is a materialized RetrievedChunk with the payload text.
        assert outcome.chunks
        assert outcome.chunks[0].document_id == document.id
        assert outcome.chunks[0].text

    def test_dense_top_k_truncates_the_answer_context_only(self, world: dict) -> None:
        store = world["store"]
        tenant = world["tenant"]
        document = world["document"]
        seed_points(
            store,
            tenant,
            [
                point_for(
                    tenant=tenant,
                    document=document,
                    chunk_index=i,
                    text=corpus_text(),
                    vector=[1.0 if i == 0 else 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                )
                for i in range(3)
            ],
        )
        outcome = retrieve(
            session=world["session"],
            identity=world["identity"],
            store=store,
            embeddings=EMBEDDINGS,
            settings=world["settings"],
            question=question_text(),
            config=RetrievalConfig.DENSE,
            top_k=2,
            candidate_limit=20,
        )
        assert outcome.retrieved_count == 3
        assert len(outcome.doc_ranks) == 1  # ranking sees the whole candidate list
        assert len(outcome.chunks) == 2  # context is capped at top_k


class TestDenseRanksOnlyViewableDocuments:
    def test_dense_hit_for_a_document_in_another_tenant_is_dropped(
        self, world: dict, make_account
    ) -> None:
        """The payload says 'eval tenant' but the row lives in tenant B.

        This is the second authorization layer: the payload filter (layer 1)
        cannot catch a payload pointing at a document that belongs to another
        tenant, so the adapter must re-verify every candidate against the
        database — and must drop it there.
        """
        store = world["store"]
        session = world["session"]
        foreign, foreign_user = make_account(
            slug="eval-retrieval-foreign", role=UserRole.VIEWER
        )
        ghost = seed_document(
            session,
            tenant=foreign,
            uploaded_by=foreign_user,
            filename="foreign-row.md",
        )
        session.commit()
        seed_points(
            store,
            world["tenant"],
            [
                point_for(
                    tenant=world["tenant"],  # payload tenant_id = eval tenant
                    document=ghost,  # …but the row belongs to the foreign tenant
                    chunk_index=0,
                    text=corpus_text(),
                    vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                )
            ],
        )
        outcome = run_retrieval(
            world, config=RetrievalConfig.DENSE, question=question_text()
        )
        # The vector store's payload filter let it through (payload said this
        # tenant) …
        assert outcome.retrieved_count == 1
        # … but the PostgreSQL re-verification layer dropped it: the row for
        # that document id belongs to a different tenant.
        assert outcome.doc_ranks == ()
        assert outcome.chunks == ()


class TestBM25Wiring:
    def test_bm25_refuses_to_fabricate_without_postgresql(self, world: dict) -> None:
        """SQLite has no full-text functions; the adapter must not fake BM25.

        The honest behaviour is a raised error (the live benchmark runs on
        PostgreSQL); a fabricated empty result would hide the constraint.
        """
        with pytest.raises(SQLAlchemyError):
            run_retrieval(world, config=RetrievalConfig.BM25, question=question_text())


class TestHybridWiring:
    def test_hybrid_falls_back_to_dense_when_bm25_is_unavailable(self, world: dict) -> None:
        """bm25_search_ids degrades to [] on SQLite, so fusion is dense-only."""
        store = world["store"]
        tenant = world["tenant"]
        document = world["document"]
        seed_points(
            store,
            tenant,
            [
                point_for(
                    tenant=tenant,
                    document=document,
                    chunk_index=0,
                    text=corpus_text(),
                    vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                )
            ],
        )
        outcome = run_retrieval(
            world, config=RetrievalConfig.HYBRID, question=question_text()
        )
        assert outcome.config is RetrievalConfig.HYBRID
        assert outcome.doc_ranks[0] == str(document.id)
        assert outcome.chunks[0].document_id == document.id


class TestRerankWiring:
    def test_rerank_degrades_to_noop_and_says_so(self, world: dict) -> None:
        """No cross-encoder is installed; the row must name the no-op, not a model."""
        store = world["store"]
        tenant = world["tenant"]
        document = world["document"]
        seed_points(
            store,
            tenant,
            [
                point_for(
                    tenant=tenant,
                    document=document,
                    chunk_index=0,
                    text=corpus_text(),
                    vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                )
            ],
        )
        outcome = run_retrieval(
            world, config=RetrievalConfig.HYBRID_RERANK, question=question_text()
        )
        assert outcome.config is RetrievalConfig.HYBRID_RERANK
        reranker_model = outcome.reranker_model
        assert reranker_model is not None
        assert "noop" in reranker_model
        assert "cross-encoder" in reranker_model
        # Same retrieval content as the hybrid row — nothing was re-ranked.
        assert outcome.doc_ranks[0] == str(document.id)

    def test_rerank_disabled_by_settings_reports_noop(self, world: dict) -> None:
        from tests.conftest import make_settings

        disabled = make_settings(
            jwt_secret_key=world["settings"].jwt_secret_key,
            storage_root=world["settings"].storage_root,
            embedding_dimension=8,
            rerank_enabled=False,
        )
        store = world["store"]
        tenant = world["tenant"]
        document = world["document"]
        seed_points(
            store,
            tenant,
            [
                point_for(
                    tenant=tenant,
                    document=document,
                    chunk_index=0,
                    text=corpus_text(),
                    vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                )
            ],
        )
        outcome = retrieve(
            session=world["session"],
            identity=world["identity"],
            store=store,
            embeddings=EMBEDDINGS,
            settings=disabled,
            question=question_text(),
            config=RetrievalConfig.HYBRID_RERANK,
            top_k=5,
            candidate_limit=20,
        )
        assert outcome.reranker_model == "noop"


class TestConfigMatrix:
    def test_all_four_configurations_are_enumerated(self) -> None:
        assert set(CONFIGS) == set(RetrievalConfig)

    def test_every_configuration_records_ranks_and_context(self, world: dict) -> None:
        store = world["store"]
        tenant = world["tenant"]
        document = world["document"]
        seed_points(
            store,
            tenant,
            [
                point_for(
                    tenant=tenant,
                    document=document,
                    chunk_index=0,
                    text=corpus_text(),
                    vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                )
            ],
        )
        for config in CONFIGS:
            if config is RetrievalConfig.BM25:
                continue  # covered by TestBM25Wiring (SQLite cannot run it)
            outcome = run_retrieval(world, config=config, question=question_text())
            assert outcome.question_id == ""
            assert outcome.candidate_limit == 20
            assert isinstance(outcome.latency_ms, float)
            assert outcome.doc_ranks == (str(document.id),)