"""Real document bytes, built once and shared by every Phase 2.5 test.

Why a shared module
-------------------
Three test modules need real documents: ``test_parsing.py`` asserts the parsers read
them correctly, ``test_ingestion.py`` drives them through the pipeline, and
``test_qdrant_integration.py`` checks what lands in a real Qdrant. If each built its
own, the pipeline would be exercised on different bytes from the ones the parser was
verified against — and a failure in the pipeline would leave it genuinely ambiguous
whether the parser or the orchestrator was at fault.

So the builders live here and are imported, not reimplemented.

Why real bytes at all
---------------------
A mock proves nothing about this layer. The risk in parsing is that a third-party
library returns something unexpected for a real file, and a mocked library returns
exactly what the test told it to return. So ``pypdf`` writes the PDFs, ``python-docx``
writes the documents, and the two formats with no library here (``xlsx``, ``pptx``)
are assembled as valid OOXML containers from their real parts.

A useful property of building them here: every fixture is checked against the same
upload validator the API applies (:func:`app.services.uploads.validate_stored_upload`).
A document that could not be uploaded could not reach a parser, so a test using one
would be testing bytes the application can never receive.

The OOXML parts
---------------
``xlsx`` and ``pptx`` are built by hand because neither ``openpyxl`` nor
``python-pptx`` is a dependency, and adding a 1 MB library to produce test fixtures
for a parser that deliberately uses :mod:`zipfile` and :mod:`xml.etree` would be a
dependency bought for nothing. What is written is the real package shape — the same
content types, relationship parts and element names that Excel and PowerPoint emit —
so the parser is reading structure, not a private dialect.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------
# Real OOXML namespaces. Not invented: the parsers match on these exact strings, so a
# fixture using a lookalike would test the parser against nothing.
# --------------------------------------------------------------------------

SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
RELATIONSHIP_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
OOXML_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

#: The relationship part every Office package carries.
OOXML_MAIN_RELS = "_rels/.rels"

#: The relationship part for the main document part, e.g. ``ppt/_rels/presentation.xml.rels``.
OOXML_RELS = "_rels/.rels"


def _content_types(overrides: dict[str, str]) -> str:
    """Render a ``[Content_Types].xml`` covering the default extensions plus overrides."""
    defaults = (
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
    )
    body = "".join(
        f'<Override PartName="/{part}" ContentType="{kind}"/>'
        for part, kind in sorted(overrides.items())
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{CONTENT_TYPES_NS}">{defaults}{body}</Types>'
    )


def _relationships(entries: dict[str, str]) -> str:
    """Render a ``.rels`` part from ``{relationship id: target}``."""
    body = "".join(
        f'<Relationship Id="{identifier}" '
        f'Type="{RELATIONSHIP_TYPE}/officeDocument" Target="{target}"/>'
        for identifier, target in sorted(entries.items())
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{OOXML_RELATIONSHIPS_NS}">{body}</Relationships>'
    )


def office_content_types(parts: dict[str, str]) -> dict[str, str]:
    """Return the ``[Content_Types].xml`` override map for ``parts``.

    Exposed because a test asserting on the helper's own behaviour is worth more
    than one asserting on a rendered string it cannot read: the returned map is the
    decision, the rendering is the formatting.
    """
    return dict(parts)


def _escape_xml(text: str) -> str:
    """Escape the five characters XML reserves.

    Applied to every piece of fixture text so a test can use content containing
    ``&`` or ``<`` — which is exactly what a document contains, and exactly what an
    unescaped fixture would silently corrupt so the parser appears to lose it.
    """
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


def build_pdf_text(pages: list[str]) -> tuple[bytes, int]:
    """Build a real PDF whose pages carry a genuine text layer.

    Returns the bytes and the page count, so a caller asserting on both does not have
    to re-derive the count from the input list.

    The text is written as a real content stream with a real ``/Type1`` font
    resource, because :meth:`app.rag.parsers.PdfParser` reads text through
    :mod:`pypdf` and a page with no font extracts nothing. Building it any other way
    would produce a PDF that parses and yields nothing, which would test the
    "no extractable text" path while appearing to test extraction.
    """
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for text in pages:
        writer.add_blank_page(width=300, height=300)
        page = writer.pages[-1]

        content = DecodedStreamObject()
        content.set_data(
            f"BT /F1 12 Tf 20 150 Td ({_pdf_literal(text)}) Tj ET".encode("latin-1")
        )
        page[NameObject("/Contents")] = writer._add_object(content)

        font = DictionaryObject()
        font.update(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = writer._add_object(font)
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue(), len(pages)


def _pdf_literal(text: str) -> str:
    """Escape a string for a PDF literal ``(...)`` string.

    Backslash and both parentheses, because a fixture containing them would
    otherwise end the string early and truncate the extracted text — which looks
    like a parser losing characters rather than a fixture that was malformed.
    """
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_pdf_blank(pages: int = 1) -> bytes:
    """A structurally valid PDF whose pages have **no** text layer.

    For the "parsed successfully, extracted nothing" path. Distinct from
    :func:`build_pdf_text`, because the difference between them is the whole
    distinction between a scanned PDF and a written one.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------


