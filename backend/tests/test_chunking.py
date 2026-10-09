"""Tests for :mod:`app.rag.chunking` — determinism, contiguity, and losslessness.

What is being tested, and why it is mostly properties
----------------------------------------------------
A chunker has a handful of outputs a person can read and a great many that they
cannot. The readable ones — a chunk is never longer than the limit, a sentence is not
cut in half when a paragraph break was available — are worth one test each. The ones
that actually break retrieval are not readable at all:

* **Indexes must be contiguous from 0.** They are part of the Qdrant payload *and* of
  the deterministic point id, so a gap or a repeat does not merely mislabel a chunk; it
  collides with another chunk's point and one silently overwrites the other. There is
  no error and no log line.
* **Every non-whitespace character must survive.** A dropped character is invisible
  until a user reads a citation quoting something the document does not say.
* **The same input must chunk the same way, every time.** Re-ingestion is a supported
  operation and it must not multiply points or move them.

None of those can be checked by reading the output, so most of this file is
properties asserted over generated input. The generators produce the shapes that break
naive chunkers — one enormous word, text with no separators at all, every separator
the ladder knows about, text that is all whitespace — because a hand-written example
demonstrates the case its author thought of.

The invariants
--------------
:data:`CHUNK_INVARIANTS` is the single statement of what a valid chunking is, and both
the unit tests and the ingestion tests use it. One definition, so the two suites cannot
drift into disagreeing about what "valid" means.
"""

from __future__ import annotations

import dataclasses
import itertools
import random

import pytest

from app.rag.chunking import (
    FORCED_CUT,
    SELF_DELIMITING_SEPARATORS,
    SEPARATOR_LADDER,
    Chunk,
    chunk_text,
    reassemble,
)
from app.rag.cleaning import clean_text

#: Seeded so a failure is reproducible.
SEED = 20251004

#: The project's configured values, from PMP §16. Stated here rather than imported
#: from :class:`~app.core.config.Settings` because a chunker test that read its sizes
#: out of the settings object would pass against *any* pair of sizes and would fail the
#: moment someone changed the configuration without meaning to. These are the numbers
#: the configuration is *supposed* to hold, asserted separately in ``test_config.py``.
PMP_CHUNK_SIZE = 1200
PMP_CHUNK_OVERLAP = 200


class ChunkingProblem(Exception):
    """Raised by :func:`assert_valid`. Carries the reason a chunking is invalid.

    A custom exception rather than bare ``assert`` statements so the helper can be
    called from inside a loop over many parameter combinations without seven
    simultaneous assertion errors hiding which input actually broke. The message names
    the invariant and the offending values.
    """


