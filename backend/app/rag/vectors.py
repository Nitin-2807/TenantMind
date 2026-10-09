"""Vector points: the id and payload every chunk becomes in Qdrant (PMP §13).

What this module is for
-----------------------
PMP §13 specifies a vector store with a documented point payload, and Phase 2.5's
requirement is that the payload be a *pure function* of a chunk and the document it
came from. Everything here is a pure function: same inputs, same outputs, no clock,
no counter, no database, no random source. That is what makes re-ingesting a document
overwrite its own points rather than accumulate duplicates.

Keeping it pure also keeps it free of ``qdrant_client`` imports. The security-relevant
part of this phase — how a point is identified, and what a payload is allowed to
contain — is then testable without a vector database of any kind, and
:mod:`app.storage.qdrant` is left doing nothing but I/O.

The point id
------------
PMP §13 requires that a point be traceable to a tenant, a document and a chunk.
:func:`point_id` derives a **UUID version 5** from exactly those three, which is where
each part of the design comes from:

* **Deterministic.** The same tenant, document and chunk index always produce the same
  id. Re-ingestion is therefore an overwrite, not an append — the property PMP §4.1's
  "track chunk counts" depends on, since the count would otherwise grow on every
  retry.
* **Collision-free across all three scopes.** All three are inputs to the derivation,
  so chunk 0 of tenant A's document 1 cannot collide with chunk 0 of tenant B's
  document 1, nor with chunk 1 of the same document. That matters even though each
  tenant has its own collection: a payload copied between collections during a
  migration must not silently merge two tenants' points.
* **Not a counter.** A process-local counter would make the id depend on how many
  points this process happened to write, so the same chunk would get a different id
  after a restart, and re-ingestion would duplicate the document instead of replacing
  it.
* **A UUID rather than an integer.** Qdrant accepts a point id as either an unsigned
  64-bit integer or a UUID. An integer would have to be *derived* from the three
  scopes — most likely by hashing them and truncating — which reintroduces collision
  risk exactly where the id matters. UUIDv5 is a full 128-bit SHA-1 derivation with no
  truncation.

The limitation is stated rather than hidden: this phase does not version documents, so
editing a document's text and re-ingesting it overwrites chunk *N* with the new text
of chunk *N* even if the chunk boundaries moved. There is no way to hold both versions
— see the module's own reference to PMP §28's future "automatic document versioning".

The payload
-----------
Exactly the fields PMP §13 lists, and nothing else:

``document_id``
    The public document UUID, as a string. A string rather than a UUID object because
    Qdrant payloads are JSON, and a JSON string round-trips identically through REST,
    gRPC and the client's local mode — a value that compared unequal after a round-trip
    would break the ``document_id`` filter that deletion depends on.

``chunk_index``
    The chunk's ordinal, as an integer. An integer so a range filter ("delete every
    chunk with index >= 12") is a numeric comparison rather than a string one.

``page``
    The 1-based page, or ``None``. Present for PDF and PowerPoint, ``None`` for
    formats with no pages. ``None`` rather than ``0`` so "not applicable" and "the
    zeroth page" cannot be confused — the first is true and the second is impossible.

``text``
    The chunk's own content. Stored rather than referenced so retrieval never has to
    fetch a document to quote it, which is what makes PMP §20's citation requirement
    answerable without a second round trip.

``allowed_roles``
    The document's role restriction, copied from :attr:`Document.allowed_roles`, or
    ``None`` when the document is unrestricted. **Copied, not referenced** — see
    :class:`VectorPoint` for why a copy is the only safe choice here.

``filename``
    The document's display filename, for citation and for the "which document did
    this come from" affordance.

Plus ``tenant_id``, which PMP §12 requires for data isolation. The per-tenant
collection is already a hard boundary; carrying the tenant in the payload as well is
defence in depth, so that a future move to a shared collection cannot be carried out
by changing one line and forgetting the other. It is written from
``document.tenant_id`` — a column on a row the tenant-scoped lookup already returned
— and there is no code path that can write another tenant's value.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

#: The namespace every TenantMind point id is derived within.
#:
#: A fixed, project-specific UUID rather than one of the standard
#: ``uuid.NAMESPACE_DNS``/``URL``/``OID`` values, because those describe *the world*
#: and this derivation describes one application's internal records. Using a project's
#: own namespace means an id can never coincide with a name-based UUID produced
#: anywhere else, which is what makes the derivation a namespace-isolated operation
#: rather than a coincidence.
#:
#: Generated once and then never changed: changing it would give every existing
#: document's chunks a new id, orphaning every point already indexed. It is recorded
#: here rather than generated at import so it is reviewable.
POINT_ID_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f9d3a1e-8b2c-4f17-9e05-2c7a4d8b1e63")

#: Payload keys, named once.
#:
#: Named constants rather than inline strings because every one of them appears in
#: three places — construction here, the delete filter in
#: :mod:`app.storage.qdrant`, and the tests — and a typo in any of them would produce
#: a filter that matches nothing. That failure is silent: deletion "succeeds" and the
#: points remain.
PAYLOAD_DOCUMENT_ID: Final[str] = "document_id"
PAYLOAD_CHUNK_INDEX: Final[str] = "chunk_index"
PAYLOAD_PAGE: Final[str] = "page"
PAYLOAD_TEXT: Final[str] = "text"
PAYLOAD_ALLOWED_ROLES: Final[str] = "allowed_roles"
PAYLOAD_FILENAME: Final[str] = "filename"
PAYLOAD_TENANT_ID: Final[str] = "tenant_id"

#: Every payload key this phase writes, in a stable order.
#:
#: Exported so a test can assert the payload contains exactly these keys — the
#: cheapest possible guard against a future field being added by accident, and the
#: clearest statement of what PMP §13 asked for.
PAYLOAD_KEYS: Final[tuple[str, ...]] = (
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_CHUNK_INDEX,
    PAYLOAD_PAGE,
    PAYLOAD_TEXT,
    PAYLOAD_ALLOWED_ROLES,
    PAYLOAD_FILENAME,
    PAYLOAD_TENANT_ID,
)

#: Upper bound on the characters stored in ``text``.
#:
#: The chunker already bounds this at ``chunk_size_chars``, so this is a second,
#: independent bound — the one that survives a chunker bug. A payload field with no
#: length limit is an unbounded write into Qdrant from a tenant-supplied document, so
#: the store would hold whatever it was given.
MAX_PAYLOAD_TEXT_CHARS: Final[int] = 8_000


def point_id(
    *, tenant_id: uuid.UUID, document_id: uuid.UUID, chunk_index: int
) -> str:
    """Derive the Qdrant point id for one chunk of one document of one tenant.

    Args:
        tenant_id: the document's owning tenant. Part of the derivation, so two
            tenants can never produce the same id for the same document id and index.
        document_id: the public document UUID.
        chunk_index: the chunk's ordinal within the document, from 0.

    Returns:
        A lowercase hyphenated UUID string, which is the form Qdrant accepts for a
        point id and the form that round-trips through REST and gRPC unchanged.

    Raises:
        ValueError: ``chunk_index`` is negative. A negative index would derive a
            perfectly valid UUID, so nothing downstream would ever notice — the
            failure would be a chunk numbered -1 in a payload, which is worse than
            a raised error because it is not an error to anything.
    """
    if chunk_index < 0:
        raise ValueError(f"chunk_index must not be negative, got {chunk_index}")
    return str(
        uuid.uuid5(
            POINT_ID_NAMESPACE,
            f"{tenant_id}/{document_id}/{chunk_index}",
        )
    )


@dataclass(frozen=True, slots=True)
class VectorPoint:
    """One chunk, its vector, and its payload — ready to be written.

    Attributes:
        id: the derived point id. See :func:`point_id`.
        vector: the embedding. Must be exactly as wide as the store's collection.
        payload: the metadata, built by :meth:`payload_for` so it cannot drift from
            what that function documents.

    :class:`Frozen` and ``slots=True`` because a point is passed between three
    components (the builder, the orchestrator, the store) and should not be mutable
    while in flight — a payload edited after its id was derived would produce a point
    whose stored metadata disagrees with the metadata its id was computed from.
    """

    id: str
    vector: list[float]
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.vector:
            raise ValueError("a vector point requires a vector")
        unexpected = set(self.payload) - set(PAYLOAD_KEYS)
        if unexpected:
            raise ValueError(
                f"unexpected payload keys: {', '.join(sorted(unexpected))}"
            )
        missing = set(PAYLOAD_KEYS) - set(self.payload)
        if missing:
            raise ValueError(
                f"missing payload keys: {', '.join(sorted(missing))}"
            )

    def with_id(self, new_id: str) -> VectorPoint:
        """Return a copy with a different id.

        Exists because the id is derived from the tenant, the document and the chunk
        index — three values the builder has, and the store has only a finished point.
        Deriving the id where the rest of the point is built, rather than leaving it to
        the store, keeps :mod:`app.storage.qdrant` free of identity logic; this method
        is the seam, so the store never has to know how an id is derived.
        """
        return VectorPoint(id=new_id, vector=self.vector, payload=self.payload)


def payload_for(
    *,
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    chunk_index: int,
    page: int | None,
    text: str,
    allowed_roles: list[str] | None,
    filename: str,
) -> dict[str, Any]:
    """Build the Qdrant payload for one chunk.

    Args:
        tenant_id: the document's owning tenant.
        document_id: the document's public UUID.
        chunk_index: the chunk's ordinal, from 0.
        page: the 1-based page, or ``None`` for formats without pages.
        text: the chunk's content. Truncated at
            :data:`MAX_PAYLOAD_TEXT_CHARS` if a chunker bug produced more.
        allowed_roles: the roles permitted to read the document, or ``None`` when it
            is unrestricted. Copied into a new list so a caller mutating its own list
            afterwards cannot change an already-derived payload.
        filename: the document's display filename.

    Returns:
        A payload holding exactly :data:`PAYLOAD_KEYS`.

    Raises:
        ValueError: ``chunk_index`` is negative or ``page`` is not a positive integer.
            Checked here rather than at the chunker because this function is the last
            point before the value reaches a store, and a page ``0`` in a payload is a
            citation pointing at a page that does not exist.

    Note on truncation
    ------------------
    ``text`` is truncated rather than refused. The chunker bounds it already, so
    reaching the limit means the bound was violated by a bug — and refusing would
    fail the whole document over one oversized chunk, while truncating degrades one
    chunk and leaves the failure visible in the recorded chunk length. Neither is
    right, and this is the lesser of the two; the assertion belongs in the chunker's
    own tests.
    """
    if chunk_index < 0:
        raise ValueError(f"chunk_index must not be negative, got {chunk_index}")
    if page is not None and page < 1:
        raise ValueError(
            f"page must be a positive 1-based number or None, got {page}"
        )
    if not text.strip():
        raise ValueError(
            "a vector point requires text; an empty chunk must never be indexed"
        )

    return {
        PAYLOAD_DOCUMENT_ID: str(document_id),
        PAYLOAD_CHUNK_INDEX: chunk_index,
        PAYLOAD_PAGE: page,
        PAYLOAD_TEXT: text[:MAX_PAYLOAD_TEXT_CHARS],
        PAYLOAD_ALLOWED_ROLES: None if allowed_roles is None else list(allowed_roles),
        PAYLOAD_FILENAME: filename,
        PAYLOAD_TENANT_ID: str(tenant_id),
    }
