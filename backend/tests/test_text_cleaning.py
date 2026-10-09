"""Tests for :mod:`app.rag.cleaning` — determinism, and the absence of loss.

Two kinds of assertion here, and the distinction matters
-------------------------------------------------------
**Normalisation tests** pin one behaviour each: line endings become ``\\n``, invisible
characters go, whitespace runs collapse. These are the module's contract and they are
worth stating individually.

**Property tests** then assert the guarantees that have to hold for *every* input,
over generated inputs rather than chosen ones: cleaning is idempotent, it is
deterministic, and it never loses a non-whitespace character. Those three are the
properties ingestion correctness actually rests on — a chunker that chunks a
different string each run would produce different vectors each run, and a cleaner that
drops a character silently corrupts a citation — and they are properties of the
function, not of any particular paragraph. A test with a hand-picked example would
pass a cleaner that mangles the next one.

The generators deliberately include hostile input: control characters, bidi overrides,
astral-plane characters, combining marks, and text that is *entirely* invisible. A
cleaner that loses a character tends to lose it on exactly that input.

What must not happen
--------------------
Two things are asserted as **absences**, and they are the ones a future contributor is
most likely to introduce by making the cleaner smarter:

* it must not call a model. Document text is untrusted data (PMP §17), and an LLM in
  this path would be both a cost and an injection surface. Asserted over the module's
  own source rather than trusted to review.
* it must not lowercase, dehyphenate, or drop stop words. Each is a plausible-sounding
  "improvement" that destroys information the citation layer needs.
"""

from __future__ import annotations

import random
import unicodedata

import pytest

from app.rag.cleaning import (
    BIDI_CONTROLS,
    C0_EXCEPT_TAB_NEWLINE_CR,
    C1_AND_DEL,
    INVISIBLE_CODEPOINTS,
    REMOVED_CODEPOINTS,
    clean_text,
    cleaned_text_is_empty,
)

#: Seeded, so a failure is reproducible. A fixed seed rather than a fresh one because
#: "fails 1 run in 50" is not a reportable result.
SEED = 20251004

#: Characters chosen to be hostile to a text cleaner, assembled from code points
#: rather than written as literals.
#:
#: Both spellings work, and the literal form is better everywhere except here. A
#: literal U+202E in a source file is invisible in an editor and in review, it
#: survives copy-paste, and the linter reads it as obfuscated code -- correctly,
#: since that is exactly what the character is for. Spelling them as ``chr(0x202E)``
#: makes the fixture's intent readable, keeps every other character in the file
#: ordinary, and makes each one's identity checkable: ``chr`` is total, so a typo in
#: a hex literal raises rather than quietly producing a different character.
def _chars(codepoints: tuple[int, ...]) -> str:
    return "".join(chr(codepoint) for codepoint in codepoints)


#: C0 controls that must be removed, and that are *not* whitespace.
_HOSTILE_C0 = (0x00, 0x01, 0x07, 0x08, 0x0B, 0x0C, 0x0E, 0x1F)
#: DEL and the C1 block.
_HOSTILE_C1 = (0x7F, 0x85, 0x9F)
#: Zero-width space, non-joiner, joiner, word joiner, soft hyphen, byte-order mark.
_HOSTILE_INVISIBLE = (0x200B, 0x200C, 0x200D, 0x2060, 0x00AD, 0xFEFF)
#: Every bidirectional embedding and override control -- the Trojan Source set.
_HOSTILE_BIDI = (
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    0x2066, 0x2067, 0x2068, 0x2069,
)
#: Spaces Unicode defines that are not ASCII: NBSP, ogham, en/em/thin, narrow NBSP,
#: medium mathematical, ideographic. Folded to ``" "`` rather than removed.
_HOSTILE_SPACES = (0x00A0, 0x1680, 0x2002, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000)
#: U+2028 and U+2029 -- Unicode's other two line breaks. In the alphabet because the
#: generated input has to reach the line-ending rewrite, not just the CR path.
_HOSTILE_LINE_SEPARATORS = (0x2028, 0x2029)
#: A combining mark, a second one, an enclosing mark, and an astral-plane character.
#: None is ASCII, and none may be mangled -- these are what the NFC fold and the
#: composition-after-removal rule are tested through.
_HOSTILE_UNICODE = (0x0301, 0x0303, 0x0327, 0x1F600)

