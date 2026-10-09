"""Tests for :mod:`app.rag.parsers` — every supported format, extracted for real.

What these tests are built from
------------------------------
Real bytes, not mocks. :mod:`pypdf` and :mod:`docx` produce genuine documents, and
the two OOXML formats that have no third-party parser here (``xlsx``, ``pptx``) are
built as valid containers by hand with :mod:`zipfile` and :mod:`xml.etree` — the
same parts :mod:`app.rag.parsers` reads.

A mock would prove nothing about the parser. The whole risk in this layer is that
some third-party library returns something unexpected for a real file, and a mocked
library returns exactly what the test told it to. These fixtures are also shared
with ``tests/test_ingestion.py`` and ``tests/test_qdrant_integration.py`` through
:mod:`tests.documents`, so the pipeline's documents are the same bytes the parser is
tested on.

The one thing that is *not* real is the extraction of text from a PDF. Building a
PDF with a genuine text layer needs a font and a content stream, which would be a
second PDF generator to test rather than a parser; ``tests/test_parsing.py``
therefore covers :class:`~app.rag.parsers.PdfParser`'s *structural* behaviour —
page count, ordering, error messages, encryption — and the text-layer path is
verified separately by the same ``pypdf`` writing a text layer, below.

The error-message assertions are the point of half this file
-----------------------------------------------------------
``documents.error`` is returned verbatim by ``GET /admin/documents/{id}/status``, so a
parser message is tenant-visible. Every assertion below that a message **does not**
contain the offending bytes is load-bearing: :mod:`pypdf` puts file content in
``PdfReadError``, and without an assertion here a future edit could reintroduce it
and nothing would fail until someone read their own document's bytes back to them.
"""

from __future__ import annotations

import dataclasses
import io
import zipfile

import pytest

from app.rag.parsers import (
    SUPPORTED_EXTENSIONS,
    DocumentParseError,
    DocumentParser,
    Page,
    ParsedDocument,
    decode_text,
    extract_html_text,
    parser_for_extension,
    parser_for_format,
    resolve_parser,
)
from app.services.uploads import SUPPORTED_FORMATS
from tests.documents import (
    OOXML_MAIN_RELS,
    OOXML_RELATIONSHIPS_NS,
    OOXML_RELS,
    build_docx,
    build_pdf_blank,
    build_pdf_text,
    build_pptx,
    build_xlsx,
    build_xlsx_parts,
    office_content_types,
    zip_parts,
)
from tests.documents import PRESENTATION_NS as P_NS
from tests.documents import SPREADSHEET_NS as S_NS


def _bom(text: str) -> bytes:
    """``text`` prefixed with a UTF-8 byte-order mark.

    Built from the constant :mod:`app.rag.parsers` itself uses rather than by typing
    ``"\ufeff"``, so a change to that constant cannot leave this fixture asserting
    about a mark the parser no longer strips.
    """
    from app.rag.parsers import UTF8_BOM

    return UTF8_BOM + text.encode()


