"""HTTP tests for ``POST /query/stream`` — SSE streaming (Phase 7).

The route is the one place streaming is observable end-to-end: the real bearer
dependency, the real rate limiter, the real cache, the real §11 pipeline, and
the real query-log/audit writes behind a ``text/event-stream`` response.

These tests assert the SSE contract (PMP §25 success criterion 9):

* the response is ``text/event-stream`` and carries the lifecycle events
  ``start → answer → citations → done`` in order;
* pipeline failures are emitted as an ``error`` event on the stream, never as
  an unexpected status or a malformed body;
* a cached answer short-circuits the pipeline (no retrieval) and is still
  logged with ``cache_hit`` set;
* every streamed answer is recorded in ``query_logs`` and ``audit_logs`` —
  history and analytics work identically for streamed answers.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client import models
from sqlalchemy.orm import Session

from app.api.deps import (
    get_embedding_provider,
    get_generation_provider,
    get_vector_store,
)
from app.core.config import Settings
from app.core.redis import get_redis_client
from app.core.security import create_access_token
from app.models import Document, DocumentStatus, UserRole
from app.rag.cache import _cache_key
from app.schemas.query import CitationResponse, QueryResponse
from app.services.query import NO_CONTEXT_ANSWER
from tests.generation import FakeGenerationProvider
from tests.records import store_of
from tests.redis import FakeRedis
from tests.test_ingestion import RecordingProvider
from tests.test_qdrant_store import FakeQdrant
from tests.test_query_service import WIDTH, RecordingStore

STREAM_PATH = "/api/v1/query/stream"
QUESTION = "What is the annual leave allowance?"

BEARER_SCHEME = "Bearer"


def credentials(token: str) -> dict[str, str]:
    return {"Authorization": f"{BEARER_SCHEME} {token}"}


def token_for(user: Any, tenant: Any, auth_settings: Settings) -> str:
    return create_access_token(
        subject=user.id,
        tenant_id=tenant.id,
        role=user.role,
        app_settings=auth_settings,
    )


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse a raw SSE body into ``[(event_type, data), ...]`` in order."""
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in text.split("\n\n"):
        frame = frame.strip()
        if not frame:
            continue
        event_type: str | None = None
        data: dict[str, Any] = {}
        for line in frame.split("\n"):
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        assert event_type is not None, f"frame without an event type: {frame!r}"
        events.append((event_type, data))
    return events


class OverflowRedis(FakeRedis):
    """A Redis that always reports the counter past the limit.

    Every ``INCR`` returns 61, which is over the 60-request-per-minute default,
    so the very first request of a window is refused. This is how the 429 path
    is reachable without a server and without waiting out a window.
    """

    def incr(self, key: str) -> int:  # type: ignore[override]
        return 61


@dataclass
class World:
    """The app with retrieval + Redis redirected, and two committed tenants.

    Mirrors ``tests/test_query_api.py``'s :class:`World`; the additions are the
    ``redis`` fake (exposed so a test can seed a cache hit or force a rate
    limit) and the point/seed helpers the streaming tests need to produce a
    grounded answer over HTTP.
    """

    session: Session
    client: TestClient
    app: FastAPI
    store: RecordingStore
    embeddings: RecordingProvider
    generation: FakeGenerationProvider
    redis: FakeRedis
    settings: Settings
    tenant_a: uuid.UUID
    token_admin: str
    token_viewer: str
    token_rival: str
    documents: list[Document] = field(default_factory=list)

    def make_document(self, *, filename: str = "handbook.txt") -> Document:
        document = Document(
            tenant_id=self.tenant_a,
            uploaded_by=None,
            filename=filename,
            storage_key=uuid.uuid4().hex,
            status=DocumentStatus.READY,
            allowed_roles=None,
            size_bytes=1,
            content_type="text/plain",
        )
        self.session.add(document)
        self.session.commit()
        self.documents.append(document)
        return document

    def chunk(
        self,
        document: Document,
        *,
        text: str = "Staff accrue twenty days of annual leave.",
        chunk_index: int = 0,
        page: int | None = 1,
        score: float = 0.9,
    ) -> models.ScoredPoint:
        """A ``ScoredPoint`` shaped exactly as the real store returns one."""
        return models.ScoredPoint(
            id=uuid.uuid4().hex,
            version=0,
            score=score,
            payload={
                "tenant_id": str(document.tenant_id),
                "document_id": str(document.id),
                "chunk_index": chunk_index,
                "page": page,
                "text": text,
                "allowed_roles": document.allowed_roles,
                "filename": document.filename,
            },
            vector=None,
        )

    def ask(self, *, token: str, question: str = QUESTION) -> Any:
        return self.client.post(
            STREAM_PATH,
            json={"question": question},
            headers=credentials(token),
        )