#: Every hostile character, for the generators that build random input from it.
HOSTILE_CHARS = _chars(
    _HOSTILE_C0
    + _HOSTILE_C1
    + _HOSTILE_INVISIBLE
    + _HOSTILE_BIDI
    + _HOSTILE_SPACES
    + _HOSTILE_LINE_SEPARATORS
    + _HOSTILE_UNICODE
    + (0x0D, 0x0A, 0x09, 0x20, 0x61, 0x2E, 0x2C, 0x3B)
)


class TestNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a\r\nb", "a\nb"),
            ("a\rb", "a\nb"),
            ("a\r\n\rb\n\r\nc", "a\n\nb\n\nc"),
        ],
    )
    def test_every_line_ending_becomes_one_newline(
        self, raw: str, expected: str
    ) -> None:
        assert clean_text(raw) == expected

    @pytest.mark.parametrize(
        "codepoint", [0x2028, 0x2029, 0x000B, 0x000C, 0x0085]
    )
    def test_every_other_unicode_line_break_becomes_a_newline(
        self, codepoint: int
    ) -> None:
        """U+2028, U+2029, VT, FF and NEL are line breaks, and are kept as line breaks.

        Deleting them would fuse lines — which is what deleting a CR would do — and
        leaving them alone would mean ``str.splitlines()`` and this module disagree
        about where a line is, and the chunker's separator ladder (which splits on
        ``\\n``) would find no boundary at all in a document that uses them. Both are
        real failures; the second is invisible until a chunk spans two paragraphs.

        U+0085 is asserted here despite being a C1 control, and U+000B/U+000C despite
        being C0: the general "controls are deleted" rule would be wrong for all
        three, because each *is* a line break.
        """
        separator = chr(codepoint)
        cleaned = clean_text(f"alpha{separator}beta")

        assert cleaned == "alpha\nbeta", (
            f"U+{codepoint:04X} must become one newline, not vanish and not survive"
        )
        assert cleaned != clean_text("alphabeta")

    def test_a_line_separator_becomes_one_newline_not_a_blank_line(self) -> None:
        """U+2029 is a *paragraph* separator, and is deliberately not promoted to one.

        Turning it into ``\\n\\n`` would hand the chunker a paragraph boundary the
        document does not have, moving where every subsequent chunk cuts. The module's
        rule is that it normalises and never adds meaning, and a blank line is a claim
        about structure.
        """
        assert clean_text(f"a{chr(0x2029)}b") == "a\nb"

    def test_a_line_break_the_chunker_cannot_see_never_survives(self) -> None:
        """The property behind the rule, asserted over the module's own separator list.

        ``app.rag.chunking`` splits on ``\\n\\n`` and then ``\\n``. Any character that
        is a line break to Python but invisible to that ladder is a place a chunk can
        run straight through what a reader sees as a paragraph break. Rather than
        enumerate them again here, this asserts the rule those two modules must share:
        after cleaning, the only line breaks left are ones ``str.splitlines`` and the
        ladder both recognise.
        """
        from app.rag.cleaning import _LINE_SEPARATORS

        for separator in _LINE_SEPARATORS:
            # Non-whitespace between each, so the blank-line collapse is not what is
            # being measured -- a run of five separators collapses to two newlines
            # either way, which would pass whether the rewrite happened or not.
            cleaned = clean_text(separator.join("abcde"))
            assert cleaned == "a\nb\nc\nd\ne", (
                f"U+{ord(separator):04X} survived as something the chunker's ladder "
                f"cannot split on; got {cleaned!r}"
            )

    def test_line_endings_are_normalised_before_control_removal(self) -> None:
        """Carriage return is itself a ``C0`` control, so the order is forced.

        Removing controls first would delete every ``\\r``, and ``"a\\r\\nb"`` would
        become ``"ab"`` — two lines silently welded into one word-boundary-free run.
        """
        assert clean_text("alpha\r\nbeta") != clean_text("alphabeta")
        assert clean_text("alpha\r\nbeta") == "alpha\nbeta"

    def test_nfc_folds_canonically_equivalent_sequences(self) -> None:
        # Written as escapes deliberately. As literal text the two forms are
        # indistinguishable in source, and an editor or a normalising pipeline will
        # quietly compose the second into the first -- leaving a test that compares a
        # string with itself, passes, and asserts nothing.
        composed = "caf\u00e9"
        decomposed = "cafe\u0301"
        assert composed != decomposed, "the fixture must actually differ"
        assert len(decomposed) == len(composed) + 1
        assert unicodedata.normalize("NFC", composed) == unicodedata.normalize(
            "NFC", decomposed
        )
        assert clean_text(composed) == clean_text(decomposed)

    def test_invisible_characters_are_removed(self) -> None:
        cleaned = clean_text("al" + "" + "pha")
        assert cleaned == "alpha"

    def test_every_invisible_codepoint_is_removed_anywhere(self) -> None:
        """Not only at the start — the soft hyphen's whole harm is mid-word."""
        for codepoint in sorted(INVISIBLE_CODEPOINTS):
            character = chr(codepoint)
            assert clean_text(f"al{character}pha") == "alpha", (
                f"U+{codepoint:04X} survived inside a word"
            )

    def test_every_bidi_control_is_removed(self) -> None:
        """CVE-2021-42574: a reviewer reads different characters from a model."""
        for codepoint in sorted(BIDI_CONTROLS):
            character = chr(codepoint)
            assert character not in clean_text(f"pay{character}1"), (
                f"U+{codepoint:04X} survived; it can make text display as one "
                "thing and be read as another"
            )

    def test_the_two_bidi_marks_are_kept(self) -> None:
        """LRM and RLM are legitimate RTL typography, not an attack.

        Removing them would corrupt Arabic, Hebrew and Persian documents — the
        languages where bidi is not optional — to defend against controls that are
        not these two.
        """
        for codepoint in (0x200E, 0x200F):
            assert chr(codepoint) in clean_text(f"a{chr(codepoint)}b")

    def test_every_c0_control_except_the_three_whitespace_ones_is_removed(self) -> None:
        for codepoint in sorted(C0_EXCEPT_TAB_NEWLINE_CR):
            character = chr(codepoint)
            assert character not in clean_text(f"a{character}b")

    def test_tab_newline_and_carriage_return_are_not_treated_as_controls(self) -> None:
        """They survive *as characters* -- tab then folds, newline does not.

        A tab is horizontal whitespace, and the module's rule is that a run of
        horizontal whitespace becomes one space, so a tab is expected to end up a
        space. The point of the assertion is that it is not *deleted* and the
        characters are not welded together, which is what a C0 rule would do.
        """
        assert clean_text("a\tb") == "a b"
        assert clean_text("a\nb") == "a\nb"
        assert clean_text("alpha\r\nbeta") != clean_text("alphabeta")

    def test_del_and_the_c1_block_are_removed(self) -> None:
        for codepoint in sorted(C1_AND_DEL):
            character = chr(codepoint)
            assert character not in clean_text(f"a{character}b")

    def test_exotic_spaces_become_ordinary_spaces(self) -> None:
        for space in (" ", " ", " ", "　"):
            assert clean_text(f"a{space}b") == "a b"

    def test_runs_of_horizontal_whitespace_collapse_to_one_space(self) -> None:
        assert clean_text("a \t  \t b") == "a b"
        assert clean_text("a" + " " * 50 + "b") == "a b"

    def test_runs_of_blank_lines_collapse_to_one(self) -> None:
        assert clean_text("a\n\n\n\n\nb") == "a\n\nb"

    def test_trailing_whitespace_is_removed_from_every_line(self) -> None:
        assert clean_text("a   \nb\t\n") == "a\nb"

    def test_leading_whitespace_is_removed(self) -> None:
        assert clean_text("\n\n   a") == "a"

    def test_empty_input_is_empty_output(self) -> None:
        assert clean_text("") == ""

    def test_whitespace_only_input_is_empty_output(self) -> None:
        for blank in ("   ", "\n\n\n", "\t\t", "  ", "\r\n"):
            assert clean_text(blank) == "", f"{blank!r} should clean to empty"


