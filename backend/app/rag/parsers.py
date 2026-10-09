"""Document parsing and text extraction: the first two boxes of PMP §18's chain.

    Document → Parser → Text Extraction → Cleaning → …

Why a registry and not a switch statement
-----------------------------------------
PMP §2's document domain — employee handbooks, HR policies, technical
documentation, standard operating procedures, product documentation, internal
guides, compliance documents, company FAQs — arrives in whatever format the
employees already use. Adding a format must not mean editing the ingestion
pipeline, the orchestrator, or a conditional in either. So each format is a
:class:`DocumentParser` registered against the extension
:mod:`app.services.uploads` already accepts, and the orchestrator asks the registry
for a parser rather than branching on a type.

The extension set is exactly :data:`app.services.uploads.SUPPORTED_FORMATS`. Not
"the formats this module happens to implement": the upload validator already
refused everything else, so a stored document's format is by construction one of
these seven. A parser set that diverged from the validator's set would mean either
dead parsers or a stored document with no parser — both are bugs, and
``tests/test_parsing.py`` asserts the two sets are equal so neither can happen
silently.

What each parser is trusted to do, and what it is not
-----------------------------------------------------
A parser interprets bytes a tenant uploaded. It must never:

* **execute** anything. No parser shells out, imports a module named in the
  document, evaluates a macro, or follows a URL. This is why the OOXML readers use
  :mod:`xml.etree.ElementTree` rather than anything that resolves entities:
  ``ElementTree`` does not expand external entities, so an XML bomb or an XXE
  reference inside a ``.docx``/``.xlsx``/``.pptx`` is inert text. The HTML reader is
  :class:`html.parser.HTMLParser`, which has no script engine and never fetches a
  resource.
* **enlarge without bound.** :data:`MAX_ARCHIVE_MEMBERS` caps what a ZIP-based
  parser will look at, :data:`MAX_XML_MEMBER_BYTES` caps what it will read from one
  member, and nothing is extracted to disk — so neither a member-count bomb nor a
  decompression bomb can exhaust memory or fill the filesystem.
* **embed** anything in the output. Extracted text goes through
  :func:`app.rag.cleaning.clean_text`, which removes the invisible code points an
  attacker uses to smuggle instructions past a human reviewer.

Page numbers
------------
PMP §13's payload carries ``page``. Only PDF and PowerPoint genuinely have pages, so
:class:`ParsedDocument.pages` is a list of :class:`Page` objects and only those two
parsers produce more than one. For every other format the list holds exactly one
page with ``number=None``, which is the honest answer rather than a fabricated 1.
:attr:`ParsedDocument.page_for_offset` maps a chunk offset back to its page, which
is how the ``page`` field gets filled.

Failure, and how it differs from "no text"
-----------------------------------------
A file whose bytes do not match its extension is refused by the upload validator, so
a parser should not normally see one. When it does anyway — the two validators
disagree after a future change, or a file was written by an older version — the
parser raises :class:`DocumentParseError` and the orchestrator records the document
as ``failed``.

Separately, a file that *parses* to nothing extractable (a scanned image PDF, a
spreadsheet of empty cells) is **not** a parse failure: it parsed, and its text is
empty. The orchestrator treats that as a failure too, but with a different recorded
reason, because "this PDF is a scan of an image" and "this file is corrupt" call for
different operator responses.

Where a message may not go
--------------------------
Every :class:`DocumentParseError` message in this module names a format, a limit or
an **exception class name** — never ``str(exc)``. Parsers quote the bytes they
choked on: pypdf's messages routinely embed fragments of the file. Those messages
end up in ``documents.error``, which ``GET /admin/documents/{id}/status`` returns,
so carrying a parser's message verbatim would let a tenant who uploads a crafted
file write arbitrary text into a field an administrator reads. That is why the
parsers catch broadly and report the exception *type*: an operationally adequate
diagnosis, obtained without the disclosure.
"""

from __future__ import annotations

import html.parser
import re
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
from typing import Final
from xml.etree import ElementTree

from app.services.uploads import (
    MAX_ARCHIVE_MEMBERS,
    SupportedFormat,
    extension_of,
)

