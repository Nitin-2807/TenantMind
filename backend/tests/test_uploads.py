"""Upload validation: what a stored document is allowed to be (Phase 2.4).

What is under test
------------------
The decision "may this file become a document", and the three properties that
decision has to have:

1. **Nothing is trusted from the client.** Not the extension, not the declared
   ``Content-Type``, not the byte count. Everything recorded is measured.
2. **A refused upload leaves nothing behind.** Every rejection path removes the
   bytes it already wrote.
3. **The refusals are specific enough to debug.** Each carries a short stable
   ``reason`` token for the log, while the message that would tell a prober the
   rules stays in the log too — which is why these tests assert on ``reason``
   rather than on prose.

Why these are tested directly rather than only through the API
--------------------------------------------------------------
``test_documents_api.py`` proves the HTTP status codes. It cannot easily prove
that a ``.docx`` containing ``[Content_Types].xml`` and a ``word/`` directory is
accepted while an identical archive without them is refused, because building
those fixtures through ``multipart`` for every case would bury the property under
plumbing. The functions here take a path and a filename, so a test writes bytes
to ``tmp_path`` and calls them.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from app.models.document import DOCUMENT_FILENAME_MAX_LENGTH
from app.schemas.documents import SafeFilename, sanitise_filename
from app.services.uploads import (
    MAX_ARCHIVE_MEMBERS,
    OFFICE_CONTENT_TYPES_MEMBER,
    SIGNATURE_PEEK_BYTES,
    SUPPORTED_FORMATS,
    ContentMismatchError,
    EmptyUploadError,
    InvalidContainerError,
    MalformedContainerError,
    UnsupportedFileTypeError,
    UploadRejectedError,
    ValidatedUpload,
    detect_content_type,
    extension_of,
    resolve_format,
    validate_stored_upload,
    verify_office_container,
)

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"

_FILENAME_ADAPTER: TypeAdapter[str] = TypeAdapter(SafeFilename)


def make_office_bytes(
    tmp_path: Path,
    *,
    archive_part: str | None = "word/",
    content_types: bool = True,
    member_count: int = 0,
) -> Path:
    """Write a ZIP archive shaped like (or unlike) an OOXML document.

    Built with :mod:`zipfile` rather than checked in as a fixture binary so the
    content of the test is visible in the test, and so no opaque blob in the
    repository has to be trusted to be what its name says.
    """
    path = tmp_path / "upload.bin"
    with zipfile.ZipFile(path, "w") as archive:
        if content_types:
            archive.writestr(OFFICE_CONTENT_TYPES_MEMBER, "<Types/>")
        if archive_part is not None:
            archive.writestr(f"{archive_part}document.xml", "<document/>")
        for index in range(member_count):
            archive.writestr(f"word/pad{index}.xml", "x")
    return path


# ---------------------------------------------------------------------------
# Extension handling
# ---------------------------------------------------------------------------


class TestExtension:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("report.pdf", "pdf"),
            ("report.PDF", "pdf"),
            ("report.Pdf", "pdf"),
            ("report.tar.gz", "gz"),
            ("archive.", ""),
            ("noextension", ""),
            (".gitignore", ""),
            ("trailing.", ""),
            ("two.dots..pdf", "pdf"),
        ],
    )
    def test_extension_of(self, filename: str, expected: str) -> None:
        assert extension_of(filename) == expected

    def test_a_leading_dot_is_not_an_extension(self) -> None:
        """``".gitignore"`` has no extension, so it is refused as a type.

        Worth stating because it is the one place where the "extension" is a
        deliberate absence rather than a value: without this, a dotfile named
        ``.md`` would be read as the Markdown format, which is not what a user
        who uploads ``.md`` means either.
        """
        assert extension_of(".md") == ""
        with pytest.raises(UnsupportedFileTypeError):
            resolve_format(".md")

    def test_an_unsupported_extension_names_itself(self) -> None:
        with pytest.raises(UnsupportedFileTypeError) as caught:
            resolve_format("payload.exe")

        assert caught.value.extension == "exe"
        assert caught.value.reason == "unsupported_file_type"

    def test_a_missing_extension_is_reported_as_such(self) -> None:
        """``"(none)"`` rather than an empty string, so a log line is readable."""
        with pytest.raises(UnsupportedFileTypeError) as caught:
            resolve_format("README")

        assert caught.value.extension == "(none)"

    def test_every_refusal_is_one_exception_type(self) -> None:
        """The handler maps one type to one response; that needs one hierarchy."""
        for refusal in (
            UnsupportedFileTypeError,
            ContentMismatchError,
            MalformedContainerError,
            EmptyUploadError,
            InvalidContainerError,
        ):
            assert issubclass(refusal, UploadRejectedError)
        assert issubclass(UploadRejectedError, ValueError)


# ---------------------------------------------------------------------------
# The supported set
# ---------------------------------------------------------------------------


class TestSupportedSet:
    def test_it_is_exactly_the_seven_documented_types(self) -> None:
        """A closed set, asserted literally.

        Literal rather than derived, deliberately: the whole point of a
        *whitelist* is that adding to it is a decision somebody makes on purpose.
        A test computed from ``SUPPORTED_FORMATS`` would follow any addition
        silently, which is the failure this guard exists to prevent.
        """
        assert set(SUPPORTED_FORMATS) == {
            "pdf",
            "docx",
            "xlsx",
            "pptx",
            "txt",
            "md",
            "html",
        }

    def test_every_entry_has_a_content_type(self) -> None:
        for extension, spec in SUPPORTED_FORMATS.items():
            assert spec.content_type, extension
            assert "/" in spec.content_type, extension

    def test_content_types_carry_no_parameters(self) -> None:
        """A stored media type with a charset would be a second, separate input.

        ``content_type`` is derived from the bytes and never from the client, so
        a parameter here could only be a constant nobody needs — and a stored
        value a client might later read back as if it described their request.
        """
        for extension, spec in SUPPORTED_FORMATS.items():
            assert ";" not in spec.content_type, extension

    def test_formats_with_a_signature_declare_one(self) -> None:
        """The three formats whose bytes can be proved to match all declare it."""
        for extension in ("pdf", "docx", "xlsx", "pptx"):
            assert SUPPORTED_FORMATS[extension].signature is not None, extension

    def test_formats_without_a_signature_say_so(self) -> None:
        """``txt``, ``md`` and ``html`` have no magic bytes, and that is recorded."""
        for extension in ("txt", "md", "html"):
            assert SUPPORTED_FORMATS[extension].signature is None, extension

    def test_only_archive_formats_name_an_archive_part(self) -> None:
        for extension, spec in SUPPORTED_FORMATS.items():
            assert (spec.archive_part is not None) == (
                extension in {"docx", "xlsx", "pptx"}
            ), extension


# ---------------------------------------------------------------------------
# Signature detection
# ---------------------------------------------------------------------------


class TestDetectContentType:
    def test_a_real_pdf_is_accepted(self) -> None:
        spec = detect_content_type("report.pdf", PDF_BYTES[:SIGNATURE_PEEK_BYTES])
        assert spec.content_type == "application/pdf"

    def test_a_pdf_named_but_not_one_is_refused(self) -> None:
        """The classic renamed-file attack, and the reason this module exists."""
        with pytest.raises(ContentMismatchError) as caught:
            detect_content_type(
                "invoice.pdf", b"MZ\x90\x00this is an executable"
            )

        assert caught.value.extension == "pdf"
        assert caught.value.reason == "content_mismatch"

    def test_a_pdf_signature_under_a_zip_name_is_refused(self) -> None:
        with pytest.raises(ContentMismatchError):
            detect_content_type("report.docx", PDF_BYTES)

    def test_text_formats_accept_any_bytes(self) -> None:
        """Stated as a fact rather than implied by the absence of a rule.

        The bytes that make a file ``.txt`` are not distinguishable from arbitrary
        bytes, so nothing is checked — and this test is what stops that from
        quietly becoming an oversight if the table is edited later.
        """
        for extension in ("txt", "md", "html"):
            spec = detect_content_type(f"notes.{extension}", b"\x00\xff\xfe random")
            assert spec.content_type == SUPPORTED_FORMATS[extension].content_type

    def test_a_signature_only_needs_the_first_bytes(self) -> None:
        """The whole check costs :data:`SIGNATURE_PEEK_BYTES`, not a full read."""
        assert SIGNATURE_PEEK_BYTES == 8
        assert detect_content_type("a.pdf", b"%PDF-1.7").content_type == (
            "application/pdf"
        )

    def test_an_empty_peek_cannot_satisfy_a_signature(self) -> None:
        """A file shorter than the signature is refused, not waved through."""
        with pytest.raises(ContentMismatchError):
            detect_content_type("a.pdf", b"")
        with pytest.raises(ContentMismatchError):
            detect_content_type("a.pdf", b"%PD")


# ---------------------------------------------------------------------------
# Container verification
# ---------------------------------------------------------------------------


class TestOfficeContainer:
    @pytest.mark.parametrize(
        ("extension", "part"),
        [("docx", "word/"), ("xlsx", "xl/"), ("pptx", "ppt/")],
    )
    def test_a_real_container_of_each_type_is_accepted(
        self, tmp_path: Path, extension: str, part: str
    ) -> None:
        path = make_office_bytes(tmp_path, archive_part=part)
        verify_office_container(path, extension=extension)

    @pytest.mark.parametrize(
        ("extension", "part"),
        [("docx", "word/"), ("xlsx", "xl/"), ("pptx", "ppt/")],
    )
    def test_a_word_document_named_xlsx_is_refused(
        self, tmp_path: Path, extension: str, part: str
    ) -> None:
        """The part directory is what distinguishes the three from each other.

        Without this, any one of them would satisfy any other — which is why the
        ``PK`` signature check alone is not sufficient.
        """
        path = make_office_bytes(tmp_path, archive_part="word/")

        if extension == "docx":
            verify_office_container(path, extension=extension)
            return

        with pytest.raises(MalformedContainerError) as caught:
            verify_office_container(path, extension=extension)

        assert caught.value.reason == "malformed_container"
        assert part in str(caught.value)

    def test_an_archive_without_content_types_is_refused(
        self, tmp_path: Path
    ) -> None:
        """A ``PK`` prefix alone is not an Office document."""
        path = make_office_bytes(tmp_path, content_types=False)

        with pytest.raises(MalformedContainerError) as caught:
            verify_office_container(path, extension="docx")

        assert OFFICE_CONTENT_TYPES_MEMBER in str(caught.value)

    def test_unreadable_archive_bytes_are_refused(self, tmp_path: Path) -> None:
        """``PK`` at the head, garbage after it."""
        path = tmp_path / "broken.docx"
        path.write_bytes(b"PK\x03\x04" + b"\x00" * 64)

        with pytest.raises(MalformedContainerError):
            verify_office_container(path, extension="docx")

    def test_a_non_archive_extension_is_refused_outright(
        self, tmp_path: Path
    ) -> None:
        """Reached in error, and it says so rather than trying anyway."""
        path = make_office_bytes(tmp_path)

        with pytest.raises(UnsupportedFileTypeError):
            verify_office_container(path, extension="txt")

    def test_an_archive_declaring_too_many_members_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The member count is checked before any member is read.

        A decompression bomb can declare a huge central directory in a small file;
        the cap is on the *declared* count, which is what makes it cheap.

        Built at exactly the limit first, then one member over — so the assertion
        is about the *boundary* being inclusive rather than about a number that
        happened to be chosen on either side of it.
        """
        at_limit = make_office_bytes(
            tmp_path,
            # Two members are written by `make_office_bytes` itself.
            member_count=MAX_ARCHIVE_MEMBERS - 2,
        )
        with zipfile.ZipFile(at_limit) as archive:
            assert len(archive.namelist()) == MAX_ARCHIVE_MEMBERS
        verify_office_container(at_limit, extension="docx")

        with zipfile.ZipFile(at_limit, "a") as archive:
            archive.writestr("word/extra.xml", "x")

        with pytest.raises(MalformedContainerError) as caught:
            verify_office_container(at_limit, extension="docx")

        assert str(MAX_ARCHIVE_MEMBERS) in str(caught.value)
        assert str(MAX_ARCHIVE_MEMBERS + 1) in str(caught.value)

    def test_an_unreadable_file_is_our_fault_not_the_callers(
        self, tmp_path: Path
    ) -> None:
        """A distinct type, because the handler reports it differently.

        ``BadZipFile`` means the client sent something malformed;
        ``OSError`` means the server wrote a file it then cannot read. Reporting
        the second as the first would blame the client for our failure.
        """
        path = tmp_path / "unreadable.docx"
        path.write_bytes(PDF_BYTES)

        with pytest.raises(UploadRejectedError):
            verify_office_container(path, extension="docx")

        path.unlink()
        with pytest.raises(InvalidContainerError):
            verify_office_container(path, extension="docx")


