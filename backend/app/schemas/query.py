"""The request and response shapes for ``POST /query``.

PMP §7 FR-09, FR-12, FR-13 and §15's ``Query`` group. One request, one response, and
neither of them carries a tenant identifier — the argument for that is
:class:`QueryRequest`'s own docstring, and it is the single most important thing to get
right about this module.

Why there is no tenant field
----------------------------
Every tenant-scoped shape in this project takes its tenant from
:class:`~app.core.identity.AuthenticatedIdentity`, which
:func:`app.api.deps.get_current_identity` builds from a verified token. That is not a
convention this module follows for tidiness; it is the property the whole system rests
on. FR-07 requires that a user reach only their own organisation's information, and the
only way to hold that line is for the tenant to be *absent* from anything a client can
influence. A ``tenant_id`` on this request would be a field that pydantic accepts,
FastAPI documents in the OpenAPI schema, and a future client helpfully fills in — and its
presence would make the tenant boundary a thing callers are trusted to respect.

So the absence is enforced three times over: there is no field to set, the route derives
everything it needs from :data:`app.api.documents.ViewingIdentity`, and the vector store's
search method takes a tenant it was given rather than one it was told.

PMP §15 lists ``GET /query/history`` and ``POST /query/{id}/feedback`` in the same group.
Neither is here: both require a query-history store, which is PMP §22 Phase 7 and does
not exist yet. They are absent because their dependency is absent, not deferred
silently — see ``PROJECT_EXECUTION_LOG.md``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.rag.retrieval import Citation

#: A question that is present, not blank, and not absurdly long.
#:
#: ``min_length=1`` alone would accept ``"   "``, which would then be answered by the
#: service's own empty-question refusal and surface as a ``422`` naming an internal rule
#: rather than the client's own input. Stripping here means the client is told at the
#: boundary, which is the only place a client can act on the message.
#:
#: The upper bound is ``query_max_question_chars``'s job at the service layer, because the
#: number belongs to configuration and duplicating it here would be a second copy to keep
#: in step. This bound is deliberately loose — it exists to stop a multi-megabyte body
#: being accepted, not to express a product rule.
Question = Annotated[str, Field(min_length=1, max_length=8000)]


class QueryRequest(BaseModel):
    """The body of ``POST /query``.

    Attributes:
        question: what the user asked, in their own words.

    Deliberately one field. ``top_k`` is absent for the same reason ``tenant_id`` is: it
    is a decision about how this system retrieves, not about what this caller needs, and
    exposing it would let a caller ask for more context than the configured budget
    permits. ``query_top_k`` in :class:`~app.core.config.Settings` is the only place that
    number is written down.
    """

    question: Question

    @field_validator("question")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Strip surrounding whitespace and refuse a question that is only whitespace.

        Returns the stripped value so the service, the log line and the prompt all see the
        same text the user typed rather than a padded copy of it.
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("the question must not be blank")
        return stripped


class CitationResponse(BaseModel):
    """One source an answer was grounded in.

    Built from :class:`~app.rag.retrieval.Citation` rather than returned as it, for the
    reason :class:`app.schemas.documents.DocumentResponse` documents: handing FastAPI an
    internal dataclass would couple the public contract to an internal type, and the
    internal type is free to gain fields that have no business in a public API.

    Every field here exists in the chunk payload written by
    :func:`app.rag.vectors.payload_for`. None is invented, and in particular there is no
    score, no snippet and no timestamp: a similarity score is not something a user can
    act on, and the retrieved text is already in the answer.
    """

    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    filename: str
    chunk_index: int
    page: int | None


class QueryResponse(BaseModel):
    """An answer and its sources.

    Attributes:
        id: the unique identifier of this query log entry. Required for feedback
            and history lookup.
        question: the question as answered, echoed back so a client rendering a
            conversation does not have to keep its own copy in step.
        answer: the generated text, or the service's fixed "nothing found" sentence when
            ``grounded`` is ``False``.
        citations: the chunks the answer came from, numbered in the order the model saw
            them. Empty when ``grounded`` is ``False``.
        grounded: whether an answer was generated. A client can branch on this instead of
            recognising a fixed sentence, which is a more stable thing to branch on than a
            string.
        context_truncated: whether evidence was dropped to fit the context budget. It does
            not mean the answer was wrong. It is surfaced rather than hidden because a
            silently narrower answer is a wrong answer for a client that then repeats the
            question more emphatically.
    """

    id: str
    question: str
    answer: str
    citations: list[CitationResponse]
    grounded: bool
    context_truncated: bool


def citations_of(answer_citations: tuple[Citation, ...]) -> list[CitationResponse]:
    """Convert internal citations to their response form.

    Exists so the conversion happens in one place with one rule — order preserved, no
    sorting, no deduplication, no dropping — rather than as a list comprehension in a
    handler where a later edit could sort them and quietly renumber the answer's markers.
    """
    return [CitationResponse.model_validate(citation) for citation in answer_citations]