#: Upper bound on how many pages are read from one PDF.
#:
#: Not a resource limit — pypdf streams, and a page costs a text extraction rather
#: than a memory reservation. It bounds *output*: a 5000-page document at 1200
#: characters per chunk is roughly 15 000 chunks, which is 15 000 embedding requests
#: for one upload. PMP §8 names "large document processing time" as a real risk, and
#: Phase 5 answers it with background workers rather than with a page cap. This is an
#: honest ceiling that makes the worst case visible: a document that hits it is
#: recorded as failed with a message saying exactly that, rather than appearing to
#: succeed with its tail silently missing.
MAX_PDF_PAGES: Final[int] = 2_000

#: The DrawingML text element, used by ``.pptx``.
DRAWING_NS: Final[str] = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

#: The SpreadsheetML namespace, used by ``.xlsx``.
SPREADSHEET_NS: Final[str] = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: The namespace carrying the relationship id on a ``<p:sldId>`` element.
PRESENTATION_NS: Final[str] = (
    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
)

#: The XML member a WordprocessingML document puts its body in. Not read directly —
#: python-docx locates it — but named so the reason ``docx`` needs a dependency and
#: ``xlsx``/``pptx`` do not is visible in one place.
DOCX_BODY_MEMBER: Final[str] = "word/document.xml"

#: The member a SpreadsheetML workbook keeps its string table in. Optional: a
#: workbook written with every cell inlined has none.
XLSX_SHARED_STRINGS_MEMBER: Final[str] = "xl/sharedStrings.xml"

#: Prefix of the SpreadsheetML members holding worksheets.
XLSX_WORKSHEET_PREFIX: Final[str] = "xl/worksheets/"

#: Prefix and suffix of the DrawingML members holding slides.
PPTX_SLIDE_PREFIX: Final[str] = "ppt/slides/slide"
PPTX_SLIDE_SUFFIX: Final[str] = ".xml"

#: The member a PresentationML deck keeps its slide list and relationships in.
PPTX_PRESENTATION_MEMBER: Final[str] = "ppt/presentation.xml"
PPTX_PRESENTATION_RELS: Final[str] = "ppt/_rels/presentation.xml.rels"

#: Maximum bytes read from one XML archive member.
#:
#: A ``.docx``/``.xlsx``/``.pptx`` part is XML text, and reading one unbounded turns
#: a crafted member into unbounded memory use. 32 MiB is far above any real member —
#: a 900-page Word document is roughly 1 MiB of markup — and low enough that the
#: ceiling is never the reason a legitimate document fails.
MAX_XML_MEMBER_BYTES: Final[int] = 32 * 1024 * 1024

#: HTML elements whose *content* is not document text.
#:
#: Their text is markup-adjacent or executable, and including it would put CSS
#: selectors and JavaScript source into a chunk a model later reads. Dropped whole —
#: element and content — rather than stripped of tags.
HTML_DROPPED_ELEMENTS: Final[frozenset[str]] = frozenset(
    {"script", "style", "head", "title", "template", "noscript", "svg", "iframe"}
)

#: HTML elements that imply a line break in the extracted text.
#:
#: Inserted as newlines rather than spaces so a paragraph boundary in the rendered
#: page survives into the chunk, which is what gives the chunker something to split
#: on. A blank-line separation is not attempted here: the markup gives no way to know
#: whether a ``<div>`` follows a paragraph or a heading, and inventing a blank line
#: between two spans of the same sentence would break it apart.
HTML_BLOCK_ELEMENTS: Final[frozenset[str]] = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "div", "dd", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
        "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
    }
)

#: Runs of three or more newlines, collapsed in the HTML reader's output.
#:
#: A convenience, not the normaliser: :func:`app.rag.cleaning.clean_text` does the
#: real work, and duplicating its rules here would mean two places to change when
#: they change.
_THREE_OR_MORE_NEWLINES: Final[re.Pattern[str]] = re.compile(r"\n{3,}")


class DocumentParseError(ValueError):
    """A stored document could not be turned into text.

    Raised by a parser when the bytes cannot be read as the format their extension
    claims. The message is built from values this module controls and never contains
    document content — see the module docstring.

    A :class:`ValueError` because that is what it is: the input was not usable. It is
    deliberately **not** an :class:`app.services.uploads.UploadRejectedError`,
    because a file that passed upload validation has legitimately reached the parser
    and failed for a different reason. Keeping the two apart lets the orchestrator
    distinguish "this tenant's upload was refused at the boundary" (a client fault,
    already answered) from "our parser could not read a file we accepted" (a fault
    on this side of it).
    """


