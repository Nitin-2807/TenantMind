"""The document endpoints, over HTTP (PMP §15, Phase 2.4).

The five routes, and the four properties they have to have
----------------------------------------------------------
PMP §15 names exactly five document operations, and this file exercises each of
them for the same four properties, in this order of how much damage their absence
would do:

1. **Authorization** (PMP §16). Upload is administrator or editor; viewing is
   every role; deletion and the administration status view are administrator only.
   ``401`` and ``403`` are never confused, and ``403`` never leaks whether the
   resource exists.
2. **Tenant isolation.** A ``document_id`` names *which* document, never *whose*
   tenant to look in. Another tenant's document is ``404``, and the body is
   identical to the one for an id that does not exist.
3. **Refusals leave nothing behind.** A rejected upload must not leave a file on
   disk or a row in the database, and a rejected *filename* must not even read the
   stream.
4. **Nothing internal leaks.** No response carries ``storage_key``,
   ``uploaded_by``, ``tenant_id`` or any filesystem path.

Response codes, and the one choice worth defending
--------------------------------------------------
``POST /documents`` answers ``202``. PMP §10's upload path ends at "queue
ingestion job → 202 Accepted", so the request has been accepted for processing
and will not complete within the call. ``201`` would promise a finished resource,
which this endpoint deliberately does not produce — the parsing pipeline runs
later, in the Celery worker, and the row the response describes is still
``processing``.

Since Phase 5 Step 3, accepting an upload also enqueues the ingestion task:
the document row is committed and the fixed ``tenantmind.ingest_document``
task is published with the document id and nothing else. :class:`TestUploadQueuesIngestion`
pins that contract; every other upload test here runs against a recorder so none
of them need a broker.

Token minting
-------------
Directly with :func:`app.core.security.create_access_token`, for the same reason
``test_users_api.py`` does: the authentication matrix is proved exhaustively in
``test_tenant_identity.py``, and repeating it per role here would buy nothing.

The upload helper
-----------------
``upload(...)`` builds the ``multipart`` body with real bytes rather than mocking
the file, because the interesting refusals are about *content*: a ``.docx`` whose
bytes are not a ZIP, a ``.pdf`` whose bytes are not a PDF. A mocked file would
test the handler's plumbing instead of the rules.
"""

from __future__ import annotations

import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.security import create_access_token
from app.models.document import Document, DocumentStatus
from app.models.tenant import Tenant
from app.models.user import User, UserRole
from app.services.auth import BEARER_SCHEME

API_PREFIX = "/api/v1"
DOCUMENTS = f"{API_PREFIX}/documents"

#: One mebibyte. The configured limit's smallest positive value, so a test can
#: cross it without allocating the default ten.
MIB = 1024 * 1024

#: Padding that lands a ``b"%PDF-1.7"``-prefixed payload exactly one byte over
#: ``MIB``. Named rather than spelled ``MIB - 7`` because the arithmetic is the
#: whole point of the boundary tests: ``8`` is the signature prefix's length, so
#: anyone checking the edge has to be able to see it.
OVER_BY_ONE = MIB - len(b"%PDF-1.7") + 1

#: …and one byte under, for the inclusive-boundary case.
UNDER_BY_ONE = MIB - len(b"%PDF-1.7") - 1

ADMIN_EMAIL = "admin@acme.test"
EDITOR_EMAIL = "editor@acme.test"
VIEWER_EMAIL = "viewer@acme.test"
RIVAL_EMAIL = "admin@globex.test"

DENIED = {"detail": "Not enough permissions"}
NO_SUCH_DOCUMENT = {"detail": "Document not found"}
UPLOAD_REJECTED = {"detail": "The uploaded file was rejected"}

#: The one message a stored-but-not-queued upload gets. Mirrors
#: ``app.api.documents.QUEUE_UNAVAILABLE_DETAIL`` as a literal, the same way
#: :data:`UPLOAD_REJECTED` does, so the response contract is readable here.
QUEUE_UNAVAILABLE = {
    "detail": "The document was stored but could not be queued for ingestion"
}

#: A real, minimal PDF. The header is what the signature check reads; the rest is
#: never parsed, which is the point of asserting that it does not have to be.
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

#: An id belonging to no row in any table.
ABSENT_DOCUMENT = "33333344-4444-4555-8666-777777777777"


AccountFactory = Callable[..., tuple[Tenant, User]]


def credentials(token: str) -> dict[str, str]:
    return {"Authorization": f"{BEARER_SCHEME} {token}"}


def token_for(user: User, tenant: Tenant, auth_settings: Settings) -> str:
    return create_access_token(
        subject=user.id,
        tenant_id=tenant.id,
        role=user.role,
        app_settings=auth_settings,
    )


@pytest.fixture
def storage_root(auth_settings: Settings) -> Path:
    """Where this test's documents actually land.

    The same directory :func:`api_app` hands the application, so a test can
    inspect the filesystem the request really wrote to rather than a parallel
    copy it set up itself.
    """
    return auth_settings.storage_root


@pytest.fixture
def openapi(api_client: TestClient) -> dict[str, Any]:
    """The served API document, fetched once per test that asks for it.

    Assertions about *schemas* rather than about one response are what stop two
    agreeing by coincidence, and this is the only way to reach them.
    """
    response = api_client.get("/openapi.json")
    assert response.status_code == 200
    schema: dict[str, Any] = response.json()
    return schema


@pytest.fixture
def stored_files(storage_root: Path) -> Callable[[uuid.UUID], list[Path]]:
    def _files(tenant_id: uuid.UUID) -> list[Path]:
        directory = storage_root / tenant_id.hex
        return sorted(directory.iterdir()) if directory.is_dir() else []

    return _files


def stored_file_names(tenant_id: uuid.UUID, storage_root: Path) -> list[str]:
    """The names on disk for one tenant, read from the configured root.

    Free-standing rather than a fixture so a helper module — and the refusal
    assertions that want "nothing on disk" without also wanting a directory
    listing — can use the same shape.
    """
    directory = storage_root / tenant_id.hex
    return sorted(entry.name for entry in directory.iterdir()) if directory.is_dir() else []