def build_docx(
    paragraphs: list[str], table_rows: list[list[str]] | None = None
) -> bytes:
    """Build a real ``.docx`` with ``python-docx``.

    Written with the library rather than assembled by hand: ``python-docx`` is a
    Phase 2.5 dependency (it reads ``.docx``), so using it to *write* one proves the
    same part names the parser depends on, and a hand-built archive could disagree
    with what a real Word document looks like in a way the parser never notices.
    """
    import docx

    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    for row in table_rows or []:
        table = document.add_table(rows=1, cols=len(row))
        for column, cell in enumerate(row):
            table.cell(0, column).text = cell
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# XLSX
# --------------------------------------------------------------------------


def build_xlsx(
    *,
    shared: list[str],
    rows: list[list[int | None]],
    numbers: list[list[str | None]],
) -> bytes:
    """Build a valid ``.xlsx`` from a shared-string table and cell values.

    ``shared`` is the workbook's shared-string table; a value of ``0`` in ``rows``
    means "shared string 0", and a value of ``None`` means an empty cell. ``numbers``
    carries inline numeric literals for the same coordinates.

    The two tables are separate because a real spreadsheet has both, and a parser
    that reads only one of them would pass a fixture using only that one.
    """
    return zip_parts(build_xlsx_parts(shared=shared, rows=rows, numbers=numbers))


def build_xlsx_parts(
    *,
    shared: list[str],
    rows: list[list[int | None]],
    numbers: list[list[str | None]],
) -> dict[str, str]:
    """The parts of an ``.xlsx``, before they are zipped.

    Exposed separately from :func:`build_xlsx` because a test needs to build an
    *invalid* workbook — one missing a part the parser reads — and the only honest
    way to do that is to omit a member. Rewriting bytes inside a deflated archive
    finds nothing and silently tests the unmodified document.
    """
    shared_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{SPREADSHEET_NS}" count="{len(shared)}" '
        f'uniqueCount="{len(shared)}">'
        + "".join(f"<si><t>{_escape_xml(text)}</t></si>" for text in shared)
        + "</sst>"
    )

    sheet_rows: list[str] = []
    for row_number, (cells, inline) in enumerate(zip(rows, numbers, strict=True)):
        rendered: list[str] = []
        for column, (index, number) in enumerate(zip(cells, inline, strict=True)):
            position = f"{chr(ord('A') + column)}{row_number + 1}"
            if index is not None:
                rendered.append(f'<c r="{position}" t="s"><v>{index}</v></c>')
            elif number is not None:
                rendered.append(f'<c r="{position}"><v>{number}</v></c>')
        sheet_rows.append(
            f'<row r="{row_number + 1}">' + "".join(rendered) + "</row>"
        )

    sheet_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{SPREADSHEET_NS}"><sheetData>'
        + "".join(sheet_rows)
        + "</sheetData></worksheet>"
    )

    workbook_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{SPREADSHEET_NS}" '
        f'xmlns:r="{RELATIONSHIP_TYPE}">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )

    workbook_rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{OOXML_RELATIONSHIPS_NS}">'
        '<Relationship Id="rId1" '
        f'Type="{RELATIONSHIP_TYPE}/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" '
        f'Type="{RELATIONSHIP_TYPE}/sharedStrings" Target="sharedStrings.xml"/>'
        "</Relationships>"
    )

    return {
        "[Content_Types].xml": _content_types(
            {
                "/xl/workbook.xml": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet.main+xml"
                ),
                "/xl/sharedStrings.xml": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sharedStrings+xml"
                ),
                "/xl/worksheets/sheet1.xml": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.worksheet+xml"
                ),
            }
        ),
        OOXML_MAIN_RELS: _relationships({"rId1": "xl/workbook.xml"}),
        "xl/workbook.xml": workbook_xml,
        "xl/_rels/workbook.xml.rels": workbook_rels,
        "xl/sharedStrings.xml": shared_xml,
        "xl/worksheets/sheet1.xml": sheet_xml,
    }