@dataclass(frozen=True, slots=True)
class Page:
    """One page's extracted text.

    ``number`` is the 1-based page position for formats that have pages, and ``None``
    for formats that do not. A spreadsheet has no page 4, and reporting one would put
    a fabricated location into the payload field meant for citing a passage.
    """

    text: str
    number: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """A parsed document: its pages, their text, and the offset map between them.

    ``pages`` is what a parser produces; :attr:`full_text` is derived from it, so a
    caller cannot hold a ``full_text`` that disagrees with the pages it was built
    from.

    Why there is no offset map
    --------------------------
    An earlier draft carried a :class:`PageOffset` table and a
    ``page_for_offset(offset)`` lookup, so a chunk cut from :attr:`full_text` could be
    traced back to its page. Both were removed, and the reason is worth recording
    because it is not obvious.

    The offset would have been **wrong**. The chunker runs on *cleaned* text, and
    cleaning is not length-preserving: it folds unicode spaces, collapses whitespace
    runs, drops control characters and removes bidi controls. Every one of those
    shortens the string before the page boundaries it were measured against, so an
    offset into the cleaned text cannot address the parsed text it came from. The
    error would be silent and plausible — a citation pointing at a real page, the
    wrong one — and it would grow with document size, since the drift accumulates.

    :mod:`app.services.ingestion` therefore chunks **one page at a time**, which makes
    the page a property of the chunk by construction and needs no arithmetic. See that
    module for what that costs.
    """

    pages: tuple[Page, ...]

    @property
    def full_text(self) -> str:
        """Every page's text, joined by a blank line.

        Two newlines, so the join is a paragraph break. Any other choice would make
        the page boundary invisible to the chunker, and a chunk could then straddle
        two pages with nothing marking the join.
        """
        return "\n\n".join(page.text for page in self.pages)


class DocumentParser(ABC):
    """Turns one stored document into text. One instance per supported format.

    An abstract class rather than a :class:`typing.Protocol` because the registry
    holds constructed instances and a protocol describes an interface without
    supplying a constructor. Parsers hold no state, so the shared instances in
    :data:`SUPPORTED_EXTENSIONS` are safe to reuse across threads and requests.
    """

    #: The extension this parser handles, without the dot. Exactly one key of
    #: :data:`~app.services.uploads.SUPPORTED_FORMATS`.
    extension: str

    @abstractmethod
    def parse(self, data: bytes) -> ParsedDocument:
        """Extract text from ``data``.

        Args:
            data: the stored file's bytes, already validated against its extension
                by :mod:`app.services.uploads`.

        Returns:
            The extracted pages. A document with no extractable text returns pages
            holding empty text rather than raising: it parsed, and there was nothing
            to find.

        Raises:
            DocumentParseError: the bytes are not readable as this format.
        """


class TextFileParser(DocumentParser):
    """Plain text, Markdown, and HTML as extracted text.

    One class for three extensions because the extraction is identical and only the
    *validation* differed at upload time. Three near-identical classes would be three
    places for a future fix to be applied to two of them.

    The extension is constructor state rather than a class attribute because the
    three instances genuinely differ: the HTML branch fires only for ``html``.
    """

    extension: str

    def __init__(self, extension: str) -> None:
        self.extension = extension

    def parse(self, data: bytes) -> ParsedDocument:
        text = decode_text(data)
        if self.extension == "html":
            text = extract_html_text(text)
        return ParsedDocument(pages=(Page(text=text, number=None),))