@pytest.fixture
def world(
    api_app: FastAPI,
    api_client: TestClient,
    make_committed_account: Any,
    model_session: Session,
    auth_settings: Settings,
) -> Iterator[World]:
    store = store_of(auth_settings, FakeQdrant(size=WIDTH), RecordingStore)
    embeddings = RecordingProvider(dimensions=WIDTH)
    generation = FakeGenerationProvider()
    redis = FakeRedis()
    api_app.dependency_overrides[get_vector_store] = lambda: store
    api_app.dependency_overrides[get_embedding_provider] = lambda: embeddings
    api_app.dependency_overrides[get_generation_provider] = lambda: generation
    api_app.dependency_overrides[get_redis_client] = lambda: redis

    acme, admin = make_committed_account(
        slug="acme", email="admin@example.com", role=UserRole.ADMIN
    )
    _, viewer = make_committed_account(
        slug="acme", email="viewer@example.com", role=UserRole.VIEWER
    )
    globex, rival = make_committed_account(
        slug="globex", email="rival@example.com", role=UserRole.ADMIN
    )
    model_session.commit()

    built = World(
        session=model_session,
        client=api_client,
        app=api_app,
        store=store,
        embeddings=embeddings,
        generation=generation,
        redis=redis,
        settings=auth_settings,
        tenant_a=acme.id,
        token_admin=token_for(admin, acme, auth_settings),
        token_viewer=token_for(viewer, acme, auth_settings),
        token_rival=token_for(rival, globex, auth_settings),
    )
    yield built
    store.close()


class TestAuthentication:
    def test_stream_requires_a_token(self, api_client: TestClient) -> None:
        response = api_client.post(STREAM_PATH, json={"question": QUESTION})

        assert response.status_code == 401


class TestLifecycleEvents:
    def test_streams_start_answer_citations_done(
        self, world: World
    ) -> None:
        document = world.make_document()
        world.store.points = [world.chunk(document)]
        world.generation.answer = "The streamed answer."

        response = world.ask(token=world.token_viewer)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(response.text)
        kinds = [kind for kind, _ in events]
        assert kinds == ["start", "answer", "citations", "done"]

        data = {kind: payload for kind, payload in events}
        (query_id,) = {d["query_id"] for _, d in events}
        assert data["answer"] == {
            "query_id": query_id,
            "answer": "The streamed answer.",
            "grounded": True,
            "context_truncated": False,
        }

        (citation,) = data["citations"]["citations"]
        assert citation["filename"] == "handbook.txt"
        assert citation["document_id"]
        assert "text" not in citation  # citation metadata only, never the chunk
        assert data["done"] == {"query_id": query_id, "cache_hit": False}

    def test_stream_is_recorded_in_history_and_audit(self, world: World) -> None:
        from app.models.audit_log import AuditLog
        from app.models.query_log import QueryLog

        document = world.make_document()
        world.store.points = [world.chunk(document)]

        world.ask(token=world.token_viewer)

        log = world.session.query(QueryLog).one()
        assert log.tenant_id == world.tenant_a
        assert log.question == QUESTION
        assert log.status == "success"
        assert log.cache_hit is False

        audit = world.session.query(AuditLog).filter(AuditLog.action == "query.requested").one()
        assert audit.tenant_id == world.tenant_a
        assert audit.outcome == "success"

    def test_an_empty_knowledge_base_streams_the_no_context_answer(
        self, world: World
    ) -> None:
        world.store.points = []

        response = world.ask(token=world.token_viewer)

        events = parse_sse(response.text)
        assert [kind for kind, _ in events] == ["start", "answer", "citations", "done"]
        data = {kind: payload for kind, payload in events}
        assert data["answer"]["answer"] == NO_CONTEXT_ANSWER
        assert data["answer"]["grounded"] is False
        assert data["citations"]["citations"] == []


class TestCachedStream:
    def test_a_cached_answer_skips_retrieval(self, world: World) -> None:
        from app.models.query_log import QueryLog

        citation = CitationResponse(
            document_id=uuid.uuid4(),
            filename="limits.pdf",
            chunk_index=3,
            page=2,
        )
        cached = QueryResponse(
            question=QUESTION,
            answer="cached answer",
            citations=[citation],
            grounded=True,
            context_truncated=False,
        )
        key = _cache_key(
            tenant_id=str(world.tenant_a), role=UserRole.VIEWER.value, question=QUESTION
        )
        world.redis.setex(
            key, world.settings.query_cache_ttl_seconds, cached.model_dump_json()
        )

        response = world.ask(token=world.token_viewer)

        events = parse_sse(response.text)
        assert [kind for kind, _ in events] == ["start", "answer", "citations", "done"]
        assert world.store.call_count == 0  # retrieval never ran
        data = {kind: payload for kind, payload in events}
        assert data["answer"]["answer"] == "cached answer"
        assert data["citations"]["citations"][0]["filename"] == "limits.pdf"
        assert data["done"]["cache_hit"] is True

        log = world.session.query(QueryLog).one()
        assert log.cache_hit is True
        assert log.status == "success"


class TestRateLimitedStream:
    def test_a_rate_limited_stream_emits_an_error_event(self, world: World) -> None:
        from app.models.query_log import QueryLog

        world.redis = OverflowRedis()
        world.app.dependency_overrides[get_redis_client] = lambda: world.redis

        response = world.ask(token=world.token_viewer)

        assert response.status_code == 200  # the stream itself is well-formed
        events = parse_sse(response.text)
        assert [kind for kind, _ in events] == ["start", "error"]
        error = events[1][1]
        assert error["code"] == "rate_limited"
        assert error["retry_after"] >= 1
        assert "answer" not in response.text  # no answer event after an error

        log = world.session.query(QueryLog).one()
        assert log.status == "error"
        assert log.error_kind == "rate_limited"