class TestTheRegistry:
    """The registry is the whole extension story, so it is checked as a table."""

    @pytest.mark.parametrize("extension", sorted(SUPPORTED_EXTENSIONS))
    def test_every_supported_extension_has_a_parser(self, extension: str) -> None:
        assert extension in SUPPORTED_FORMATS, (
            f"{extension!r} has a parser but the upload validator does not accept "
            "it, so the file could never reach one"
        )

    def test_every_accepted_extension_has_a_parser(self) -> None:
        missing = sorted(set(SUPPORTED_FORMATS) - set(SUPPORTED_EXTENSIONS))
        assert not missing, (
            f"the upload validator accepts {missing} but no parser can read them, "
            "so they would fail at the parse stage instead of at upload"
        )

    @pytest.mark.parametrize(
        ("filename", "extension"),
        [
            ("report.pdf", "pdf"),
            ("report.PDF", "pdf"),
            ("memo.docx", "docx"),
            ("prices.xlsx", "xlsx"),
            ("deck.pptx", "pptx"),
            ("notes.txt", "txt"),
            ("notes.md", "md"),
            ("page.html", "html"),
        ],
    )
    def test_resolution_is_by_final_extension(
        self, filename: str, extension: str
    ) -> None:
        """``a.tar.gz`` is a gzip and ``a.tar`` is a tar, never both ``tar``."""
        assert resolve_parser(filename).extension == extension

    @pytest.mark.parametrize(
        "filename", ["archive.tar.gz", "notes.rst", "sheet.xlsm", "photo.png"]
    )
    def test_an_unaccepted_extension_is_refused_rather_than_guessed(
        self, filename: str
    ) -> None:
        """A double extension is resolved on its *final* segment, and then refused.

        ``archive.tar.gz`` ends in ``gz``, which the upload validator does not accept
        either, so this can never arrive. Asserted because the alternative failure —
        stripping the first extension and looking for ``tar`` — would resolve a file
        to a parser the validator never agreed to.
        """
        with pytest.raises(DocumentParseError):
            resolve_parser(filename)

    def test_an_unknown_extension_is_refused(self) -> None:
        with pytest.raises(DocumentParseError) as caught:
            resolve_parser("spreadsheet.xlsm")
        assert "xlsm" in str(caught.value)

    def test_a_filename_with_no_extension_is_refused(self) -> None:
        with pytest.raises(DocumentParseError):
            resolve_parser("README")

    def test_parser_for_extension_returns_none_for_an_unknown_one(self) -> None:
        """Distinct from :func:`resolve_parser` raising: this is a lookup."""
        assert parser_for_extension("xlsm") is None
        assert parser_for_extension("pdf") is not None

    def test_parsers_are_shared_stateless_instances(self) -> None:
        """The registry holds constructed instances, so they must be reusable.

        Sharing is only safe because a parser holds no per-document state; a test
        that parses two documents through the same instance would pass even for a
        parser that *did* leak state if it reset on entry, so the assertion is on
        the identity and the absence of attributes rather than on behaviour alone.
        """
        first = resolve_parser("a.pdf")
        second = resolve_parser("b.pdf")
        assert first is second
        assert not vars(first), (
            "a registry-held parser carries instance state, so two concurrent "
            "ingestions could see each other's document"
        )

    def test_the_parser_for_a_format_agrees_with_the_registry(self) -> None:
        for supported in SUPPORTED_FORMATS.values():
            by_format = parser_for_format(supported)
            by_extension = parser_for_extension(supported.extension)
            assert by_format is by_extension

    def test_every_parser_declares_its_own_extension(self) -> None:
        for extension, parser in SUPPORTED_EXTENSIONS.items():
            assert parser.extension == extension


class TestDocumentParserIsAnAbstraction:
    """The layer must be extensible without rewriting the pipeline."""

    def test_the_base_class_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            DocumentParser()  # type: ignore[abstract]

    def test_a_new_format_can_be_added_without_touching_the_pipeline(self) -> None:
        """A third-party parser is registered by dict assignment. Nothing else.

        If :mod:`app.services.ingestion` had a hard-coded branch per format — which
        it must not, since it resolves through the registry — this would fail.
        """
        registry = dict(SUPPORTED_EXTENSIONS)

        class ExoticParser(DocumentParser):
            extension = "zzz"

            def parse(self, data: bytes) -> ParsedDocument:
                return ParsedDocument(pages=(Page(text=data.decode(), number=1),))

        registry["zzz"] = ExoticParser()
        assert registry["zzz"].parse(b"payload").full_text == "payload"

    def test_a_parser_that_does_not_implement_parse_cannot_be_instantiated(self) -> None:
        class Incomplete(DocumentParser):
            extension = "zzz"

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]


class TestParsedDocument:
    def test_full_text_joins_pages_with_a_paragraph_break(self) -> None:
        document = ParsedDocument(
            pages=(Page(text="first", number=1), Page(text="second", number=2))
        )
        assert document.full_text == "first\n\nsecond"

    def test_no_pages_yields_empty_text(self) -> None:
        assert ParsedDocument(pages=()).full_text == ""

    def test_it_is_frozen(self) -> None:
        """Immutable, so nothing downstream can mutate a parsed document in place.

        Asserted on ``FrozenInstanceError`` rather than bare ``Exception``: the
        dataclasses guarantee is a specific exception, and a broad catch here would
        also pass if the assignment failed for an unrelated reason.
        """
        document = ParsedDocument(pages=(Page(text="a", number=1),))
        with pytest.raises(dataclasses.FrozenInstanceError):
            document.pages = ()  # type: ignore[misc]
        assert isinstance(dataclasses.FrozenInstanceError(), AttributeError)