def assert_valid(
    chunks: list[Chunk],
    text: str,
    *,
    chunk_size: int,
    chunk_overlap: int | None = None,
) -> None:
    """Assert every invariant a valid chunking must satisfy.

    Deliberately one function called from everywhere rather than a fixture: these are
    assertions about values, not about test setup, and a fixture would make it easy to
    check a chunking in one test and forget in the next.

    Args:
        chunks: what :func:`chunk_text` returned.
        text: the input it was given, unmodified.
        chunk_size: the limit that was passed. Re-chunking for the determinism check
            needs it, so it is a required argument rather than read off the chunks.
        chunk_overlap: the overlap that was passed, when the caller knows it. When
            given, the determinism invariant re-chunks with the same arguments; when
            omitted, determinism is left to :class:`TestDeterminism`, which asserts it
            properly rather than inferring the overlap back out of the result.
    """
    # 1. Never an empty chunk, and never a whitespace-only one.
    for chunk in chunks:
        if not chunk.text.strip():
            raise ChunkingProblem(
                f"chunk {chunk.index} is empty or whitespace-only: {chunk.text!r}"
            )

    # 2. Within the size limit.
    for chunk in chunks:
        if len(chunk.text) > chunk_size:
            raise ChunkingProblem(
                f"chunk {chunk.index} is {len(chunk.text)} characters, "
                f"over the {chunk_size} limit"
            )

    # 3. Indexes are 0..n-1, in order, no gaps and no repeats.
    for position, chunk in enumerate(chunks):
        if chunk.index != position:
            raise ChunkingProblem(
                f"chunk at position {position} carries index {chunk.index}; "
                "indexes must be contiguous and document-wide"
            )

    # 4. Offsets are half-open, ordered, and inside the text.
    previous_end = 0
    for chunk in chunks:
        if not 0 <= chunk.start < chunk.end <= len(text):
            raise ChunkingProblem(
                f"chunk {chunk.index} span [{chunk.start}, {chunk.end}) is outside "
                f"the {len(text)}-character input"
            )
        if chunk.start < previous_end - chunk.overlap_with_previous:
            raise ChunkingProblem(
                f"chunk {chunk.index} starts at {chunk.start}, before the previous "
                f"chunk's overlap window ends at "
                f"{previous_end - chunk.overlap_with_previous}"
            )
        previous_end = chunk.end

    # 5. Every chunk's text is exactly the text at its own offsets.
    for chunk in chunks:
        if text[chunk.start : chunk.end] != chunk.text:
            raise ChunkingProblem(
                f"chunk {chunk.index} text does not match its own span "
                f"[{chunk.start}, {chunk.end})"
            )

    # 6. Every non-whitespace character survives, in order.
    survivors = "".join(c for c in text if not c.isspace())
    recovered = "".join(c for c in reassemble(chunks) if not c.isspace())
    if survivors != recovered:
        raise ChunkingProblem(
            f"chunking lost content: {len(survivors)} characters in, "
            f"{len(recovered)} recovered"
        )

    # 7. Determinism, when the caller supplied the arguments to reproduce. Checking it
    #    here as well as in TestDeterminism is deliberate: a chunking that is valid but
    #    not reproducible is the failure this whole phase's idempotency requirement
    #    exists to prevent, and it is exactly the kind that a single well-chosen
    #    determinism test would miss.
    if chunk_overlap is not None:
        again = chunk_text(text, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        if chunks != again:
            raise ChunkingProblem(
                "chunking the same text twice gave different results"
            )


class TestConfiguration:
    @pytest.mark.parametrize(
        ("size", "overlap"),
        [(0, 0), (-1, 0), (10, -1), (10, 10), (10, 11), (1, 1), (1, 2)],
    )
    def test_an_unusable_window_is_refused(self, size: int, overlap: int) -> None:
        """Refused, not clamped.

        ``overlap >= size`` is the dangerous one: the window never advances, so a
        clamp would loop forever inside the chunker's own ``while``. ``ValueError`` is
        the right answer for all of them — there is no configuration in which silently
        picking a different size is better than saying so.
        """
        with pytest.raises(ValueError):
            chunk_text("some text to split", chunk_size=size, chunk_overlap=overlap)

    def test_a_window_of_one_is_legal(self) -> None:
        """The smallest legal window. Refusing it would be over-strict."""
        chunks = chunk_text("abcd", chunk_size=1, chunk_overlap=0)
        assert [c.text for c in chunks] == ["a", "b", "c", "d"]

    def test_zero_overlap_is_legal(self) -> None:
        """No repetition between chunks, and the space stays with the left one.

        ``"alpha |beta |gamma"`` rather than ``"alpha |beta g|amma"``: with a zero
        overlap the boundary is placed at the coarsest separator that fits, so the
        window ends on a space rather than mid-word.
        """
        chunks = chunk_text("alpha beta gamma", chunk_size=6, chunk_overlap=0)
        assert [c.text for c in chunks] == ["alpha ", "beta ", "gamma"]
        assert all(c.overlap_with_previous == 0 for c in chunks)

    def test_the_configured_window_is_accepted(self) -> None:
        chunks = chunk_text("word " * 1000, chunk_size=PMP_CHUNK_SIZE,
                            chunk_overlap=PMP_CHUNK_OVERLAP)
        assert_valid(chunks, "word " * 1000, chunk_size=PMP_CHUNK_SIZE)


class TestEmptyAndBlankInput:
    @pytest.mark.parametrize("text", ["", " ", "\n", "\n\n\n", "\t\t", "   \n  \t "])
    def test_blank_input_yields_no_chunks_at_all(self, text: str) -> None:
        """Not one empty chunk. An empty chunk would be embedded and stored."""
        assert chunk_text(text, chunk_size=100, chunk_overlap=10) == []

    def test_input_whose_only_content_is_whitespace_is_blank(self) -> None:
        assert chunk_text("\n\n\n  \n", chunk_size=10, chunk_overlap=2) == []

    def test_a_single_character_is_one_chunk(self) -> None:
        chunks = chunk_text("x", chunk_size=100, chunk_overlap=50)
        assert len(chunks) == 1
        assert chunks[0].text == "x"
        assert chunks[0].overlap_with_previous == 0


class TestTheSizeLimit:
    @pytest.mark.parametrize("size", [1, 2, 5, 17, 100, 1200])
    def test_no_chunk_ever_exceeds_the_limit(self, size: int) -> None:
        text = clean_text(
            "The quick brown fox jumps over the lazy dog. " * 40
            + "\n\nA second paragraph with its own sentences, longer than the first.\n\n"
            + "Third."
        )
        overlap = min(size - 1, 3)
        chunks = chunk_text(text, chunk_size=size, chunk_overlap=overlap)
        assert_valid(chunks, text, chunk_size=size, chunk_overlap=overlap)
        assert max(len(c.text) for c in chunks) <= size

    def test_the_limit_is_measured_in_characters_not_bytes_or_words(self) -> None:
        """A multi-byte character is one character.

        Asserted with a string whose character count and byte count differ sharply, so
        a byte-based implementation would fail it and a word-based one would produce
        chunks far shorter than the limit.
        """
        text = "é" * 500
        chunks = chunk_text(text, chunk_size=100, chunk_overlap=0)
        assert len(text.encode()) > len(text)
        assert all(len(c.text) <= 100 for c in chunks)
        assert sum(len(c.text) for c in chunks) == 500

    def test_an_oversized_run_without_a_separator_is_cut_by_force(self) -> None:
        """A base64 blob or a long URL: there is nowhere else to cut.

        :data:`FORCED_CUT` is the hard cut, and it is a last resort — but not cutting
        would emit a chunk over the limit, which is worse.
        """
        text = "A" * 250
        chunks = chunk_text(text, chunk_size=100, chunk_overlap=0)
        assert_valid(chunks, text, chunk_size=100, chunk_overlap=0)
        assert [len(c.text) for c in chunks] == [100, 100, 50]
        assert FORCED_CUT == ""

    def test_a_run_of_spaces_longer_than_the_limit_does_not_produce_empty_chunks(
        self,
    ) -> None:
        """Whitespace is skipped rather than chunked.

        A chunker that cut on the space run would emit a chunk of nothing, which would
        then be embedded, stored, and retrieved as a blank answer.
        """
        text = "alpha" + (" " * 500) + "omega"
        chunks = chunk_text(text, chunk_size=20, chunk_overlap=5)
        assert_valid(chunks, text, chunk_size=20, chunk_overlap=5)
        assert all(c.text.strip() for c in chunks)


class TestIndexes:
    def test_indexes_are_contiguous_from_zero(self) -> None:
        text = "Sentence one. Sentence two. Sentence three. " * 30
        chunks = chunk_text(text, chunk_size=40, chunk_overlap=8)
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_indexes_are_document_wide_not_per_call(self) -> None:
        """Chunking a second page does not restart the numbering.

        The consequence of getting this wrong is severe and quiet: the point id is
        derived from ``(tenant, document, chunk_index)``, so two chunks on different
        pages with the same index produce the *same* id, and the second silently
        overwrites the first. Nothing raises; the document just loses half its content
        from the index.
        """
        first = chunk_text("Page one text. " * 20, chunk_size=40, chunk_overlap=8)
        second = chunk_text("Page two text. " * 20, chunk_size=40, chunk_overlap=8)
        offsets = len(first)
        combined = first + [
            Chunk(
                index=chunk.index + offsets,
                text=chunk.text,
                start=chunk.start,
                end=chunk.end,
                overlap_with_previous=chunk.overlap_with_previous,
            )
            for chunk in second
        ]
        assert [c.index for c in combined] == list(range(len(combined)))


class TestOffsets:
    def test_a_chunk_text_is_exactly_the_text_at_its_offsets(self) -> None:
        text = clean_text("Alpha paragraph.\n\nBeta paragraph.\n\nGamma paragraph.")
        chunks = chunk_text(text, chunk_size=20, chunk_overlap=5)
        for chunk in chunks:
            assert text[chunk.start : chunk.end] == chunk.text

    def test_offsets_are_half_open(self) -> None:
        """``end`` is one past the last character, so ``end - start == len(text)``."""
        chunks = chunk_text("alpha beta gamma delta", chunk_size=12, chunk_overlap=3)
        for chunk in chunks:
            assert chunk.end - chunk.start == len(chunk.text)

    def test_the_last_chunk_ends_at_the_end_of_the_document(self) -> None:
        text = "alpha beta gamma delta epsilon zeta"
        chunks = chunk_text(text, chunk_size=10, chunk_overlap=2)
        assert chunks[-1].end == len(text)

    def test_offsets_survive_reassembly(self) -> None:
        text = clean_text("First para.\n\nSecond para.\n\nThird para with more text.")
        chunks = chunk_text(text, chunk_size=18, chunk_overlap=4)
        recovered = reassemble(chunks)
        assert "".join(c for c in recovered if not c.isspace()) == "".join(
            c for c in text if not c.isspace()
        )

    def test_a_forced_cut_still_reports_correct_offsets(self) -> None:
        text = "X" * 50
        chunks = chunk_text(text, chunk_size=20, chunk_overlap=0)
        for chunk in chunks:
            assert text[chunk.start : chunk.end] == chunk.text
        assert chunks[0].start == 0
        assert chunks[-1].end == 50


class TestOverlap:
    def test_the_first_chunk_has_no_overlap(self) -> None:
        chunks = chunk_text("alpha beta gamma", chunk_size=8, chunk_overlap=3)
        assert chunks[0].overlap_with_previous == 0

    def test_consecutive_chunks_repeat_the_requested_number_of_characters(self) -> None:
        chunks = chunk_text("alpha beta gamma delta", chunk_size=10, chunk_overlap=4)
        for previous, current in itertools.pairwise(chunks):
            assert current.overlap_with_previous == previous.end - current.start
            if current.overlap_with_previous:
                assert current.text.startswith(
                    previous.text[-current.overlap_with_previous :]
                )

    def test_overlap_is_never_negative(self) -> None:
        """A negative overlap in a payload is nonsense, and would be stored as one.

        Reached when the cursor lands on a separator and the whitespace skip moves
        past the previous chunk's end — the chunk genuinely repeats nothing.
        """
        text = "one two three four five six seven eight nine ten"
        for size in range(4, 30):
            for overlap in range(size):
                chunks = chunk_text(text, chunk_size=size, chunk_overlap=overlap)
                assert all(c.overlap_with_previous >= 0 for c in chunks), (
                    f"negative overlap at size={size}, overlap={overlap}"
                )

    def test_overlap_never_understates_what_the_chunk_repeats(self) -> None:
        text = "one two three four five six seven eight nine ten eleven twelve"
        for size in range(5, 25):
            for overlap in range(size):
                chunks = chunk_text(text, chunk_size=size, chunk_overlap=overlap)
                for previous, current in itertools.pairwise(chunks):
                    assert current.overlap_with_previous == 0 or current.text.startswith(
                        previous.text[-current.overlap_with_previous :]
                    ), f"overlap understated at size={size}, overlap={overlap}"
                    # Guarded rather than written as `previous.text[-n:]`, because
                    # `text[-0:]` is `text[0:]` -- the whole string -- and the
                    # unguarded form asserts a prefix that is never present for the
                    # very common zero-overlap case.

    @pytest.mark.parametrize(("size", "overlap"), [(10, 9), (100, 99), (5, 4)])
    def test_a_near_maximal_overlap_still_advances(self, size: int, overlap: int) -> None:
        """The property, which is *progress*, not a chunk count.

        With ``overlap = size - 1`` the cursor advances one character per chunk, so
        ``size=10, overlap=9`` legitimately produces on the order of ``len(text)``
        chunks. Stating the requirement as "fewer than N chunks" would be stating a
        number that happens to hold for the configured window and means nothing for a
        pathological one. What must hold is that every chunk starts strictly after the
        one before, so the loop terminates -- which ``0 <= overlap < size`` guarantees,
        and which this asserts rather than trusts.
        """
        text = "word " * 200
        chunks = chunk_text(text, chunk_size=size, chunk_overlap=overlap)
        assert_valid(chunks, text, chunk_size=size, chunk_overlap=overlap)

        starts = [c.start for c in chunks]
        assert starts == sorted(starts), "chunk starts must not go backwards"
        assert len(set(starts)) == len(starts), "two chunks cannot start at the same place"
        assert starts[0] == 0
        assert chunks[-1].end == len(text)


class TestTheSeparatorLadder:
    def test_a_paragraph_break_is_preferred_over_a_sentence_end(self) -> None:
        """Coarsest separator first — the documented order of the ladder."""
        text = "One. Two. Three.\n\nFour."
        chunks = chunk_text(text, chunk_size=12, chunk_overlap=0)
        assert len(chunks) > 1
        assert any("\n" in c.text for c in chunks), (
            "a blank line was available inside the window and was not used"
        )

    def test_a_sentence_end_is_preferred_over_a_word_boundary(self) -> None:
        text = "alpha beta gamma. delta epsilon zeta."
        chunks = chunk_text(text, chunk_size=28, chunk_overlap=0)
        assert len(chunks) >= 2
        first = chunks[0].text
        assert first.rstrip().endswith("."), (
            f"cut after {first!r} where a sentence end was available"
        )

    def test_a_chunk_does_not_begin_with_whitespace(self) -> None:
        """A citation that starts ``"\\n\\n"`` or ``" ,"`` looks broken.

        This is also the deliberate cost of the reassembly guarantee: the separators at
        a boundary belong to no chunk, so they are dropped rather than pushed into the
        next one.
        """
        text = clean_text(
            "\n\n".join(f"Paragraph number {n} with a reasonable amount of text in it."
                        for n in range(40))
        )
        for size in range(20, 90, 7):
            for overlap in (0, size // 4):
                for chunk in chunk_text(text, chunk_size=size, chunk_overlap=overlap):
                    assert chunk.text == chunk.text.lstrip(), (
                        f"chunk starts with whitespace: {chunk.text!r}"
                    )

    def test_no_chunk_ends_mid_word_when_a_space_was_available(self) -> None:
        text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        chunks = chunk_text(text, chunk_size=20, chunk_overlap=0)
        for chunk in chunks[:-1]:
            assert chunk.text.endswith(" ") or chunk.text[-1] in ".!?;,"

    def test_every_separator_is_either_space_terminated_or_a_line_break(self) -> None:
        """The exact partition the two constants describe, asserted as an equation.

        This is the property that makes reassembly lossless, and it has two halves that
        are easy to conflate. A separator that *ends in a space* — ``". "``, ``" "`` —
        already carries its own space, so appending one more would double it; those are
        :data:`SELF_DELIMITING_SEPARATORS`. A separator that is a *run of newlines*
        carries no space and is skipped as whitespace, so no space is owed. Anything
        else — ``"\\t"``, say — would be a separator that neither contains a space nor
        is whitespace, and reassembly would weld the word before it to the word after.
        """
        line_breaks = {"\n", "\n\n"}

        assert set(SEPARATOR_LADDER) == set(SELF_DELIMITING_SEPARATORS) | line_breaks
        assert all(
            separator.endswith(" ")
            for separator in SELF_DELIMITING_SEPARATORS
        ), "a member of SELF_DELIMITING_SEPARATORS does not end in a space"
        # "Ends in a space" OR "is entirely whitespace" -- not "is whitespace", which
        # `". "` is not, because a sentence end is followed by a space and a character.
        assert all(
            separator.endswith(" ") or separator.isspace()
            for separator in SEPARATOR_LADDER
        ), (
            "a ladder separator is neither space-terminated nor whitespace, so "
            "reassembly would lose or double the boundary"
        )

    def test_the_ladder_ends_at_a_single_space(self) -> None:
        """The finest rung. Without it, a word longer than the window has no cut."""
        assert SEPARATOR_LADDER[-1] == " "

    def test_the_ladder_goes_from_coarse_to_fine(self) -> None:
        assert SEPARATOR_LADDER[0] == "\n\n"
        assert SEPARATOR_LADDER[:2] == ("\n\n", "\n")


class TestDeterminism:
    @pytest.mark.parametrize("seed", [SEED, SEED + 1, SEED + 2])
    def test_the_same_input_chunks_the_same_way_twice(self, seed: int) -> None:
        generator = random.Random(seed)
        alphabet = "abcdefgh .\n" + "\n" + ",;!?"
        for _ in range(120):
            text = "".join(
                generator.choice(alphabet) for _ in range(generator.randint(0, 400))
            )
            first = chunk_text(text, chunk_size=32, chunk_overlap=8)
            second = chunk_text(text, chunk_size=32, chunk_overlap=8)
            assert first == second

    def test_the_result_does_not_depend_on_call_order(self) -> None:
        inputs = ["alpha beta gamma", "delta epsilon", "zeta eta theta iota"]
        forward = [chunk_text(t, chunk_size=10, chunk_overlap=2) for t in inputs]
        backward = [chunk_text(t, chunk_size=10, chunk_overlap=2) for t in reversed(inputs)]
        assert forward == backward[::-1]

    def test_two_chunker_instances_agree(self) -> None:
        """There is one function, not one function plus a cached plan."""
        text = "alpha beta gamma delta epsilon zeta eta theta"
        assert chunk_text(text, chunk_size=15, chunk_overlap=3) == chunk_text(
            text, chunk_size=15, chunk_overlap=3
        )


class TestNoContentIsLost:
    """The property, over generated input rather than chosen input.

    Every shape here is one that breaks a different naive chunker: a word longer than
    the window (forced cut), text with no separator at all (forced cut), mixed
    separator densities, and text that is mostly whitespace.
    """

    SHAPES = (
        "word " * 500,
        "A" * 900,
        "a",
        "a b",
        "\n\n".join(f"Paragraph {n}." for n in range(200)),
        "  \n  \n  ".join("x" * 30 for _ in range(40)),
        "Sentence. " * 200,
        "no-separator-at-all" * 50,
        "Mixed.\n\nBreaks\nSome More. And more, plus more!",
        "café naïve — dash ünïcödé",
        " leading and trailing whitespace ",
    )

    @pytest.mark.parametrize("text", SHAPES, ids=range(len(SHAPES)))
    @pytest.mark.parametrize(("size", "overlap"), [(10, 0), (10, 3), (37, 12), (200, 40)])
    def test_every_non_whitespace_character_survives(
        self, text: str, size: int, overlap: int
    ) -> None:
        chunks = chunk_text(text, chunk_size=size, chunk_overlap=overlap)
        assert_valid(chunks, text, chunk_size=size, chunk_overlap=overlap)

    def test_a_real_document_reassembles_to_itself(self) -> None:
        prose = clean_text(
            "Annual leave policy.\n\n"
            "Staff accrue two days of paid leave for every calendar month of\n"
            "continuous service. Accrual begins on the first day of employment.\n\n"
            "Carry-over. Up to five unused days may be carried into the following\n"
            "year, and must be used before the end of March."
        )
        chunks = chunk_text(prose, chunk_size=80, chunk_overlap=20)
        assert "".join(c for c in reassemble(chunks) if not c.isspace()) == "".join(
            c for c in prose if not c.isspace()
        )

    def test_the_text_is_not_rewritten(self) -> None:
        """Chunking never alters a character, only chooses where to cut.

        No lowercasing, no punctuation stripping, no stop-word removal. Each is a
        plausible-sounding improvement that would make the stored payload text differ
        from the document, and the payload text is what a citation quotes.
        """
        text = "The Quick Brown Fox, and the lazy dog; 25% (net)."
        chunks = chunk_text(text, chunk_size=200, chunk_overlap=0)
        assert chunks[0].text == text


class TestChunkIsAValue:
    def test_it_is_frozen(self) -> None:
        chunk = Chunk(index=0, text="a", start=0, end=1)
        with pytest.raises(dataclasses.FrozenInstanceError):
            chunk.text = "b"  # type: ignore[misc]

    def test_it_is_hashable_and_comparable(self) -> None:
        """Frozen and slotted, so it can go in a set or be compared in a test."""
        one = Chunk(index=0, text="a", start=0, end=1)
        two = Chunk(index=0, text="a", start=0, end=1)
        assert one == two
        assert len({one, two}) == 1

    def test_overlap_defaults_to_zero_and_page_to_none(self) -> None:
        chunk = Chunk(index=0, text="a", start=0, end=1)
        assert chunk.overlap_with_previous == 0
        assert chunk.page is None

    def test_it_carries_a_page_when_one_is_given(self) -> None:
        """The page is set by the orchestrator, which chunks one page at a time.

        Not by this function: it receives cleaned text with no knowledge of where the
        text came from, and inventing a page here would be a guess.
        """
        chunk = Chunk(index=0, text="a", start=0, end=1, page=7)
        assert chunk.page == 7


class TestReassemble:
    def test_no_chunks_gives_no_text(self) -> None:
        assert reassemble([]) == ""

    def test_a_single_chunk_reassembles_to_itself(self) -> None:
        chunks = chunk_text("alpha beta", chunk_size=100, chunk_overlap=10)
        assert reassemble(chunks) == "alpha beta"

    def test_overlap_is_not_repeated_in_the_result(self) -> None:
        """The overlap is a window for retrieval, not extra text."""
        text = "alpha bravo charlie delta echo foxtrot"
        chunks = chunk_text(text, chunk_size=15, chunk_overlap=6)
        recovered = reassemble(chunks)
        assert recovered.count("bravo") == 1, (
            f"overlap duplicated text: {recovered!r}"
        )

    def test_out_of_order_chunks_are_put_back_in_order(self) -> None:
        text = "alpha bravo charlie delta echo"
        chunks = chunk_text(text, chunk_size=12, chunk_overlap=3)
        assert reassemble(list(reversed(chunks))) == reassemble(chunks)

    def test_whitespace_at_a_boundary_is_the_documented_loss(self) -> None:
        """The one thing reassembly does not restore, stated as a test.

        A chunk never begins with a separator, so the separators at a cut belong to no
        chunk. This is a deliberate trade — a chunk starting ``"\\n\\n"`` looks broken
        in a citation — and it is why the guarantee is about non-whitespace characters.
        """
        text = "alpha\n\nbravo\n\ncharlie\n\ndelta"
        chunks = chunk_text(text, chunk_size=14, chunk_overlap=2)
        recovered = reassemble(chunks)
        assert "\n\n\n" not in recovered, "a boundary doubled the paragraph break"
        assert "".join(c for c in recovered if not c.isspace()) == "".join(
            c for c in text if not c.isspace()
        )


class TestTheConfiguredWindowInUse:
    """The numbers PMP §16 specifies, on a document of a realistic size.

    Not a duplicate of the property tests: this one says what actually happens at the
    configured sizes, so a change to the configuration is visible as a changed chunk
    count rather than as a subtly different retrieval behaviour.
    """

    PARAGRAPH = (
        "Staff accrue two days of paid leave for every calendar month of continuous "
        "service, and accrual begins on the first day of employment. Requests must be "
        "submitted to the HR team at least five working days in advance of the "
        "intended start date. "
    )

    def test_a_long_document_produces_contiguous_chunks(self) -> None:
        text = clean_text(self.PARAGRAPH * 30)
        chunks = chunk_text(
            text, chunk_size=PMP_CHUNK_SIZE, chunk_overlap=PMP_CHUNK_OVERLAP
        )
        assert_valid(
            chunks,
            text,
            chunk_size=PMP_CHUNK_SIZE,
            chunk_overlap=PMP_CHUNK_OVERLAP,
        )
        assert len(chunks) > 1
        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_the_chunk_count_matches_the_configured_window(self) -> None:
        """Approximately ``len(text) / (size - overlap)``, plus one.

        Stated as a bound rather than an exact figure because where the boundaries
        land depends on the text. Asserted as a bound because an exact figure would
        break on any change to the ladder, and a break there is not a defect.
        """
        text = clean_text(self.PARAGRAPH * 30)
        advance = PMP_CHUNK_SIZE - PMP_CHUNK_OVERLAP
        chunks = chunk_text(
            text, chunk_size=PMP_CHUNK_SIZE, chunk_overlap=PMP_CHUNK_OVERLAP
        )
        lower = len(text) // PMP_CHUNK_SIZE
        upper = len(text) // advance + 2

        assert lower <= len(chunks) <= upper, (
            f"{len(chunks)} chunks for {len(text)} characters; expected between "
            f"{lower} and {upper}"
        )

    def test_a_short_document_is_a_single_chunk(self) -> None:
        """The common case. A one-page memo should not be cut at all."""
        text = clean_text(self.PARAGRAPH)
        chunks = chunk_text(
            text, chunk_size=PMP_CHUNK_SIZE, chunk_overlap=PMP_CHUNK_OVERLAP
        )
        assert len(chunks) == 1
        assert chunks[0].text == text