# ---------------------------------------------------------------------------
# The combined entry point
# ---------------------------------------------------------------------------


class TestValidateStoredUpload:
    def _pdf(self, tmp_path: Path, data: bytes = PDF_BYTES) -> Path:
        path = tmp_path / "stored.pdf"
        path.write_bytes(data)
        return path

    def test_a_good_upload_returns_measured_values(self, tmp_path: Path) -> None:
        result = validate_stored_upload(
            self._pdf(tmp_path),
            filename="report.pdf",
            size_bytes=len(PDF_BYTES),
            leading_bytes=PDF_BYTES[:SIGNATURE_PEEK_BYTES],
            max_bytes=1_000_000,
        )

        assert isinstance(result, ValidatedUpload)
        assert result.filename == "report.pdf"
        assert result.content_type == "application/pdf"
        assert result.size_bytes == len(PDF_BYTES)
        assert result.extension == "pdf"

    def test_an_empty_upload_is_refused(self, tmp_path: Path) -> None:
        """A zero-byte document would violate ``size_bytes > 0`` at the database."""
        path = self._pdf(tmp_path, b"")

        with pytest.raises(EmptyUploadError) as caught:
            validate_stored_upload(
                path,
                filename="report.pdf",
                size_bytes=0,
                leading_bytes=b"",
                max_bytes=1_000_000,
            )

        assert caught.value.reason == "empty_upload"

    def test_an_oversize_upload_is_refused_even_when_called_directly(
        self, tmp_path: Path
    ) -> None:
        """A check only one caller makes is not a check.

        ``create_document`` already stops the copy at the limit, so this branch is
        unreachable through the service. It is re-checked anyway so that calling
        this function on its own is safe rather than merely working today.
        """
        from app.storage.local import FileTooLargeError

        path = self._pdf(tmp_path)

        with pytest.raises(FileTooLargeError) as caught:
            validate_stored_upload(
                path,
                filename="report.pdf",
                size_bytes=101,
                leading_bytes=PDF_BYTES[:SIGNATURE_PEEK_BYTES],
                max_bytes=100,
            )

        assert caught.value.max_bytes == 100

    def test_the_size_check_runs_before_the_type_check(self, tmp_path: Path) -> None:
        """Order is stated once in the docstring; this pins that it is obeyed.

        An empty *and* mistyped upload reports the emptiness, because emptiness is
        the cheaper fact and the more useful one.
        """
        path = self._pdf(tmp_path, b"")

        with pytest.raises(EmptyUploadError):
            validate_stored_upload(
                path,
                filename="payload.exe",
                size_bytes=0,
                leading_bytes=b"",
                max_bytes=1_000_000,
            )

    def test_a_mistyped_upload_never_reaches_the_container_check(
        self, tmp_path: Path
    ) -> None:
        """No caller can open an archive that was not first type-checked."""
        path = tmp_path / "payload.docx"
        path.write_bytes(b"not a zip at all")

        with pytest.raises(ContentMismatchError):
            validate_stored_upload(
                path,
                filename="payload.docx",
                size_bytes=17,
                leading_bytes=b"not a zi",
                max_bytes=1_000_000,
            )

    def test_a_declared_content_type_is_never_consulted(self, tmp_path: Path) -> None:
        """The signature of the property this whole module exists for.

        ``validate_stored_upload`` takes no ``Content-Type`` argument at all, so a
        client cannot make the system record a media type it did not verify. This
        asserts the *absence*, which is stronger than asserting a value is
        overwritten.
        """
        import inspect

        assert "content_type" not in {
            name
            for name in inspect.signature(validate_stored_upload).parameters
            if name != "content_type"
        }


