"""``POST /query`` — ask this tenant's knowledge base a question.

PMP §15's ``Query`` group, and the first route in this project that **reads** tenant
content on the caller's behalf rather than returning a record the caller already asked
for. That difference is why this module is longer than its handler suggests.

What this route is responsible for, and what it is not
------------------------------------------------------
It does three things: derive identity from the existing dependency, call
:func:`app.services.query.answer_query`, and translate the service's failure types into
statuses. It applies no authorization rule of its own. The role allowlist comes from
:data:`app.api.documents.ViewingIdentity` — which already exists, built from
:func:`app.api.rbac.require_role`, because PMP §16 permits all three roles to query the
knowledge base — and the document-level filter is inside the retrieval pipeline, applied
against the payload and then re-checked against PostgreSQL. A route that added its own
role check would be a third place where the same policy is expressed, and the third is
where one of the three would eventually be wrong.

Why the tenant never appears here
---------------------------------
Not one handler parameter, form field or query parameter in this module is a tenant. It
comes from :class:`~app.core.identity.AuthenticatedIdentity`, and the vector store's
search method takes the tenant it is given rather than one it was told. See
:mod:`app.schemas.query` for why that absence is enforced rather than merely observed.

Why a failed provider is a ``503`` and not a ``502``
---------------------------------------------------
Both the embedding provider and the generation provider are dependencies this project
calls, and both their failure modes here are "that dependency is unavailable or
misconfigured" — no key configured, a timeout, an unparsable response. ``503`` says
"this is not your fault and not your request's, try again" and is the status a client can
correctly act on by retrying. It also keeps the two failures distinguishable from each
other by *detail*, because a client that cannot act on the difference should not have to
parse one: :data:`RETRIEVAL_UNAVAILABLE_DETAIL` and :data:`GENERATION_UNAVAILABLE_DETAIL`
are fixed sentences and an operator gets the precise reason from the log.

No ``GET /query/history`` and no ``POST /query/{id}/feedback`` here
---------------------------------------------------------------------
PMP §15 lists all three under ``Query``, and this module implements one: those two live
in :mod:`app.api.history`, mounted on the same prefix, because they read the
``query_logs`` table that the pipeline here writes (PMP §22 Phase 7).

Phase 7 additions to the read path
----------------------------------
This handler now runs the full PMP §11 read order: rate-limit check (``429`` over the
limit), Redis cache lookup (fail-open), retrieval + generation, cache write, and query
logging including audit events. Caching is tenant-scoped and role-aware; the cache key
always contains tenant context. Redis failures in the cache, rate limiter or audit
write are logged and never break the answer path — the query pipeline fails open.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, status
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

#: The one message returned when retrieval could not run.
#:
#: Fixed, and it names neither the provider nor the vector store. "the question could not
#: be turned into a query vector" and "Qdrant is unreachable" are two facts, and the first
#: is wrong half the time; a message that varies with the cause is a description of this
#: deployment's health handed to whoever provoked the request. The precise cause is logged.
RETRIEVAL_UNAVAILABLE_DETAIL = "The knowledge base is temporarily unavailable"

#: The one message returned when the provider produced no answer.
#:
#: Deliberately different from :data:`RETRIEVAL_UNAVAILABLE_DETAIL` even though a client
#: should retry both. Search succeeded, so the question may be perfectly answerable once
#: generation recovers, and a client that could tell the difference could avoid discarding a
#: result it had already computed.
GENERATION_UNAVAILABLE_DETAIL = "An answer could not be generated right now"


def _unavailable(detail: str, stage: str, exc: Exception) -> HTTPException:
    """Build the ``503`` for a failed dependency, from a logged reason.

    One function so the logged-and-returned split is identical for both stages: the
    operator learns which stage failed and why, and the caller learns only that the
    service is unavailable. This is the same separation :mod:`app.api.deps` applies to
    token failures and :mod:`app.api.documents` applies to document lookups.
    """
    logger.warning("query %s failed: %s: %s", stage, type(exc).__name__, exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    )


@router.post(
    "",
    response_model=QueryResponse,
    summary="Ask the knowledge base a question",
)
def ask_question(
    request: QueryRequest,
    identity: ViewingIdentity,
    store: VectorStore,
    embedding_provider: EmbeddingProviderDep,
    generation_provider: GenerationProviderDep,
    app_settings: AppSettings,
    redis_client: RedisClient,
    session: Session = DbSession,
) -> QueryResponse:
    """Answer ``request.question`` from the caller's tenant, with citations.

    The read path (PMP §11): rate limit check → Redis cache → hybrid retrieval
    → generation → SSE-ready answer + citations → query logging.

    Responds ``200`` even when nothing was retrieved. An empty knowledge base is not a
    failed request — it is a successful answer to the question "do you have anything on
    this?", and the response says so through ``grounded: false`` and an empty
    ``citations`` list. A client that wants a 404 for "no results" would have to be
    written against a contract this module does not have.

    Args:
        request: the question. Already length-checked and stripped by
            :class:`~app.schemas.query.QueryRequest`.
        identity: the authenticated caller. Supplies the tenant and the role, and both
            reach the vector store.
        session: read-only. Nothing on this path writes.
        store: the tenant-scoped vector store.
        embedding_provider: embeds the question.
        generation_provider: writes the answer.
        app_settings: supplies ``query_top_k`` and ``query_max_context_chars``.
        redis_client: the shared Redis client (cache + rate limiting).

    Raises:
        fastapi.HTTPException: ``422`` for a question the service refuses, ``429`` when
            rate limited, ``503`` when retrieval or generation is unavailable.
    """
    start_time = time.perf_counter()

    # 1. Rate limit check (PMP §11) — fail-open via the limiter.
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
        # Record the rate-limited event for analytics and audit.
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
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc

    # 2. Redis cache check. The key must contain tenant context and capture the
    #    role so answers generated for one role are never served to another.
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
        return cached

    # 3. Hybrid retrieval + generation (the expensive path).
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
    except QueryRejectedError as exc:
        # The only failure here that is the caller's own doing, so it is the only one
        # answered with a specific reason. The reason names the limit that was exceeded,
        # which is the client's own configuration's shape rather than anything secret.
        logger.info("query rejected: %s", exc)
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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The question was rejected",
        ) from exc
    except RetrievalFailedError as exc:
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
        raise _unavailable(RETRIEVAL_UNAVAILABLE_DETAIL, "retrieval", exc) from exc
    except GenerationFailedError as exc:
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
        raise _unavailable(GENERATION_UNAVAILABLE_DETAIL, "generation", exc) from exc
    except QueryError as exc:
        # Unreachable while the four subclasses above are exhaustive, and kept so that a
        # future stage failure is a 503 by default instead of a 500. The default for a
        # pipeline failure on this route has to be "not the caller's fault", the same way
        # app.api.rbac's empty allowlist has to be "denied".
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
        raise _unavailable(RETRIEVAL_UNAVAILABLE_DETAIL, "query", exc) from exc

    latency_ms = int((time.perf_counter() - start_time) * 1000)

    # 4. Store the response in the cache (best-effort, fail-open).
    # We create the response after logging so we can include the query log ID.
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

    cache.set(
        tenant_id=str(identity.tenant_id),
        role=identity.role.value,
        question=request.question,
        response=response,
    )

    record_audit(
        session=session,
        identity=identity,
        action="query.requested",
        resource_type="query",
        outcome="success",
        details={"cache_hit": False},
    )

    return response


def _empty_answer(question: str) -> QueryAnswer:
    """A minimal answer object for error-path query logging (no context).

    Error events have no generated answer; this carries the question and an
    empty answer while keeping QA-compatible shape.
    """
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