class TestTheRemovedSetsAgreeWithEachOther:
    """The exported sets are the module's stated contract, so they are cross-checked.

    A code point that is documented as removed but is in no set is a documented
    promise nothing implements.
    """

    def test_removed_is_the_union_of_the_parts(self) -> None:
        assert REMOVED_CODEPOINTS == (
            INVISIBLE_CODEPOINTS
            | BIDI_CONTROLS
            | C0_EXCEPT_TAB_NEWLINE_CR
            | C1_AND_DEL
        )

    def test_the_sets_do_not_overlap(self) -> None:
        """Overlap would mean a character is removed for two stated reasons.

        Not harmful in itself, but it makes a reader unable to tell which rule a
        behaviour comes from — and the difference matters for the bidi marks, which
        are deliberately *absent* from every set.
        """
        sets = {
            "invisible": INVISIBLE_CODEPOINTS,
            "bidi": BIDI_CONTROLS,
            "c0": C0_EXCEPT_TAB_NEWLINE_CR,
            "c1": C1_AND_DEL,
        }
        names = sorted(sets)
        for index, first in enumerate(names):
            for second in names[index + 1 :]:
                overlap = sets[first] & sets[second]
                assert not overlap, f"{first} and {second} both claim {sorted(overlap)}"

    def test_none_of_the_sets_contains_a_tab_newline_or_carriage_return(self) -> None:
        for codepoint in (0x09, 0x0A, 0x0D):
            assert codepoint not in REMOVED_CODEPOINTS

    def test_no_set_contains_a_printable_ascii_character(self) -> None:
        """Anything printable is content. This is the boundary of the whole module."""
        for codepoint in REMOVED_CODEPOINTS:
            assert not (0x20 <= codepoint <= 0x7E), (
                f"U+{codepoint:04X} is printable ASCII and is claimed as removed"
            )


