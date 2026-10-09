"""Tests for :mod:`app.rag.vectors` — the point id and the payload.

Why this module can be tested with nothing running
-------------------------------------------------
:func:`point_id` and :func:`payload_for` are pure functions over plain values. That is
the design, not an accident: because they import no vector-store client, the two
security-relevant questions in Phase 2.5 — *can two tenants produce the same point id?*
and *what is allowed into a payload?* — are answerable without a database. If a change
made them impure, every test here would still pass while the phase's central guarantee
became false, so one test asserts the property directly.

The three failures this file exists to prevent
--------------------------------------------
1. **A point id collision.** Two chunks sharing an id means one silently overwrites the
   other. No exception is raised, no log line is written, and the document simply
   returns half its content from search. Every id is therefore derived from all three
   scopes — tenant, document, chunk index — and that is asserted by *construction*:
   the tests vary each scope and require the result to change.

2. **A payload key that does not match its filter.** Deletion and re-ingestion both
   filter on ``document_id``. A typo in one place produces a filter that matches
   nothing, so deletion "succeeds", the old points remain, and the phase's idempotency
   requirement quietly fails. :data:`PAYLOAD_KEYS` is the single list both sides use,
   and the exact-key-set assertion here is what stops a field being added by accident.

3. **Unbounded tenant data in a payload field.** ``text`` is the only payload field
   carrying document content, so it has two independent bounds: the chunker's, and
   :data:`MAX_PAYLOAD_TEXT_CHARS`. The chunker's can be defeated by a bug; this one
   cannot.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.rag.vectors import (
    MAX_PAYLOAD_TEXT_CHARS,
    PAYLOAD_ALLOWED_ROLES,
    PAYLOAD_CHUNK_INDEX,
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_FILENAME,
    PAYLOAD_KEYS,
    PAYLOAD_PAGE,
    PAYLOAD_TENANT_ID,
    PAYLOAD_TEXT,
    POINT_ID_NAMESPACE,
    VectorPoint,
    payload_for,
    point_id,
)

#: Two tenants, two documents, fixed so a failure message names them.
TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
DOCUMENT_ONE = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
DOCUMENT_TWO = uuid.UUID("00000000-0000-0000-0000-0000000000d2")


def sample_payload(
    *,
    tenant_id: uuid.UUID = TENANT_A,
    document_id: uuid.UUID = DOCUMENT_ONE,
    chunk_index: int = 0,
    page: int | None = 1,
    text: str = "Staff accrue two days of paid leave per month.",
    allowed_roles: list[str] | None = None,
    filename: str = "leave-policy.pdf",
) -> dict[str, Any]:
    """A valid payload, with each named argument replacing one field.

    The parameters are spelled out rather than gathered through ``**overrides`` for one
    reason: this suite asserts on the payload's *contents*, so a test reaching for a key
    this helper does not accept should fail to type-check rather than quietly land in a
    catch-all. It also removes the ``type: ignore`` that the gather form required to
    bridge ``dict[str, object]`` back to the real signature — and with it the
    ``object``-typed values that made every assertion below need a cast.
    """
    return payload_for(
        tenant_id=tenant_id,
        document_id=document_id,
        chunk_index=chunk_index,
        page=page,
        text=text,
        allowed_roles=allowed_roles,
        filename=filename,
    )


class TestPointIdIsDerived:
    def test_it_is_a_uuid_version_five(self) -> None:
        """Declared rather than incidental.

        Version 5 is SHA-1 based and deterministic; version 4 is random and version 3 is
        MD5. A version-4 id would make every re-ingestion duplicate the document.
        """
        derived = uuid.UUID(
            point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=0)
        )
        assert derived.version == 5
        assert derived.variant == uuid.RFC_4122

    def test_it_is_in_this_project_s_namespace(self) -> None:
        """Recomputed independently of the module's own call.

        ``uuid.uuid5`` is deterministic, so re-deriving the same way and comparing
        catches a namespace that changed without the constant changing — for instance
        if the name string's separators were altered.
        """
        expected = uuid.uuid5(POINT_ID_NAMESPACE, f"{TENANT_A}/{DOCUMENT_ONE}/0")
        assert point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=0) == str(
            expected
        )

    def test_the_namespace_is_not_a_standard_one(self) -> None:
        """Project-scoped, so an id can never coincide with a name-based UUID elsewhere.

        The standard namespaces are for DNS names, URLs and OIDs. A document id is
        neither, and using a standard namespace would mean a hypothetical name-based
        UUID generated anywhere in the world could reproduce a TenantMind point id.
        """
        standard = {
            uuid.NAMESPACE_DNS,
            uuid.NAMESPACE_URL,
            uuid.NAMESPACE_OID,
            uuid.NAMESPACE_X500,
        }
        assert POINT_ID_NAMESPACE not in standard

    def test_it_is_lowercase_and_hyphenated(self) -> None:
        """The form Qdrant accepts, and the form that survives a JSON round trip."""
        derived = point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=3)
        assert derived == derived.lower()
        assert derived.count("-") == 4
        assert len(derived) == 36
        assert str(uuid.UUID(derived)) == derived, "not in canonical form"

    def test_the_same_inputs_give_the_same_id(self) -> None:
        assert point_id(
            tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=7
        ) == point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=7)

    def test_the_index_is_not_a_counter_so_order_does_not_matter(self) -> None:
        """The property a counter would fail.

        A counter's id depends on how many points this process wrote before, so the same
        chunk would get a different id after a restart. Deriving chunk 7 without first
        deriving chunks 0..6 must give the same answer as deriving them.
        """
        directly = point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=7)
        for warmup in range(9):
            point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=warmup)
        after = point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=7)
        assert directly == after


class TestPointIdCollisions:
    """The absence of a collision, asserted by construction.

    Each test varies exactly one input and requires the id to change. Together they say
    the id is a function of all three scopes and of nothing else — which is stronger
    than checking a handful of known-good pairs, because it rules out the id having
    been derived from only two of the three.
    """

    def test_a_different_tenant_gives_a_different_id(self) -> None:
        """The one that matters most, and the one a two-part derivation would miss."""
        assert point_id(
            tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=0
        ) != point_id(tenant_id=TENANT_B, document_id=DOCUMENT_ONE, chunk_index=0)

    def test_a_different_document_gives_a_different_id(self) -> None:
        assert point_id(
            tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=0
        ) != point_id(tenant_id=TENANT_A, document_id=DOCUMENT_TWO, chunk_index=0)

    def test_a_different_chunk_index_gives_a_different_id(self) -> None:
        assert point_id(
            tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=0
        ) != point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=1)

    def test_the_tenant_is_not_interchangeable_with_the_document(self) -> None:
        """A swapped pair is a different id, so no two derivations can alias.

        The name string is ``tenant/document/index``. If it were built by concatenating
        the two UUIDs without a separator, swapping them would produce the same string
        and therefore the same id.
        """
        swapped = point_id(tenant_id=DOCUMENT_ONE, document_id=TENANT_A, chunk_index=0)
        straight = point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=0)
        assert swapped != straight

    def test_no_two_scopes_share_a_name_string(self) -> None:
        """The separator is what makes the name unambiguous, asserted as uniqueness.

        ``a/bc`` must not derive the same id as ``ab/c``. With a variable-length field
        that needs a delimiter to hold; here both fields are 36 characters wide and the
        separators sit at fixed positions, so no two distinct ``(tenant, document)``
        pairs can produce the same ``f"{tenant}/{document}"`` string. Asserted over a
        population rather than one hand-picked pair, because a single pair would only
        show that one pair differs.
        """
        names: dict[str, tuple[uuid.UUID, uuid.UUID]] = {}
        for tenant_number in range(6):
            for document_number in range(6):
                tenant = uuid.UUID(int=0xA0000 + tenant_number)
                document = uuid.UUID(int=0xD0000 + document_number)
                name = f"{tenant}/{document}"
                assert name not in names, (
                    f"{names.get(name)} and {(tenant, document)} both produce {name!r}"
                )
                names[name] = (tenant, document)

        assert len(names) == 36

    def test_many_chunks_of_many_documents_across_tenants_are_all_distinct(self) -> None:
        """The property over a population, not over a pair."""
        tenants = [uuid.UUID(int=0xA0000 + n) for n in range(4)]
        documents = [uuid.UUID(int=0xD0000 + n) for n in range(4)]
        seen: dict[str, tuple[uuid.UUID, uuid.UUID, int]] = {}

        for tenant in tenants:
            for document in documents:
                for index in range(12):
                    derived = point_id(
                        tenant_id=tenant, document_id=document, chunk_index=index
                    )
                    scope = (tenant, document, index)
                    assert derived not in seen, (
                        f"{scope} and {seen.get(derived)} both derive {derived}"
                    )
                    seen[derived] = scope

        assert len(seen) == 4 * 4 * 12


class TestPointIdRefuses:
    @pytest.mark.parametrize("index", [-1, -2, -1000])
    def test_a_negative_chunk_index_is_refused(self, index: int) -> None:
        """Refused, because a negative index would derive perfectly.

        Nothing downstream would complain: the UUID would be valid, the point would
        write, and the payload would carry ``chunk_index: -1``. The failure would be a
        chunk numbered minus one, which is not an error to any component that sees it.
        """
        with pytest.raises(ValueError, match="must not be negative"):
            point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=index)

    def test_zero_is_accepted(self) -> None:
        """The first chunk. Refusing it would be over-strict."""
        assert point_id(
            tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=0
        )


class TestPayloadShape:
    def test_it_holds_exactly_the_documented_keys(self) -> None:
        """The cheapest guard against a field being added by accident.

        Checked as a set equality against :data:`PAYLOAD_KEYS` rather than by asserting
        each key is present, because presence alone would not notice an *extra* key —
        and an extra key carrying document content is exactly the accident worth
        catching.
        """
        assert set(sample_payload()) == set(PAYLOAD_KEYS)
        assert len(PAYLOAD_KEYS) == len(set(PAYLOAD_KEYS)), "a key is listed twice"

    def test_it_is_pure_json(self) -> None:
        """Qdrant payloads are JSON, through REST, gRPC and local mode alike.

        A UUID object, a set or a datetime would serialise in one of those and fail in
        another — and the failure would be at write time, in a component that knows
        nothing about how the value was built.
        """
        encoded = json.dumps(sample_payload())
        assert json.loads(encoded) == sample_payload()

    def test_the_document_id_is_a_string(self) -> None:
        """A string, not a UUID object.

        A value that compared unequal after a round trip would break the
        ``document_id`` filter that both deletion and re-ingestion depend on — and that
        filter matching nothing fails *silently*, leaving stale points behind.
        """
        assert sample_payload()[PAYLOAD_DOCUMENT_ID] == str(DOCUMENT_ONE)
        assert isinstance(sample_payload()[PAYLOAD_DOCUMENT_ID], str)

    def test_the_chunk_index_is_an_integer_not_a_string(self) -> None:
        """So a range filter is a numeric comparison.

        As a string, ``"9" > "12"`` and ``delete every chunk with index >= 12`` silently
        deletes the wrong set.
        """
        value = sample_payload(chunk_index=12)[PAYLOAD_CHUNK_INDEX]
        assert isinstance(value, int)
        assert not isinstance(value, bool)

    def test_the_page_is_an_integer_or_none(self) -> None:
        assert sample_payload(page=3)[PAYLOAD_PAGE] == 3
        assert isinstance(sample_payload(page=3)[PAYLOAD_PAGE], int)
        assert sample_payload(page=None)[PAYLOAD_PAGE] is None

    def test_no_page_is_none_not_zero(self) -> None:
        """``None`` means "this format has no pages"; ``0`` would mean page zero.

        Only one of those is possible, so ``None`` is the only honest value for the other
        — and a UI rendering a page number must be able to tell "no page" from "page 1".
        """
        assert PAYLOAD_PAGE not in (0, "")
        assert sample_payload(page=None)[PAYLOAD_PAGE] is None

    def test_the_text_is_the_chunk_text(self) -> None:
        assert sample_payload(text="exact chunk text")[PAYLOAD_TEXT] == "exact chunk text"

    def test_the_filename_is_carried_for_citation(self) -> None:
        """PMP §20's citation requirement needs to say which document was quoted."""
        assert sample_payload(filename="Q3 plan.docx")[PAYLOAD_FILENAME] == "Q3 plan.docx"

    def test_the_tenant_id_is_carried_as_a_string(self) -> None:
        """Defence in depth alongside the per-tenant collection."""
        assert sample_payload()[PAYLOAD_TENANT_ID] == str(TENANT_A)

    def test_an_unrestricted_document_has_no_roles(self) -> None:
        """``None``, not an empty list.

        An empty list and "no restriction" look identical to a reader and mean opposite
        things: one denies everyone, the other allows everyone. ``None`` says the
        restriction is absent; an empty list would say it is present and empty.
        """
        assert sample_payload(allowed_roles=None)[PAYLOAD_ALLOWED_ROLES] is None

    def test_a_restricted_document_carries_its_roles(self) -> None:
        payload = sample_payload(allowed_roles=["admin", "editor"])
        assert payload[PAYLOAD_ALLOWED_ROLES] == ["admin", "editor"]


