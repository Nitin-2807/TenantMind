"""Upload validation: what a stored document is allowed to be.

Where each check happens, and why not all of them are here
-----------------------------------------------------------
Four of the checks operate on the filename alone and happen at the request
boundary, in :mod:`app.schemas.documents`, because that is where a filename first
exists as a Python string and where a Pydantic validator can turn a bad one into
a ``422`` without the request ever reaching a service. The two checks that need
bytes — the size limit and the content signature — live here.

The split is not arbitrary:

* a filename is attacker-controlled *text*, and the cheapest correct place to
  reject it is where that text enters the system;
* a size and a signature are properties of a *stream*, and the stream only exists
  once Starlette's multipart parser has written it to a spooled temporary file.

The extension check is here even though it too is derivable from the filename,
because grouping it with the byte checks keeps the whole "what *is* this file?"
decision — extension, size, signature, container — in one module, while the
filename module holds only the one decision it can actually make.

The size limit, and an honest statement of what it does not do
--------------------------------------------------------------
:attr:`~app.core.config.Settings.max_upload_size_mb` is enforced while the stream
is copied to storage, in
:meth:`app.storage.local.LocalFileStore.copy_stream_into`. At most that many bytes
are ever written into a tenant's directory.

It is *not* enforced before the request body is read. Starlette's multipart parser
writes the body to a spooled temporary file before the handler is invoked, so the
server has already buffered what the client sent by the time any application code
runs. Refusing earlier would mean reading ``Content-Length`` in middleware, and
this project has added no middleware by deliberate decision — PMP §17 lists rate
limiting and request IDs as middleware concerns, and a Phase 1 test asserts none
exist. It would also mean a route reading ``request.headers``, which
``tests/test_rbac.py`` forbids across ``app/api`` precisely so that a header can
never become a source of tenant or role identity.

So the property that is guaranteed and tested is the one that matters for the
filesystem: **no upload writes more than the limit into tenant storage.** What a
caller can still do is make the server buffer a body they declared. That gap is
recorded as a deferred item rather than described as solved.

Content signatures: what is checked and what is not
---------------------------------------------------
PMP §17 requires "MIME/content validation" but never lists a set of permitted
types, so this module defines the smallest set that makes the PMP's own domain
(§2: employee handbooks, HR policies, technical documentation, standard operating
procedures, product documentation, internal guides, compliance documents, company
FAQs) actually usable, and records the boundary of that decision rather than
presenting it as a policy the PMP asked for.

Each supported extension has either a required byte signature or an explicit
statement that the format has none:

* :data:`SUPPORTED_FORMATS` carries a ``signature`` for ``pdf`` (``%PDF-``) and
  for the three ZIP-based Office formats (``PK\\x03\\x04``);
* ``txt``, ``md`` and ``html`` have no signature — the byte sequences that make a
  file one of those are not distinguishable from arbitrary bytes — so for them the
  check that is possible is the container check below, and nothing more;
* the ZIP-based formats get a second pass, :func:`verify_office_container`, which
  opens the archive and requires both ``[Content_Types].xml`` and the part
  directory that format is built from (``word/``, ``xl/``, ``ppt/``). A ``PK``
  prefix alone would accept any archive under any of the three names.

A declared ``Content-Type`` is never trusted and never stored. The value in
:attr:`~app.models.document.Document.content_type` is the one *this* module
derived from the bytes, so a client cannot make the system record a media type
that was never verified. What is **not** claimed: a matching signature proves a
file has the right *shape*, not that it is safe to interpret. No uploaded document
is executed, is never returned under a content type a browser would render, and
never reaches a shell command; the parser that will actually interpret these bytes
is Phase 4's work and is not built here.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.storage.local import FileTooLargeError

#: How many bytes :func:`detect_content_type` inspects.
#:
#: 8 is enough for the longest signature in :data:`SUPPORTED_FORMATS` plus a byte
#: of slack. Deliberately tiny: this is a peek at the head of a file, not a read of
#: it, so a 10 MB document costs eight bytes to type-check.
SIGNATURE_PEEK_BYTES: Final[int] = 8

#: Upper bound on the number of members an Office container may declare.
#:
#: A decompression bomb does not need a large *file* to declare a large *central
#: directory*: an archive can list hundreds of thousands of members in a few
#: hundred kilobytes and then expand to terabytes. Refusing on the declared member
#: count, before any member is read, is the cheapest available defence and is
#: entirely sufficient here — nothing in this project extracts anything.
MAX_ARCHIVE_MEMBERS: Final[int] = 4096

#: The member every Office Open XML document contains, whatever its type.
OFFICE_CONTENT_TYPES_MEMBER: Final[str] = "[Content_Types].xml"


class UploadRejectedError(ValueError):
    """The upload cannot be accepted.

    Base class for the client-fault refusals below, so the handler maps one
    exception type to one response and never has to enumerate them. Carries a
    :attr:`reason`, a short stable token, so the handler can log precisely what
    was refused while the client is told only that the file was rejected.

    The message is **not** returned to the client. It names the rule that failed
    and, for the format checks, which extensions are supported — useful in a log,
    and one step short of a description of the validation rules handed to whoever
    is probing them.
    """

    def __init__(self, *, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class UnsupportedFileTypeError(UploadRejectedError):
    """The extension is missing, or is not one this project accepts.

    :attr:`extension` is kept for the log. It is the sanitised, lower-cased
    extension and therefore safe to record — the client's raw filename is never
    logged, because a filename is unbounded attacker-controlled text.
    """

    def __init__(self, *, extension: str) -> None:
        super().__init__(
            reason="unsupported_file_type",
            message=(
                f"{extension!r} is not a supported document type; supported "
                f"extensions are {', '.join(sorted(SUPPORTED_FORMATS))}"
            ),
        )
        self.extension = extension


class ContentMismatchError(UploadRejectedError):
    """The bytes do not carry the signature the extension promised.

    The check that catches the classic upload attack: a file named ``invoice.pdf``
    that is not a PDF, renamed so that a later handler treats it as one.
    """

    def __init__(self, *, extension: str) -> None:
        super().__init__(
            reason="content_mismatch",
            message=(
                f"the uploaded bytes do not look like a {extension!r} file, so "
                "the extension and the content disagree"
            ),
        )
        self.extension = extension


class MalformedContainerError(UploadRejectedError):
    """The archive is not readable, or is not the Office format it claims."""

    def __init__(self, *, extension: str, detail: str) -> None:
        super().__init__(
            reason="malformed_container",
            message=f"the uploaded {extension!r} archive is not usable: {detail}",
        )


class EmptyUploadError(UploadRejectedError):
    """The upload contained no bytes.

    Separate from the size limit because the two directions are different rules
    with different causes — an empty file input submitted with a filename, versus
    a file that was genuinely too large — and because a zero-byte document would
    otherwise be accepted into the database, violating the ``size_bytes > 0``
    constraint on :class:`~app.models.document.Document`.
    """

    def __init__(self) -> None:
        super().__init__(
            reason="empty_upload",
            message="the uploaded file contained no bytes",
        )


class InvalidContainerError(UploadRejectedError):
    """A stored object could not be read back, so nothing can be asserted about it.

    Not a property of the upload: it means the file the application just wrote is
    not readable. That is an operational fault and must not be reported as though
    the client had done something wrong — so it is a distinct type rather than a
    :class:`MalformedContainerError`, and the handler logs it differently.
    """

    def __init__(self, *, detail: str) -> None:
        super().__init__(
            reason="unreadable_container",
            message=f"the stored file could not be read back: {detail}",
        )


@dataclass(frozen=True, slots=True)
class SupportedFormat:
    """One accepted file type.

    ``signature`` is ``None`` for formats that have none, which is a statement
    rather than an omission: :func:`detect_content_type` treats ``None`` as "accept
    any leading bytes", and the field exists so that "this format was not
    signature-checked" is visible in the table instead of implied by its absence.
    """

    extension: str
    content_type: str
    signature: bytes | None
    #: The archive member prefix a ZIP-based format must contain, or ``None`` for
    #: formats that are not archives. Together with
    #: :data:`MAX_ARCHIVE_MEMBERS` this is what separates ``.docx`` from an
    #: arbitrary archive merely named ``.docx``.
    archive_part: str | None = None


#: The complete set of accepted document types.
#:
#: Ordered by descending strictness — the formats whose bytes can be *proved* to
#: match come first — so the table reads as a strength ordering rather than an
#: arbitrary list. Every entry is either required by the PMP's stated document
#: domain (§2) or is the plain-text form of it. Nothing here is speculative: a
#: format is present because a document of that kind is what §2 says the product
#: ingests, and absent because nothing in the PMP implies it should be.
SUPPORTED_FORMATS: Final[dict[str, SupportedFormat]] = {
    spec.extension: spec
    for spec in (
        SupportedFormat(
            extension="pdf",
            content_type="application/pdf",
            signature=b"%PDF-",
        ),
        SupportedFormat(
            extension="docx",
            content_type=(
                "application/vnd.openxmlformats-officedocument"
                ".wordprocessingml.document"
            ),
            signature=b"PK\x03\x04",
            archive_part="word/",
        ),
        SupportedFormat(
            extension="xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            ),
            signature=b"PK\x03\x04",
            archive_part="xl/",
        ),
        SupportedFormat(
            extension="pptx",
            content_type=(
                "application/vnd.openxmlformats-officedocument"
                ".presentationml.presentation"
            ),
            signature=b"PK\x03\x04",
            archive_part="ppt/",
        ),
        SupportedFormat(
            extension="txt",
            content_type="text/plain",
            signature=None,
        ),
        SupportedFormat(
            extension="md",
            content_type="text/markdown",
            signature=None,
        ),
        SupportedFormat(
            extension="html",
            content_type="text/html",
            signature=None,
        ),
    )
}


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """What the application now knows about a stored upload.

    Produced only by :func:`validate_stored_upload`, so a caller holding one of
    these can rely on every field having been measured rather than declared.
    """

    filename: str
    content_type: str
    size_bytes: int
    extension: str


def extension_of(filename: str) -> str:
    """Return the lower-case extension of an already-sanitised filename.

    Args:
        filename: a bare filename — no directory component, which
            :class:`app.schemas.documents.SafeFilename` has already guaranteed.

    Returns:
        The extension without its dot, lower-cased. ``""`` when there is none.

    ``rpartition`` rather than :meth:`str.split` because a name like
    ``"report.tar.gz"`` must yield ``gz`` — the part after the last dot — and
    ``"archive."`` must yield ``""`` rather than raising, so it is refused as an
    unsupported type instead of producing a confusing error from somewhere further
    in. A leading dot is not an extension: ``".gitignore"`` has none, because a
    dotfile's single dot is part of its name, not a separator.
    """
    stem, dot, suffix = filename.rpartition(".")
    if not dot or not stem:
        return ""
    return suffix.lower()


def resolve_format(filename: str) -> SupportedFormat:
    """Return the :class:`SupportedFormat` a filename claims to be.

    Raises:
        UnsupportedFileTypeError: the extension is missing or not accepted.
    """
    extension = extension_of(filename)
    supported = SUPPORTED_FORMATS.get(extension)
    if supported is None:
        raise UnsupportedFileTypeError(extension=extension or "(none)")
    return supported


def detect_content_type(filename: str, leading_bytes: bytes) -> SupportedFormat:
    """Confirm the leading bytes match the format the extension claims.

    Args:
        filename: an already-sanitised filename.
        leading_bytes: the first :data:`SIGNATURE_PEEK_BYTES` of the stored file.

    Returns:
        The matching :class:`SupportedFormat`, whose ``content_type`` is the value
        to persist — the one derived here, never the one the client declared.

    Raises:
        UnsupportedFileTypeError: the extension is not accepted.
        ContentMismatchError: the bytes do not carry the required signature.

    The archive is *not* opened here. That needs the whole file on disk and
    therefore happens in :func:`verify_office_container`, after the write. Keeping
    it out leaves this function pure — a function of a filename and eight bytes —
    so it is testable without a filesystem at all.
    """
    supported = resolve_format(filename)

    if supported.signature is None:
        # This format has no signature. Accepted, and :data:`SUPPORTED_FORMATS`
        # records that fact rather than leaving it to be inferred from its absence.
        return supported

    if not leading_bytes.startswith(supported.signature):
        raise ContentMismatchError(extension=supported.extension)

    return supported


def verify_office_container(path: Path, *, extension: str) -> None:
    """Check that a ZIP-based upload really is the Office format it claims.

    Args:
        path: the stored file.
        extension: the accepted extension, already confirmed to be a ZIP-based
            format by :func:`detect_content_type`.

    Raises:
        UnsupportedFileTypeError: the extension is not an archive format, which
            would mean this function was reached in error.
        MalformedContainerError: the bytes are not a readable archive, declare too
            many members, or lack the part directory identifying the format.
        InvalidContainerError: the file could not be *read* — an ``OSError``
            rather than a ``BadZipFile``, and therefore a fault on this side of
            the boundary rather than the caller's.

    A ``PK\\x03\\x04`` prefix distinguishes "some zip" from "not a zip" and
    nothing more. This is what distinguishes ``.docx`` from an arbitrary archive
    renamed to one, and the member cap is checked against the central directory,
    so a small file declaring a hundred thousand entries is refused before a single
    member is read.
    """
    supported = SUPPORTED_FORMATS.get(extension)
    if supported is None or supported.archive_part is None:
        raise UnsupportedFileTypeError(extension=extension or "(none)")

    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile as exc:
        raise MalformedContainerError(
            extension=extension, detail=f"the archive could not be read ({exc})"
        ) from exc
    except OSError as exc:
        # A read failure, not a malformed file: the bytes are the caller's problem,
        # an unreadable file is ours.
        raise InvalidContainerError(detail=str(exc)) from exc

    if len(names) > MAX_ARCHIVE_MEMBERS:
        raise MalformedContainerError(
            extension=extension,
            detail=(
                f"the archive declares {len(names)} members, above the "
                f"{MAX_ARCHIVE_MEMBERS} permitted"
            ),
        )

    present = set(names)
    if OFFICE_CONTENT_TYPES_MEMBER not in present:
        raise MalformedContainerError(
            extension=extension,
            detail=(
                "the archive is not an Office document: it has no "
                f"{OFFICE_CONTENT_TYPES_MEMBER}"
            ),
        )
    if not any(name.startswith(supported.archive_part) for name in names):
        raise MalformedContainerError(
            extension=extension,
            detail=(
                f"the archive declares no {supported.archive_part!r} part, so it "
                f"is not a {extension} document"
            ),
        )


def validate_stored_upload(
    path: Path,
    *,
    filename: str,
    size_bytes: int,
    leading_bytes: bytes,
    max_bytes: int,
) -> ValidatedUpload:
    """Validate an upload that has already been written to disk.

    The single entry point the service uses, so the order of the checks — size,
    then extension, then signature, then container — is stated once instead of
    being re-derived at each call site, and so no caller can reach the container
    check against a file that was never type-checked.

    The order is not arbitrary. Size comes first because it is the only check that
    can decide without reading anything, and because the upper bound has already
    been enforced by the copy. Extension comes next because every later check
    needs it. Signature is cheap and independent of file size. The container check
    is last because it is the only one that opens the file.

    Args:
        path: where the upload was written.
        filename: the sanitised filename the metadata will carry.
        size_bytes: how many bytes were written, as counted by the copy.
        leading_bytes: the first :data:`SIGNATURE_PEEK_BYTES` of the stored file.
        max_bytes: the configured limit, used to make the error message accurate.

    Returns:
        A :class:`ValidatedUpload` whose every field was measured.

    Raises:
        EmptyUploadError, UnsupportedFileTypeError, ContentMismatchError,
            MalformedContainerError: the upload is not acceptable.
        FileTooLargeError: the size is above the limit. Unreachable through
            :func:`app.services.documents.create_document`, which enforces it
            during the copy, and re-checked here so the function is safe to call
            on its own. A check only one caller makes is not a check.
        InvalidContainerError: the stored file could not be read back.
    """
    if size_bytes <= 0:
        raise EmptyUploadError()
    if size_bytes > max_bytes:
        raise FileTooLargeError(max_bytes=max_bytes)

    supported = detect_content_type(filename, leading_bytes)

    if supported.archive_part is not None:
        verify_office_container(path, extension=supported.extension)

    return ValidatedUpload(
        filename=filename,
        content_type=supported.content_type,
        size_bytes=size_bytes,
        extension=supported.extension,
    )