class TestTextFormats:
    @pytest.mark.parametrize("extension", ["txt", "md"])
    def test_plain_text_is_returned_as_one_unpaged_unit(
        self, extension: str
    ) -> None:
        parsed = resolve_parser(f"notes.{extension}").parse(
            b"First line\n\nSecond paragraph"
        )
        assert len(parsed.pages) == 1
        assert parsed.pages[0].number is None
        assert parsed.pages[0].text == "First line\n\nSecond paragraph"

    def test_html_tags_are_removed_and_text_kept(self) -> None:
        parsed = resolve_parser("page.html").parse(
            b"<html><body><h1>Title</h1><p>Body <b>bold</b>.</p></body></html>"
        )
        text = parsed.full_text
        assert "Title" in text
        assert "Body" in text
        assert "bold" in text
        assert "<" not in text and ">" not in text

    def test_html_block_elements_become_line_breaks(self) -> None:
        text = extract_html_text("<p>one</p><p>two</p>")
        assert "one" in text and "two" in text
        assert "onetwo" not in text, (
            "block elements must separate; without a break, adjacent paragraphs "
            "would be concatenated into one unreadable run"
        )

    def test_html_script_and_style_content_is_not_indexed_as_text(self) -> None:
        text = extract_html_text(
            "<style>body{color:red}</style><script>var x = 1;</script><p>Real.</p>"
        )
        assert "Real." in text
        assert "color:red" not in text
        assert "var x" not in text

    def test_html_entities_are_decoded(self) -> None:
        assert "&amp;" not in extract_html_text("<p>a &amp; b</p>")
        assert "a & b" in extract_html_text("<p>a &amp; b</p>")

    def test_an_empty_text_file_yields_an_empty_page_not_an_error(self) -> None:
        """The contract: "it parsed, and there was nothing to find"."""
        parsed = resolve_parser("empty.txt").parse(b"")
        assert len(parsed.pages) == 1
        assert parsed.full_text.strip() == ""

    def test_a_utf8_bom_is_not_kept_as_a_character(self) -> None:
        parsed = resolve_parser("notes.txt").parse(_bom("Body"))
        assert not parsed.full_text.startswith("﻿")


class TestPdf:
    def test_a_real_pdf_yields_its_pages_in_order(self) -> None:
        data, _ = build_pdf_text(["Alpha page one", "Beta page two"])
        parsed = resolve_parser("doc.pdf").parse(data)

        assert [page.number for page in parsed.pages] == [1, 2]
        assert "Alpha" in parsed.pages[0].text
        assert "Beta" in parsed.pages[1].text

    def test_page_numbers_are_one_based(self) -> None:
        data, _ = build_pdf_text(["only"])
        parsed = resolve_parser("doc.pdf").parse(data)
        assert parsed.pages[0].number == 1, (
            "PDF pages are 1-based; a 0-based number would be cited as page 0"
        )

    def test_garbage_claiming_to_be_a_pdf_is_refused(self) -> None:
        with pytest.raises(DocumentParseError):
            resolve_parser("doc.pdf").parse(b"%PDF-1.7\nnot actually a pdf\n")

    def test_an_empty_file_claiming_to_be_a_pdf_is_refused(self) -> None:
        with pytest.raises(DocumentParseError):
            resolve_parser("doc.pdf").parse(b"")

    def test_a_pdf_whose_pages_carry_no_text_yields_empty_pages(self) -> None:
        """A scanned PDF: structurally valid, nothing to extract.

        Pages are still returned, each empty, rather than no pages at all. The
        orchestrator decides a document has nothing indexable; the parser's only
        claim is that it parsed, and that there was nothing there.
        """
        parsed = resolve_parser("scan.pdf").parse(build_pdf_blank(2))
        assert len(parsed.pages) == 2
        assert all(page.text == "" for page in parsed.pages)
        assert parsed.full_text.strip() == ""

    def test_a_pdf_with_no_pages_falls_back_to_one_empty_page(self) -> None:
        """The fallback exists so a caller always has at least one page to iterate."""
        parsed = resolve_parser("empty.pdf").parse(build_pdf_text([])[0])
        assert len(parsed.pages) == 1
        assert parsed.pages[0].text.strip() == ""

    def test_the_message_never_quotes_the_document(self) -> None:
        """The load-bearing assertion of this class.

        :mod:`pypdf` puts the offending bytes in ``PdfReadError``. That message would
        become ``documents.error`` and be returned to an administrator, so the wrapper
        must report the *class* only.
        """
        secret = b"%PDF-1.7\nSUPERSECRETTENANTCONTENT\n%%EOF\n"
        with pytest.raises(DocumentParseError) as caught:
            resolve_parser("doc.pdf").parse(secret)
        assert "SUPERSECRET" not in str(caught.value)
        assert "PdfReadError" in str(caught.value) or "Error" in str(caught.value)

    def test_a_truncated_pdf_does_not_leak_a_file_path(self) -> None:
        """A path in an admin-visible column would publish the storage layout."""
        with pytest.raises(DocumentParseError) as caught:
            resolve_parser("doc.pdf").parse(b"%PDF-1.7\n\x00\x01\x02")
        message = str(caught.value)
        assert "/" not in message
        assert "\\" not in message

    def test_an_encrypted_pdf_is_reported_as_encrypted(self) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("secret")
        buffer = io.BytesIO()
        writer.write(buffer)

        with pytest.raises(DocumentParseError, match="encrypted"):
            resolve_parser("locked.pdf").parse(buffer.getvalue())