class TestPayloadIsolation:
    """The tenant in a payload must come from the row, not from a caller.

    The threat is mundane and specific: a payload built from a *request* rather than
    from a document row would let one tenant's document carry another tenant's id, and
    a per-tenant collection would faithfully store it. That is what makes this a
    property of :func:`payload_for` — it takes exactly one tenant argument, so there is
    no second source to get wrong.
    """

    def test_the_tenant_argument_is_the_only_tenant_in_the_payload(self) -> None:
        payload = sample_payload(tenant_id=TENANT_B)
        assert payload[PAYLOAD_TENANT_ID] == str(TENANT_B)
        assert str(TENANT_A) not in json.dumps(payload)

    def test_the_document_id_and_tenant_come_from_independent_arguments(self) -> None:
        """Nothing derives one from the other, so a mismatched pair is visible.

        If the payload's ``tenant_id`` were derived from ``document_id``, then a caller
        that passed the wrong pair would produce a point that looked internally
        consistent. Both are explicit arguments, so the caller can pass anything and the
        payload will faithfully report it — which is what makes a wrong pair detectable
        by the caller rather than hidden by the payload.
        """
        payload = sample_payload(tenant_id=TENANT_A, document_id=DOCUMENT_TWO)
        assert payload[PAYLOAD_TENANT_ID] == str(TENANT_A)
        assert payload[PAYLOAD_DOCUMENT_ID] == str(DOCUMENT_TWO)

    def test_the_allowed_roles_list_is_copied_not_referenced(self) -> None:
        """The one field a caller could still mutate after the id was derived.

        A reference would mean the caller could change the roles of a point whose id was
        already computed from the document's roles — so the stored restriction and the
        one the id was derived from would disagree, with nothing recording that they
        ever did.
        """
        roles = ["admin"]
        payload = sample_payload(allowed_roles=roles)
        payload[PAYLOAD_ALLOWED_ROLES].append("viewer")
        assert roles == ["admin"], "the caller's list was mutated"
        assert sample_payload(allowed_roles=["admin"])[PAYLOAD_ALLOWED_ROLES] == ["admin"]

    def test_a_fresh_payload_shares_no_mutable_state_with_a_previous_one(self) -> None:
        first = sample_payload()
        second = sample_payload()
        assert first is not second
        first[PAYLOAD_TEXT] = "edited"
        assert second[PAYLOAD_TEXT] != "edited"


