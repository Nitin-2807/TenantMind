"""Tests for the deterministic answer path: provider, context, and scoring.

The answer stand-in is clearly labelled (``extractive-eval-v1``), returns a
verbatim evidence sentence, and refuses on empty context exactly like
production's no-hallucination path. These tests pin down that contract and the
grounding claim the report makes: Faithfulness ≈ 1 by construction for
grounded answers, retrieved-context quality still shows up in relevance and
correctness, and an empty retrieval produces a refusal — never a fabricated
answer.
"""

from __future__ import annotations

import pytest

from app.models import UserRole
from app.rag.generation import GenerationProvider
from app.rag.retrieval import RetrievedChunk
from evaluation.answer_eval import AnswerOutcome, generate_and_score
from evaluation.generation import (
    MODEL_NAME,
    NO_EVIDENCE_ANSWER,
    ExtractiveAnswerProvider,
    best_evidence_sentence,
    context_for,
)
from evaluation.metrics import faithfulness
from evaluation.retrieval_eval import RetrievalConfig
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

ANSWERABLE_QUESTION = question_text()


@pytest.fixture
def world(auth_settings, model_session, make_account):
    from tests.conftest import make_settings

    settings = make_settings(
        jwt_secret_key=auth_settings.jwt_secret_key,
        access_token_expire_minutes=auth_settings.access_token_expire_minutes,
        refresh_token_expire_days=auth_settings.refresh_token_expire_days,
        storage_root=auth_settings.storage_root,
        embedding_dimension=8,
    )
    tenant, user = make_account(slug="eval-answers", role=UserRole.VIEWER)
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


def generate(world: dict, *, config: RetrievalConfig) -> AnswerOutcome:
    return generate_and_score(
        session=world["session"],
        identity=world["identity"],
        store=world["store"],
        embeddings=EMBEDDINGS,
        generation=ExtractiveAnswerProvider(),
        settings=world["settings"],
        question_id="q001",
        question=ANSWERABLE_QUESTION,
        config=config,
        top_k=5,
        candidate_limit=20,
    )


class TestExtractiveProvider:
    def test_implements_the_production_generation_contract(self) -> None:
        provider = ExtractiveAnswerProvider()
        assert isinstance(provider, GenerationProvider)
        assert provider.model_name == MODEL_NAME

    def test_answers_with_the_best_evidence_sentence(self) -> None:
        evidence = (
            "Employees accrue 25 days of annual leave each year. "
            "Leave is credited at the start of January."
        )
        answer = best_evidence_sentence("How many days of annual leave?", evidence)
        assert answer == "Employees accrue 25 days of annual leave each year."

    def test_is_deterministic_and_tie_breaks_to_first(self) -> None:
        evidence = "Alpha sentence. Beta sentence. Alpha sentence again."
        question = "alpha"
        first = best_evidence_sentence(question, evidence)
        second = best_evidence_sentence(question, evidence)
        assert first == second == "Alpha sentence."

    def test_refuses_on_empty_evidence(self) -> None:
        assert best_evidence_sentence("anything", "") == NO_EVIDENCE_ANSWER

    def test_generate_records_each_call(self) -> None:
        provider = ExtractiveAnswerProvider()
        provider.generate(
            instructions="answer from evidence", question="q", evidence="A fact."
        )
        assert len(provider.calls) == 1
        assert provider.calls[0]["question"] == "q"


class TestContextBuilder:
    def test_context_uses_the_production_builder(self, world: dict) -> None:
        chunk = RetrievedChunk(
            document_id=world["document"].id,
            filename="leave-policy.md",
            chunk_index=0,
            page=1,
            text=corpus_text(),
            score=0.9,
        )
        evidence = context_for([chunk], ANSWERABLE_QUESTION, max_chars=4000)
        assert "25 days" in evidence


class TestGenerateAndScore:
    def test_grounded_answer_for_an_answerable_question(self, world: dict) -> None:
        seed_points(
            world["store"],
            world["tenant"],
            [
                point_for(
                    tenant=world["tenant"],
                    document=world["document"],
                    chunk_index=0,
                    text=corpus_text(),
                )
            ],
        )
        outcome = generate(world, config=RetrievalConfig.DENSE)
        assert not outcome.refusal
        assert outcome.grounded
        assert outcome.context_chunk_count == 1
        assert "25 days" in outcome.answer
        # Verbatim evidence sentences are fully supported by that evidence.
        assert outcome.faithfulness == pytest.approx(1.0)
        assert faithfulness(outcome.answer, corpus_text()) == pytest.approx(1.0)

    def test_empty_retrieval_produces_a_refusal_not_a_hallucination(
        self, world: dict
    ) -> None:
        # Collection exists but holds nothing; dense retrieval returns no chunks.
        world["store"].ensure_collection(world["tenant"].id)
        outcome = generate(world, config=RetrievalConfig.DENSE)
        assert outcome.refusal
        assert not outcome.grounded
        assert outcome.answer == ""
        assert outcome.context_chunk_count == 0
        assert outcome.generation_latency_ms == 0.0
        assert outcome.faithfulness == pytest.approx(1.0)  # nothing claimed

    def test_hybrid_and_rerank_configs_drive_the_same_answer_path(
        self, world: dict
    ) -> None:
        seed_points(
            world["store"],
            world["tenant"],
            [
                point_for(
                    tenant=world["tenant"],
                    document=world["document"],
                    chunk_index=0,
                    text=corpus_text(),
                )
            ],
        )
        for config in (RetrievalConfig.HYBRID, RetrievalConfig.HYBRID_RERANK):
            outcome = generate(world, config=config)
            assert outcome.grounded
            assert "25 days" in outcome.answer

    def test_latencies_are_recorded(self, world: dict) -> None:
        seed_points(
            world["store"],
            world["tenant"],
            [
                point_for(
                    tenant=world["tenant"],
                    document=world["document"],
                    chunk_index=0,
                    text=corpus_text(),
                )
            ],
        )
        outcome = generate(world, config=RetrievalConfig.DENSE)
        assert outcome.retrieval_latency_ms >= 0.0
        assert outcome.generation_latency_ms >= 0.0
        assert outcome.total_latency_ms >= outcome.retrieval_latency_ms