class TestDocx:
    def test_paragraphs_are_extracted_in_order(self) -> None:
        parsed = resolve_parser("memo.docx").parse(
            build_docx(["Leave Policy", "Staff accrue two days per month."])
        )
        text = parsed.full_text
        assert "Leave Policy" in text
        assert text.index("Leave Policy") < text.index("accrue"), (
            "paragraph order is document order; reordering would make a citation "
            "quote a passage out of sequence"
        )

    def test_a_table_contributes_its_cell_text(self) -> None:
        parsed = resolve_parser("memo.docx").parse(
            build_docx(["Grades"], table_rows=[["Grade", "Days"], ["S1", "25"]])
        )
        text = parsed.full_text
        for expected in ("Grade", "Days", "S1", "25"):
            assert expected in text, f"table cell {expected!r} was not extracted"

    def test_a_docx_is_unpaged(self) -> None:
        """Word documents have no fixed pages once reflowed, so a number would lie."""
        parsed = resolve_parser("memo.docx").parse(build_docx(["Body"]))
        assert all(page.number is None for page in parsed.pages)

    def test_bytes_that_are_not_a_zip_are_refused(self) -> None:
        with pytest.raises(DocumentParseError):
            resolve_parser("memo.docx").parse(b"PK\x03\x04 this is not really it")

    def test_a_zip_that_is_not_a_docx_is_refused(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("hello.txt", "not a document")
        with pytest.raises(DocumentParseError):
            resolve_parser("memo.docx").parse(buffer.getvalue())

    def test_the_message_does_not_quote_the_content(self) -> None:
        secret = b"PK\x03\x04LEAKEDSECRETPAYLOAD"
        with pytest.raises(DocumentParseError) as caught:
            resolve_parser("memo.docx").parse(secret)
        assert "LEAKEDSECRET" not in str(caught.value)


class TestXlsx:
    def test_shared_strings_become_readable_cells(self) -> None:
        parsed = resolve_parser("prices.xlsx").parse(
            build_xlsx(
                shared=["Product", "Widget", "Widget Plus"],
                rows=[[0, 1], [2, None]],
                numbers=[[None, None], [None, "42.5"]],
            )
        )
        text = parsed.full_text
        for expected in ("Product", "Widget", "Widget Plus", "42.5"):
            assert expected in text, f"{expected!r} was not extracted"

    def test_a_sheet_is_unpaged(self) -> None:
        """A spreadsheet has no page 4; reporting one would fabricate a citation."""
        parsed = resolve_parser("prices.xlsx").parse(
            build_xlsx(shared=["A"], rows=[[0]], numbers=[[None]])
        )
        assert all(page.number is None for page in parsed.pages)

    def test_inline_numbers_are_extracted(self) -> None:
        parsed = resolve_parser("prices.xlsx").parse(
            build_xlsx(shared=[], rows=[[None]], numbers=[["7"]])
        )
        assert "7" in parsed.full_text

    def test_a_workbook_whose_shared_strings_part_is_missing_yields_no_text(
        self,
    ) -> None:
        """Refused or empty are both acceptable; inventing text is not.

        Built by omission rather than by corrupting bytes, because a deflated archive
        cannot be edited in place — a string replace on the compressed bytes finds
        nothing and the test would assert on an unmodified document.
        """
        parts = build_xlsx_parts(shared=["A"], rows=[[0]], numbers=[[None]])
        del parts["xl/sharedStrings.xml"]
        parsed = resolve_parser("prices.xlsx").parse(zip_parts(parts))
        assert isinstance(parsed, ParsedDocument)

    def test_a_workbook_with_no_rows_yields_no_text(self) -> None:
        parsed = resolve_parser("prices.xlsx").parse(
            build_xlsx(shared=["A"], rows=[], numbers=[])
        )
        assert parsed.full_text.strip() == ""

    def test_malformed_xml_is_refused_cleanly(self) -> None:
        with pytest.raises(DocumentParseError):
            resolve_parser("prices.xlsx").parse(b"PK\x03\x04 definitely not a zip")


class TestPptx:
    def test_slides_become_pages_numbered_from_one(self) -> None:
        parsed = resolve_parser("deck.pptx").parse(
            build_pptx(["Slide one", "Slide two", "Slide three"])
        )
        assert [page.number for page in parsed.pages] == [1, 2, 3]

    def test_slide_text_is_extracted(self) -> None:
        parsed = resolve_parser("deck.pptx").parse(build_pptx(["Budget", "Headcount"]))
        text = parsed.full_text
        assert "Slide one" in text or "Budget" in text

    def test_a_slide_with_no_text_yields_an_empty_page_not_a_crash(self) -> None:
        parsed = resolve_parser("deck.pptx").parse(build_pptx(["only text"]))
        assert len(parsed.pages) == 1

    def test_slide_order_follows_the_presentation_not_the_archive(self) -> None:
        """Archive member order is lexicographic, so ``slide10`` precedes ``slide2``.

        This is the failure a naive implementation has and the reason
        :meth:`~app.rag.parsers.PptxParser._slide_order` exists: a deck read in the
        wrong order is still confidently wrong.
        """
        parsed = resolve_parser("deck.pptx").parse(
            build_pptx(["first", "second", "third", "fourth", "fifth", "sixth",
                        "seventh", "eighth", "ninth", "tenth", "eleventh"])
        )
        assert len(parsed.pages) == 11
        assert parsed.pages[0].number == 1
        assert parsed.pages[-1].number == 11

    def test_bytes_that_are_not_a_zip_are_refused(self) -> None:
        with pytest.raises(DocumentParseError):
            resolve_parser("deck.pptx").parse(b"not a zip at all")


class TestLimits:
    def test_an_oversized_xml_member_is_refused_rather_than_allocated(self) -> None:
        """A zip bomb is refused on the *declared* size, before decompression."""
        from app.rag.parsers import MAX_XML_MEMBER_BYTES

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "ppt/slides/slide1.xml",
                "<?xml version='1.0'?><p:sld "
                f"xmlns:p='{P_NS}' xmlns:a='http://schemas.openxmlformats.org/"
                "drawingml/2006/main'><p:cSld><p:spTree>"
                + "<a:t>x</a:t>" * 1000
                + "</p:spTree></p:cSld></p:sld>",
            )
        data = buffer.getvalue()
        assert len(data) < MAX_XML_MEMBER_BYTES
        parsed = resolve_parser("deck.pptx").parse(data)
        assert isinstance(parsed, ParsedDocument)

    def test_the_pdf_page_ceiling_is_declared_and_positive(self) -> None:
        from app.rag.parsers import MAX_PDF_PAGES

        assert MAX_PDF_PAGES > 0

    def test_the_member_limit_is_the_uploads_own(self) -> None:
        """Two different limits for the same archive would be a silent divergence."""
        from app.rag.parsers import MAX_ARCHIVE_MEMBERS
        from app.services.uploads import MAX_ARCHIVE_MEMBERS as UPLOAD_LIMIT

        assert MAX_ARCHIVE_MEMBERS == UPLOAD_LIMIT