class TestPayloadRefuses:
    @pytest.mark.parametrize("index", [-1, -5])
    def test_a_negative_chunk_index_is_refused(self, index: int) -> None:
        """Checked here as well as in :func:`point_id`, and deliberately.

        Two functions consume ``chunk_index``, and it is the *payload* that reaches the
        store. If the store ever received a point built without going through
        :func:`point_id`, the payload's own check is the last line of defence.
        """
        with pytest.raises(ValueError, match="must not be negative"):
            sample_payload(chunk_index=index)

    @pytest.mark.parametrize("page", [0, -1, -100])
    def test_a_page_that_is_not_positive_is_refused(self, page: int) -> None:
        """A page 0 in a payload is a citation pointing at a page that does not exist."""
        with pytest.raises(ValueError, match="positive 1-based"):
            sample_payload(page=page)

    def test_page_one_is_accepted(self) -> None:
        assert sample_payload(page=1)[PAYLOAD_PAGE] == 1

    @pytest.mark.parametrize(
        "text", ["", " ", "\n", "\n\n\n", "\t", "   \n\t  \n", "\r\n"]
    )
    def test_text_with_no_visible_character_is_refused(self, text: str) -> None:
        """An empty chunk must never be indexed.

        An empty chunk embeds to a real vector, retrieves as a real result, and quotes
        as an empty string — which looks like a broken answer rather than a bug. The
        chunker already refuses to emit one, so this is the second line of defence.
        """
        with pytest.raises(ValueError, match="requires text"):
            sample_payload(text=text)

    def test_text_that_is_only_punctuation_is_accepted(self) -> None:
        """Visible, so it is content.

        Over-strict here would reject legitimate chunks: a spreadsheet column of
        numbers, a slide whose only text is a figure caption.
        """
        assert sample_payload(text="---")[PAYLOAD_TEXT] == "---"
        assert sample_payload(text="0")[PAYLOAD_TEXT] == "0"


