"""Request and response schemas for the document API (PMP §15, Phase 2.4).

The one rule that shapes every request model here
------------------------------------------------
No field a client sends can name a tenant. There is no ``tenant_id`` anywhere in
this module, and that is the same deliberate absence
:mod:`app.schemas.users` documents: :mod:`app.services.documents` takes an
:class:`~app.core.identity.AuthenticatedIdentity` and reads
``identity.tenant_id``, so a tenant identifier has exactly one possible source in
the whole request. Its absence is worth a test, and has one.

What :class:`DocumentResponse` deliberately omits
------------------------------------------------
Three fields are stored and none of them appears here, and each omission has a
reason rather than a preference:

``storage_key``
    The name of the object on disk. Returning it hands a caller a filesystem path
    fragment, and PMP §8 Security requires that server-side locations are not
    exposed. A client has no use for it either — every document operation takes
    the document's id.

``uploaded_by``
    Attribution, for the audit trail and for the ingestion worker. It is a raw
    user UUID, and the roles allowed to *read* documents are all three, while the
    role allowed to resolve a UUID into a name is ``admin`` alone, via
    ``GET /users``. So a viewer would receive an identifier they cannot use and an
    editor an identifier they cannot resolve. Phase 4 can add it to a response the
    moment there is a client that needs it.

``tenant_id``
    The caller's own context, already fixed by their token. Echoing it adds
    nothing they do not already have, which is the same reasoning that keeps it
    out of :class:`~app.schemas.users.UserResponse`.

:class:`SafeFilename`, and why it refuses rather than repairs
------------------------------------------------------------
A filename is attacker-controlled text, and the cheapest place to judge it is
where that text enters the system. :data:`SafeFilename` therefore reduces a
filename to something that could legitimately appear in a directory listing *and*
be rendered in a user interface, and **refuses** anything trying to be something
else.

The refusals are:

* a control or bidirectional-override character — no legitimate document name
  contains one, and every consumer of this value (an HTTP header, a terminal, a
  database, a log line) treats them as an attack. The rejected set is the Unicode
  ``Cc`` and ``Cf`` categories plus ``DEL``, spelled out in
  :data:`FILENAME_FORBIDDEN_CODEPOINTS`;
* a ``/`` or ``\\`` — the client is claiming the name contains a path. No browser
  file picker produces that, so it is either a broken client or an attempt, and
  answering ``422`` is what lets an operator find out which. It is deliberately
  *not* silently stripped down to a basename, because quietly accepting
  ``../../etc/passwd`` as ``passwd`` means the traversal attempt succeeded and
  was hidden;
* a leading dot — ``.env`` is not a document, and ``..`` is a traversal token
  rather than a name.

Note what is *not* refused: ``report..v2.pdf`` is accepted. A doubled dot in the
middle of a name is unusual and completely harmless, because this value is never
a path component at all — :mod:`app.storage.local` builds every path from a
generated token and a UUID. Refusing it would be a rule shaped by a threat this
value does not carry.

Surrounding whitespace is stripped rather than refused, because that is
normalisation and :mod:`app.schemas.fields` does the same. The length is bounded
by :data:`~app.models.document.DOCUMENT_FILENAME_MAX_LENGTH`, imported rather
than restated, so the widest name the boundary accepts is exactly the widest the
column stores; exceeding it is a ``422`` rather than a truncation, because two
names differing only past the cut would otherwise become one row.

What this schema is **not** responsible for
-------------------------------------------
Whether the file is a permitted type, how large it is, and whether its bytes
match its name. Those need the stream, so they are in
:mod:`app.services.uploads`. The split is load-bearing: a bad *name* is refused
here before a single byte is read, while a bad *file* is refused after it has
been written and then removed. See
:func:`app.services.documents.create_document` for why that ordering cannot be
improved without giving up the ability to inspect the content at all.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
)

from app.models.document import DOCUMENT_FILENAME_MAX_LENGTH, DocumentStatus
from app.models.user import UserRole

#: Code points that may never appear in a stored filename.
#:
#: Unicode ``Cc`` (control) and ``Cf`` (format, which includes the bidi
#: overrides) plus ``DEL``. Spelled out as an explicit set rather than computed
#: with :func:`unicodedata.category` for two reasons: the *check* has to be
#: visible where the rule is stated, and a category lookup per character is a
#: runtime cost bought for no additional safety on a value this application only
#: ever displays.
#:
#: Kept beside :data:`FILENAME_FORBIDDEN_DESCRIPTION` so the error message and the
#: rule cannot describe different sets.
FILENAME_FORBIDDEN_CODEPOINTS: frozenset[int] = frozenset(range(0x20)) | {
    # DEL.
    0x7F,
    # Cf: zero-width and bidirectional formatting, as inclusive ranges expanded.
    *(range(0x200B, 0x2010)),
    *(range(0x202A, 0x202F)),
    *(range(0x2060, 0x2065)),
    0xFEFF,
}

#: How :data:`FILENAME_FORBIDDEN_CODEPOINTS` is described in an error.
#:
#: A separate name because printing the code points back into a response or a log
#: reproduces the problem it is reporting.
FILENAME_FORBIDDEN_DESCRIPTION = "control, format, or bidirectional-override characters"


def _clean_filename(value: Any) -> Any:
    """Reduce an uploaded filename to a bare, displayable name.

    Non-string input is returned untouched so Pydantic reports its own "input
    should be a valid string" error rather than this function reporting something
    vaguer — the same contract as :func:`app.schemas.fields._strip`.

    Raises:
        ValueError: the name is unusable. Pydantic turns this into a ``422``
            carrying the message, so the client is told what to fix and the same
            text will appear in the log.
    """
    if not isinstance(value, str):
        return value

    candidate = value.strip()

    if "/" in candidate or "\\" in candidate:
        raise ValueError(
            "a filename must not contain a directory path; send the file's name only"
        )

    if candidate.startswith("."):
        raise ValueError("a filename must not begin with a dot")

    if any(ord(character) in FILENAME_FORBIDDEN_CODEPOINTS for character in candidate):
        raise ValueError(
            f"a filename must not contain {FILENAME_FORBIDDEN_DESCRIPTION}"
        )

    return candidate


#: A filename, stripped and validated. See the module docstring.
SafeFilename = Annotated[
    str,
    BeforeValidator(_clean_filename),
    StringConstraints(min_length=1, max_length=DOCUMENT_FILENAME_MAX_LENGTH),
]

#: The compiled form of :data:`SafeFilename`, for the one caller that has a bare
#: string rather than a Pydantic model to attach it to.
#:
#: Built once at import time because building a :class:`~pydantic.TypeAdapter`
#: compiles a validator, and an upload would otherwise pay that on every request.
#: This is what lets :data:`SafeFilename` stay a declarative *type* — usable as a
#: model field, where it composes with ``Annotated`` — while still being callable
#: on a value that arrives as an HTTP multipart attribute rather than as JSON.
_SAFE_FILENAME: TypeAdapter[str] = TypeAdapter(SafeFilename)


def sanitise_filename(value: str) -> str:
    """Validate an uploaded filename the way :data:`SafeFilename` would.

    Needed because the filename on a ``multipart/form-data`` upload does not arrive
    as a request field at all: it is the ``filename`` parameter of the part's
    ``Content-Disposition`` header, which Starlette hands back as
    :attr:`fastapi.UploadFile.filename`. So it cannot be a declared parameter of a
    handler, and the validator that would otherwise run as part of model parsing
    has to be invoked directly.

    Args:
        value: the raw filename from the upload. May be ``None`` in practice, which
            the caller substitutes with ``""`` so the refusal is a ``422`` about the
            name rather than an ``AttributeError``.

    Returns:
        The stripped, validated name — exactly what will be stored.

    Raises:
        pydantic.ValidationError: the name is empty, too long, a dotfile, carries a
            directory separator, or contains a forbidden character. The caller turns
            this into a ``422`` with a fixed detail and logs the precise message.
    """
    return _SAFE_FILENAME.validate_python(value)

#: A non-empty list of the roles permitted to view a document.
#:
#: Present on the upload form because PMP §14 gives every document an
#: ``allowed_roles`` column while PMP §15 defines no endpoint that sets it later —
#: so this is the only moment at which a client can express the restriction, and a
#: column nothing can populate would be a column that is always ``NULL``.
#:
#: ``min_length=1`` so that an explicitly empty list is a ``422`` rather than a
#: document nobody in the tenant can ever read. ``None`` is the unrestricted case
#: and is a *different* value, which :mod:`app.models.document` documents.
#:
#: Values are :class:`~app.models.user.UserRole` members, so an unrecognised role
#: is a ``422`` rather than a silently stored string. That is also why this field
#: cannot be used to escalate: it can only *remove* access from the roles listed,
#: and a role an editor holds but does not name simply cannot read the document
#: back.
DocumentAllowedRoles = Annotated[list[UserRole], Field(min_length=1)]


class DocumentResponse(BaseModel):
    """A document as seen by callers already inside the document's tenant.

    Built by the API layer from a :class:`~app.models.document.Document` rather
    than returned as the ORM object, for the reason
    :class:`~app.schemas.users.UserResponse` documents: handing FastAPI the model
    as a ``response_model`` works today and stops being safe the first time a
    column is added. That this is a closed allowlist — with a test asserting the
    exact field set — is what makes ``storage_key`` unreachable rather than
    merely absent today.

    ``from_attributes=True`` lets the API layer validate the ORM instance
    directly instead of copying fields one at a time, which is where a forgotten
    field would otherwise hide.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    status: DocumentStatus
    #: ``None`` means every authenticated user in the tenant may see it. Exposed
    #: because a client rendering "restricted to admins" needs to know, and because
    #: it is the caller's own restriction rather than anyone else's.
    allowed_roles: list[str] | None
    #: The media type derived from the file's bytes, never the one the client
    #: declared — see :mod:`app.services.uploads`.
    content_type: str
    size_bytes: int
    #: Always ``0`` until the ingestion pipeline exists. Present because PMP §4.1
    #: puts "track chunk counts" in scope; see :mod:`app.models.document`.
    chunk_count: int
    #: Always ``None`` until an ingestion failure has been recorded. Bounded by
    #: :data:`~app.models.document.DOCUMENT_ERROR_MAX_LENGTH` in the column, and
    #: present for the same reason ``chunk_count`` is.
    error: str | None
    created_at: datetime
    updated_at: datetime