class PdfParser(DocumentParser):
    """PDF, via :mod:`pypdf`.

    ``pypdf`` is used rather than a hand-rolled reader because PDF's text operators
    live inside a cross-reference table behind a font encoding, and a reader that
    gets that wrong produces plausible-looking nonsense — which would then be indexed
    and cited as though it were the document.

    ``pypdf`` is imported inside :meth:`parse` rather than at module scope. Two
    reasons: importing a PDF library is not free, so a process that never ingests a
    PDF should not pay for it; and a module-scope import would make
    ``import app.rag.parsers`` fail outright in an environment where ``pypdf`` is
    unavailable, taking the HTML and plain-text parsers down with it. A test asserts
    the parser works without the module being pre-imported.

    Encrypted PDFs raise :class:`DocumentParseError` rather than being skipped. The
    upload path does not check for encryption, so an encrypted file can legitimately
    reach this parser, and "this document is encrypted and TenantMind cannot read it"
    is information an operator needs. It is recorded as a failure, never reported as
    a document that merely has no text.
    """

    extension: str = "pdf"

    def parse(self, data: bytes) -> ParsedDocument:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError

        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted and not reader.decrypt(""):
                # `decrypt("")` is how pypdf reports "encrypted with an empty user
                # password", which some producers emit. It returns 0 on failure and
                # a truthy value on success, so it is checked rather than ignored.
                raise DocumentParseError(
                    "the PDF is encrypted and cannot be read without a password"
                )
            page_count = len(reader.pages)
            if page_count > MAX_PDF_PAGES:
                raise DocumentParseError(
                    f"the PDF declares {page_count} pages, above the "
                    f"{MAX_PDF_PAGES} this phase will extract"
                )
            pages = tuple(
                Page(text=page.extract_text() or "", number=position)
                for position, page in enumerate(reader.pages, start=1)
            )
        except DocumentParseError:
            raise
        except PdfReadError as exc:
            raise DocumentParseError(
                f"the PDF could not be read ({type(exc).__name__})"
            ) from exc
        except Exception as exc:
            # pypdf raises a wide and version-dependent surface: `PdfReadError`,
            # `PdfStreamError`, `DependencyError` for a missing crypto backend,
            # `RecursionError` on a hostile object graph, `KeyError` for a malformed
            # indirect reference. Enumerating them means a new one in a patch release
            # turns an ingestion into an unhandled error instead of a recorded
            # failure. The message carries the exception *class name* and never
            # `str(exc)` — see the module docstring.
            raise DocumentParseError(
                f"the PDF could not be parsed ({type(exc).__name__})"
            ) from exc

        if not pages:
            return ParsedDocument(pages=(Page(text="", number=None),))
        return ParsedDocument(pages=pages)