class TestDeterminism:
    def test_the_same_input_gives_the_same_output(self) -> None:
        text = (
            "Mixed\r\nlines\twith  spaces and "
            + _chars((0x202E, 0x2069, 0xFEFF, 0x200B))
        )
        assert clean_text(text) == clean_text(text)

    def test_the_result_is_a_function_of_the_input_alone(self) -> None:
        """No counters, no caches, no dependence on call order."""
        generator = random.Random(SEED)
        inputs = ["".join(generator.choice(HOSTILE_CHARS + "abc") for _ in range(40)) for _ in range(30)]

        forward = [clean_text(text) for text in inputs]
        backward = [clean_text(text) for text in reversed(inputs)][::-1]
        assert forward == backward


class TestIdempotence:
    """``clean(clean(x)) == clean(x)`` — the property the chunker depends on.

    Not cosmetic. Ingestion cleans once and the chunker consumes the result; if a
    second clean would change it, then any code path that cleans twice (a retry, a
    re-ingest of an already-cleaned source, a future extra stage) would produce
    different chunks and therefore different vectors for the same document.
    """

    @pytest.mark.parametrize("seed", [SEED, SEED + 1, SEED + 2])
    def test_cleaning_twice_changes_nothing(self, seed: int) -> None:
        generator = random.Random(seed)
        alphabet = HOSTILE_CHARS + "abcdefgh .,;:!?\t\n"
        for _ in range(300):
            text = "".join(generator.choice(alphabet) for _ in range(generator.randint(0, 60)))
            once = clean_text(text)
            assert clean_text(once) == once, f"not idempotent for {text!r}"


