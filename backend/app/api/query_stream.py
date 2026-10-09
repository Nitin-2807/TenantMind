"""SSE streaming query endpoint (Phase 7).

``POST /query/stream`` — ask a question and receive the answer as a
Server-Sent Events stream (PMP §25 success criterion 9: "Responses can be
streamed using SSE").

Event format
------------
Each event is a standard SSE frame::

    event: <type>
    data: <json>

Types emitted, in order:

* ``start`` — the request passed authentication; carries the query id.
* ``answer`` — the full answer text (the generation provider is synchronous,
  so the answer arrives whole rather than token-by-token).
* ``citations`` — the citations the answer is grounded in.
* ``done`` — the stream is complete.
* ``error`` — the pipeline failed (rate limited, rejected, retrieval or
  generation unavailable). No further events follow.

Why the answer is delivered whole rather than token-by-token
------------------------------------------------------------
The generation provider's contract — :meth:`GenerationProvider.generate`
— is "one call, one answer string". Making this endpoint stream tokens
would require a new streaming transport against the provider, i.e. a
rewrite of the Phase 4 generation boundary. PMP requires responses *can be
streamed using SSE*, which this endpoint satisfies: a client receives
incremental lifecycle progress and the final answer over an SSE channel,
with the standard ``text/event-stream`` media type and keep-alive headers.

The pipeline inside the stream follows PMP §11's read order, exactly like
``POST /query``: rate limit check → Redis cache → hybrid retrieval →
generation → cache write → query logging. Query history and analytics work
identically for streamed answers.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import (
    AppSettings,
    EmbeddingProviderDep,
    GenerationProviderDep,
    VectorStore,
)
from app.api.documents import ViewingIdentity
from app.core.redis import RedisClient
from app.db.session import DbSession
from app.rag.cache import QueryCache
from app.rag.rate_limit import RateLimiter, RateLimitExceeded
from app.rag.retrieval import Citation
from app.schemas.query import QueryRequest, QueryResponse, citations_of
from app.services.audit import record_audit
from app.services.history import record_query
from app.services.query import (
    GenerationFailedError,
    QueryAnswer,
    QueryError,
    QueryRejectedError,
    RetrievalFailedError,
    answer_query,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/query", tags=["query"])


def _sse_event(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, separators=(",", ":"))}\n\n"


def _empty_answer(question: str) -> QueryAnswer:
    """Minimal answer for error-path query logging (no context)."""
    return QueryAnswer(
        question=question,
        answer="",
        citations=(),
        grounded=False,
        context_truncated=False,
    )


def _cached_to_answer(cached: QueryResponse) -> QueryAnswer:
    """Convert a cached response back into an answer object for logging."""
    citations = tuple(
        Citation(
            document_id=c.document_id,
            filename=c.filename,
            chunk_index=c.chunk_index,
            page=c.page,
        )
        for c in cached.citations
    )
    return QueryAnswer(
        question=cached.question,
        answer=cached.answer,
        citations=citations,
        grounded=cached.grounded,
        context_truncated=cached.context_truncated,
    )


def _error_event(
    query_id: uuid.UUID | str, code: str, detail: str, *, retry_after: int | None = None
) -> str:
    """Build the single ``error`` event for a failed pipeline."""
    data: dict = {
        "query_id": str(query_id),
        "code": code,
        "detail": detail,
    }
    if retry_after is not None:
        data["retry_after"] = retry_after
    return _sse_event("error", data)


@router.post(
    "/stream",
    summary="Stream a query answer via Server-Sent Events",
    responses={
        200: {
            "description": "SSE stream of query lifecycle events",
            "content": {"text/event-stream": {}},
        }
    },
)
def stream_query(
    request: QueryRequest,
    identity: ViewingIdentity,
    store: VectorStore,
    embedding_provider: EmbeddingProviderDep,
    generation_provider: GenerationProviderDep,
    app_settings: AppSettings,
    redis_client: RedisClient,
    session: Session = DbSession,
) -> StreamingResponse:
    """Stream the answer to ``request.question`` as SSE lifecycle events.

    The handler returns a :class:`~fastapi.responses.StreamingResponse`
    immediately; the generator below runs the §11 pipeline and emits events
    as it progresses. All failures inside the pipeline are emitted as an
    ``error`` event rather than an HTTP error status, so the client always
    receives a well-formed stream. The HTTP status here is always ``200``
    once the request is authenticated and not rate limited.
    """
    def stream() -> Iterator[str]:
        start_time = time.perf_counter()
        query_id = uuid.uuid4()

        yield _sse_event("start", {"query_id": str(query_id)})

        limiter = RateLimiter(
            client=redis_client,
            limit=app_settings.rate_limit_per_minute,
            window_seconds=app_settings.rate_limit_window_seconds,
        )
        try:
            limiter.check(
                tenant_id=str(identity.tenant_id),
                user_id=str(identity.user_id),
            )
        except RateLimitExceeded as exc:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            record_query(
                session=session,
                identity=identity,
                answer=_empty_answer(request.question),
                cache_hit=False,
                latency_ms=latency_ms,
                status="error",
                error_kind="rate_limited",
            )
            record_audit(
                session=session,
                identity=identity,
                action="query.rate_limited",
                resource_type="query",
                outcome="denied",
            )
            yield _error_event(
                query_id, "rate_limited", "Too many requests", retry_after=exc.retry_after
            )
            return

        cache = QueryCache(
            client=redis_client,
            ttl_seconds=app_settings.query_cache_ttl_seconds,
        )
        cached = cache.get(
            tenant_id=str(identity.tenant_id),
            role=identity.role.value,
            question=request.question,
        )
        if cached is not None:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            record_query(
                session=session,
                identity=identity,
                answer=_cached_to_answer(cached),
                cache_hit=True,
                latency_ms=latency_ms,
                status="success",
            )
            record_audit(
                session=session,
                identity=identity,
                action="query.requested",
                resource_type="query",
                outcome="success",
                details={"cache_hit": True},
            )
            yield _sse_event(
                "answer",
                {
                    "query_id": str(query_id),
                    "answer": cached.answer,
                    "grounded": cached.grounded,
                    "context_truncated": cached.context_truncated,
                },
            )
            yield _sse_event(
                "citations",
                {
                    "query_id": str(query_id),
                    "citations": [c.model_dump(mode="json") for c in cached.citations],
                },
            )
            yield _sse_event("done", {"query_id": str(query_id), "cache_hit": True})
            return

        # Expensive path: hybrid retrieval + generation.
        try:
            answer = answer_query(
                session=session,
                identity=identity,
                store=store,
                embedding_provider=embedding_provider,
                generation_provider=generation_provider,
                settings=app_settings,
                question=request.question,
            )
        except QueryRejectedError:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            record_query(
                session=session,
                identity=identity,
                answer=_empty_answer(request.question),
                cache_hit=False,
                latency_ms=latency_ms,
                status="error",
                error_kind="rejected",
            )
            yield _error_event(query_id, "rejected", "The question was rejected")
            return
        except RetrievalFailedError:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            record_query(
                session=session,
                identity=identity,
                answer=_empty_answer(request.question),
                cache_hit=False,
                latency_ms=latency_ms,
                status="error",
                error_kind="retrieval",
            )
            yield _error_event(
                query_id, "retrieval_failed", "The knowledge base is temporarily unavailable"
            )
            return
        except GenerationFailedError:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            record_query(
                session=session,
                identity=identity,
                answer=_empty_answer(request.question),
                cache_hit=False,
                latency_ms=latency_ms,
                status="error",
                error_kind="generation",
            )
            yield _error_event(query_id, "generation_failed", "An answer could not be generated right now")
            return
        except QueryError:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            record_query(
                session=session,
                identity=identity,
                answer=_empty_answer(request.question),
                cache_hit=False,
                latency_ms=latency_ms,
                status="error",
                error_kind="retrieval",
            )
            yield _error_event(
                query_id, "retrieval_failed", "The knowledge base is temporarily unavailable"
            )
            return

        latency_ms = int((time.perf_counter() - start_time) * 1000)
        log = record_query(
            session=session,
            identity=identity,
            answer=answer,
            cache_hit=False,
            latency_ms=latency_ms,
            status="success",
        )
        response = QueryResponse(
            id=str(log.id) if log else "",
            question=answer.question,
            answer=answer.answer,
            citations=citations_of(answer.citations),
            grounded=answer.grounded,
            context_truncated=answer.context_truncated,
        )

        # Cache the response (best-effort, fail-open).
        cache.set(
            tenant_id=str(identity.tenant_id),
            role=identity.role.value,
            question=request.question,
            response=response,
        )

        # Query logging + audit (PMP §11: last step of the read path).
        record_audit(
            session=session,
            identity=identity,
            action="query.requested",
            resource_type="query",
            outcome="success",
            details={"cache_hit": False},
        )

        query_id = log.id if log else query_id

        yield _sse_event(
            "answer",
            {
                "query_id": str(query_id),
                "answer": answer.answer,
                "grounded": answer.grounded,
                "context_truncated": answer.context_truncated,
            },
        )
        yield _sse_event(
            "citations",
            {
                "query_id": str(query_id),
                "citations": [c.model_dump(mode="json") for c in response.citations],
            },
        )
        yield _sse_event("done", {"query_id": str(query_id), "cache_hit": False})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )