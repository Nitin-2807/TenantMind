"""Security tests for the Phase 8 evaluator: it cannot bypass tenant isolation.

The phase brief (§8, §19, §25) requires the benchmark to move through the same
tenant-scoped, role-filtered, re-verified architecture as production. These
tests pin the two structural guarantees the evaluator relies on:

* the retrieval adapter exposes **no** tenant-id parameter, so a benchmark
  cannot scope itself to another tenant even by mistake;
* the production components are the ones measured (module identity, not a
  second retrieval implementation), and the production authorization layers
  still apply end to end (payload role filter and the PostgreSQL
  re-verification mirror).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.models import Tenant, User, UserRole
from app.rag import bm25, hybrid, rerank
from evaluation import retrieval_eval
from evaluation.retrieval_eval import (
    RetrievalConfig,
    RetrievalOutcome,
    retrieve,
)
from tests.eval_helpers import (
    EMBEDDINGS,
    make_identity,
    make_store,
    point_for,
    question_text,
    seed_document,
    seed_points,
)


class TestNoTenantBypassSurface:
    def test_retrieve_accepts_no_tenant_id_argument(self) -> None:
        parameters = inspect.signature(retrieve).parameters
        assert "tenant_id" not in parameters
        assert "identity" in parameters

    def test_retrieve_accepts_no_role_argument(self) -> None:
        parameters = inspect.signature(retrieve).parameters
        assert "role" not in parameters

    def test_no_second_retrieval_implementation(self) -> None:
        """The adapters drive the production components, not copies of them."""
        assert retrieval_eval.bm25_search is bm25.bm25_search
        assert retrieval_eval.hybrid_retrieve is hybrid.hybrid_retrieve
        assert retrieval_eval.load_candidate_chunks is hybrid.load_candidate_chunks
        assert retrieval_eval.rerank_chunks is rerank.rerank_chunks


@pytest.fixture
def world(auth_settings, model_session, make_account) -> dict[str, Any]:
    from tests.conftest import make_settings

    settings = make_settings(
        jwt_secret_key=auth_settings.jwt_secret_key,
        access_token_expire_minutes=auth_settings.access_token_expire_minutes,
        refresh_token_expire_days=auth_settings.refresh_token_expire_days,
        storage_root=auth_settings.storage_root,
        embedding_dimension=8,
    )
    tenant, user = make_account(slug="eval-sec", role=UserRole.VIEWER)
    document = seed_document(
        model_session,
        tenant=tenant,
        uploaded_by=user,
        filename="restricted.md",
        allowed_roles=["admin"],
    )
    model_session.commit()
    store = make_store(settings)
    viewer_identity = make_identity(settings, tenant, user)
    return {
        "settings": settings,
        "session": model_session,
        "tenant": tenant,
        "user": user,
        "identity": viewer_identity,
        "document": document,
        "store": store,
    }


def run_dense(world: dict[str, Any], *, identity) -> RetrievalOutcome:
    return retrieve(
        session=world["session"],
        identity=identity,
        store=world["store"],
        embeddings=EMBEDDINGS,
        settings=world["settings"],
        question=question_text(),
        config=RetrievalConfig.DENSE,
        top_k=5,
        candidate_limit=20,
    )


class TestRoleFilteringSurvivesInTheBenchmark:
    def test_admin_only_document_is_invisible_to_the_viewer(self, world: dict) -> None:
        seed_points(
            world["store"],
            world["tenant"],
            [
                point_for(
                    tenant=world["tenant"],
                    document=world["document"],
                    chunk_index=0,
                    text=question_text(),
                    allowed_roles=["admin"],
                )
            ],
        )
        outcome = run_dense(world, identity=world["identity"])
        # The store's payload filter never returned the chunk to the caller.
        assert outcome.retrieved_count == 0
        assert outcome.doc_ranks == ()
        assert outcome.chunks == ()

    def test_admin_identity_can_retrieve_the_same_document(
        self, world: dict, make_account
    ) -> None:
        _, admin = make_account(
            slug="eval-sec-admin",
            email="admin@tenantmind.local",
            role=UserRole.ADMIN,
        )
        admin_identity = make_identity(world["settings"], world["tenant"], admin)
        seed_points(
            world["store"],
            world["tenant"],
            [
                point_for(
                    tenant=world["tenant"],
                    document=world["document"],
                    chunk_index=0,
                    text=question_text(),
                    allowed_roles=["admin"],
                )
            ],
        )
        outcome = run_dense(world, identity=admin_identity)
        assert outcome.doc_ranks == (str(world["document"].id),)
        assert outcome.chunks


class TestEmptyKnowledgeBase:
    def test_empty_collection_produces_an_honest_empty_outcome(
        self, world: dict
    ) -> None:
        world["store"].ensure_collection(world["tenant"].id)
        outcome = run_dense(world, identity=world["identity"])
        assert outcome.retrieved_count == 0
        assert outcome.doc_ranks == ()
        assert outcome.chunks == ()


class TestEvaluationIdentityIsMintedThroughTheRealPath:
    def test_eval_identity_maps_to_the_eval_account_row(
        self, model_session, auth_settings
    ) -> None:
        from evaluation import runner
        from tests.conftest import make_settings

        settings = make_settings(
            jwt_secret_key=auth_settings.jwt_secret_key,
            storage_root=auth_settings.storage_root,
        )
        identity = runner.eval_identity(model_session, settings)
        assert identity.role is UserRole.VIEWER
        tenant = model_session.get(Tenant, identity.tenant_id)
        assert tenant is not None
        assert tenant.slug == runner.EVAL_TENANT_SLUG
        user = model_session.get(User, identity.user_id)
        assert user is not None
        assert user.tenant_id == identity.tenant_id