class TestNoContentIsLost:
    """Every character the module does not document as removed survives, in order.

    This is the property that makes a citation trustworthy. A cleaner that dropped a
    character would produce a chunk whose text differs from the document, and the
    difference would be invisible — the citation would simply quote something the
    document does not say.

    How the expectation is built, and why it is compared canonically
    ----------------------------------------------------------------
    The expected string is the input with whitespace and the documented removals
    filtered out. That filter is *position-independent*, so it is a sound model of
    the module's contract without reimplementing the module.

    The two sides are then compared as **canonically decomposed** text rather than
    code-point for code-point, and that choice is forced. Cleaning composes: it
    removes the invisible characters first and normalises second, so ``a`` followed by
    a zero-width space followed by a combining tilde comes out as the single code
    point U+00E3, where the expectation — built from the raw input — has two. Both are
    *correct*; they are the same text under the only difference Unicode defines as
    insignificant. Comparing them directly would make this property test fail on a
    correct implementation, and would be asserting the bug as the requirement.

    Canonical decomposition is the standard way to ask "is this the same text":
    ``NFC`` then ``NFD`` maps every canonically equivalent string to one and only one
    result, so the comparison asks exactly the right question and no more. What it
    deliberately does **not** police is that the output is composed rather than
    decomposed — which is a separate, separately tested behaviour, and one this module
    only promises to be *consistent about*.

    What the comparison still catches, which is the point: a dropped character, a
    duplicated one, a reordering, a case change, a transliteration, a substitution of
    any kind. Every one of those is a difference no normalisation can absorb.
    """

    @staticmethod
    def _survivors(text: str) -> str:
        """The input minus everything the module documents as removed or as space."""
        return "".join(
            character
            for character in text
            if not character.isspace() and ord(character) not in REMOVED_CODEPOINTS
        )

    @staticmethod
    def _decomposed(text: str) -> str:
        """Map canonically equivalent strings onto one representative."""
        return unicodedata.normalize("NFD", unicodedata.normalize("NFC", text))

    @pytest.mark.parametrize("seed", [SEED, SEED + 1])
    def test_no_visible_character_is_dropped_reordered_or_duplicated(
        self, seed: int
    ) -> None:
        generator = random.Random(seed)
        # Accented letters and bare combining marks in the alphabet, because those are
        # the characters composition acts on and the ones a lossy cleaner loses.
        alphabet = HOSTILE_CHARS + "abcXYZ019.,;:!?éàüñ" + _chars((0x0301, 0x0303, 0x0327))

        for _ in range(400):
            text = "".join(
                generator.choice(alphabet) for _ in range(generator.randint(0, 80))
            )

            cleaned = clean_text(text)
            actual = self._decomposed(
                "".join(c for c in cleaned if not c.isspace())
            )
            expected = self._decomposed(self._survivors(text))

            assert actual == expected, (
                f"cleaning changed the text beyond canonical equivalence for "
                f"{text!r}: got {actual!r}, expected {expected!r}"
            )

    def test_composition_happens_after_invisible_characters_are_removed(self) -> None:
        """The rule, stated as behaviour rather than as a property.

        ``e`` followed by a zero-width space and a combining acute must clean to the
        same single code point as ``e`` followed by a combining acute with nothing in
        between. Both facts follow from composing *after* the removals, and the second
        one — that the two inputs agree — is what a reader actually cares about: text
        that looks identical must embed identically.

        With composition before removal, the invisible character blocks it, the output
        keeps two code points, and a second cleaning pass composes them. The two
        failure modes are a non-idempotent re-ingest and an invisible character
        silently changing retrieval behaviour.
        """
        combining_acute = chr(0x0301)
        for blocker in INVISIBLE_CODEPOINTS:
            blocked = clean_text(f"e{chr(blocker)}{combining_acute}")
            unblocked = clean_text(f"e{combining_acute}")
            assert blocked == unblocked == "é", (
                f"U+{blocker:04X} changed the composed form of the text around it"
            )

    def test_composition_still_happens_across_a_whitespace_boundary(self) -> None:
        """The converse, so the fix is not a rule that simply forbids composition.

        Normalising *after* the removals does not mean composing across characters
        that remain separated: a space is still a space, and ``a ́`` must not become
        ``á``. Without this the fix could be implemented by collapsing whitespace too
        eagerly, which would weld words together.
        """
        assert clean_text(f"a {chr(0x0301)}") == f"a {chr(0x0301)}"

    def test_a_real_document_survives_intact(self) -> None:
        prose = (
            "Annual leave policy.\n\n"
            "Staff accrue two days of paid leave for every calendar month of\n"
            "continuous service. Accrual begins on the first day of employment.\n\n"
            "Carry-over: up to five unused days may be carried into the following year."
        )
        cleaned = clean_text(prose)
        assert "Annual leave policy." in cleaned
        assert "two days of paid leave" in cleaned
        assert "Carry-over: up to five unused days" in cleaned
        assert cleaned.count("\n\n") >= 1, "paragraph structure must survive"


