"""Deterministic chunking: the "Chunking" box of PMP §18's ingestion chain.

The strategy, and why this one
-----------------------------
PMP §18 writes "Semantic/Recursive Chunking" and defines neither a size, nor an
overlap, nor a boundary rule. Three properties are required regardless of the
strategy chosen, and this module is built around them:

1. **Deterministic.** Same text plus same configuration yields the same chunks,
   always. This is not a nicety: the chunk index is part of the vector point's
   identity (see :mod:`app.rag.vectors`), so a non-deterministic chunker makes
   re-ingestion create *new* points instead of overwriting the old ones, and a
   document is silently duplicated in the index.
2. **No empty chunks.** A zero-length chunk embeds into a vector that describes
   nothing and still occupies a slot in the top-k. A trailing separator must not
   produce one.
3. **No silent text loss.** Every non-whitespace character of the input appears in
   the chunks, in order. ``tests/test_chunking.py`` asserts this by reassembling
   the chunks and comparing against the input, rather than by trusting the
   implementation. Whitespace in the gap between two chunks is the one documented
   exception — see :func:`reassemble`.

**Recursive** character splitting, not semantic chunking
-------------------------------------------------------
The recursion is over separator granularities: split on blank lines, and where a
piece is still too large, split it again on single newlines, then on sentence
boundaries, then on words. The first granularity that fits wins, and each chunk
keeps as much of its surrounding text as fits.

This is the "Recursive" half of what PMP §18 names, and it is the half that can be
built without a language model. The "Semantic" half — asking an LLM where a topic
begins — is deliberately **not** implemented, for three reasons stated in full in
:mod:`app.rag.chunking`'s own history below, the shortest of which is that it would
be non-deterministic and property 1 forbids that. It also puts untrusted document
text (PMP §17 "RAG Security") in a position where it steers model output, for no
requirement PMP §7 actually asks for: FR-06 says "parse, chunk, embed, and index".

Paragraphs are preferred over sentences, which are preferred over words, because a
chunk that ends mid-topic is worse than one that is slightly too long — and because
most enterprise documents (handbooks, policies, SOPs, per PMP §2) are already
written in paragraphs.

Overlap, and what it costs
--------------------------
Consecutive chunks repeat :attr:`Chunk.overlap_with_previous` characters, taken
from the tail of the preceding chunk. This is the standard defence against a
sentence straddling a boundary being retrievable from neither chunk; see
:attr:`~app.core.config.Settings.chunk_overlap_chars` for the size and the
reasoning behind it.

The overlap is **computed, not stored as intent**: :attr:`Chunk.overlap_with_previous`
is derived from the previous chunk's ``end`` and this chunk's ``start``, so it
cannot claim more overlap than the chunk actually contains. A structural test in
``tests/test_chunking.py`` asserts that relationship holds for every chunk of
every input it is given.

Why chunk indexes are stable but are not content-addressed
----------------------------------------------------------
:attr:`Chunk.index` is the chunk's ordinal in the document, assigned by this module
and used by :mod:`app.rag.vectors` as part of the point id. It is deliberately
**not** a hash of the chunk's text, so inserting content near the start of a
document renumbers later chunks.

That is correct for this phase's purpose. The point id means "chunk N of this
document", and an id that *survived* a content edit would be worse: a stale chunk
whose text no longer matches its own id could never be overwritten, because
re-ingestion would not know it was stale. Idempotency therefore comes from
re-ingestion deleting the document's existing points before upserting — see
:func:`app.services.ingestion.ingest_document` — and not from the ids being
insensitive to content. PMP §28 lists "automatic document versioning" as a *future*
enhancement; this phase does not invent it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Separator granularities, tried in order when a piece is still too large.
#:
#: A tuple of ``(separator, keep_separator)`` pairs. ``keep_separator=True`` for
#: newlines and sentence punctuation means the separator stays at the end of the
#: chunk, which is what lets consecutive chunks reassemble into the original text
#: instead of gluing words together across a boundary.
SEPARATOR_LADDER: Final[tuple[str, ...]] = (
    "\n\n",
    "\n",
    ". ",
    "! ",
    "? ",
    "; ",
    ", ",
    " ",
)

#: Separators after which a space is *not* wanted in the reassembled text.
#:
#: ``", "`` and ``". "`` already contain their own space, so keeping the separator
#: keeps exactly one space. This constant is not needed by the algorithm — it is
#: asserted in the tests to document that the ladder's separators are
#: self-delimiting, which is the property that makes reassembly lossless.
SELF_DELIMITING_SEPARATORS: Final[frozenset[str]] = frozenset({". ", "! ", "? ", "; ", ", ", " "})

#: The empty separator, used only when a run of characters contains no separator at
#: all and must be cut at the size limit by force.
#:
#: Real extracted text always contains a space or a newline, so this is the last
#: resort for input like a base64 blob inside a text file. It is a hard cut, not a
#: boundary choice, which is why it is excluded from :data:`SEPARATOR_LADDER`.
FORCED_CUT: Final[str] = ""


@dataclass(frozen=True, slots=True)
class Chunk:
    """One indexed piece of a document, with the metadata retrieval will need.

    Attributes:
        index: this chunk's ordinal within the document, from 0, contiguous and
            strictly increasing.
        text: the chunk's content. Never empty, never whitespace-only.
        start: offset of ``text`` within the cleaned document, inclusive.
        end: offset one past the last character of ``text``.
        overlap_with_previous: how many leading characters of this chunk are
            repeated from the previous chunk's tail. ``0`` for the first chunk, and
            for any chunk that begins at or after the previous one's end — which
            happens whenever the overlap is small enough to land the cursor on a
            separator. Never negative, and never greater than ``len(text)``.
        page: the 1-based page this chunk starts on, or ``None`` when the source
            format has no pages. Filled by the parser, not by the chunker: the
            chunker sees one continuous string and cannot know where page breaks
            were.
    """

    index: int
    text: str
    start: int
    end: int
    overlap_with_previous: int = 0
    page: int | None = None


def chunk_text(
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int,
) -> list[Chunk]:
    """Split ``text`` into deterministic chunks of at most ``chunk_size`` characters.

    Args:
        text: **cleaned** text. Call :func:`app.rag.cleaning.clean_text` first.
            This function does not clean, because a chunker's offsets are only
            meaningful relative to the text the pipeline stores in the payload —
            cleaning afterwards would silently invalidate every one of them.
        chunk_size: maximum characters per chunk. Must be at least 1.
        chunk_overlap: characters of the previous chunk repeated at the start of
            this one. Must satisfy ``0 <= chunk_overlap < chunk_size``;
            :class:`~app.core.config.Settings` validates that, and it is re-checked
            here so the function is safe to call directly.

    Returns:
        Chunks in document order with indexes ``0..n-1``. Empty for empty or
        whitespace-only input — never a list holding one empty chunk.

    Raises:
        ValueError: ``chunk_size`` is below 1, or the overlap is negative or at or
            above the size. The last case is an infinite loop rather than a wrong
            answer, so it is refused rather than clamped.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap must not be negative")
    if chunk_overlap >= chunk_size:
        raise ValueError(
            f"chunk_overlap ({chunk_overlap}) must be less than chunk_size "
            f"({chunk_size}); otherwise the chunk window never advances"
        )

    if not text.strip():
        return []

    spans = _plan_spans(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    chunks: list[Chunk] = []
    for position, (start, end) in enumerate(spans):
        previous_end = spans[position - 1][1] if position else start
        chunks.append(
            Chunk(
                index=position,
                text=text[start:end],
                start=start,
                end=end,
                # Zero for the first chunk, and derived from the spans rather than
                # from `chunk_overlap` so it can never overstate what this chunk
                # actually contains. `max(0, ...)` because a chunk that begins just
                # past the previous one — which happens when a zero overlap lands
                # the cursor on a separator and the whitespace skip moves it on —
                # genuinely repeats nothing, and reporting a negative overlap would
                # put a nonsensical number in the payload.
                overlap_with_previous=max(0, previous_end - start),
            )
        )
    return chunks


def _plan_spans(
    text: str, *, chunk_size: int, chunk_overlap: int
) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` spans to emit, in order.

    Each span is at most ``chunk_size`` characters. Every span except the last is
    at least ``chunk_overlap + 1`` characters, which is what guarantees the window
    advances: without it, a span shorter than the overlap would place the next
    start at or before the current one and the loop would never terminate.

    The step is:

    1. **Choose a provisional window** of ``chunk_size`` from the cursor.
    2. **Stop if the window reaches the end of the document.** The tail is emitted
       as its own chunk even when it is shorter than the overlap, and the scan
       finishes. This is the case that would otherwise loop forever: for the last
       ``overlap`` characters of a document, ``end - chunk_overlap`` lands *on or
       before* the cursor, so a cursor-only advance never moves. Sizing the final
       chunk by ``end - chunk_overlap`` rather than by the remaining text is the
       bug; the tail is smaller than the overlap and has to be emitted as-is.
    3. **Prefer a boundary** — the last occurrence of the coarsest separator from
       :data:`SEPARATOR_LADDER` that fits inside the window, keeping the separator
       at the chunk's end. A boundary is only taken when it leaves a window longer
       than the overlap, so preferring a boundary can never stall the scan.
    4. **Skip leading whitespace**, so a chunk never begins with a separator. The
       skipped characters are whitespace only, which is why this loses no content.
    5. **Advance** the cursor to ``end - chunk_overlap``.

    Termination is therefore not a hope but a property of the two branches: step 2
    returns, and in every other iteration the new cursor is at least ``cursor + 1``.

    Returns:
        Half-open ``(start, end)`` spans in increasing order, covering every
        non-whitespace character of ``text``.
    """
    spans: list[tuple[int, int]] = []
    length = len(text)
    cursor = 0

    while cursor < length:
        # Advance past whitespace the previous window stopped inside. Nothing is
        # lost: whitespace that no chunk claims is whitespace no chunk needs.
        while cursor < length and text[cursor].isspace():
            cursor += 1
        if cursor >= length:
            break

        limit = min(cursor + chunk_size, length)
        if limit >= length:
            spans.append((cursor, length))
            break

        spans.append(
            (
                cursor,
                _boundary_before(
                    text, cursor, limit, minimum_end=cursor + chunk_overlap + 1
                ),
            )
        )
        cursor = spans[-1][1] - chunk_overlap

    return spans


def _boundary_before(text: str, start: int, limit: int, *, minimum_end: int) -> int:
    """The best split point in ``(start, limit)``, or ``limit`` if none qualifies.

    Walks :data:`SEPARATOR_LADDER` from the coarsest separator to the finest and
    returns the *last* occurrence of the first separator found. Coarser first, so a
    blank line beats a single newline beats a sentence end: a chunk that ends at a
    paragraph break is worth more than one that ends mid-paragraph but at a period.

    The separator is kept at the end of the chunk, so consecutive chunks reassemble
    into the original text.

    Args:
        start: the window's first index.
        limit: the window's exclusive end. Strictly less than ``len(text)`` — the
            caller handles the final window itself, so the ``limit >= len(text)``
            case never arrives here and needs no answer.
        minimum_end: the earliest end that still lets the cursor advance. A
            boundary earlier than this is refused and a coarser or no boundary is
            used instead, which is what prevents the stall in step 2 above from
            becoming reachable through this function.

    Returns:
        An index in ``[minimum_end, limit]``. Never below ``minimum_end``, and never
        above ``limit``.
    """
    for separator in SEPARATOR_LADDER:
        index = text.rfind(separator, start + 1, limit)
        if index == -1:
            continue
        candidate = index + len(separator)
        if candidate >= minimum_end:
            return candidate

    # No usable boundary: cut at the size limit. Correct rather than a fallback —
    # this is the case where a single unit is longer than one chunk.
    return limit


def reassemble(chunks: list[Chunk]) -> str:
    """Join chunk texts back into the document they came from.

    Exists for the tests, not for the application: the pipeline never needs the
    original text, because each chunk's own content is already stored in its
    payload. Its value is that it makes the losslessness property *mechanical* — a
    test can chunk a document, reassemble it, and compare against the input —
    instead of a claim about the implementation.

    What it restores, and what it cannot
    -------------------------------------
    Every **non-whitespace** character of the input appears in the result, in order.
    Whitespace that fell in the gap between two chunks is not restored: a chunk
    never begins with a separator, so the run of separators at a boundary is in no
    chunk at all. That is a deliberate trade — a chunk starting ``"\\n\\n"`` looks
    broken in a citation — and it is the reason the guarantee is stated in terms of
    non-whitespace characters rather than as byte equality.

    The overlap between consecutive chunks is counted once, using each chunk's
    ``start`` offset, so duplicated text does not appear twice.
    """
    if not chunks:
        return ""

    ordered = sorted(chunks, key=lambda chunk: chunk.start)
    # `cursor` tracks how far the previous chunk reached. Text before it has already
    # been emitted, so each chunk contributes only what lies beyond it.
    pieces: list[str] = []
    cursor = 0
    for chunk in ordered:
        begin = max(chunk.start, cursor)
        if chunk.end > begin:
            pieces.append(chunk.text[begin - chunk.start :])
            cursor = chunk.end
    return "".join(pieces)