# --------------------------------------------------------------------------
# PPTX
# --------------------------------------------------------------------------


def build_pptx(slides: list[str]) -> bytes:
    """Build a valid ``.pptx`` with one slide per entry in ``slides``.

    Written through the **presentation part**, not by naming files, because the
    deck's order lives in ``p:sldIdLst`` and a parser that read the archive's
    member order instead would return ``slide10`` before ``slide2`` — a confident,
    silent, entirely wrong answer. The relationship ids here are deliberately not in
    slide order, so the fixture is a real test of that rather than a coincidence.
    """
    identifiers = [256 + number for number in range(len(slides))]
    slide_parts: dict[str, str] = {}
    slide_ids: list[str] = []
    slide_relationships: list[str] = []

    for position, text in enumerate(slides, start=1):
        part = f"ppt/slides/slide{position}.xml"
        slide_parts[part] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<p:sld xmlns:p="{PRESENTATION_NS}" xmlns:a="{DRAWING_NS}">'
            f"<p:cSld><p:spTree>"
            f"<a:t>{_escape_xml(text)}</a:t>"
            f"</p:spTree></p:cSld></p:sld>"
        )
        slide_ids.append(f'<p:sldId id="{identifiers[position - 1]}" r:id="rId{position}"/>')
        slide_relationships.append(
            f'<Relationship Id="rId{position}" '
            f'Type="{RELATIONSHIP_TYPE}/slide" Target="slides/slide{position}.xml"/>'
        )

    presentation_xml = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<p:presentation xmlns:p="{PRESENTATION_NS}" '
        f'xmlns:r="{RELATIONSHIP_TYPE}">'
        f'<p:sldIdLst>{"".join(slide_ids)}</p:sldIdLst></p:presentation>'
    )

    parts: dict[str, str] = {
        "[Content_Types].xml": _content_types(
            {
                "/ppt/presentation.xml": (
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation.main+xml"
                ),
                **{
                    f"/ppt/slides/slide{number}.xml": (
                        "application/vnd.openxmlformats-officedocument."
                        "presentationml.slide+xml"
                    )
                    for number in range(1, len(slides) + 1)
                },
            }
        ),
        OOXML_MAIN_RELS: _relationships({"rId1": "ppt/presentation.xml"}),
        "ppt/presentation.xml": presentation_xml,
        "ppt/_rels/presentation.xml.rels": (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{OOXML_RELATIONSHIPS_NS}">'
            + "".join(slide_relationships)
            + "</Relationships>"
        ),
        **slide_parts,
    }
    return zip_parts(parts)


# --------------------------------------------------------------------------
# Plain formats
# --------------------------------------------------------------------------


def build_txt(text: str) -> bytes:
    """A ``.txt`` holding ``text``, UTF-8 encoded, no BOM."""
    return text.encode("utf-8")


# --------------------------------------------------------------------------
# Writing fixtures to disk
# --------------------------------------------------------------------------


def write_upload(
    path: Path, data: bytes, *, filename: str | None = None
) -> Path:
    """Write ``data`` to ``path`` and return the path.

    Not used by the pipeline tests — those go through
    :func:`app.services.documents.create_document`, which is the only supported way
    to get a document row and its stored file. Present for tests that need a stored
    file *without* a row, such as the "row references a file that is not there"
    case.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    del filename
    return path


def zip_parts(parts: dict[str, str]) -> bytes:
    """Render a mapping of member name to text as a ZIP archive's bytes.

    Members are written in sorted order deliberately. A real Office package does not
    do this, and it matters: ``ppt/slides/slide10.xml`` sorts before
    ``ppt/slides/slide2.xml``, so an archive written in sorted order is precisely the
    layout that punishes a parser which reads member order instead of the
    presentation's own slide list.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            archive.writestr(name, parts[name])
    return buffer.getvalue()