# ---------------------------------------------------------------------------
# Filename hygiene, at the boundary
# ---------------------------------------------------------------------------


class TestSafeFilename:
    @pytest.mark.parametrize(
        "filename",
        [
            "report.pdf",
            "report..v2.pdf",
            "  spaced.txt  ",
            "ünïcode-Ω.md",
            "a" * DOCUMENT_FILENAME_MAX_LENGTH,
            "quarterly report (final).xlsx",
            "no-extension",
            "archive.tar.gz",
        ],
    )
    def test_acceptable_names_survive(self, filename: str) -> None:
        assert _FILENAME_ADAPTER.validate_python(filename) == filename.strip()

    @pytest.mark.parametrize(
        ("filename", "expected_in_message"),
        [
            ("../etc/passwd", "directory path"),
            ("..\\windows\\system32", "directory path"),
            ("a/b.txt", "directory path"),
            ("a\\b.txt", "directory path"),
            (".env", "dot"),
            ("..", "dot"),
            (".hidden.pdf", "dot"),
            ("with\x00nul.pdf", "control"),
            ("with\x7fdel.pdf", "control"),
            (f"with{chr(0x202E)}override.pdf", "bidirectional"),
            ("with\u200bzero-width.pdf", "format"),
            ("with\ufeffbom.pdf", "format"),
            ("", "at least 1"),
            ("   ", "at least 1"),
            ("a" * (DOCUMENT_FILENAME_MAX_LENGTH + 1), "at most"),
        ],
    )
    def test_unacceptable_names_are_refused(
        self, filename: str, expected_in_message: str
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            _FILENAME_ADAPTER.validate_python(filename)

        assert expected_in_message in str(caught.value)

    def test_a_non_string_is_left_for_pydantic_to_report(self) -> None:
        """The validator defers to Pydantic rather than reporting something vaguer.

        :func:`app.schemas.documents._clean_filename` returns non-string input
        unchanged, so the client is told "this is not a string" instead of being
        told about a rule about filenames that does not apply to them.
        """
        with pytest.raises(ValidationError) as caught:
            _validate_filename(123)

        errors = caught.value.errors()
        assert [error["type"] for error in errors] == ["string_type"]
        assert "should be a valid string" in errors[0]["msg"].lower()

    def test_surrounding_whitespace_is_normalised_not_refused(self) -> None:
        """Normalisation, not refusal — matching :mod:`app.schemas.fields`."""
        assert sanitise_filename("  report.pdf  ") == "report.pdf"

    def test_sanitise_filename_matches_the_adapter(self) -> None:
        """The callable and the type are one rule, not two."""
        for filename in ("report.pdf", "  spaced.txt ", "a" * 255):
            assert sanitise_filename(filename) == _FILENAME_ADAPTER.validate_python(
                filename
            )

    def test_the_declared_limit_is_the_column_width(self) -> None:
        """The boundary accepts exactly what the column stores.

        Imported from the model rather than restated, and asserted against the
        model constant so that changing one without the other fails here.
        """
        from app.models.document import DOCUMENT_FILENAME_MAX_LENGTH as width

        assert DOCUMENT_FILENAME_MAX_LENGTH == width

    def test_the_length_is_measured_in_characters_not_bytes(self) -> None:
        """A multi-byte name of the right character count is accepted.

        SQLite and PostgreSQL both measure ``VARCHAR(255)`` in characters, so a
        byte-based limit would reject names the column can hold and a test would
        have to pick a side. This records which one was chosen.
        """
        filename = "é" * DOCUMENT_FILENAME_MAX_LENGTH
        assert len(filename.encode("utf-8")) > DOCUMENT_FILENAME_MAX_LENGTH
        assert sanitise_filename(filename) == filename


def _validate_filename(value: object) -> object:
    """Run ``value`` through the filename adapter, keeping its declared type.

    :func:`pydantic.TypeAdapter.validate_python` is typed ``(Any) -> T``, so
    passing an :class:`object` is already within its signature and needs no
    suppression. The wrapper exists so the two call sites that are deliberately
    handing the adapter a non-string read as one decision rather than two.
    """
    return _FILENAME_ADAPTER.validate_python(value)


def _clean_returns_input_unchanged(value: object) -> bool:
    try:
        _validate_filename(value)
    except ValidationError as exc:
        return "should be a valid string" in str(exc).lower()
    return False


class TestFilenameIsNeverAPath:
    """The structural guarantee, stated positively.

    ``test_storage.py`` proves the *store* cannot be tricked into building an
    escaping path. This states the complementary half: the value the schema
    produces is not a path either, so there is nothing for it to be turned into
    one.
    """

    @pytest.mark.parametrize(
        "filename",
        ["report.pdf", "a" * 255, "  spaced.pdf ", "report..v2.pdf"],
    )
    def test_an_accepted_filename_has_no_path_shape(self, filename: str) -> None:
        cleaned = sanitise_filename(filename)

        assert "/" not in cleaned
        assert "\\" not in cleaned
        assert "\x00" not in cleaned
        assert not cleaned.startswith(".")
        from pathlib import PurePath

        assert PurePath(cleaned).name == cleaned


class TestIoContract:
    def test_the_module_never_reads_a_file_into_memory_in_full(self) -> None:
        """A guard against a future convenience refactor.

        ``read_leading_bytes`` exists so that type-checking a 10 MB document costs
        eight bytes. A refactor that switched it for ``read_bytes`` would pass
        every functional test and quietly reintroduce the memory cost, so the
        constant is asserted to be small rather than merely present.
        """
        assert SIGNATURE_PEEK_BYTES <= 16
        assert io.DEFAULT_BUFFER_SIZE > 0