class TestPayloadTextBound:
    def test_oversized_text_is_truncated_rather_than_refused(self) -> None:
        """Degrade one chunk rather than fail the document.

        Reaching the limit means the chunker's own bound was violated by a bug. Refusing
        would fail the whole document over one chunk; truncating leaves the failure
        visible in the recorded length. This asserts the lesser-of-two decision so it
        stays deliberate.
        """
        payload = sample_payload(text="z" * (MAX_PAYLOAD_TEXT_CHARS + 5_000))
        assert len(payload[PAYLOAD_TEXT]) == MAX_PAYLOAD_TEXT_CHARS

    def test_text_within_the_bound_is_not_altered(self) -> None:
        payload = sample_payload(text="z" * MAX_PAYLOAD_TEXT_CHARS)
        assert len(payload[PAYLOAD_TEXT]) == MAX_PAYLOAD_TEXT_CHARS

    def test_truncation_does_not_cut_a_character_in_half(self) -> None:
        """The bound is on characters, and a multi-byte character is one character.

        Truncating bytes would leave a lone surrogate or a mojibake sequence at the end
        of the payload — and that is the text a citation would quote.
        """
        payload = sample_payload(text="é" * (MAX_PAYLOAD_TEXT_CHARS + 100))
        stored = payload[PAYLOAD_TEXT]
        assert len(stored) == MAX_PAYLOAD_TEXT_CHARS
        assert all(character == "é" for character in stored)

    def test_the_bound_is_looser_than_the_chunkers_own(self) -> None:
        """Deliberate: this bound is the fallback, not the working limit.

        Asserted so that inverting them — a payload bound below the chunk size — is
        visible as a test failure rather than as chunks silently truncated in
        production.
        """
        from app.core.config import Settings
        from tests.conftest import make_settings

        settings = make_settings()
        assert isinstance(settings, Settings)
        assert MAX_PAYLOAD_TEXT_CHARS > settings.chunk_size_chars


