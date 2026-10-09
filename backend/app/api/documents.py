"""Document-management endpoints (PMP §15, Phase 2.4).

    POST   /documents
    GET    /documents
    GET    /documents/{document_id}
    DELETE /documents/{document_id}

    GET    /admin/documents/{document_id}/status

PMP §15 lists all five, and no others are invented here. In particular there is
**no** ``PATCH /documents/{id}``: the PMP defines no endpoint that updates a
document, and inventing a metadata-update route would create a contract no client
was written against — and, worse, an ``allowed_roles`` edit that could be used to
hand a document away after the fact.

Where the role rules come from
------------------------------
Every route's requirement is declared once, by an alias built from
:func:`app.api.rbac.require_role`, and is readable straight off the handler
signature:

* :data:`UploadingIdentity` — ``admin`` and ``editor``. PMP §6.1 and §6.2 both
  list uploading; §6.3 does not.
* :data:`ViewingIdentity` — all three roles. PMP §6.3 lists "querying authorized
  organizational information", and the document list is what such a query reads.
* :data:`AdminIdentity` — ``admin``. PMP §16 grants document deletion to the
  administrator alone, and §15 places the status endpoint under Administration.

The alias and the annotation are one declaration, not two. ``require_role``
returns the identity unchanged, so annotating a handler's ``identity`` parameter
both authorizes the request and supplies the identity — there is no separate step
that could be forgotten. ``tests/test_documents_api.py`` asserts the resulting
matrix over HTTP for all three roles on all five routes, because the one failure
mode this shape invites is an alias that is declared, documented and never used.

No route in this module reads a role or compares one. That is enforced by a test
rather than by convention: ``tests/test_rbac.py`` parses every module under
``app/api`` and fails if a bare role comparison appears, so the invariant cannot
quietly erode as the router grows.

The tenant boundary is the identity, not a parameter
----------------------------------------------------
:mod:`app.services.documents` takes the identity and no ``tenant_id``, so no route
here can pass one. A path parameter names *which* document; it never names *whose*
tenant it is looked up in. See that module for why another tenant's document is a
``404`` rather than a ``403``.

Upload shape, and why a form rather than JSON
----------------------------------------------
An upload cannot be JSON — the bytes have to arrive as ``multipart/form-data``.
``allowed_roles`` rides along as a form field for the same reason: one request, one
body, one request parser. The tenant is *not* a form field, and there is no field
on this route that could carry one.

Where ingestion starts (Phase 5 Step 3)
--------------------------------------
An accepted upload commits the document row and then publishes the fixed
:data:`~app.workers.ingestion.INGEST_DOCUMENT_TASK_NAME` task with the document id
— the *only* argument — through
:func:`~app.workers.ingestion.enqueue_ingestion`. The tenant never leaves the
request: the worker re-derives it from the row, so the chain stays
``AuthenticatedIdentity -> Document.tenant_id -> Celery(document_id) ->
Document.tenant_id -> tenant-scoped ingestion``. The single door into the worker
package is that function; see :mod:`app.workers.ingestion` for why there is
exactly one.

Handlers are ``def``, not ``async def`` — see :mod:`app.api.auth` for why.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.deps import AppSettings, FileStore
from app.api.rbac import require_role
from app.core.identity import AuthenticatedIdentity
from app.db.session import DbSession
from app.models.user import UserRole
from app.schemas.documents import (
    DocumentAllowedRoles,
    DocumentListResponse,
    DocumentResponse,
    DocumentStatusResponse,
    sanitise_filename,
)
from app.services.documents import (
    DocumentNotFoundError,
    create_document,
    delete_document,
    get_document,
    get_document_status,
    list_documents,
)
from app.services.uploads import UploadRejectedError
from app.storage.local import FileTooLargeError
from app.workers.ingestion import EnqueueError, enqueue_ingestion

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])

#: ``GET /admin/documents/{id}/status`` is a separate router so the
#: administrator-only surface is visible as its own group in the API document,
#: matching how PMP §15 lists it under "Administration" rather than beside the
#: tenant's own endpoints. Both routers are mounted from the same module because the
#: two surfaces share one service and one set of role rules.
admin_router = APIRouter(prefix="/admin/documents", tags=["documents"])

#: Administrators and editors may upload — PMP §6.1, §6.2.
MAY_UPLOAD = require_role(UserRole.ADMIN, UserRole.EDITOR)

#: Every role may view — PMP §6.3's viewer needs something to query.
MAY_VIEW = require_role(UserRole.ADMIN, UserRole.EDITOR, UserRole.VIEWER)

#: Deletion and the administration status view are the administrator's alone —
#: PMP §16.
ADMIN_ONLY = require_role(UserRole.ADMIN)

#: The annotated identity each allowlist produces.
#:
#: Built once at import time, exactly as :data:`app.api.users.CurrentAdmin` is,
#: and *used*: a route declares its requirement by its annotation and nothing
#: else. That is the whole mechanism — ``require_role`` returns the identity
#: unchanged, so annotating ``identity`` this way both authorises the request and
#: hands the handler the identity it needs, with no second declaration and no
#: chance of the two disagreeing.
UploadingIdentity = Annotated[AuthenticatedIdentity, Depends(MAY_UPLOAD)]
ViewingIdentity = Annotated[AuthenticatedIdentity, Depends(MAY_VIEW)]
AdminIdentity = Annotated[AuthenticatedIdentity, Depends(ADMIN_ONLY)]

#: The one message returned for every refused upload.
#:
#: Fixed, and it names neither the rule that failed nor the supported extensions.
#: :mod:`app.services.uploads` builds precise messages carrying exactly that,
#: because they are wanted in a log; this is what the caller receives instead.
UPLOAD_REJECTED_DETAIL = "The uploaded file was rejected"

#: The one message returned when a document is not visible.
#:
#: Same string :mod:`app.services.documents` raises with, and it names neither the
#: tenant nor the role. A client cannot tell "does not exist" from "not yours".
NOT_FOUND_DETAIL = "Document not found"

#: The one message returned when a stored document could not be queued.
#:
#: The upload succeeded — the document exists and its status is ``processing`` —
#: but no ingestion task was published, and reporting ``202`` would falsely claim
#: one was. ``503`` says "try again", which is the honest advice: the document is
#: real, and a later Phase 5 step owns reconciling rows no task ever picked up.
#: The detail names no task name, no queue and no broker, and carries no document
#: identifier: the caller knows which upload failed because this is the response
#: to *that* request.
QUEUE_UNAVAILABLE_DETAIL = "The document was stored but could not be queued for ingestion"


def _reject(reason: str, exc: Exception) -> HTTPException:
    """Build the ``422`` for a refused upload, from a logged reason.

    One function so every refusal in this module is handled identically: the precise
    explanation — which rule failed, and for a bad filename which character — goes
    to the log, and the caller receives a fixed sentence. That follows the rule
    :mod:`app.api.deps` applies to token failures for the same reason: an operator
    needs the diagnosis and an attacker gets nothing from it.
    """
    logger.info("upload rejected: reason=%s detail=%s", reason, exc)
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=UPLOAD_REJECTED_DETAIL,
    )


def _not_found(exc: DocumentNotFoundError) -> HTTPException:
    """Build the ``404`` for an invisible document.

    Logged with the exception and returned with a fixed detail, the same
    separation :mod:`app.api.deps` applies to token failures: the operator needs to
    know a lookup was refused, and the caller needs to learn nothing about what
    exists.
    """
    logger.info("document lookup refused: %s", exc)
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=NOT_FOUND_DETAIL,
    )


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DocumentResponse,
    summary="Upload a document",
)
def upload_document(
    identity: UploadingIdentity,
    file: Annotated[UploadFile, File()],
    storage: FileStore,
    app_settings: AppSettings,
    allowed_roles: Annotated[DocumentAllowedRoles | None, Form()] = None,
    session: Session = DbSession,
) -> DocumentResponse:
    """Accept a document for ingestion and return its record.

    Responds ``202``, not ``201``, because PMP §10's write path ends this request
    at "queue ingestion job → 202 Accepted": the request has been accepted for
    processing and will not complete within this call. ``201`` would promise a
    finished resource that this endpoint deliberately does not produce.

    The stored status is ``processing`` and stays there until the worker's
    pipeline ends it at ``ready`` or ``failed``. Nothing in this request parses
    a document.

    Ordering, and why the commit is here rather than inherited:
    ``create_document`` flushes but does not commit — committing belongs to
    :func:`app.db.session.get_db`, whose commit happens *after* the handler
    returns. A task published from this handler before that commit could be
    picked up by a worker that finds no row. So the transaction is committed
    here, **before** the enqueue, matching PMP §10's ordering — "PostgreSQL
    Document Record" then "Queue Ingestion Job". :func:`get_db` commits again
    when the handler returns; with nothing pending that second commit is a
    no-op. The remaining crash window (commit succeeds, publish fails) is
    written up in the execution log and is Phase 5 failure/retry work.

    Args:
        identity: the uploader. Supplies both the owning tenant and the uploader's
            user id.
        file: the uploaded bytes. Never trusted for its name, its declared type, or
            its size — see :mod:`app.services.uploads`.
        allowed_roles: the roles permitted to view it, or omitted for
            unrestricted.
        session: committed by this handler before the task is published; the
            framework's later commit is a no-op.
        storage: where the bytes go.
        app_settings: supplies the upload size limit.

    Raises:
        fastapi.HTTPException: ``413`` for an over-size upload, ``422`` for a file
            that is empty, of an unsupported type, or whose content disagrees with
            its extension, ``422`` for a filename that is not a bare name, and
            ``503`` when the document was stored but could not be queued for
            ingestion.
    """
    try:
        filename = sanitise_filename(file.filename or "")
    except ValidationError as exc:
        # Refused before a single byte is read, so a bad name costs no disk write
        # and leaves nothing behind. `file.filename` can be `None` for a part sent
        # without one, which the `or ""` turns into this same refusal rather than
        # an `AttributeError`.
        raise _reject("invalid_filename", exc) from exc

    try:
        document = create_document(
            session,
            identity=identity,
            storage=storage,
            source=file.file,
            filename=filename,
            allowed_roles=allowed_roles,
            max_upload_size_mb=app_settings.max_upload_size_mb,
        )
    except FileTooLargeError as exc:
        # 413 is the registered status for exactly this condition, and it tells a
        # client something 422 cannot: retrying differently will not help. The
        # number reported is the limit this server actually enforced, not a
        # hard-coded copy of the setting.
        logger.info("upload refused as too large (limit %d bytes)", exc.max_bytes)
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                "The uploaded file exceeds the maximum permitted size of "
                f"{exc.max_bytes} bytes"
            ),
        ) from exc
    except UploadRejectedError as exc:
        raise _reject(exc.reason, exc) from exc

    # Commit before publish: the worker can collect a message as soon as it is
    # on the broker, and it must find the row already committed. See the
    # docstring above for why this duplicates get_db's commit.
    session.commit()

    try:
        enqueue_ingestion(document.id)
    except EnqueueError as exc:
        # The document is committed and real; only the publication failed, and
        # a 202 would falsely claim the document was queued. `processing` is the
        # truthful status for a stored-but-not-yet-ingested document, and the id
        # goes to the log (safe — no content, no credentials) so an operator can
        # reconcile the gap this step records as a known crash window.
        logger.error(
            "document stored but not queued: document=%s tenant=%s",
            document.id,
            identity.tenant_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=QUEUE_UNAVAILABLE_DETAIL,
        ) from exc

    return DocumentResponse.model_validate(document)


@router.get(
    "",
    response_model=DocumentListResponse,
    summary="List the caller's documents",
)
def list_tenant_documents(
    identity: ViewingIdentity, session: Session = DbSession
) -> DocumentListResponse:
    """Every document of the caller's tenant that the caller's role may view.

    Unpaginated, because PMP §15 defines no pagination — see
    :class:`app.schemas.documents.DocumentListResponse` for why inventing one
    would be the worse choice.
    """
    documents = list_documents(session, identity=identity)
    return DocumentListResponse(
        items=[DocumentResponse.model_validate(document) for document in documents]
    )


@router.get(
    "/{document_id}",
    response_model=DocumentResponse,
    summary="Retrieve one document",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such document"}},
)
def retrieve_document(
    document_id: uuid.UUID,
    identity: ViewingIdentity,
    session: Session = DbSession,
) -> DocumentResponse:
    """One document's metadata, or ``404``.

    The ``404`` covers a document that does not exist, one belonging to another
    tenant, and one the caller's role may not view. Those are not collapsed for
    convenience: collapsing them is what keeps the endpoint from answering "does
    document X exist over there?".
    """
    try:
        document = get_document(
            session, identity=identity, document_id=document_id
        )
    except DocumentNotFoundError as exc:
        raise _not_found(exc) from exc
    return DocumentResponse.model_validate(document)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one document",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such document"}},
)
def remove_document(
    document_id: uuid.UUID,
    identity: AdminIdentity,
    storage: FileStore,
    session: Session = DbSession,
) -> Response:
    """Delete one document and its stored file.

    ``204``, and administrators only — PMP §16. Unlike every read in this module,
    the delete is **not** narrowed by the document's ``allowed_roles``: see
    :mod:`app.services.documents` for the reason, which is a document that would
    otherwise be permanently undeletable.

    Idempotency is partial, and deliberately: deleting a document twice fails the
    second time with ``404``, because after the first delete there is no document
    left to identify. That is the honest answer — the second request is not
    "successfully already done", it is "no such resource".
    """
    try:
        delete_document(
            session, identity=identity, document_id=document_id, storage=storage
        )
    except DocumentNotFoundError as exc:
        raise _not_found(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@admin_router.get(
    "/{document_id}/status",
    response_model=DocumentStatusResponse,
    summary="View a document's processing status",
    responses={status.HTTP_404_NOT_FOUND: {"description": "No such document"}},
)
def view_document_status(
    document_id: uuid.UUID,
    identity: AdminIdentity,
    session: Session = DbSession,
) -> DocumentStatusResponse:
    """The ingestion state of one document.

    Administrators only — PMP §15 lists this under Administration and §6.1 gives
    "viewing document processing status" to the administrator. It is a narrower view
    of the same row as :func:`retrieve_document`, kept separate so the
    administrator-only surface stays enumerable from the API document.
    """
    try:
        document = get_document_status(
            session, identity=identity, document_id=document_id
        )
    except DocumentNotFoundError as exc:
        raise _not_found(exc) from exc
    return DocumentStatusResponse.model_validate(document)