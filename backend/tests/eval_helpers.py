"""Shared test harness for the Phase 8 evaluation adapters.

Both ``test_eval_retrieval.py`` and ``test_eval_security.py`` drive the *real*
``evaluation.retrieval_eval`` adapters against a SQLite session and an
in-memory :class:`~tests.test_qdrant_store.FakeQdrant`, with identities minted
through the real token path. Everything they share lives here once, so a
behaviour change in one file cannot silently leave the other asserting on a
stale copy of the harness.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.identity import AuthenticatedIdentity
from app.core.security import create_access_token
from app.models.document import Document, DocumentStatus
from app.models.tenant import Tenant
from app.models.user import User
from app.rag.vectors import VectorPoint, payload_for, point_id
from app.storage.qdrant import QdrantVectorStore
from evaluation.embeddings import LexicalHashEmbeddings
from tests.records import fake_client_of, store_over
from tests.test_qdrant_store import FakeQdrant

#: Matches the collection width the test-suite store fake is built with.
WIDTH = 8

#: Deterministic stand-in embeddings the production pipeline would drive with.
EMBEDDINGS = LexicalHashEmbeddings(dimensions=WIDTH)


def make_store(settings: Settings) -> QdrantVectorStore:
    """An evaluation store over the in-memory fake client."""
    return store_over(settings, FakeQdrant(size=WIDTH))


def fake_of(store: QdrantVectorStore) -> FakeQdrant:
    """The fake client an :func:`make_store` store is holding."""
    return fake_client_of(store)


def make_identity(
    settings: Settings, tenant: Tenant, user: User
) -> AuthenticatedIdentity:
    """The evaluated identity, minted through the real token path."""
    token = create_access_token(
        subject=user.id,
        tenant_id=tenant.id,
        role=str(user.role.value),
        app_settings=settings,
    )
    return AuthenticatedIdentity.from_access_token(token, app_settings=settings)


def seed_document(
    session: Session,
    *,
    tenant: Tenant,
    uploaded_by: User,
    filename: str,
    allowed_roles: list[str] | None = None,
    status: DocumentStatus = DocumentStatus.READY,
) -> Document:
    """A committed ``documents`` row, like the query-service tests build."""
    document = Document(
        tenant_id=tenant.id,
        uploaded_by=uploaded_by.id,
        filename=filename,
        storage_key=uuid.uuid4().hex,
        status=status,
        allowed_roles=allowed_roles,
        size_bytes=1,
        content_type="text/plain",
    )
    session.add(document)
    session.flush()
    return document


def point_for(
    *,
    tenant: Tenant,
    document: Document,
    chunk_index: int = 0,
    text: str,
    vector: list[float] | None = None,
    page: int | None = 1,
    allowed_roles: list[str] | None = None,
) -> VectorPoint:
    """A Qdrant point for one chunk, exactly as the production pipeline writes one."""
    return VectorPoint(
        id=point_id(
            tenant_id=tenant.id, document_id=document.id, chunk_index=chunk_index
        ),
        vector=vector if vector is not None else EMBEDDINGS.embed_texts([text])[0],
        payload=payload_for(
            tenant_id=tenant.id,
            document_id=document.id,
            chunk_index=chunk_index,
            page=page,
            text=text,
            allowed_roles=allowed_roles,
            filename=document.filename,
        ),
    )


def seed_points(
    store: QdrantVectorStore, tenant: Tenant, points: list[VectorPoint]
) -> None:
    """Write points through the store's own collection-ensuring API."""
    store.ensure_collection(tenant.id)
    store.upsert_points(tenant.id, points)


def question_text() -> str:
    """A generic question whose tokens appear in the corpus below."""
    return "How many days of annual leave do employees get over a year?"


def corpus_text() -> str:
    """Fictional corpus text mentioning the answer to :func:`question_text`."""
    return (
        "Employees accrue 25 days of annual leave each year. "
        "Leave is credited at the start of January."
    )