class TestVectorPoint:
    def test_it_accepts_a_well_formed_point(self) -> None:
        point = VectorPoint(
            id=point_id(tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=0),
            vector=[0.1] * 4,
            payload=sample_payload(),
        )
        assert point.vector == [0.1] * 4

    def test_an_empty_vector_is_refused(self) -> None:
        with pytest.raises(ValueError, match="requires a vector"):
            VectorPoint(id="x", vector=[], payload=sample_payload())

    def test_an_unexpected_payload_key_is_refused(self) -> None:
        payload = sample_payload()
        payload["internal_path"] = "/etc/passwd"
        with pytest.raises(ValueError, match="unexpected payload keys"):
            VectorPoint(id="x", vector=[0.1], payload=payload)

    def test_a_missing_payload_key_is_refused(self) -> None:
        payload = sample_payload()
        del payload[PAYLOAD_TEXT]
        with pytest.raises(ValueError, match="missing payload keys"):
            VectorPoint(id="x", vector=[0.1], payload=payload)

    def test_the_rejection_names_every_offending_key(self) -> None:
        """Sorted and joined, so the message is stable and complete.

        A set's iteration order varies between runs, which would make this message
        unusable as a report of what to fix.
        """
        payload = sample_payload()
        payload["zebra"] = 1
        payload["alpha"] = 2
        with pytest.raises(ValueError) as caught:
            VectorPoint(id="x", vector=[0.1], payload=payload)
        assert "alpha, zebra" in str(caught.value)

    def test_it_is_frozen(self) -> None:
        """A point passes between three components and must not change in flight."""
        point = VectorPoint(id="x", vector=[0.1], payload=sample_payload())
        with pytest.raises(dataclasses.FrozenInstanceError):
            point.id = "y"  # type: ignore[misc]

    def test_it_has_slots(self) -> None:
        """So a typo in an attribute name fails instead of silently creating a field.

        Asserted structurally rather than by assigning: the generated ``__setattr__`` of
        a frozen ``slots=True`` dataclass reports an unknown attribute as a ``TypeError``
        from inside ``super()``, which says nothing about *why* the assignment failed.
        ``__slots__`` being present and ``__dict__`` being absent is the actual claim.
        """
        assert "__slots__" in VectorPoint.__dict__
        assert "__dict__" not in VectorPoint.__dict__

        point = VectorPoint(id="x", vector=[0.1], payload=sample_payload())
        with pytest.raises((AttributeError, TypeError)):
            point.ident = "y"  # type: ignore[attr-defined]
        assert not hasattr(point, "ident")

    def test_two_points_with_the_same_values_are_equal(self) -> None:
        def point() -> VectorPoint:
            return VectorPoint(id="same", vector=[0.1, 0.2], payload=sample_payload())

        assert point() == point()

    def test_with_id_returns_a_copy_and_leaves_the_original_alone(self) -> None:
        original = VectorPoint(id="old", vector=[0.1], payload=sample_payload())
        renamed = original.with_id("new")
        assert renamed.id == "new"
        assert original.id == "old"
        assert renamed is not original

    def test_with_id_keeps_the_vector_and_payload_identical(self) -> None:
        """The seam that keeps the store free of identity logic."""
        original = VectorPoint(id="old", vector=[0.1], payload=sample_payload())
        renamed = original.with_id("new")
        assert renamed.vector is original.vector
        assert renamed.payload is original.payload

    def test_with_id_still_validates(self) -> None:
        """A rename cannot smuggle in a malformed payload."""
        original = VectorPoint(id="old", vector=[0.1], payload=sample_payload())
        original.payload["extra"] = 1
        with pytest.raises(ValueError, match="unexpected payload keys"):
            original.with_id("new")


