"""Document management inside one tenant: PMP §15's ``/documents`` surface.

Every function here takes an :class:`~app.core.identity.AuthenticatedIdentity`
and **no tenant identifier**
--------------------------------------------------------------------------
The same contract as :mod:`app.services.users`, and for the same reason. A
tenant-scoped function taking a ``tenant_id`` parameter relies on every caller
passing the right one; a function taking the *authenticated identity* cannot be
pointed at the wrong tenant without constructing a forged identity, and forging
one requires a validly signed access token, because
:class:`~app.core.identity.AuthenticatedIdentity` is only built from verified
claims.

So ``tenant_id`` is not a parameter anywhere in this module. It is read from
``identity.tenant_id``, which came from a token this application signed. No
function here accepts a tenant from a request body, a query parameter, a header or
a cookie, and no request schema even offers one — see :mod:`app.schemas.documents`.

404, not 403, for another tenant's document
-------------------------------------------
A document id belonging to a different tenant is reported as *not found*, exactly
like an id that does not exist. A ``403`` would confirm "that document is real, you
just may not touch it", which is an enumeration oracle across tenant boundaries: a
tenant could probe ids and learn from the status code alone which documents exist
elsewhere. Indistinguishability is the point, so :class:`DocumentNotFoundError`
covers both cases and its message names neither.

File and database consistency
-----------------------------
An upload touches two systems that cannot share a transaction. The ordering here
is **write the file, then insert the row**, with the reverse considered and
rejected:

* *row first, file second* would leave a row describing content that does not
  exist — a document reporting ``processing`` forever, because there is nothing to
  parse. That failure would be silent and permanent.
* *file first* can leave an unreferenced file, but only if the **commit itself**
  fails, which happens after this function has returned. That residue is visible (a
  stored object no row names), locatable (a ``storage_key`` with no row), and
  removable.

Every failure *inside* these functions — validation, flush — is compensated by
deleting the file before the error propagates, so the ordinary rejection paths
leave nothing behind. That is not a distributed transaction and is not described
as one: the only genuine gap is a commit failure after a successful return, and
reconciling it is deferred work rather than a claim this phase makes.

Why the visibility rule is applied here, not in SQL
---------------------------------------------------
:attr:`~app.models.document.Document.allowed_roles` is a JSON column, because most
of this project's tests run against SQLite, which has no array type — an
``ARRAY`` column would make ``create_all`` fail and take the whole non-PostgreSQL
suite with it. The visible set is therefore computed in Python.

That does not weaken the boundary, and the distinction is the point:

* the **tenant** predicate — the property that keeps organisations apart — is in
  SQL, in every query below. It cannot be evaluated client-side, and it never is;
* the **role** predicate is a refinement *within* one tenant, applied to a set the
  caller already owns, and it is a three-value decision that reads far more
  clearly here than as portable SQL across two dialects.

A caller outside the tenant therefore never reaches the role filter, and a caller
inside the tenant cannot widen their own visibility with it.

Why deletion does not consult the role filter
---------------------------------------------
:func:`get_document` and :func:`list_documents` hide a document whose
``allowed_roles`` excludes the caller's role. :func:`delete_document`
deliberately does not, and the reason is a trap rather than a preference: a
document restricted to, say, ``["viewer"]`` is invisible to administrators, so
applying the read filter to deletion would make that document **permanently
undeletable**. No administrator could ever remove it, and the only way out would be
a database edit.

PMP §16 grants ``admin`` the delete operation without qualification, and an
administrator's authority inside their own organisation does not depend on which
documents they are allowed to read. So deletion is authorised by route-level RBAC
plus the tenant predicate, and nothing else. The information it reveals — that a
restricted document exists — is information an administrator who may delete every
document in the tenant already has.

What this module does not do
----------------------------
It never writes ``status``, ``chunk_count`` or ``error`` over HTTP. A document
enters as ``processing`` and leaves as ``processing``, because the worker that
would move it to ``ready`` or ``failed`` is PMP §22's Phase 5 and does not exist.
:func:`record_ingestion_outcome` is that writer, kept here so the columns PMP §4.1
puts in scope have one correct owner — and it is reachable from no route, because
a status a client can set would let an editor declare a document ``ready`` that was
never parsed.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from typing import IO

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.identity import AuthenticatedIdentity
from app.models.document import (
    DOCUMENT_ERROR_MAX_LENGTH,
    Document,
    DocumentStatus,
)
from app.models.user import UserRole
from app.services.uploads import SIGNATURE_PEEK_BYTES, validate_stored_upload
from app.storage.local import LocalFileStore

logger = logging.getLogger(__name__)


class DocumentNotFoundError(LookupError):
    """No such document in the caller's tenant — or no such document at all.

    One exception for both, so the HTTP layer cannot accidentally distinguish
    them. See the module docstring.
    """


#: The order role values are stored in, from :class:`~app.models.user.UserRole`.
#:
#: Computed once so :func:`_normalise_roles` sorts by the declaration order a
#: reader already knows rather than alphabetically, and so the mapping cannot drift
#: between the two functions that use it.
_ROLE_ORDER: dict[str, int] = {
    role.value: index for index, role in enumerate(UserRole)
}


def _scoped_document(
    session: Session, identity: AuthenticatedIdentity, document_id: uuid.UUID
) -> Document:
    """Load a document by id, scoped to the caller's tenant.

    The single lookup every function below uses, so the tenant filter cannot be
    forgotten at a call site. ``session.get`` is deliberately not used: it takes a
    primary key and offers nowhere to add the tenant predicate, and a
    primary-key-only lookup is precisely the bug this project must not ship.

    This is the concrete form of the ``WHERE document.id = ? AND document.tenant_id
    = ?`` rule. It applies no role filter, because the callers differ on whether
    that is appropriate — see the module docstring — and the filter belongs in the
    public functions where that decision is made, not here where it would be made
    silently for all of them.
    """
    document = session.scalar(
        select(Document).where(
            Document.id == document_id,
            Document.tenant_id == identity.tenant_id,
        )
    )
    if document is None:
        raise DocumentNotFoundError("no such document in this tenant")
    return document


def upload_limit_in_bytes(*, max_upload_size_mb: int) -> int:
    """Convert the configured megabyte limit into bytes.

    Exists so the conversion happens in one place with one rounding decision.
    Multiplying by ``1024 * 1024`` rather than ``1000 * 1000`` because the setting
    is named in megabytes and every other limit in computing is a power of two; with
    decimal multiplication a "10 MB" upload would be refused at 10 240 000 bytes by
    a server that believes it accepted 10 000 000.
    """
    return max_upload_size_mb * 1024 * 1024


def create_document(
    session: Session,
    *,
    identity: AuthenticatedIdentity,
    storage: LocalFileStore,
    source: IO[bytes],
    filename: str,
    allowed_roles: Sequence[UserRole] | None,
    max_upload_size_mb: int,
) -> Document:
    """Store an upload and its metadata as a new document of the caller's tenant.

    Args:
        session: the request's session. This function **flushes but never
            commits** — committing belongs to :func:`app.db.session.get_db`, so the
            row is inside the request's transaction and a handler that raises rolls
            it back.
        identity: the authenticated uploader. Supplies ``tenant_id`` *and*
            ``user_id``; neither is a parameter.
        storage: the store to write into.
        source: the uploaded stream. Read in bounded chunks, never held in memory
            in full.
        filename: already sanitised by :data:`app.schemas.documents.SafeFilename`.
        allowed_roles: the restriction to record, or ``None`` for unrestricted.
        max_upload_size_mb: the configured limit.

    Returns:
        The flushed :class:`~app.models.document.Document`, with its timestamps
        read back from the database rather than guessed in Python.

    Raises:
        app.services.uploads.UploadRejectedError: the file is empty, of an
            unsupported type, or its bytes disagree with its extension. Any file
            already written is removed first.
        app.storage.local.FileTooLargeError: the upload exceeds the limit. The
            partially written file is removed by the store before it propagates.
    """
    # Both tokens are minted before any I/O, which is what makes a residual file
    # identifiable: it sits at a `storage_key` that no row names.
    document_id = uuid.uuid4()
    storage_key = LocalFileStore.new_storage_key()
    limit = upload_limit_in_bytes(max_upload_size_mb=max_upload_size_mb)

    size_bytes = storage.copy_stream_into(
        identity.tenant_id,
        storage_key,
        source,
        max_bytes=limit,
    )
    logger.debug(
        "stored %d bytes for tenant %s as document %s",
        size_bytes,
        identity.tenant_id,
        document_id,
    )

    try:
        validated = validate_stored_upload(
            storage.path_for(identity.tenant_id, storage_key),
            filename=filename,
            size_bytes=size_bytes,
            leading_bytes=storage.read_leading_bytes(
                identity.tenant_id, storage_key, SIGNATURE_PEEK_BYTES
            ),
            max_bytes=limit,
        )
    except BaseException:
        # Compensating delete. Runs for a rejection *and* for an I/O failure, and
        # swallows its own errors: the caller is already unwinding from something
        # more informative, and a secondary storage error must not replace it.
        storage.discard_partial(identity.tenant_id, storage_key)
        raise

    document = Document(
        id=document_id,
        tenant_id=identity.tenant_id,
        uploaded_by=identity.user_id,
        filename=validated.filename,
        storage_key=storage_key,
        allowed_roles=_normalise_roles(allowed_roles),
        size_bytes=validated.size_bytes,
        content_type=validated.content_type,
    )
    session.add(document)

    try:
        session.flush()
    except BaseException:
        # The row never becomes visible, so neither should the file. Same
        # compensation, same reason.
        storage.discard_partial(identity.tenant_id, storage_key)
        raise

    return document


def list_documents(
    session: Session, *, identity: AuthenticatedIdentity
) -> Sequence[Document]:
    """Every document of the caller's tenant that the caller's role may view.

    Ordered by ``created_at`` then ``id``. Creation time alone is not deterministic
    when two documents land in the same millisecond — which is exactly what happens
    in a test that uploads two files back to back — and an unstable list order is a
    real pagination bug waiting to happen even though the PMP defines no
    pagination. ``id`` breaks the tie irreversibly.
    """
    documents = session.scalars(
        select(Document)
        .where(Document.tenant_id == identity.tenant_id)
        .order_by(Document.created_at, Document.id)
    ).all()

    return [
        document for document in documents if document.role_may_view(identity.role)
    ]


def get_document(
    session: Session, *, identity: AuthenticatedIdentity, document_id: uuid.UUID
) -> Document:
    """One document of the caller's tenant that the caller's role may view.

    Raises:
        DocumentNotFoundError: no such document in this tenant, **or** the
            caller's role may not view it, **or** the document belongs to another
            tenant. All three are reported identically, which is what keeps this
            endpoint from distinguishing "does not exist" from "exists but is not
            yours" from "exists in another organisation".
    """
    document = _scoped_document(session, identity, document_id)
    if not document.role_may_view(identity.role):
        raise DocumentNotFoundError("no such document in this tenant")
    return document


def get_document_status(
    session: Session, *, identity: AuthenticatedIdentity, document_id: uuid.UUID
) -> Document:
    """The ingestion state of one document, for an administrator.

    A read, so it applies the same visibility rule as :func:`get_document`; see the
    module docstring for why deletion is the sole operation that does not.

    Raises:
        DocumentNotFoundError: no such document, or the caller's role may not view
            it.
    """
    return get_document(session, identity=identity, document_id=document_id)


def get_document_for_ingestion(
    session: Session, *, identity: AuthenticatedIdentity, document_id: uuid.UUID
) -> Document:
    """One document of the caller's tenant, with **no** role filter.

    The lookup :mod:`app.services.ingestion` uses, and the reason it cannot use
    :func:`get_document`.

    A role filter here would be a bug, not a precaution. ``allowed_roles`` restricts
    who may **read** a document, and ingestion reads a document's bytes in order to
    index them — but it is the *system* doing that, not a person, and the result is not
    disclosed to the caller: it is a vector in the tenant's own collection and a row's
    status. Applying the filter would mean a document uploaded by an editor and
    restricted to ``admin`` could never become ``ready``, because the identity running
    the pipeline is not an admin — a failure that presents as "documents silently
    never finish indexing" and would be very hard to attribute.

    So the rule is: role governs **disclosure to a person**; tenant governs
    **everything else**. This function enforces the second and ignores the first,
    because it is the only kind of access in the project that is not disclosure.

    It still refuses a document from another tenant, by exactly the mechanism
    :func:`get_document` uses — :func:`_scoped_document` — so there is one tenant
    predicate in this module rather than two that could drift apart.

    Raises:
        DocumentNotFoundError: no such document in this tenant, which is deliberately
            also the answer for a document belonging to another tenant.
    """
    return _scoped_document(session, identity, document_id)


def delete_document(
    session: Session,
    *,
    identity: AuthenticatedIdentity,
    document_id: uuid.UUID,
    storage: LocalFileStore,
) -> Document:
    """Delete one document of the caller's tenant: its row and its stored file.

    Args:
        session: the request's session. As in :func:`create_document`, the delete
            is flushed here and committed by the caller.
        storage: the store holding the file.

    Returns:
        The deleted :class:`~app.models.document.Document`. Returned rather than
        discarded because the API layer logs its ``id``, and reading an attribute
        off an instance that has just been deleted is a trap.

    Ordering is **row first, file second** — the inverse of
    :func:`create_document`, for the same reason. A file with no row is removable
    and discoverable; a row with no file would advertise a document that does not
    exist, and the row is what a client can see.

    If the file is already absent — removed by hand, or by an operator — the delete
    still succeeds and the situation is logged rather than raised. The row is gone
    either way, so this is a cleanup that has already happened, and refusing would
    leave the caller unable to remove the record.

    Raises:
        DocumentNotFoundError: no such document in this tenant.
    """
    document = _scoped_document(session, identity, document_id)
    document_id_for_log = document.id

    session.delete(document)
    session.flush()

    if not storage.delete(identity.tenant_id, document.storage_key):
        logger.warning(
            "document %s was deleted but its stored object was already absent "
            "(tenant %s)",
            document_id_for_log,
            identity.tenant_id,
        )

    return document


def record_ingestion_outcome(
    session: Session,
    *,
    identity: AuthenticatedIdentity,
    document_id: uuid.UUID,
    status: DocumentStatus,
    chunk_count: int | None = None,
    error: str | None = None,
    search_vector: str | None = None,
) -> Document:
    """Move a document out of ``processing``, recording what the pipeline found.

    The write side of PMP §4.1's "track ingestion errors" and "track chunk counts",
    and of §20's "failures will be recorded in PostgreSQL". It lives here so the
    three columns have exactly one writer, and it is reachable from **no route** —
    see the module docstring.

    Args:
        status: the new status. A :class:`~app.models.document.DocumentStatus`, so
            a value PMP §10 does not name cannot be assigned at all.
        chunk_count: the pipeline's total, when it has one.
        error: why ingestion failed, when it has.
        search_vector: precomputed tsvector text for BM25 full-text search (PMP
            Phase 6). Populated on successful ingestion from the concatenated
            chunk text.

    Raises:
        DocumentNotFoundError: no such document in this tenant.
        ValueError: ``chunk_count`` is negative, or ``error`` is longer than the
            column accepts. Checked here because the database would otherwise catch
            it at flush time, where the failure reads as a server fault rather than
            the caller's mistake.
    """
    document = _scoped_document(session, identity, document_id)

    if chunk_count is not None and chunk_count < 0:
        raise ValueError("a chunk count cannot be negative")
    if error is not None and len(error) > DOCUMENT_ERROR_MAX_LENGTH:
        raise ValueError(
            f"an ingestion error is limited to {DOCUMENT_ERROR_MAX_LENGTH} characters"
        )

    document.status = status
    if chunk_count is not None:
        document.chunk_count = chunk_count
    document.error = error
    if search_vector is not None:
        document.search_vector = search_vector

    session.flush()
    return document


def _normalise_roles(
    allowed_roles: Sequence[UserRole] | None,
) -> list[str] | None:
    """Return the role values to persist, or ``None`` for unrestricted.

    One pass, so the stored value has a single canonical spelling: lower-cased,
    de-duplicated, ordered by :data:`_ROLE_ORDER`. Duplicates are removed because a
    column reading ``["admin", "admin"]`` looks like a bug in a user interface, and
    because equality between two documents' restrictions should mean they are the
    same restriction.

    ``None`` is passed through untouched. "Unrestricted" is a state, not an empty
    list — see :mod:`app.models.document` for why that distinction is a disclosure
    decision rather than a formality.
    """
    if allowed_roles is None:
        return None

    unique = {role.value for role in allowed_roles}
    return sorted(unique, key=_ROLE_ORDER.__getitem__)