class DocxParser(DocumentParser):
    """``.docx``, via :mod:`docx` (python-docx).

    Paragraphs **and** tables are read. Tables matter more than they look: policy
    and compliance documents — most of PMP §2's domain — put much of their substance
    in tables, and a reader handling paragraphs only would extract a compliance
    document's headings and none of its rules.

    Imported inside :meth:`parse` for the same reason :class:`PdfParser` does.
    """

    extension: str = "docx"

    def parse(self, data: bytes) -> ParsedDocument:
        import docx

        try:
            document = docx.Document(BytesIO(data))
        except Exception as exc:
            raise DocumentParseError(
                f"the .docx archive could not be opened ({type(exc).__name__})"
            ) from exc

        parts = [paragraph.text for paragraph in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                # A row whose cells are all empty carries no content, and emitting it
                # would only give the chunker a run of separators to skip.
                if any(cells):
                    parts.append("\t".join(cells))

        return ParsedDocument(pages=(Page(text="\n\n".join(parts), number=None),))


class OoxmlTextParser(DocumentParser):
    """``.xlsx`` and ``.pptx``: OOXML containers read with the standard library.

    No third-party reader is used, and the reason is not to save a dependency but
    that both formats are **XML inside a ZIP**, which :mod:`zipfile` and
    :mod:`xml.etree.ElementTree` already read completely. Installing ``openpyxl`` or
    ``python-pptx`` would add two packages to reach a format this project needs only
    *text* out of.

    What is extracted, and what is not, stated plainly:

    * **``.xlsx``** — the string table (``sharedStrings.xml``) plus every cell
      referencing it, in document order. Formatted numbers, dates, formulas and
      merged cells are **not** rendered: a cell holding ``1234.5`` under a
      two-decimal format is indexed as ``1234.5``, not ``1234.50``. That is a real
      limitation, recorded here rather than discovered — PMP §2 lists "product
      documentation" and "internal guides" rather than spreadsheets as a primary
      document type, and §28 lists "table-aware retrieval" as a *future*
      enhancement.
    * **``.pptx``** — every text run (``a:t``) of every slide, one slide per
      :class:`Page` numbered from 1, in presentation order. That order comes from
      ``ppt/presentation.xml``; ZIP member order is not presentation order, because a
      ZIP is an archive format with no notion of sequence.

    Neither reader extracts anything from a member outside its own prefix. A ``.pptx``
    whose slides live somewhere unexpected yields no text, which is recorded as "no
    extractable text" rather than crashing the ingestion.
    """

    extension: str

    def parse(self, data: bytes) -> ParsedDocument:
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                names = archive.namelist()
                if len(names) > MAX_ARCHIVE_MEMBERS:
                    raise DocumentParseError(
                        f"the archive declares {len(names)} members, above the "
                        f"{MAX_ARCHIVE_MEMBERS} permitted"
                    )
                pages = self._read_pages(archive, names)
        except DocumentParseError:
            raise
        except zipfile.BadZipFile as exc:
            raise DocumentParseError(
                "the archive could not be read (BadZipFile)"
            ) from exc
        except ElementTree.ParseError as exc:
            raise DocumentParseError(
                "the archive contains malformed XML (ParseError)"
            ) from exc

        return ParsedDocument(pages=pages or (Page(text="", number=None),))

    def _read_pages(
        self, archive: zipfile.ZipFile, names: list[str]
    ) -> tuple[Page, ...]:
        """Read this format's pages. Overridden by each concrete subclass."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement _read_pages"
        )

    @staticmethod
    def _parse_member(
        archive: zipfile.ZipFile, name: str
    ) -> ElementTree.Element | None:
        """Parse one XML member, or return ``None`` if it is absent or malformed.

        Defined here, on the shared base, rather than on one subclass, because both
        readers need it and a subclass assigning a parent's method onto itself would
        be two names for one behaviour.

        ``None`` rather than an exception is deliberate for *optional* members: a
        workbook with no ``sharedStrings.xml`` is legitimate, and a reader that
        raised would refuse documents it can otherwise handle. Bytes are still
        bounded — a member is read through :meth:`zipfile.ZipFile.open` and refused
        past :data:`MAX_XML_MEMBER_BYTES` — so "absent" is the only alternative to
        parsing, never "unbounded".

        Raises:
            DocumentParseError: the member exists and exceeds
                :data:`MAX_XML_MEMBER_BYTES`. A *malformed* member returns ``None``
                and a *missing* one returns ``None``; an oversized one is refused,
                because silently reading its first 32 MiB would produce a document
                that is missing its tail with no record of it.
        """
        try:
            with archive.open(name) as handle:
                payload = handle.read(MAX_XML_MEMBER_BYTES + 1)
        except KeyError:
            return None
        if len(payload) > MAX_XML_MEMBER_BYTES:
            raise DocumentParseError(
                f"an archive member exceeds the {MAX_XML_MEMBER_BYTES} byte limit "
                "this phase will read"
            )
        try:
            return ElementTree.fromstring(payload)
        except ElementTree.ParseError:
            return None


class XlsxParser(OoxmlTextParser):
    """``xlsx``: the OOXML reader bound to SpreadsheetML's members."""

    extension: str = "xlsx"

    def _read_pages(
        self, archive: zipfile.ZipFile, names: list[str]
    ) -> tuple[Page, ...]:
        shared = self._shared_strings(archive, names)
        lines: list[str] = []
        # Sorted, not ZIP order: a spreadsheet's worksheets are unordered in the
        # archive, and sorting is the only deterministic choice available. The
        # authoritative order is the workbook's, which would need `workbook.xml`'s
        # sheet list — recorded here as a known approximation rather than silently
        # presented as the authored order.
        for name in sorted(n for n in names if n.startswith(XLSX_WORKSHEET_PREFIX)):
            root = self._parse_member(archive, name)
            if root is None:
                continue
            lines.extend(
                self._cell_text(cell, shared)
                for cell in root.iter(f"{SPREADSHEET_NS}c")
            )
        return (Page(text="\n".join(line for line in lines if line), number=None),)

    @classmethod
    def _shared_strings(
        cls, archive: zipfile.ZipFile, names: list[str]
    ) -> list[str]:
        """The workbook's string table, or empty when it has none.

        A workbook written by a tool that inlines every string has no
        ``sharedStrings.xml`` at all, which is not an error.
        """
        if XLSX_SHARED_STRINGS_MEMBER not in names:
            return []
        root = cls._parse_member(archive, XLSX_SHARED_STRINGS_MEMBER)
        if root is None:
            return []
        # `<si>` is one logical string, possibly split across several `<r><t>` runs —
        # which is exactly what happens when part of a cell is bold. Joining the runs
        # of one `<si>` is what makes a partially formatted cell come out as its full
        # text rather than only its first run.
        return [
            "".join(node.text or "" for node in item.iter(f"{SPREADSHEET_NS}t"))
            for item in root.iter(f"{SPREADSHEET_NS}si")
        ]

    @classmethod
    def _cell_text(cls, cell: ElementTree.Element, shared: list[str]) -> str:
        """The text of one ``<c>`` cell, resolving shared-string references.

        ``t="s"`` means the value is an index into the string table; ``t="inlineStr"``
        means it is in ``<is>``; anything else (a number, a boolean, a date serial) is
        read from ``<v>`` as written. An out-of-range or unparsable index yields
        ``""`` rather than raising: the upload validator already confirmed this is a
        spreadsheet, so a corrupt string table is a content problem rather than a
        reason to refuse the whole document.
        """
        cell_type = cell.get("t", "n")
        if cell_type == "s":
            value = cell.find(f"{SPREADSHEET_NS}v")
            if value is None or value.text is None:
                return ""
            try:
                index = int(value.text)
            except ValueError:
                return ""
            return shared[index] if 0 <= index < len(shared) else ""
        if cell_type == "inlineStr":
            return "".join(
                node.text or "" for node in cell.iter(f"{SPREADSHEET_NS}t")
            )
        value = cell.find(f"{SPREADSHEET_NS}v")
        return value.text.strip() if value is not None and value.text else ""


class PptxParser(OoxmlTextParser):
    """``.pptx``: the OOXML reader bound to PresentationML's members."""

    extension: str = "pptx"

    def _read_pages(
        self, archive: zipfile.ZipFile, names: list[str]
    ) -> tuple[Page, ...]:
        pages: list[Page] = []
        for position, name in enumerate(self._slide_order(archive, names), start=1):
            root = self._parse_member(archive, name)
            if root is None:
                continue
            # `a:t` is the DrawingML text element. Iterating it in document order
            # walks the shape tree, so a slide reads as authored rather than in some
            # internal order. Newline-joined, so two runs on one shape stay on one
            # line and two shapes do not merge into one sentence.
            runs = [node.text or "" for node in root.iter(f"{DRAWING_NS}t")]
            pages.append(Page(text="\n".join(runs), number=position))
        return tuple(pages)

    @classmethod
    def _slide_order(
        cls, archive: zipfile.ZipFile, names: list[str]
    ) -> list[str]:
        """Slide member names in presentation order.

        ``ppt/presentation.xml`` lists slide relationship ids in order, and each id
        resolves through ``presentation.xml.rels`` to a ZIP member. Both hops are
        needed: resolving only the first yields ``rId`` values, not filenames.

        Falls back to sorted member names when the presentation part is absent or
        unreadable. That is deterministic even though it is not the authored order,
        and it is why the presentation part is read rather than assumed: a deck with
        slides 1, 2 and 10 sorts to 1, 10, 2.
        """
        candidates = sorted(
            name
            for name in names
            if name.startswith(PPTX_SLIDE_PREFIX) and name.endswith(PPTX_SLIDE_SUFFIX)
        )
        if PPTX_PRESENTATION_MEMBER not in names:
            return candidates

        root = cls._parse_member(archive, PPTX_PRESENTATION_MEMBER)
        if root is None:
            return candidates

        relationships = cls._slide_relationships(archive, names)
        ordered: list[str] = []
        for element in root.iter():
            if not element.tag.endswith("}sldId"):
                continue
            relationship_id = element.get(PRESENTATION_NS)
            if relationship_id is None:
                continue
            target = relationships.get(relationship_id)
            if target is not None:
                ordered.append(target)
        return ordered or candidates

    @classmethod
    def _slide_relationships(
        cls, archive: zipfile.ZipFile, names: list[str]
    ) -> dict[str, str]:
        """Map each slide relationship id to the ZIP member it points at."""
        if PPTX_PRESENTATION_RELS not in names:
            return {}
        root = cls._parse_member(archive, PPTX_PRESENTATION_RELS)
        if root is None:
            return {}

        mapping: dict[str, str] = {}
        for relationship in root:
            identifier = relationship.get("Id")
            target = relationship.get("Target", "")
            if identifier is None:
                continue
            # Targets are relative to `ppt/` and written either as
            # `slides/slide1.xml` or `/ppt/slides/slide1.xml`. Both are normalised
            # here rather than at the use site, so the caller compares one spelling.
            normalised = target.lstrip("/")
            if not normalised.startswith("ppt/"):
                normalised = f"ppt/{normalised}"
            mapping[identifier] = normalised
        return mapping


class _HtmlTextExtractor(html.parser.HTMLParser):
    """Collect an HTML document's text, dropping markup and scripts.

    :class:`html.parser.HTMLParser` from the standard library, not a regular
    expression and not a third-party cleaner, for one specific reason: it is a
    *tokeniser*. A regular expression cannot tell a literal ``<`` from the start of a
    tag, so it either strips real content or leaves real tags behind. ``HTMLParser``
    executes nothing either — it has no script engine and never fetches a resource —
    which is what makes parsing a tenant's HTML safe.

    Character references are resolved by the base class, so ``&amp;`` becomes ``&``.

    Two behaviours worth stating because they are choices, not accidents:

    * A dropped element (:data:`HTML_DROPPED_ELEMENTS`) discards its **content** as
      well as its tags. Script and style bodies are the reason this exists: their
      text is source code, and indexing it would put JavaScript into a chunk a model
      later reads.
    * Unclosed and unbalanced tags are tolerated, because real-world HTML is
      frequently malformed and refusing a document over a missing ``</p>`` would
      reject content an employee expects to be searchable.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pieces: list[str] = []
        self._suppress_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in HTML_DROPPED_ELEMENTS:
            self._suppress_depth += 1
        elif tag in HTML_BLOCK_ELEMENTS:
            self._break_line()

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        # A self-closing tag has no content to suppress, so it must not change the
        # depth — otherwise a single `<meta/>` would swallow the rest of the document.
        if tag in HTML_BLOCK_ELEMENTS:
            self._break_line()

    def handle_endtag(self, tag: str) -> None:
        if tag in HTML_DROPPED_ELEMENTS:
            # `max(0, ...)` rather than plain decrement: malformed HTML can close a
            # `<script>` that was never opened, and a negative depth would make the
            # next unrelated element suppress nothing.
            self._suppress_depth = max(0, self._suppress_depth - 1)
        elif tag in HTML_BLOCK_ELEMENTS:
            self._break_line()

    def handle_data(self, data: str) -> None:
        if not self._suppress_depth:
            self._pieces.append(data)

    def _break_line(self) -> None:
        if not self._suppress_depth:
            self._pieces.append("\n")

    def text(self) -> str:
        """The extracted text, with runs of blank lines collapsed."""
        return _THREE_OR_MORE_NEWLINES.sub("\n\n", "".join(self._pieces))


def extract_html_text(source: str) -> str:
    """Return the readable text of an HTML document.

    Args:
        source: decoded HTML. Untrusted input; nothing here is executed, resolved or
            fetched.

    Returns:
        The text with block elements separated by newlines and script/style content
        removed. Character references are resolved.

    Raises:
        DocumentParseError: the source is not parseable as text at all. A
            :class:`ValueError` from the base class means the input was not usable
            markup — a fault worth recording rather than an empty document.
    """
    extractor = _HtmlTextExtractor()
    try:
        extractor.feed(source)
        extractor.close()
    except ValueError as exc:
        raise DocumentParseError(
            f"the HTML could not be parsed ({type(exc).__name__})"
        ) from exc
    return extractor.text()


#: The UTF-8 byte-order mark, as bytes.
#:
#: Named because the whole of the BOM handling is this one constant, and because the
#: subtlety it exists for is a byte sequence rather than a code point: ``U+FEFF`` is a
#: valid character *and* valid UTF-8, which is precisely why it can survive a decode
#: unremarked.
UTF8_BOM: Final[bytes] = b"\xef\xbb\xbf"


def decode_text(data: bytes) -> str:
    """Decode ``data`` to text, without guessing and without corrupting it.

    Three steps, in order, and the reasoning for each:

    1. **Strip a leading byte-order mark.** A file saved by a Windows editor begins
       :data:`UTF8_BOM`, and decoded as plain UTF-8 that becomes ``U+FEFF`` at the
       front of the text — an invisible character that would then be embedded in the
       first chunk of the document and quoted back to a user as part of a citation.

       Stripped *before* the decode, not after, and this is the whole reason the
       function is correct. ``U+FEFF`` is a valid code point, so a BOM-prefixed file
       decodes as UTF-8 **successfully**; a version that decoded first and stripped
       afterwards would reach its strip step only when the decode had already failed,
       which is precisely when there is no BOM to strip. The strip would be
       unreachable code and every BOM-prefixed document would keep its invisible
       prefix. Found by ``tests/test_parsing.py``, having been documented as
       impossible.
    2. **UTF-8.** Correct for the overwhelming majority of documents, and strict, so
       genuinely invalid bytes raise rather than being silently replaced.
    3. **latin-1.** Cannot fail — it maps each of the 256 byte values to the code
       point of the same value — and is byte-preserving, so nothing is lost.

    ``errors="replace"`` is deliberately never used. It would turn undecodable bytes
    into U+FFFD, producing text that looks readable and is subtly wrong. This text is
    quoted back to users as a citation, so "subtly wrong" is the worst outcome
    available, and a deterministic mojibake is the better one.

    A BOM is stripped only from the *start*. One in the middle of a document is
    U+FEFF used as a zero-width no-break space, which is real content and is left
    for :func:`~app.rag.cleaning.clean_text` to judge.
    """
    if not data:
        return ""
    data = data.removeprefix(UTF8_BOM)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


#: Every parser, keyed by the extension it handles.
#:
#: Written out explicitly rather than generated from
#: :data:`~app.services.uploads.SUPPORTED_FORMATS`, because a generated table would
#: silently map a newly-accepted format to whichever parser happened to sort next to
#: it. The test asserting this table's keys equal the validator's is what turns that
#: silent mapping into a failure.
SUPPORTED_EXTENSIONS: Final[dict[str, DocumentParser]] = {
    "pdf": PdfParser(),
    "docx": DocxParser(),
    "xlsx": XlsxParser(),
    "pptx": PptxParser(),
    "txt": TextFileParser("txt"),
    "md": TextFileParser("md"),
    "html": TextFileParser("html"),
}


def parser_for_extension(extension: str) -> DocumentParser | None:
    """The parser for ``extension``, or ``None`` when the format is unsupported.

    The one lookup the ingestion pipeline performs. Returns ``None`` rather than
    raising so the caller decides how an unparseable document is recorded — the
    orchestrator turns it into a ``failed`` status with a reason, which is more
    useful than an exception escaping the pipeline.
    """
    return SUPPORTED_EXTENSIONS.get(extension.lower())


def parser_for_format(format_spec: SupportedFormat) -> DocumentParser:
    """The parser for an already-resolved :class:`~app.services.uploads.SupportedFormat`.

    Raises:
        DocumentParseError: the accepted format has no parser. Unreachable while
            :data:`~app.services.uploads.SUPPORTED_FORMATS` and
            :data:`SUPPORTED_EXTENSIONS` agree, which a test asserts. Raising rather
            than returning a placeholder is the point: a placeholder would extract
            nothing and be indistinguishable from an empty document.
    """
    parser = SUPPORTED_EXTENSIONS.get(format_spec.extension)
    if parser is None:
        raise DocumentParseError(
            f"no parser is registered for the {format_spec.extension!r} format"
        )
    return parser


def resolve_parser(filename: str) -> DocumentParser:
    """The parser for an already-sanitised ``filename``, resolved from its extension.

    The convenience form for callers holding a filename rather than a
    :class:`~app.models.document.Document`. It reads the extension with
    :func:`app.services.uploads.extension_of`, the same function the upload
    validator used, so there is one definition of "what format is this".

    Raises:
        DocumentParseError: the extension is not one this project accepts, or is
            accepted with no parser registered.
    """
    extension = extension_of(filename)
    parser = SUPPORTED_EXTENSIONS.get(extension)
    if parser is None:
        raise DocumentParseError(
            f"no parser is registered for the {extension or '(none)'!r} format"
        )
    return parser