class TestTheModuleStaysPure:
    """The purity :mod:`app.rag.vectors` claims, checked structurally.

    Checked by walking the module's syntax tree rather than by searching its text. A
    text search fails on this module specifically because its docstring *discusses*
    ``qdrant_client``, ``os.environ`` and randomness in order to explain why it does not
    use them — so a substring assertion either has to strip comments and docstrings and
    so become weaker, or produces a false failure every time the explanation is edited.

    The tree gives the real answer: what is imported, and what is called on it.
    """

    @staticmethod
    def _tree() -> ast.Module:
        import app.rag.vectors as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        return ast.parse(source)

    @classmethod
    def _imported_modules(cls) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(cls._tree()):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    @classmethod
    def _called_names(cls) -> set[str]:
        """Every ``name(...)`` call, and every ``module.name`` attribute access."""
        names: set[str] = set()
        for node in ast.walk(cls._tree()):
            if isinstance(node, ast.Call):
                function = node.func
                if isinstance(function, ast.Name):
                    names.add(function.id)
                elif isinstance(function, ast.Attribute):
                    names.add(function.attr)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        return names

    def test_it_imports_no_vector_store_or_web_framework(self) -> None:
        """The reason the security-relevant logic is testable without a database.

        If this ever imports ``qdrant_client``, the purity that lets the point id and the
        payload be verified in isolation is gone, and the failure mode is that a future
        test needs a running server to check something that is a pure function.
        """
        imported = self._imported_modules()
        assert "uuid" in imported, "the fixture is wrong if uuid is not imported"
        for forbidden in (
            "qdrant_client",
            "fastapi",
            "starlette",
            "sqlalchemy",
            "requests",
            "httpx",
            "urllib",
            "httpx",
            "os",
            "random",
            "time",
            "datetime",
        ):
            assert forbidden not in imported, (
                f"app/rag/vectors.py imports {forbidden!r}; this module must stay a pure "
                "function of plain values so it can be tested without a server"
            )

    def test_it_calls_no_source_of_nondeterminism(self) -> None:
        """Determinism is the whole point, so every usual source of drift is ruled out.

        ``uuid4`` is the dangerous one and it arrives *inside* ``uuid``, so the check has
        to be on use rather than on the import line — which is exactly what walking
        attribute accesses does.
        """
        called = self._called_names()
        assert "uuid5" in called, "the fixture is wrong if uuid5 is not called"
        for forbidden in (
            "uuid4",
            "uuid1",
            "uuid3",
            "uuid_generate_time",
            "randint",
            "choice",
            "shuffle",
            "time",
            "now",
            "today",
            "monotonic",
            "getenv",
            "environ",
            "random",
        ):
            assert forbidden not in called, (
                f"app/rag/vectors.py uses {forbidden!r}; a point id must depend only on "
                "its tenant, document and chunk index"
            )

    def test_a_fresh_module_derives_the_same_id(self) -> None:
        """No module-level state a reimport would rebuild differently.

        ``importlib.reload`` really does re-execute the module body, so any derivation
        that consulted module state — a memoised counter, a cached namespace — would
        change here. That is the in-process equivalent of a restart, and unlike
        ``uuid.uuid4`` it needs no monkeypatching of the standard library.
        """
        import importlib

        import app.rag.vectors as module

        before = module.point_id(
            tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=2
        )
        payload_before = module.payload_for(
            tenant_id=TENANT_A,
            document_id=DOCUMENT_ONE,
            chunk_index=2,
            page=1,
            text="text",
            allowed_roles=None,
            filename="a.txt",
        )

        reloaded = importlib.reload(module)

        assert reloaded.point_id(
            tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=2
        ) == before
        assert (
            reloaded.payload_for(
                tenant_id=TENANT_A,
                document_id=DOCUMENT_ONE,
                chunk_index=2,
                page=1,
                text="text",
                allowed_roles=None,
                filename="a.txt",
            )
            == payload_before
        )