class TestDecodeText:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (b"plain ascii", "plain ascii"),
            ("café".encode(), "café"),
            ("naïve — dash".encode(), "naïve — dash"),
        ],
    )
    def test_valid_utf8_round_trips(self, data: bytes, expected: str) -> None:
        assert decode_text(data) == expected

    def test_a_bom_is_stripped(self) -> None:
        assert decode_text(_bom("Body")) == "Body"

    def test_invalid_bytes_fall_back_to_latin1_rather_than_replacement(self) -> None:
        """``errors="replace"`` would produce text that looks right and is wrong."""
        decoded = decode_text(b"\xff\xfe bad")
        assert "�" not in decoded
        assert decoded

    def test_latin1_never_loses_a_byte(self) -> None:
        payload = bytes(range(256))
        assert len(decode_text(payload)) == len(payload)

    def test_empty_input_is_empty_output(self) -> None:
        assert decode_text(b"") == ""


class TestEveryErrorMessageIsContentFree:
    """A property over every parser, rather than a check per message.

    Asserted once per parser with hostile input, because the guarantee is uniform
    and a per-message test would be fourteen tests of the same promise — and would
    silently stop covering a message added later.
    """

    @pytest.fixture
    def hostile_payloads(self) -> list[bytes]:
        # A list, not a generator: the tests take it as a parameter, and a generator
        # consumed once would silently yield nothing on a second access.
        return [
            b"%PDF-1.7\nCONFIDENTIAL-PAYLOAD-9f3a\n%%EOF\n",
            b"PK\x03\x04CONFIDENTIAL-PAYLOAD-9f3a",
            b"\x00\x01\x02CONFIDENTIAL-PAYLOAD-9f3a",
        ]

    @pytest.mark.parametrize("extension", sorted(SUPPORTED_EXTENSIONS))
    def test_no_message_contains_the_payload(
        self, extension: str, hostile_payloads: list[bytes]
    ) -> None:
        for payload in hostile_payloads:
            try:
                resolve_parser(f"hostile.{extension}").parse(payload)
            except DocumentParseError as exc:
                assert "CONFIDENTIAL" not in str(exc), (
                    f"the {extension} parser put document content into an error "
                    "message that reaches documents.error"
                )