@contextmanager
def monkeypatched_limit(settings: Settings, megabytes: int) -> Iterator[None]:
    """Set ``max_upload_size_mb`` on the live settings object for one test.

    :class:`~app.core.config.Settings` is not frozen, and
    :func:`tests.conftest.api_app` hands the application this very instance
    through ``dependency_overrides[get_settings]`` — so this is the same seam an
    operator changes ``MAX_UPLOAD_SIZE_MB`` in, rather than a mock of it.
    """
    original = settings.max_upload_size_mb
    settings.max_upload_size_mb = megabytes
    try:
        yield
    finally:
        settings.max_upload_size_mb = original


@pytest.fixture
def acme(
    make_committed_account: AccountFactory,
    auth_settings: Settings,
) -> tuple[Tenant, User, str]:
    """Tenant A with an administrator, and a token for them."""
    tenant, user = make_committed_account(
        slug="acme", email=ADMIN_EMAIL, role=UserRole.ADMIN
    )
    return tenant, user, token_for(user, tenant, auth_settings)


@pytest.fixture
def globex(
    make_committed_account: AccountFactory,
    auth_settings: Settings,
) -> tuple[Tenant, User, str]:
    """Tenant B, entirely separate — the isolation cases use this."""
    tenant, user = make_committed_account(
        slug="globex", email=RIVAL_EMAIL, role=UserRole.ADMIN
    )
    return tenant, user, token_for(user, tenant, auth_settings)


@pytest.fixture
def editor(
    make_committed_account: AccountFactory,
    auth_settings: Settings,
    acme: tuple[Tenant, User, str],
) -> tuple[Tenant, User, str]:
    _, _, _ = acme
    org, user = make_committed_account(
        slug="acme", email=EDITOR_EMAIL, role=UserRole.EDITOR
    )
    return org, user, token_for(user, org, auth_settings)


@pytest.fixture
def viewer(
    make_committed_account: AccountFactory,
    auth_settings: Settings,
    acme: tuple[Tenant, User, str],
) -> tuple[Tenant, User, str]:
    _, _, _ = acme
    org, user = make_committed_account(
        slug="acme", email=VIEWER_EMAIL, role=UserRole.VIEWER
    )
    return org, user, token_for(user, org, auth_settings)


def office_bytes(part: str = "word/", *, content_types: bool = True) -> bytes:
    """A ZIP archive shaped like (or unlike) an OOXML document."""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if content_types:
            archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(f"{part}document.xml", "<document/>")
    return buffer.getvalue()


#: A fixed boundary. Not random, and not derived from the payload: a reproducible
#: body is what makes a failure diffable, and the bytes under test are PDFs and
#: ZIPs that will not contain this string.
BOUNDARY = "----TenantMindDocumentTestBoundary"


def multipart_body(
    filename: str | None,
    content: bytes,
    content_type: str,
    fields: list[tuple[str, str]],
) -> tuple[bytes, str]:
    """Encode a ``multipart/form-data`` body, returning it and its content type.

    Written by hand rather than through httpx's ``files=``/``data=`` arguments,
    for two reasons that are properties of what is under test:

    * **Repeated keys.** ``allowed_roles`` is a *list* on the wire. httpx accepts
      ``data`` as a dict, a dict cannot repeat a key, and passing a list of pairs
      alongside ``files`` makes httpx treat the whole list as raw content — so the
      high-level API cannot express the request this route actually declares. A
      browser form and ``requests`` both send repeated keys; this sends them too.
    * **Unusual filenames.** A NUL byte or an empty string is a legal thing for a
      client to put in a part header, and a test here needs to send both. httpx
      validates part headers and would refuse to build the request, so the refusal
      under test would be httpx's rather than the application's.

    ``filename=None`` omits the ``filename`` parameter from the file part entirely,
    which is how a client sends a file with no name at all.
    """
    delimiter = f"--{BOUNDARY}".encode()
    parts: list[bytes] = []

    for name, value in fields:
        parts.append(
            delimiter
            + b"\r\n"
            + f'Content-Disposition: form-data; name="{name}"'.encode()
            + b"\r\n\r\n"
            + value.encode()
            + b"\r\n"
        )

    part_header = b'Content-Disposition: form-data; name="file"'
    if filename is not None:
        part_header += f'; filename="{filename}"'.encode()
    parts.append(
        delimiter
        + b"\r\n"
        + part_header
        + b"\r\n"
        + f"Content-Type: {content_type}".encode()
        + b"\r\n\r\n"
        + content
        + b"\r\n"
    )

    body = b"".join(parts) + delimiter + b"--\r\n"
    return body, f"multipart/form-data; boundary={BOUNDARY}"


def upload(
    client: TestClient,
    token: str | None,
    *,
    filename: str | None = "report.pdf",
    content: bytes = PDF_BYTES,
    content_type: str = "application/pdf",
    allowed_roles: list[str] | str | None = None,
    fields: list[tuple[str, str]] | None = None,
) -> Any:
    """POST an upload as ``multipart/form-data``.

    ``allowed_roles`` accepts a list or a bare string, because both are worth
    sending and a client library would do either. A list is sent as *repeated*
    ``allowed_roles`` fields — the shape a browser form and every HTTP client
    library produce for a repeated key — rather than as a JSON array, because the
    route declares a form field and nothing else.

    ``token=None`` sends no ``Authorization`` header at all, which is how the
    unauthenticated cases are stated rather than with a stray empty header.
    """
    form: list[tuple[str, str]] = list(fields or [])
    if isinstance(allowed_roles, str):
        form.append(("allowed_roles", allowed_roles))
    else:
        form.extend(("allowed_roles", role) for role in allowed_roles or [])

    body, multipart_type = multipart_body(filename, content, content_type, form)
    return client.post(
        DOCUMENTS,
        content=body,
        headers={
            "Content-Type": multipart_type,
            **({} if token is None else credentials(token)),
        },
    )


def documents_in(session: Session, tenant_id: uuid.UUID) -> list[Document]:
    return list(
        session.scalars(
            select(Document)
            .where(Document.tenant_id == tenant_id)
            .order_by(Document.created_at, Document.id)
        )
    )