class DocumentListResponse(BaseModel):
    """Every document of the caller's tenant that the caller may view.

    The wrapped ``items`` shape mirrors
    :class:`~app.schemas.users.UserListResponse` and follows the same reasoning:
    PMP §15 defines no envelope for ``GET /documents``, so this is the one
    decision made once rather than ad hoc. Adding a field to a list response is
    backwards-compatible; changing its top level from an object to a bare array is
    not, and choosing the object shape now makes that change unnecessary.

    Pagination is **not** implemented because the PMP defines none. Inventing
    ``?page=``/``?limit=`` would add both an unbounded-query failure mode and a
    contract nobody specified. The honest position — a tenant's document list is
    currently unbounded — is recorded as a deferred item rather than resolved by
    guesswork.
    """

    items: list[DocumentResponse]


class DocumentStatusResponse(BaseModel):
    """The ingestion state of one document, for an administrator.

    Serves PMP §15's ``GET /admin/documents/{id}/status`` and §6.1's "viewing
    document processing status". Every field here also appears on
    :class:`DocumentResponse`, and that is worth stating plainly rather than
    hiding: this endpoint is a focused view of the same row, not a different
    capability. It exists because PMP §15 names it and assigns it to the
    administrator, and it is kept as a separate route rather than folded into
    ``GET /documents/{id}`` so that the administrator-only surface stays
    enumerable from the API document alone.

    ``chunk_count`` and ``error`` are here because they *are* the processing
    status. ``updated_at`` is here because it is the only signal available for
    "how long has this been stuck in ``processing``" — the failure PMP §8 asks
    about — while no dedicated transition timestamp exists.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DocumentStatus
    chunk_count: int
    error: str | None
    updated_at: datetime