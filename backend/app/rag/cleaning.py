"""Deterministic text cleaning: the "Cleaning" box of PMP §18's ingestion chain.

PMP §18 places cleaning between text extraction and chunking without specifying
what it does, so this module defines the smallest set of operations that ingestion
correctness actually needs, and every one of them is a **normalisation** — the
output is the same content as the input, in a canonical form. Nothing here
rewrites, summarises, translates, reorders or removes meaning.

What is normalised, and why each is required
--------------------------------------------
**Line endings.** ``\\r\\n`` and bare ``\\r`` become ``\\n``. Two documents with
identical visible content must produce identical chunks and therefore identical
vectors; a Windows-authored file and a Linux-authored one that differ only in line
endings would otherwise chunk differently. This is the single most important rule
in the module.

**Unicode NFC.** Canonically equivalent sequences render identically but are
different byte strings, so ``"café"`` written as one code point and as
``e`` + combining acute would embed differently. NFC folds them to one form.

**Invisible characters.** Zero-width space/non-joiner/joiner, zero-width
spaceword joiner, the soft hyphen and the byte-order mark are removed. They are
invisible in a viewer, they survive copy-paste out of word processors, and they
end up inside chunks where they fragment the token a model sees — and a soft
hyphen silently hyphenates a word. Removing them changes no visible content.

**Bidirectional embedding and override controls.** The eight characters behind
"Trojan Source" (CVE-2021-42574) are removed. They reorder how the rest of the
document *displays* without changing a stored character, so a reviewer approves
text whose rendering is not what a model would read — and PMP §17 requires
retrieved content to be treated as untrusted. The two bidirectional *marks* are
deliberately kept, because they are legitimate typography in Arabic, Hebrew and
Persian; see :data:`BIDI_CONTROLS`.

**Control characters.** ``C0`` controls except tab, newline and carriage return,
plus ``DEL`` and the ``C1`` block, become nothing or a space. The exceptions are
load-bearing: newline is the paragraph structure this module exists to preserve,
carriage return is a line ending that is normalised before this rule runs, and tab
is horizontal whitespace the next rule collapses.

**Runs of horizontal whitespace** collapse to one space. Tabs and runs of spaces
are how word processors pad, and eight spaces of indentation must not become eight
tokens of noise.

**Runs of blank lines** collapse to exactly one blank line. Three newlines and one
newline both mean "new paragraph" to a human; the difference is invisible and
changes where a chunk boundary falls.

**Trailing whitespace on each line and at the end of the document** is removed,
because it is invisible, contributes nothing, and would otherwise sit at the end of
a chunk forever.

What is deliberately NOT done
-----------------------------
No lowercasing (case carries meaning in prose, and an embedding model handles it).
No stop-word removal (it is a retrieval concern, not an ingestion one). No
aggressive whitespace stripping that would join paragraphs. No dehyphenation of
words split across lines — that needs a dictionary to do correctly and guessing
wrongly corrupts text. No HTML entity rewriting beyond what the parser already
handles. No LLM in this path, ever: a document's cleaned form must be a pure
function of its bytes.

The one judgement call, stated plainly
--------------------------------------
:func:`clean_text` collapses paragraph breaks but preserves paragraph *identity*,
and it never returns an empty string for a document that had non-whitespace
content. What it does return for genuinely empty input is ``""``, and the chunker
turns that into zero chunks rather than into one empty chunk — an empty chunk
embedded and indexed would be a retrieval result that can never help.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

#: Invisible code points removed outright.
#:
#: Spelled out as an explicit set rather than derived from :func:`unicodedata` for
#: the reason :mod:`app.schemas.documents` spells its set out: the rule has to be
#: readable where it is stated.
INVISIBLE_CODEPOINTS: Final[frozenset[int]] = frozenset(
    {
        0x00AD,  # soft hyphen
        0x200B,  # zero width space
        0x200C,  # zero width non-joiner
        0x200D,  # zero width joiner
        0x2060,  # word joiner
        0xFEFF,  # byte order mark / zero width no-break space
    }
)

#: U+FEFF is removed wherever it appears, not only at the start of a document.
#:
#: Stated because :func:`app.rag.parsers.decode_text` strips a *leading* byte-order mark
#: and its docstring says a mark elsewhere is real content left for this module to
#: judge. The judgement is: remove it there too. U+FEFF has no glyph and no width, so
#: a copy-paste out of a word processor or a PDF extractor can carry one into the
#: middle of a paragraph, and an invisible character in the middle of a chunk is
#: indistinguishable from a bug to whoever reads the citation later.

#: Bidirectional **embedding and override** controls.
#:
#: ``U+202A``-``U+202E`` (LRE, RLE, PDF, LRO, RLO) and ``U+2066``-``U+2069`` (LRI,
#: RLI, FSI, PDI) are the "Trojan Source" characters of CVE-2021-42574: they reorder
#: how everything after them *displays* without changing a single stored character.
#: A document containing one is invisible to the human reviewing it and fully
#: effective on the model reading it — the reviewer sees ``if (isAdmin)`` while the
#: chunk carries ``if (isAdmin)`` with the condition visually moved into a comment.
#: PMP §17 requires retrieved content to be handled as untrusted data, and a
#: reviewer is exactly the check this defeats, so they are removed.
#:
#: Deliberately **excluded**: ``U+200E`` LEFT-TO-RIGHT MARK and ``U+200F``
#: RIGHT-TO-LEFT MARK. Those are *marks* rather than embeddings, and they are how
#: legitimate Arabic, Hebrew and Persian text disambiguates a number or a bracket
#: at a script boundary. Removing them would corrupt real multilingual documents,
#: which is the aggressive rewriting this module refuses to do.
BIDI_CONTROLS: Final[frozenset[int]] = frozenset(
    set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A))
)

#: ``C0`` control characters except tab (``0x09``), newline (``0x0A``) and carriage
#: return (``0x0D``).
#:
#: Tab is excluded because it is horizontal whitespace that
#: :data:`_HORIZONTAL_WHITESPACE` collapses to one space. Newline is excluded
#: because it is the paragraph structure this module exists to preserve. Carriage
#: return is excluded because :func:`clean_text` normalises line endings **before**
#: this set is applied — a CR is a line ending, and removing it first would join
#: every line of a ``\r`-only document (classic Mac OS, and what a few text exporters
#: still emit) into a single paragraph. Order is load-bearing, and this exclusion is
#: what makes that order safe.
C0_EXCEPT_TAB_NEWLINE_CR: Final[frozenset[int]] = (
    frozenset(range(0x09))
    | frozenset(range(0x0B, 0x0D))
    | frozenset(range(0x0E, 0x20))
)

#: ``DEL`` plus the ``C1`` control block.
C1_AND_DEL: Final[frozenset[int]] = frozenset({0x7F}) | frozenset(range(0x80, 0xA0))

#: Every code point dropped outright, as one set for the membership test.
#:
#: Union of all four categories rather than four tests, so the comprehension in
#: :func:`clean_text` does one membership check per character.
REMOVED_CODEPOINTS: Final[frozenset[int]] = (
    INVISIBLE_CODEPOINTS | BIDI_CONTROLS | C0_EXCEPT_TAB_NEWLINE_CR | C1_AND_DEL
)

#: Line endings that are not ``CR``, ``LF`` or ``CRLF``.
#:
#: U+2028 and U+2029 are ``Zl`` and ``Zp`` — Unicode's *other* two line-break
#: characters, formally distinct from CR and LF. ``str.splitlines()`` breaks on both,
#: so Python's own notion of "a line" includes them, and they are exactly what a
#: document carries when it arrives from a web page, a JSON string, or anything that
#: has been through a JavaScript toolchain.
#:
#: Left alone they would be invisible here and fatal downstream: the chunker's
#: separator ladder splits on ``"\n\n"`` then ``"\n"``, so a document whose lines are
#: separated by U+2028 has *no* boundary the chunker can find, and a chunk runs
#: straight through what a reader sees as a paragraph break. Converting them is the
#: same normalisation as ``CRLF`` to ``LF``, and it is lossless: both become one
#: line break.
#:
#: Both map to a single ``\n`` and **not** U+2029 to ``\n\n``. A paragraph separator
#: becoming a blank line would invent emphasis the source did not have — it would
#: change where the chunker prefers to cut, which is a claim about the document's
#: structure, and the module's rule is that it never adds meaning.
_LINE_SEPARATORS: Final[tuple[str, ...]] = (" ", " ", "\x0b", "\x0c", "\x85")

#: Horizontal whitespace: tab and space only.
#:
#: Deliberately excludes vertical tab and form feed, which :data:`_LINE_SEPARATORS`
#: has already rewritten to ``"\n"`` by the time this runs, and excludes the Unicode
#: spaces (``U+00A0``, ``U+2000``-``U+200A``) which :func:`clean_text` converts to a
#: plain space *before* this runs. Listing only what this pattern matches keeps the
#: substitution honest about what it collapses.
_HORIZONTAL_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"[ \t]+")

#: Three or more consecutive newlines.
_BLANK_LINE_RUN: Final[re.Pattern[str]] = re.compile(r"\n{3,}")

#: Code points that are a Unicode space rather than an ASCII one, and so are folded
#: to ``" "`` rather than removed.
#:
#: Named individually rather than generated by a range, because two different
#: generations of this set have been wrong and a third would not be trusted:
#:
#: * The C1 range ``0x80–0x9F`` and the C0 range must **not** be folded — those are
#:   control characters, and they are in :data:`C1_AND_DEL` and
#:   :data:`C0_EXCEPT_TAB_NEWLINE_CR`, where they are *deleted*. Folding them to a
#:   space would resurrect nine invisible control bytes as visible whitespace.
#:
#: * Every space here is a *separator*, and the point of folding one to ``" "`` is
#:   that a reader — and the chunker's separator ladder — sees a break there. So the
#:   set is exactly the characters Unicode defines as space separators plus the
#:   no-break spaces, and it includes the ideographic space U+3000: collapsing its
#:   width is cosmetic, but *keeping it* would let a chunk boundary fall in the
#:   middle of a run of CJK text, because ``str.split()`` and the ladder in
#:   :mod:`app.rag.chunking` do not treat U+3000 as whitespace.
#:
#: U+200E and U+200F are deliberately absent: they are the bidirectional *marks*,
#: which are kept (see :data:`BIDI_CONTROLS`), and folding them to a space would
#: insert text rather than remove it.
UNICODE_SPACES: Final[dict[int, str]] = {
    codepoint: " "
    for codepoint in (
        0x00A0,  # no-break space
        *range(0x1680, 0x1681),  # ogham space mark
        *range(0x2000, 0x200B),  # en/em/thin/hair spaces, and the rest of the run
        0x202F,  # narrow no-break space
        0x205F,  # medium mathematical space
        0x3000,  # ideographic (full-width) space
    )
}


def clean_text(text: str) -> str:
    """Return ``text`` in canonical form, deterministically.

    A pure function: the same input always produces the same output, with no
    dependency on locale, environment, time or any external service. That is what
    makes re-ingesting a document produce identical chunks, and therefore identical
    point ids — the property PMP §4.1's "track chunk counts" and this project's
    idempotency requirement both rest on.

    Args:
        text: extracted text, as the parser produced it. Any content is accepted,
            including text that is entirely whitespace, which returns ``""``.

    Returns:
        The cleaned text. Never ``None``, never a non-string. Empty only when the
        input held no non-whitespace content.

    Order of operations, which is load-bearing
    -------------------------------------------
    Five orderings are forced. Each was found by a test rather than by reasoning, which
    is the argument for having the properties asserted rather than the examples.

    **Every removal happens before every collapsing**, and before composition. A step
    that deletes a character exposes work for the step after it — removing a
    zero-width joiner between two spaces leaves a run to collapse, and collapsing a run
    can bring two newlines into contact — so removal is finished first and no step has
    to revisit its predecessor's output.

    Composition (NFC) is last of the transformations, not first. This one is the
    least obvious and has the worst failure mode. Canonical composition only combines
    a base character with a following combining mark **when the two are adjacent**, so
    normalising *before* removing invisible characters means a soft hyphen or
    zero-width space sitting between ``e`` and ``U+0301`` blocks the composition at
    the time it could still happen; the two characters are then deleted apart and the
    result is emitted uncombined. Two consequences, both bad for this project:

    * it is not idempotent — cleaning the output a second time finds the now-adjacent
      pair and composes it, so the same document produces different text, different
      chunks and different vectors on a re-ingest; and
    * two documents whose visible text is identical but which differ by one invisible
      character embed *differently*, so the invisible character changes retrieval
      behaviour.

    Composing after the removals means NFC sees exactly the sequence that will be
    emitted, which is the only point at which "adjacent" means what it will mean in
    the result. Found by the idempotence property in ``tests/test_text_cleaning.py``
    over generated hostile input.

    Line endings are normalised **before** any character is dropped. Carriage return
    is itself a ``C0`` control, so a stage that removes controls first would delete
    the CR of a ``CRLF`` pair and then see a bare ``LF`` — which happens to be right
    — but would delete the CR of a ``CR``-only document outright and fuse every line
    of it into one paragraph. That is real data loss on a format (classic Mac OS, and
    what some text exporters still emit) that the upload validator happily accepts.

    Unicode spaces are folded to ASCII spaces **before** whitespace collapsing, or a
    non-breaking space between two words would survive as a non-breaking space
    rather than becoming the single space a reader sees. Folding before composition
    costs nothing: no canonical composition has a space as its result, so a folded
    space can never be one half of a pair that composition was going to join.

    Trailing whitespace is removed **before** blank-line collapsing. A whitespace-only
    line between two others is not matched by ``\\n{3,}`` — the newlines are not
    adjacent, there is a space between them — so collapsing first leaves the run
    alone, and only then does the trim empty those lines and *create* a run nothing
    goes on to collapse.
    """
    if not text:
        return ""

    # Line endings FIRST, while CR still exists. See the docstring.
    unified = text.replace("\r\n", "\n").replace("\r", "\n")

    # ...and then the line breaks that are not CR or LF at all, before anything looks
    # at `\n`. Vertical tab and form feed are in here rather than in
    # `REMOVED_CODEPOINTS` because a document that uses them has *lines* — deleting
    # them would fuse those lines into one, exactly as deleting a CR would. U+0085 is
    # here for the same reason despite being a C1 control.
    for separator in _LINE_SEPARATORS:
        unified = unified.replace(separator, "\n")

    # Fold spaces, then remove everything the module documents as removed. One pass
    # over one intermediate string rather than two passes over two, because the fold
    # cannot introduce a character that the removal would then have to look at
    # (a space is not in `REMOVED_CODEPOINTS`, and the sets are asserted disjoint).
    stripped = "".join(
        " " if ord(character) in UNICODE_SPACES else character
        for character in unified
        if ord(character) not in REMOVED_CODEPOINTS
    )

    # NFC last, once every character that could have blocked a composition is gone.
    # See the docstring for why this is not the first line of the function.
    normalised = unicodedata.normalize("NFC", stripped)

    collapsed_whitespace = _HORIZONTAL_WHITESPACE.sub(" ", normalised)

    # Trailing whitespace per line, BEFORE the blank-line collapse. `[ \t]+$` with
    # MULTILINE rather than a global rstrip, so every line is trimmed and not just the
    # last one. The order is the fix described in the docstring.
    trimmed = re.sub(r"[ \t]+$", "", collapsed_whitespace, flags=re.MULTILINE)

    collapsed_paragraphs = _BLANK_LINE_RUN.sub("\n\n", trimmed)

    # Finally the document as a whole. Leading whitespace usually separates a
    # document's first paragraph from whatever preceded it, and a leading blank
    # line would offset every chunk boundary by one.
    return collapsed_paragraphs.strip()


def cleaned_text_is_empty(text: str) -> bool:
    """Whether ``clean_text(text)`` would produce nothing.

    Exists so the ingestion pipeline can distinguish "this document has no
    extractable text" — a PDF that is a scan of an image, a spreadsheet of empty
    cells — from "this document has text". The first is a *failure worth
    recording*, because a document that can never be retrieved from should not be
    reported as ``ready``; the second is a document that parsed fine.
    """
    return not clean_text(text).strip()