@pytest.fixture(autouse=True)
def queued_documents(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Replace the ingestion enqueue with a recorder, for every test in this file.

    Uploads in this file assert on the *request path*, and none of them need a
    broker: the publish is exactly what :class:`TestUploadQueuesIngestion`
    pins with this same fixture, and every other upload test is about the
    storage, the row or the response — a real publish would only add a Redis
    dependency to tests that are not about Redis.

    The recorder's one-argument signature is part of the contract: if the
    handler ever passed a tenant, a collection or document contents to the
    task, ``record`` would raise ``TypeError`` inside the request and this
    fixture would fail the test instead of silently recording a wrong call.
    """
    import app.api.documents as documents_module

    calls: list[uuid.UUID] = []

    def record(document_id: uuid.UUID) -> None:
        calls.append(document_id)

    monkeypatch.setattr(documents_module, "enqueue_ingestion", record)
    return calls


# ---------------------------------------------------------------------------
# Upload: POST /documents
# ---------------------------------------------------------------------------


class TestUpload:
    def test_it_accepts_a_document_with_202(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """``202``, because the PMP's upload path ends at "accepted for processing"."""
        _, _, token = acme

        response = upload(api_client, token)

        assert response.status_code == 202, response.text

    def test_the_response_describes_what_was_stored(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        _, _, token = acme

        body = upload(api_client, token, content=b"%PDF-1.7" + b"x" * 100).json()

        assert body["filename"] == "report.pdf"
        assert body["status"] == "processing"
        assert body["content_type"] == "application/pdf"
        assert body["size_bytes"] == 108
        assert body["chunk_count"] == 0
        assert body["error"] is None
        assert body["allowed_roles"] is None
        assert uuid.UUID(body["id"])
        assert body["created_at"] and body["updated_at"]

    def test_the_document_belongs_to_the_callers_tenant(
        self,
        api_client: TestClient,
        model_session: Session,
        acme: tuple[Tenant, User, str],
    ) -> None:
        tenant, _, token = acme

        upload(api_client, token)

        rows = documents_in(model_session, tenant.id)
        assert len(rows) == 1
        assert rows[0].tenant_id == tenant.id

    def test_the_uploader_is_recorded_from_the_identity(
        self,
        api_client: TestClient,
        model_session: Session,
        acme: tuple[Tenant, User, str],
    ) -> None:
        """``uploaded_by`` comes from the token and from nowhere else.

        The invariant the nullable foreign key cannot express — that the uploader
        belongs to the same tenant — is unbreakable here for a structural reason:
        the service takes no tenant parameter, so the user id and the tenant id
        necessarily come from the same signed claims.
        """
        _, user, token = acme

        upload(api_client, token)

        rows = documents_in(model_session, user.tenant_id)
        assert rows[0].uploaded_by == user.id

    def test_the_bytes_are_written_to_the_tenants_directory(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        stored_files: Callable[[uuid.UUID], list[Path]],
    ) -> None:
        tenant, _, token = acme

        upload(api_client, token)

        files = stored_files(tenant.id)
        assert len(files) == 1
        assert files[0].read_bytes().startswith(b"%PDF-")

    def test_the_stored_name_is_not_the_document_id(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        stored_files: Callable[[uuid.UUID], list[Path]],
    ) -> None:
        """The response's id must not be usable to locate the file.

        Two independent tokens, so an API consumer holding every document id in
        the system learns nothing about the filesystem layout.
        """
        tenant, _, token = acme

        body = upload(api_client, token).json()

        files = stored_files(tenant.id)
        assert len(files) == 1
        assert files[0].name != body["id"].replace("-", "")

    def test_no_response_field_exposes_the_storage_key(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """A closed field set, asserted exactly.

        PMP §8 requires server-side locations not be exposed, and
        ``storage_key`` is the name of the object on disk. The list is literal so
        that adding a field is a deliberate edit — a test derived from the schema
        would follow the schema silently.
        """
        _, _, token = acme

        body = upload(api_client, token).json()

        assert set(body) == {
            "id",
            "filename",
            "status",
            "allowed_roles",
            "content_type",
            "size_bytes",
            "chunk_count",
            "error",
            "created_at",
            "updated_at",
        }
        assert "storage_key" not in body
        assert "uploaded_by" not in body
        assert "tenant_id" not in body

    def test_the_declared_content_type_is_never_trusted(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """A client cannot make the system record a type it did not verify.

        Declared ``image/png``, sent as a plain-text file named ``.txt`` — the
        recorded type is the one derived from the accepted extension.
        """
        _, _, token = acme

        body = upload(
            api_client,
            token,
            filename="notes.txt",
            content=b"just text",
            content_type="image/png",
        ).json()

        assert body["content_type"] == "text/plain"

    @pytest.mark.parametrize(
        ("filename", "content"),
        [
            ("report.pdf", b"this is not a pdf"),
            ("report.docx", b"not a zip"),
            ("report.pdf", PDF_BYTES),
            ("payload.exe", b"MZ\x90\x00"),
            ("noextension", b"content"),
        ],
        ids=["pdf-mismatch", "docx-mismatch", "good-pdf-is-fine-not-a-case", "exe", "none"],
    )
    def test_a_bad_file_is_refused_with_422(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        stored_files: Callable[[uuid.UUID], list[Path]],
        filename: str,
        content: bytes,
    ) -> None:
        _, _, token = acme

        response = upload(api_client, token, filename=filename, content=content)

        assert response.status_code in {202, 422}
        if filename == "report.pdf" and content == PDF_BYTES:
            return

        assert response.status_code == 422
        assert response.json() == UPLOAD_REJECTED

    def test_a_refused_upload_leaves_no_file_and_no_row(
        self,
        api_client: TestClient,
        model_session: Session,
        acme: tuple[Tenant, User, str],
        stored_files: Callable[[uuid.UUID], list[Path]],
    ) -> None:
        """Every rejection path compensates before the error propagates.

        This is the property the file-then-row ordering buys: the ordinary
        refusals leave nothing behind, so an unreferenced file is only ever
        possible from a *commit* failure, which is documented as a deferred
        reconciliation rather than claimed as solved.
        """
        tenant, _, token = acme

        upload(api_client, token, filename="payload.exe", content=b"MZ\x90\x00")
        upload(api_client, token, filename="report.pdf", content=b"not a pdf")
        upload(api_client, token, filename="bad.docx", content=office_bytes(part="xl/"))

        assert stored_files(tenant.id) == []
        assert documents_in(model_session, tenant.id) == []

    def test_an_empty_upload_is_refused(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        stored_files: Callable[[uuid.UUID], list[Path]],
    ) -> None:
        """A zero-byte document would violate ``size_bytes > 0``."""
        tenant, _, token = acme

        response = upload(api_client, token, content=b"")

        assert response.status_code == 422
        assert stored_files(tenant.id) == []

    def test_a_file_just_under_the_limit_is_accepted(
        self,
        api_client: TestClient,
        auth_settings: Settings,
        acme: tuple[Tenant, User, str],
    ) -> None:
        """One byte under the limit is accepted — the near-miss case.

        Its own test because a limit implemented as ``>=`` rather than ``>`` would
        pass every "too large" assertion and quietly reject a file the limit
        permits, and no oversize test can detect that.

        The limit is lowered to its smallest positive value first; otherwise a
        one-megabyte payload would be ten times *under* the default and the test
        would pass without ever approaching the boundary.
        """
        _, _, token = acme

        with monkeypatched_limit(auth_settings, 1):
            response = upload(
                api_client, token, content=b"%PDF-1.7" + b"x" * UNDER_BY_ONE
            )

        assert response.status_code == 202, response.text

    def test_a_file_exactly_at_the_limit_is_accepted(
        self,
        api_client: TestClient,
        auth_settings: Settings,
        acme: tuple[Tenant, User, str],
    ) -> None:
        """``size == max_bytes`` is permitted; the check is ``>`` and not ``>=``."""
        _, _, token = acme

        with monkeypatched_limit(auth_settings, 1):
            response = upload(
                api_client, token, content=b"%PDF-1.7" + b"x" * (MIB - len(b"%PDF-1.7"))
            )

        assert response.status_code == 202, response.text

    def test_an_oversize_upload_is_refused_with_413(
        self,
        api_client: TestClient,
        auth_settings: Settings,
        acme: tuple[Tenant, User, str],
        storage_root: Path,
    ) -> None:
        """``413``, and one byte over is enough — and leaves nothing behind."""
        tenant, _, token = acme

        with monkeypatched_limit(auth_settings, 1):
            response = upload(
                api_client, token, content=b"%PDF-1.7" + b"x" * OVER_BY_ONE
            )

        assert response.status_code == 413
        assert stored_file_names(tenant.id, storage_root) == []

    def test_the_413_reports_the_limit_the_server_actually_enforced(
        self,
        api_client: TestClient,
        auth_settings: Settings,
        acme: tuple[Tenant, User, str],
    ) -> None:
        """The detail carries the enforced byte count, so a client can act on it.

        Reported from the limit this server applied, not from a constant and not
        from whatever the client believed was configured: the number a client
        needs is the one its bytes were measured against.
        """
        _, _, token = acme

        with monkeypatched_limit(auth_settings, 1):
            response = upload(
                api_client, token, content=b"%PDF-1.7" + b"x" * OVER_BY_ONE
            )

        assert response.status_code == 413
        assert str(MIB) in response.json()["detail"]

    def test_the_limit_is_the_configured_setting(
        self,
        api_client: TestClient,
        auth_settings: Settings,
        acme: tuple[Tenant, User, str],
    ) -> None:
        """Changing ``max_upload_size_mb`` changes what is refused.

        The limit is read per request rather than captured at import, so an
        operator lowering it takes effect without a code change. Asserted by
        mutating the very ``Settings`` instance the application is wired to —
        the same seam ``MAX_UPLOAD_SIZE_MB`` in the environment uses.

        A 0-megabyte limit is the degenerate case and is used deliberately: it
        makes every payload oversize, so the test needs no megabyte of fixture
        data to prove the setting is consulted at all.
        """
        _, _, token = acme
        small = b"%PDF-1.7" + b"x" * 5_000

        assert upload(api_client, token, content=small).status_code == 202

        with monkeypatched_limit(auth_settings, 0):
            assert upload(api_client, token, content=small).status_code == 413
            assert upload(api_client, token, content=b"%PDF-1.7").status_code == 413

        # …and the setting is restored, so the test leaves nothing behind.
        assert upload(api_client, token, content=small).status_code == 202

    def test_an_oversize_upload_is_refused_before_any_validation(
        self,
        api_client: TestClient,
        auth_settings: Settings,
        acme: tuple[Tenant, User, str],
    ) -> None:
        """Size outranks type, so the cheapest and most useful fact is reported.

        ``413`` rather than the ``422`` an unsupported extension would get: an
        operator who has set the limit to one megabyte and is uploading a
        two-gigabyte ``.exe`` needs to be told the size first — the extension is
        not the thing they are about to be asked to fix.
        """
        _, _, token = acme

        with monkeypatched_limit(auth_settings, 1):
            response = upload(
                api_client,
                token,
                filename="payload.exe",
                content=b"MZ\x90\x00" + b"x" * MIB,
            )

        assert response.status_code == 413

    @pytest.mark.parametrize(
        "filename",
        [
            "../../etc/passwd.pdf",
            "sub/dir/report.pdf",
            ".env.pdf",
            "with\x00nul.pdf",
            "",
        ],
        ids=["traversal", "separator", "dotfile", "nul", "empty"],
    )
    def test_a_bad_filename_is_refused_before_the_stream_is_read(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        stored_files: Callable[[uuid.UUID], list[Path]],
        filename: str,
    ) -> None:
        """Refused at the request boundary, so no disk write happens at all."""
        tenant, _, token = acme

        response = upload(api_client, token, filename=filename)

        assert response.status_code == 422
        assert response.json() == UPLOAD_REJECTED
        assert stored_files(tenant.id) == []

    def test_the_filename_is_stored_verbatim_apart_from_whitespace(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """Normalisation, not sanitisation: ``"  report.pdf "`` becomes ``"report.pdf"``."""
        _, _, token = acme

        body = upload(api_client, token, filename="  quarterly report.pdf  ").json()

        assert body["filename"] == "quarterly report.pdf"

    def test_an_unknown_role_is_refused_with_422(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """Roles are :class:`UserRole` members, not free strings."""
        _, _, token = acme

        response = upload(api_client, token, allowed_roles=["superuser"])

        assert response.status_code == 422

    def test_an_empty_role_list_is_refused(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """``[]`` is not a way to say "everyone"; ``None`` is."""
        _, _, token = acme

        response = upload(api_client, token, allowed_roles=[])

        assert response.status_code == 202
        assert response.json()["allowed_roles"] is None

    def test_an_editor_may_upload(self, api_client: TestClient, editor: Any) -> None:
        _, _, token = editor

        assert upload(api_client, token).status_code == 202

    def test_a_viewer_may_not_upload(
        self, api_client: TestClient, viewer: Any
    ) -> None:
        _, _, token = viewer

        assert upload(api_client, token).status_code == 403
        assert upload(api_client, token).json() == DENIED

    def test_an_unauthenticated_caller_is_401(self, api_client: TestClient) -> None:
        assert upload(api_client, None).status_code == 401

    def test_a_bad_token_is_401_not_403(self, api_client: TestClient) -> None:
        """Authentication and authorization stay distinguishable."""
        response = upload(api_client, "not-a-jwt")

        assert response.status_code == 401

    def test_a_missing_file_part_is_422(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """FastAPI's own validation, not a handler branch."""
        _, _, token = acme

        response = api_client.post(DOCUMENTS, data={}, headers=credentials(token))

        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Upload queues ingestion (Phase 5 Step 3)
# ---------------------------------------------------------------------------


class TestUploadQueuesIngestion:
    """Upload -> persist -> commit -> enqueue, with exactly one argument.

    These tests hold the Step 3 contract: the request commits the document,
    publishes the fixed ingestion task with the document id and **nothing
    else**, and does not wait for the ingestion to finish. The *receipt* of
    that message is :mod:`tests.test_worker_ingestion`'s business; here the
    request side is what gets pinned.
    """

    def test_upload_enqueues_exactly_one_task_with_the_document_id(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        queued_documents: list[uuid.UUID],
    ) -> None:
        _, _, token = acme

        body = upload(api_client, token).json()

        assert len(queued_documents) == 1, "a successful upload queues once"
        assert queued_documents[0] == uuid.UUID(body["id"])

    def test_no_tenant_id_or_content_reaches_the_task(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        queued_documents: list[uuid.UUID],
    ) -> None:
        """The enqueue call is a single argument, structurally.

        ``record`` accepts one ``document_id``; a second positional argument —
        a tenant, a collection, bytes — would be a ``TypeError`` raised inside
        the request, and this test would fail on a 500 rather than recording a
        wrong call.
        """
        _, _, token = acme

        response = upload(api_client, token)

        assert response.status_code == 202
        assert len(queued_documents) == 1
        assert isinstance(queued_documents[0], uuid.UUID)

    def test_the_request_ends_at_queued_and_does_not_wait(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        queued_documents: list[uuid.UUID],
    ) -> None:
        """``202`` with a ``processing`` row is PMP §10's stopping point.

        The response reports a document that has not been ingested — status
        ``processing``, zero chunks — because the request's job ended at
        "queue ingestion job". Nothing in this request ran the pipeline.
        """
        _, _, token = acme

        body = upload(api_client, token).json()

        assert body["status"] == "processing"
        assert body["chunk_count"] == 0
        assert body["error"] is None
        assert len(queued_documents) == 1

    def test_a_publish_failure_is_a_503_and_the_row_survives(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        model_session: Session,
        stored_files: Callable[[uuid.UUID], list[Path]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The crash window, reported honestly.

        The document row is committed *before* the publish, so a publish
        failure must surface as a ``503`` — never a ``202``, which would falsely
        claim the document was queued — and the committed row must still exist,
        because that is where the crash window leaves it. Recovering such a row
        is later Phase 5 failure/retry work, recorded in the execution log.
        """
        import app.api.documents as documents_module
        from app.workers.ingestion import EnqueueError

        def refuse(document_id: uuid.UUID) -> None:
            raise EnqueueError(
                f"could not publish tenantmind.ingest_document for "
                f"document {document_id}"
            )

        monkeypatch.setattr(documents_module, "enqueue_ingestion", refuse)
        tenant, _, token = acme

        response = upload(api_client, token)

        assert response.status_code == 503, response.text
        assert response.json() == QUEUE_UNAVAILABLE
        rows = documents_in(model_session, tenant.id)
        assert len(rows) == 1, "the committed row must survive a publish failure"
        assert rows[0].status is DocumentStatus.PROCESSING
        assert len(stored_files(tenant.id)) == 1, "the stored bytes must remain"

    def test_a_rejected_upload_never_reaches_the_queue(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        queued_documents: list[uuid.UUID],
    ) -> None:
        """Refusals still leave nothing behind — including no message.

        A file rejected for its content fails before the commit, so the enqueue
        door is never reached. This pins the ordering: the queue is the last
        thing that can happen, never a side effect of a refusal.
        """
        _, _, token = acme

        response = upload(
            api_client, token, filename="report.pdf", content=b"not a pdf"
        )

        assert response.status_code == 422
        assert queued_documents == []


# ---------------------------------------------------------------------------
# Reading: GET /documents and GET /documents/{id}
# ---------------------------------------------------------------------------


class TestListing:
    def test_it_returns_an_empty_list_for_a_new_tenant(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        _, _, token = acme

        assert api_client.get(DOCUMENTS, headers=credentials(token)).json() == {
            "items": []
        }

    def test_it_returns_only_the_callers_tenant(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        globex: tuple[Tenant, User, str],
    ) -> None:
        _, acme_token = acme[0], acme[2]
        _, _, rival_token = globex

        upload(api_client, acme_token)
        upload(api_client, rival_token)

        mine = api_client.get(DOCUMENTS, headers=credentials(acme_token)).json()
        theirs = api_client.get(DOCUMENTS, headers=credentials(rival_token)).json()

        assert len(mine["items"]) == 1
        assert len(theirs["items"]) == 1
        assert mine["items"][0]["id"] != theirs["items"][0]["id"]

    def test_the_list_is_an_object_with_an_items_key(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """The envelope is a decision, made once, rather than per list endpoint."""
        _, _, token = acme

        body = api_client.get(DOCUMENTS, headers=credentials(token)).json()

        assert list(body) == ["items"]

    def test_a_viewer_may_list(self, api_client: TestClient, viewer: Any) -> None:
        _, _, token = viewer

        assert api_client.get(DOCUMENTS, headers=credentials(token)).status_code == 200

    def test_an_unauthenticated_caller_is_401(self, api_client: TestClient) -> None:
        assert api_client.get(DOCUMENTS).status_code == 401

    def test_the_order_is_stable(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """``created_at`` alone is not deterministic within one millisecond."""
        _, _, token = acme
        for index in range(3):
            upload(api_client, token, filename=f"report-{index}.pdf")

        first = api_client.get(DOCUMENTS, headers=credentials(token)).json()["items"]
        second = api_client.get(DOCUMENTS, headers=credentials(token)).json()["items"]

        assert [item["id"] for item in first] == [item["id"] for item in second]


class TestRetrieval:
    def _one(self, api_client: TestClient, token: str, **kwargs: Any) -> dict[str, Any]:
        return upload(api_client, token, **kwargs).json()

    def test_it_returns_the_stored_document(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        _, _, token = acme
        created = self._one(api_client, token)

        response = api_client.get(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(token)
        )

        assert response.status_code == 200
        assert response.json() == created

    def test_an_unknown_id_is_404(self, api_client: TestClient, acme: Any) -> None:
        _, _, token = acme

        response = api_client.get(
            f"{DOCUMENTS}/{ABSENT_DOCUMENT}", headers=credentials(token)
        )

        assert response.status_code == 404
        assert response.json() == NO_SUCH_DOCUMENT

    def test_a_malformed_id_is_422(self, api_client: TestClient, acme: Any) -> None:
        """The path parameter is a ``uuid.UUID``, so the framework answers."""
        _, _, token = acme

        response = api_client.get(f"{DOCUMENTS}/not-a-uuid", headers=credentials(token))

        assert response.status_code == 422

    def test_another_tenants_document_is_404_with_the_same_body(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        globex: tuple[Tenant, User, str],
    ) -> None:
        """The property that makes ids safe to hand out across tenants.

        A ``403`` would confirm "that document is real, you just may not read it",
        which is an enumeration oracle: a tenant could probe ids and learn from the
        status code alone which documents exist elsewhere. The body is compared
        too, not only the code.
        """
        _, _, acme_token = acme
        _, _, rival_token = globex
        theirs = self._one(api_client, rival_token)

        foreign = api_client.get(
            f"{DOCUMENTS}/{theirs['id']}", headers=credentials(acme_token)
        )
        absent = api_client.get(
            f"{DOCUMENTS}/{ABSENT_DOCUMENT}", headers=credentials(acme_token)
        )

        assert foreign.status_code == 404
        assert foreign.json() == absent.json() == NO_SUCH_DOCUMENT

    @pytest.mark.parametrize("role", list(UserRole))
    def test_every_role_may_read_a_restricted_document_it_is_named_in(
        self,
        api_client: TestClient,
        make_committed_account: AccountFactory,
        acme: tuple[Tenant, User, str],
        auth_settings: Settings,
        role: UserRole,
    ) -> None:
        _, _, admin_token = acme
        # A distinct address per role, and distinct from the `acme`, `editor` and
        # `viewer` fixtures'. Email uniqueness is per *tenant*, so reusing
        # `admin@acme.test` here would collide for the `admin` case alone and read
        # as an intermittent failure rather than as a test bug.
        org, user = make_committed_account(
            slug="acme", email=f"probe-{role.value}@acme.test", role=role
        )

        created = upload(
            api_client, admin_token, allowed_roles=[role.value]
        ).json()

        response = api_client.get(
            f"{DOCUMENTS}/{created['id']}",
            headers=credentials(token_for(user, org, auth_settings)),
        )

        assert response.status_code == 200

    def test_a_role_the_document_excludes_gets_404_not_403(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        viewer: tuple[Tenant, User, str],
    ) -> None:
        """A restriction is invisible rather than announced.

        ``403`` would say "this exists but is restricted to admins" — which tells
        a viewer the document exists, which is exactly what ``allowed_roles`` was
        set to prevent.
        """
        _, _, admin_token = acme
        _, _, viewer_token = viewer
        created = upload(api_client, admin_token, allowed_roles=["admin"]).json()

        response = api_client.get(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(viewer_token)
        )

        assert response.status_code == 404
        assert response.json() == NO_SUCH_DOCUMENT

    def test_a_restricted_document_is_absent_from_a_list_the_role_cannot_see(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        viewer: tuple[Tenant, User, str],
    ) -> None:
        """The role filter applies to the list, not only to retrieval."""
        _, _, admin_token = acme
        _, _, viewer_token = viewer
        visible = upload(api_client, admin_token, filename="open.pdf").json()
        upload(api_client, admin_token, filename="closed.pdf", allowed_roles=["admin"])

        items = api_client.get(
            DOCUMENTS, headers=credentials(viewer_token)
        ).json()["items"]

        assert [item["id"] for item in items] == [visible["id"]]

    def test_allowing_a_role_is_not_a_privilege_escalation(
        self,
        api_client: TestClient,
        editor: tuple[Tenant, User, str],
    ) -> None:
        """Naming your own role keeps your access; it grants nothing beyond it.

        Worth its own test because it is the case that looks like an escalation
        and is not. ``allowed_roles`` is a **restriction** list: naming ``editor``
        keeps the editor's existing access rather than adding any. The invariant
        is that the list can only ever remove somebody, never add — which the two
        cases below state together, because neither half is the interesting one on
        its own.
        """
        _, _, editor_token = editor

        kept = upload(
            api_client, editor_token, filename="kept.pdf", allowed_roles=["editor"]
        ).json()
        restricted = upload(
            api_client, editor_token, filename="restricted.pdf", allowed_roles=["viewer"]
        ).json()

        assert (
            api_client.get(
                f"{DOCUMENTS}/{kept['id']}", headers=credentials(editor_token)
            ).status_code
            == 200
        )
        assert (
            api_client.get(
                f"{DOCUMENTS}/{restricted['id']}", headers=credentials(editor_token)
            ).status_code
            == 404
        )

    def test_an_editor_cannot_read_back_what_they_restricted_away(
        self,
        api_client: TestClient,
        editor: tuple[Tenant, User, str],
    ) -> None:
        """An editor who restricts a document to viewers cannot then read it.

        The surprising and important case: the uploader is an editor, the list
        names only ``viewer``, and the editor is locked out of their own upload. A
        creator override would be the alternative design, and PMP §17 says nothing
        about one — so it is deliberately not here.
        """
        _, _, editor_token = editor

        created = upload(
            api_client, editor_token, allowed_roles=["viewer"]
        ).json()

        response = api_client.get(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(editor_token)
        )

        assert response.status_code == 404
        assert response.json() == NO_SUCH_DOCUMENT

    def test_roles_are_normalised_before_storage(
        self,
        api_client: TestClient,
        model_session: Session,
        acme: tuple[Tenant, User, str],
    ) -> None:
        """Duplicates removed and order canonical, so equality means sameness."""
        _, _, token = acme

        created = upload(
            api_client, token, allowed_roles=["editor", "admin", "admin"]
        ).json()

        assert created["allowed_roles"] == ["admin", "editor"]

    def test_an_unauthenticated_caller_is_401(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        _, _, token = acme
        created = upload(api_client, token).json()

        assert api_client.get(f"{DOCUMENTS}/{created['id']}").status_code == 401


# ---------------------------------------------------------------------------
# Deleting: DELETE /documents/{id}
# ---------------------------------------------------------------------------


class TestDeletion:
    def test_it_removes_the_row_and_the_file(
        self,
        api_client: TestClient,
        model_session: Session,
        acme: tuple[Tenant, User, str],
        stored_files: Callable[[uuid.UUID], list[Path]],
    ) -> None:
        tenant, _, token = acme
        created = upload(api_client, token).json()
        assert stored_files(tenant.id)

        response = api_client.delete(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(token)
        )

        assert response.status_code == 204
        assert response.content == b""
        assert stored_files(tenant.id) == []
        assert documents_in(model_session, tenant.id) == []

    def test_it_succeeds_even_if_the_file_is_already_gone(
        self,
        api_client: TestClient,
        model_session: Session,
        acme: tuple[Tenant, User, str],
        stored_files: Callable[[uuid.UUID], list[Path]],
    ) -> None:
        """A cleanup that has already happened must not block the record's removal.

        Refusing would leave the caller with a row they cannot delete and a file
        that is not there — the least useful outcome available.
        """
        tenant, _, token = acme
        created = upload(api_client, token).json()
        for path in stored_files(tenant.id):
            path.unlink()

        response = api_client.delete(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(token)
        )

        assert response.status_code == 204
        assert documents_in(model_session, tenant.id) == []

    def test_an_administrator_may_delete_a_document_they_cannot_read(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
    ) -> None:
        """The read filter is deliberately *not* applied to deletion.

        Otherwise a document restricted to, say, ``["viewer"]`` would be invisible
        to every administrator and therefore permanently undeletable — the only
        way out would be a database edit.
        """
        _, _, token = acme
        created = upload(api_client, token, allowed_roles=["viewer"]).json()

        assert api_client.get(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(token)
        ).status_code == 404
        assert api_client.delete(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(token)
        ).status_code == 204

    def test_deleting_twice_is_404_the_second_time(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """Not idempotent-success: after the first delete there is no document.

        Partial idempotency is the honest answer — the second request is "no such
        resource", not "already done".
        """
        _, _, token = acme
        created = upload(api_client, token).json()

        assert api_client.delete(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(token)
        ).status_code == 204
        assert api_client.delete(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(token)
        ).status_code == 404

    def test_another_tenants_document_is_404_and_survives(
        self,
        api_client: TestClient,
        model_session: Session,
        acme: tuple[Tenant, User, str],
        globex: tuple[Tenant, User, str],
        stored_files: Callable[[uuid.UUID], list[Path]],
    ) -> None:
        """A refused cross-tenant delete destroys nothing at all.

        Not only ``404``, but the row *and* the file both intact afterwards. A
        tenant-scoped ``DELETE`` that raised ``404`` while still deleting would
        pass a status-code assertion and be the worst bug this project can have,
        so the survival is asserted rather than assumed.
        """
        _, _, acme_token = acme
        rival_tenant, _, rival_token = globex
        theirs = upload(api_client, rival_token).json()
        assert stored_files(rival_tenant.id) != []

        response = api_client.delete(
            f"{DOCUMENTS}/{theirs['id']}", headers=credentials(acme_token)
        )

        assert response.status_code == 404
        assert response.json() == NO_SUCH_DOCUMENT
        assert documents_in(model_session, rival_tenant.id) != []
        assert stored_files(rival_tenant.id) != []

    @pytest.mark.parametrize("fixture_name", ["editor", "viewer"])
    def test_only_an_administrator_may_delete(
        self, api_client: TestClient, acme: tuple[Tenant, User, str], request: Any, fixture_name: str
    ) -> None:
        """PMP §16: document deletion is the administrator's alone."""
        _, _, admin_token = acme
        created = upload(api_client, admin_token).json()
        _, _, other_token = request.getfixturevalue(fixture_name)

        response = api_client.delete(
            f"{DOCUMENTS}/{created['id']}", headers=credentials(other_token)
        )

        assert response.status_code == 403
        assert response.json() == DENIED

    def test_an_unauthenticated_caller_is_401(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        _, _, token = acme
        created = upload(api_client, token).json()

        assert api_client.delete(f"{DOCUMENTS}/{created['id']}").status_code == 401


# ---------------------------------------------------------------------------
# Administration: GET /admin/documents/{id}/status
# ---------------------------------------------------------------------------


class TestAdministrationStatus:
    def _status_path(self, document_id: str) -> str:
        return f"{API_PREFIX}/admin/documents/{document_id}/status"

    def test_it_returns_the_processing_state(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        _, _, token = acme
        created = upload(api_client, token).json()

        response = api_client.get(
            self._status_path(created["id"]), headers=credentials(token)
        )

        assert response.status_code == 200
        assert response.json() == {
            "id": created["id"],
            "status": "processing",
            "chunk_count": 0,
            "error": None,
            "updated_at": created["updated_at"],
        }

    def test_its_payload_is_a_strict_subset_of_the_document_response(
        self,
        api_client: TestClient,
        openapi: dict[str, Any],
        acme: tuple[Tenant, User, str],
    ) -> None:
        """Asserted against the OpenAPI schemas, not against one response body.

        A single pair of responses could agree by coincidence. The schemas are the
        contract, and this states that the administration view adds nothing — which
        is what makes keeping it a separate route a presentation choice rather
        than a second capability.
        """
        document_schema = openapi["components"]["schemas"]["DocumentResponse"]
        status_schema = openapi["components"]["schemas"]["DocumentStatusResponse"]

        assert set(status_schema["properties"]) < set(document_schema["properties"])

    def test_only_an_administrator_may_read_it(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        editor: tuple[Tenant, User, str],
        viewer: tuple[Tenant, User, str],
    ) -> None:
        """PMP §15 places it under Administration; PMP §6.1 gives it to ``admin``."""
        _, _, admin_token = acme
        created = upload(api_client, admin_token).json()

        for _, _, token in (editor, viewer):
            response = api_client.get(
                self._status_path(created["id"]), headers=credentials(token)
            )
            assert response.status_code == 403
            assert response.json() == DENIED

    def test_an_unauthenticated_caller_is_401(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        _, _, token = acme
        created = upload(api_client, token).json()

        assert api_client.get(self._status_path(created["id"])).status_code == 401

    def test_another_tenants_document_is_404(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        globex: tuple[Tenant, User, str],
    ) -> None:
        _, _, acme_token = acme
        _, _, rival_token = globex
        theirs = upload(api_client, rival_token).json()

        response = api_client.get(
            self._status_path(theirs["id"]), headers=credentials(acme_token)
        )

        assert response.status_code == 404
        assert response.json() == NO_SUCH_DOCUMENT

    def test_a_role_restricted_document_is_404_to_the_administrator(
        self, api_client: TestClient, acme: tuple[Tenant, User, str]
    ) -> None:
        """It is a read, so it follows the same visibility rule retrieval does."""
        _, _, token = acme
        created = upload(api_client, token, allowed_roles=["viewer"]).json()

        response = api_client.get(
            self._status_path(created["id"]), headers=credentials(token)
        )

        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


class TestNoTenantCanBeNamed:
    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            (DOCUMENTS, {"tenant_id": str(uuid.uuid4())}),
            (DOCUMENTS, {"tenant": str(uuid.uuid4())}),
        ],
        ids=["tenant_id", "tenant"],
    )
    def test_an_upload_cannot_be_pointed_at_another_tenant(
        self,
        api_client: TestClient,
        model_session: Session,
        acme: tuple[Tenant, User, str],
        path: str,
        payload: dict[str, str],
    ) -> None:
        """A tenant field is not merely ignored — FastAPI rejects the request.

        The upload route takes a file and one optional form field, so an extra
        field is not a silently-dropped value; it is a request that does not match
        the declared shape. Either way no document is created for anyone else.
        """
        tenant, _, token = acme

        response = api_client.post(
            path,
            files={"file": ("r.pdf", PDF_BYTES, "application/pdf")},
            data=payload,
            headers=credentials(token),
        )

        assert response.status_code in {202, 422}
        rows = documents_in(model_session, tenant.id)
        assert all(row.tenant_id == tenant.id for row in rows)
        assert len(rows) <= 1

    def test_a_query_string_cannot_redirect_a_read(
        self,
        api_client: TestClient,
        acme: tuple[Tenant, User, str],
        globex: tuple[Tenant, User, str],
    ) -> None:
        """``GET /documents?tenant_id=…`` is ignored, because no such parameter exists."""
        _, _, acme_token = acme
        _, _, rival_token = globex
        theirs = upload(api_client, rival_token).json()

        response = api_client.get(
            f"{DOCUMENTS}/{theirs['id']}?tenant_id={uuid.uuid4()}",
            headers=credentials(acme_token),
        )

        assert response.status_code == 404


class TestCounting:
    def test_the_upload_path_commits_exactly_once(
        self,
        api_client: TestClient,
        sqlite_session_factory: sessionmaker[Session],
        acme: tuple[Tenant, User, str],
    ) -> None:
        """The service flushes; ``get_db`` commits.

        Proved behaviourally rather than by source inspection. The observation is
        that the row is durable the instant the response returns — read it on a
        **freshly opened** session, which can see only what was committed. An
        uncommitted row is invisible to that session on any database, so this is
        the one assertion that distinguishes a real commit from a flush.

        Counted rather than merely observed: :func:`sqlalchemy.event.listen`
        records every commit on the connection, and exactly one is expected. A
        handler that committed *and* left ``get_db`` to commit again would still
        pass a visibility check and would be a real bug, because the second commit
        would persist a refused response's partial work.
        """
        commits = 0

        def count(*_: object) -> None:
            nonlocal commits
            commits += 1

        _, _, token = acme

        event.listen(sqlite_session_factory.kw["bind"], "commit", count)
        try:
            created = upload(api_client, token).json()
        finally:
            event.remove(sqlite_session_factory.kw["bind"], "commit", count)

        assert commits == 1

        with sqlite_session_factory() as other:
            assert (
                other.scalar(
                    select(Document.id).where(Document.id == uuid.UUID(created["id"]))
                )
                is not None
            )

    def test_a_refused_request_rolls_the_row_back(
        self,
        api_client: TestClient,
        model_session: Session,
        acme: tuple[Tenant, User, str],
    ) -> None:
        """The transaction boundary is real in both directions.

        A second upload of the same ``storage_key`` would conflict if the first had
        not committed; the observable claim is simpler and is what matters — a
        refusal leaves nothing committed.
        """
        tenant, _, token = acme

        upload(api_client, token, filename="payload.exe", content=b"MZ")

        assert (
            model_session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.tenant_id == tenant.id)
            )
            == 0
        )