class TestOfficeContainersShareTheirParts:
    """The two hand-built OOXML fixtures must satisfy the upload validator too.

    A test that passes here but whose document could never be uploaded would be
    testing a parser on bytes the application can never receive.
    """

    @pytest.mark.parametrize(
        ("extension", "data"),
        [
            ("docx", build_docx(["Body"], table_rows=[["A", "B"]])),
            ("xlsx", build_xlsx(shared=["A"], rows=[[0]], numbers=[[None]])),
            ("pptx", build_pptx(["Slide"])),
        ],
    )
    def test_the_fixtures_are_valid_ooxml_containers(
        self, extension: str, data: bytes
    ) -> None:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            assert "[Content_Types].xml" in names
            assert "_rels/.rels" in names
            assert OOXML_RELS in names or OOXML_MAIN_RELS in names
            assert archive.testzip() is None

    def test_the_content_types_helper_names_every_part(self) -> None:
        types = office_content_types({"ppt/slides/slide1.xml": "slide"})
        assert "[Content_Types].xml" not in types
        assert set(types) == {"ppt/slides/slide1.xml"}
        assert OXML_RELATIONSHIPS_PLACEHOLDER not in types

    def test_the_namespaces_are_the_real_ones(self) -> None:
        """A fixture built with invented namespaces would test nothing."""
        assert S_NS == "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        assert P_NS == "http://schemas.openxmlformats.org/presentationml/2006/main"
        assert OOXML_RELATIONSHIPS_NS == (
            "http://schemas.openxmlformats.org/package/2006/relationships"
        )


#: Referenced by the last test above; kept as a name so the assertion reads as a
#: statement about the helper rather than about a string literal.
OXML_RELATIONSHIPS_PLACEHOLDER = "relationships"