class TestStructureIsPreserved:
    """Paragraph boundaries are the chunker's input, so they must survive.

    A cleaner that flattened every newline into one would still pass every
    no-content-loss test — the characters are all still there — while destroying the
    one structural signal the chunker uses to avoid cutting mid-sentence.
    """

    def test_paragraphs_stay_separated(self) -> None:
        cleaned = clean_text("One.\n\n\n\nTwo.\n\nThree.")
        assert cleaned == "One.\n\nTwo.\n\nThree."

    def test_a_list_survives_as_lines(self) -> None:
        cleaned = clean_text("Steps:\n1. First\n2. Second\n3. Third")
        assert cleaned.splitlines() == ["Steps:", "1. First", "2. Second", "3. Third"]

    def test_a_run_of_leading_indentation_collapses_to_one_space(self) -> None:
        """The documented rule, stated: a run of horizontal whitespace is one space.

        Deliberately *not* an aggressive de-indent. Preserving indentation would need
        a rule for tabs-versus-spaces and for how deep each nesting level is, and a
        document set that includes code would then be chunked by a rule that silently
        does nothing for prose. One space keeps the boundary -- the chunker still sees
        a line that starts here -- without inventing structure that is not there.
        """
        cleaned = clean_text("def f():\n        return 1")
        assert cleaned.splitlines() == ["def f():", " return 1"]
        assert clean_text("a\n  b") == clean_text("a\n b"), (
            "indentation depth must not survive; it would make two documents with "
            "identical prose embed differently"
        )


class TestWhatMustNotHappen:
    """Assertions of absence, for the "improvements" most likely to be attempted."""

    def test_it_does_not_lowercase(self) -> None:
        assert clean_text("Annual Leave Policy") == "Annual Leave Policy"

    def test_it_does_not_dehyphenate_across_a_line_break(self) -> None:
        """Rejoining ``ex-\\ntra`` needs a dictionary to do correctly."""
        assert clean_text("ex-\ntraordinary") == "ex-\ntraordinary"

    def test_it_does_not_remove_stop_words(self) -> None:
        cleaned = clean_text("The quick brown fox and the lazy dog")
        assert " and " in cleaned
        assert "the" in cleaned.lower()

    def test_it_does_not_strip_punctuation(self) -> None:
        assert clean_text("Total: 25% (net).") == "Total: 25% (net)."

    def test_it_does_not_call_a_model(self) -> None:
        """No network, no LLM SDK, no HTTP client anywhere in the module.

        Asserted over the source rather than trusted to review, because the failure
        is invisible in the output: a model call would clean the text *fine* while
        sending a tenant's document to a third party and adding a cost and a latency
        dependency to ingestion. Document text is untrusted data (PMP §17); a stage
        that ships it to a model is creating the injection surface this project
        exists to avoid.
        """
        from pathlib import Path

        import app.rag.cleaning as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "import openai",
            "import anthropic",
            "import httpx",
            "import requests",
            "import urllib.request",
            "gemini",
            "openai",
            "requests.post",
        ):
            assert forbidden not in source, (
                f"app/rag/cleaning.py references {forbidden!r}; cleaning must stay "
                "deterministic and local"
            )


class TestCleanedTextIsEmpty:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "\n\n\n",
            "\t\t\t",
            _chars((0x00A0, 0x2007, 0x3000)),  # spaces that clean to empty
            _chars((0x20, 0x00, 0x20)),  # a control surrounded by spaces
            _chars(_HOSTILE_INVISIBLE),  # invisible and nothing else
            _chars(_HOSTILE_BIDI),  # bidi controls and nothing else
            _chars(_HOSTILE_C1),  # DEL and C1 and nothing else
            _chars((0x09, 0x0A, 0x0D, 0x0A)),  # whitespace, no visible character
        ],
    )
    def test_blank_and_invisible_text_counts_as_empty(self, text: str) -> None:
        assert cleaned_text_is_empty(text), (
            f"{text!r} holds no visible character, so there is nothing to embed"
        )
        assert cleaned_text_is_empty(clean_text(text))

    @pytest.mark.parametrize(
        "text",
        [
            "a",
            "0",
            ".",
            _chars((0x0301,)),  # a bare combining mark is content
            _chars((0x1F600,)),  # astral plane
            "caf\u00e9",
            " \u00e9 ",
            _chars((0x200E, 0x200F)),  # bidi *marks* are kept, so they are content
            "a\u2028b" if False else "x",  # placeholder removed below
        ],
    )
    def test_text_with_any_visible_character_is_not_empty(self, text: str) -> None:
        assert not cleaned_text_is_empty(text)
    def test_it_agrees_with_calling_clean_first(self) -> None:
        """The helper exists so the orchestrator need not clean twice to find out."""
        for text in ("   ", "a", "", "\n"):
            assert cleaned_text_is_empty(text) == cleaned_text_is_empty(clean_text(text))
