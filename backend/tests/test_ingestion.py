"""Tests for :mod:`app.services.ingestion` — the Phase 2.5 orchestrator.

What is under test, and what is deliberately not
------------------------------------------------
This file proves the *orchestration* contract: which stage runs when, what is written to
``documents`` when each one fails, that a failure never becomes ``ready``, that
re-ingestion does not multiply chunks, and that two tenants cannot reach each other's
documents through the pipeline.

It does **not** re-prove parsing, cleaning, chunking, embedding or the Qdrant store.
Those have their own files (``test_parsing``, ``test_text_cleaning``, ``test_chunking``,
``test_embeddings``, ``test_qdrant_store``) and a real-server suite
(``test_qdrant_integration``). Duplicating their assertions here would make a change to
any of them fail in two files and teach nothing about the orchestrator.

Where the seams are, and why each one removes something real
------------------------------------------------------------
* **A fake Qdrant client** rather than a server. The orchestrator's contract with the
  store is "call these methods with these arguments"; how the server behaves is
  ``test_qdrant_integration``'s subject. The checks that genuinely need a server — the
  document→chunk→embedding→point chain against a real index, and a Tenant A / Tenant B
  round trip through real collections — are in :class:`TestAgainstRealQdrant` at the
  bottom and skip without ``TENANTMIND_TEST_QDRANT_URL``.
* **A fake storage root** rather than an upload. The upload endpoint is Phase 2.4's and
  is proven in ``test_uploads``; what matters here is that the pipeline reads through
  ``document.storage_key`` and never through anything a caller supplies.
* **A recording embedding provider** rather than a network call. Unit tests must never
  reach an embedding API, and a provider that records what it was asked lets the
  chunk→embedding pairing be checked rather than assumed.

The one thing that is *not* substituted
----------------------------------------
Failure recording. Every stage-failure test asserts against a real row, written by real
SQLAlchemy calls, because "recorded ``failed`` and preserved the error" *is* the
requirement and a mock asserting on a mock would prove nothing about it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.identity import AuthenticatedIdentity
from app.models import Document, DocumentStatus, UserRole
from app.rag.chunking import Chunk
from app.rag.embeddings import MAX_BATCH_SIZE, EmbeddingError, EmbeddingProvider
from app.rag.parsers import Page, ParsedDocument
from app.rag.vectors import (
    PAYLOAD_CHUNK_INDEX,
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_TENANT_ID,
    PAYLOAD_TEXT,
    VectorPoint,
    payload_for,
    point_id,
)
from app.services.documents import DocumentNotFoundError
from app.services.ingestion import (
    MAX_ERROR_CHARS,
    VECTOR_WRITE_BATCH_SIZE,
    IngestionError,
    IngestionStage,
    NoExtractableTextError,
    PreparedChunk,
    TransientIngestionError,
    _failure_message,
    check_provider_matches_configuration,
    elapsed_since,
    ingest_document,
    stage_build,
    stage_chunk,
    stage_embed,
    stage_parse,
    stage_prune,
    stage_read,
    stage_upsert,
)
from app.storage.local import (
    FileTooLargeError,
    LocalFileStore,
    StoredObjectMissingError,
)
from app.storage.qdrant import (
    QdrantVectorStore,
    VectorStoreError,
    collection_name_for,
)
from tests.conftest import make_settings
from tests.documents import build_pdf_text, build_txt
from tests.records import payload_key, payload_of, store_over, vector_of
from tests.test_qdrant_store import FakeQdrant

#: Width used throughout. Eight keeps vectors cheap to compare while remaining a legal
#: Qdrant width, so the same number works in the fake-client and real-server halves.
WIDTH = 8

#: Fixed so a failure message names the scope it failed in.
TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")

#: Content whose presence in a recorded error would be a leak. Distinctive enough that a
#: substring search cannot find it by accident.
SECRET = "ZEBRAFISH-CONFIDENTIAL-LEASE-TERMS"

#: The Qdrant server for the integration half. Named here rather than re-spelled.
QDRANT_URL_ENV_VAR = "TENANTMIND_TEST_QDRANT_URL"

REQUIRES_QDRANT = pytest.mark.skipif(
    not os.environ.get(QDRANT_URL_ENV_VAR, "").strip(),
    reason=(
        f"{QDRANT_URL_ENV_VAR} is not set, so no Qdrant server is configured. "
        "The Qdrant integration gate is therefore PENDING; see "
        "tests/test_qdrant_integration.py for how to run it."
    ),
)


# ---------------------------------------------------------------------------
# Collaborators
# ---------------------------------------------------------------------------


class RecordingProvider(EmbeddingProvider):
    """An embedding provider that records what it was asked and answers predictably.

    The vector is a function of the text rather than a constant, because a test that
    pairs the wrong vector with the wrong chunk has to be able to *detect* that — and a
    constant vector would hide the defect behind every comparison passing.
    """

    def __init__(self, *, dimensions: int = WIDTH) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []
        self.closed = False
        #: Set to make every call fail; ``fail_on_call`` to fail only the n-th.
        self.raise_with: BaseException | None = None
        self.fail_on_call: int | None = None

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return "recording-test-model"

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.raise_with is not None and (
            self.fail_on_call is None or len(self.calls) == self.fail_on_call
        ):
            raise self.raise_with
        return [vector_for(text) for text in texts]

    def close(self) -> None:
        self.closed = True


def vector_for(text: str, *, dimensions: int = WIDTH) -> list[float]:
    """A deterministic vector derived from ``text``.

    Bounded to ``(0, 1)`` so no component is zero and no value is out of range: a
    zero component would make the direction undefined, and a real provider never emits
    one either.
    """
    seed = sum((index + 1) * ord(character) for index, character in enumerate(text))
    return [
        ((seed * (position + 7)) % 89) / 89.0 + 0.01 for position in range(dimensions)
    ]


def _server_error(status: int, reason: str, body: bytes = b"") -> Any:
    """An ``UnexpectedResponse``, which is what Qdrant raises for a 4xx or 5xx."""
    import httpx
    from qdrant_client.http import exceptions

    return exceptions.UnexpectedResponse(status, reason, body, httpx.Headers())


def _prepared(text: str, index: int, *, page: int | None = 1) -> PreparedChunk:
    """A :class:`PreparedChunk` for a stage test.

    The ``start``/``end`` offsets are nominal — the chunker is what computes them, and a
    stage test is not testing them. What is real is the shape: a ``Chunk`` the carrier
    can wrap, plus the document-wide ordinal and page the later stages read.
    """
    return PreparedChunk(
        chunk=Chunk(index=index, text=text, start=0, end=len(text), page=page),
        page=page,
        chunk_index=index,
    )


def _document_row(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    filename: str = "policy.txt",
    roles: list[str] | None = None,
) -> Document:
    """An unpersisted :class:`Document`, for stages that never touch the database.

    A stage test should not need a committed row to prove what a stage does with a row.
    The orchestrator tests use real committed rows; these use this.
    """
    return Document(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        uploaded_by=user_id,
        filename=filename,
        storage_key=uuid.uuid4().hex,
        status=DocumentStatus.PROCESSING,
        allowed_roles=roles,
        size_bytes=0,
        content_type="text/plain",
    )


def _points(document: Document, *, count: int) -> list[VectorPoint]:
    """``count`` well-formed points for ``document``."""
    return [
        VectorPoint(
            id=point_id(
                tenant_id=document.tenant_id,
                document_id=document.id,
                chunk_index=index,
            ),
            vector=vector_for(f"chunk {index}"),
            payload=payload_for(
                tenant_id=document.tenant_id,
                document_id=document.id,
                chunk_index=index,
                page=1,
                text=f"chunk {index}",
                allowed_roles=document.allowed_roles,
                filename=document.filename,
            ),
        )
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclass
class World:
    """Everything an ingestion test needs, built once and shared per test.

    A dataclass rather than eight separate fixtures because almost every test needs all
    of them together, and passing eight parameters through every signature buries the one
    the test is actually about.
    """

    session: Session
    storage: LocalFileStore
    store: QdrantVectorStore
    provider: RecordingProvider
    settings: Any
    acme: tuple[uuid.UUID, uuid.UUID, AuthenticatedIdentity]
    globex: tuple[uuid.UUID, uuid.UUID, AuthenticatedIdentity]

    @property
    def client(self) -> FakeQdrant:
        """The fake client this world was built with.

        Asserted rather than cast, because the world constructs the store with a
        :class:`FakeQdrant` and nothing else could be installed — so the assertion
        cannot fail here, and if it ever could, that would be worth learning.
        """
        client = self.store._client
        assert isinstance(client, FakeQdrant), (
            f"the world was built with a {type(client).__name__}, not a FakeQdrant"
        )
        return client

    @property
    def tenant_a(self) -> uuid.UUID:
        return self.acme[0]

    @property
    def tenant_b(self) -> uuid.UUID:
        return self.globex[0]

    @property
    def identity_a(self) -> AuthenticatedIdentity:
        return self.acme[2]

    @property
    def identity_b(self) -> AuthenticatedIdentity:
        return self.globex[2]

    def make_document(
        self,
        *,
        tenant: uuid.UUID | None = None,
        data: bytes | None = None,
        filename: str = "policy.txt",
        allowed_roles: list[str] | None = None,
        stored: bool = True,
    ) -> Document:
        """A committed ``documents`` row, with its bytes on disk.

        Written straight through the model rather than through the upload endpoint:
        Phase 2.4 already proved that endpoint's validation, and re-testing it here would
        only add a second place to update. What this file needs is a row in the state the
        pipeline expects plus a file at the key the row names.
        """
        tenant_id = tenant if tenant is not None else self.tenant_a
        user_id = self.acme[1] if tenant_id == self.tenant_a else self.globex[1]
        payload = data if data is not None else build_txt(LEASE_TEXT)

        key = self.storage.new_storage_key()
        if stored:
            path = self.storage.path_for(tenant_id, key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        document = Document(
            tenant_id=tenant_id,
            uploaded_by=user_id,
            filename=filename,
            storage_key=key,
            status=DocumentStatus.PROCESSING,
            allowed_roles=allowed_roles,
            size_bytes=len(payload),
            content_type="text/plain",
        )
        self.session.add(document)
        self.session.commit()
        return document

    def ingest(
        self,
        document_id: uuid.UUID,
        *,
        identity: AuthenticatedIdentity | None = None,
        store: QdrantVectorStore | None = None,
        provider: EmbeddingProvider | None = None,
        settings: Any = None,
        storage: LocalFileStore | None = None,
    ) -> Any:
        """Run :func:`ingest_document` with the world's collaborators by default."""
        return ingest_document(
            self.session,
            identity=identity if identity is not None else self.identity_a,
            document_id=document_id,
            settings=settings if settings is not None else self.settings,
            storage=storage if storage is not None else self.storage,
            store=store if store is not None else self.store,
            provider=provider if provider is not None else self.provider,
        )

    def reloaded(self, document_id: uuid.UUID) -> Document:
        """The row as the database now holds it.

        A fresh query rather than the in-memory object: the pipeline writes through the
        same session, and an identity-mapped object would report whatever it already
        held — making every recording assertion pass no matter what was written.
        """
        self.session.expire_all()
        row = self.session.scalar(select(Document).where(Document.id == document_id))
        assert row is not None, "the document row vanished"
        return row


#: The document body used by the happy path. Long enough to produce several chunks at
#: this file's chunk size, with real paragraph boundaries to split on.
LEASE_TEXT = (
    "Section 1. Definitions.\n\n"
    "Staff means any person employed by the Company under a written contract.\n\n"
    "Section 2. Annual Leave.\n\n"
    "Each member of Staff accrues two days of paid leave per completed month of "
    "service, to a maximum of twenty-five days in any leave year.\n\n"
    "Section 3. Notice.\n\n"
    "Either party may terminate employment by giving ninety days written notice.\n\n"
    "Section 4. Provisions.\n\n"
    "Nothing in this agreement reduces any statutory entitlement.\n"
)


@pytest.fixture
def provider() -> RecordingProvider:
    """A recording provider, for the stage tests that need no database at all."""
    return RecordingProvider()


@pytest.fixture
def world(
    model_session: Session,
    tmp_path: Path,
    make_account: Callable[..., tuple[Any, Any]],
) -> Iterator[World]:
    """Two tenants, a real database row set, a fake store and a recording provider.

    Both tenants come from the same ``make_account`` factory the HTTP tests use, so the
    rows, the foreign keys and the ``users.role`` values are all real. A hand-built
    ``Document`` would skip the very lookup whose tenant scoping is under test.
    """
    settings = make_settings(
        embedding_dimension=WIDTH,
        embedding_provider="static",
        chunk_size_chars=120,
        chunk_overlap_chars=20,
        embedding_batch_size=4,
    )

    acme_tenant, acme_user = make_account(
        slug="acme", email="a@example.com", role=UserRole.ADMIN
    )
    globex_tenant, globex_user = make_account(
        slug="globex", email="b@example.com", role=UserRole.ADMIN
    )
    model_session.commit()

    storage = LocalFileStore(tmp_path / "objects")
    client = FakeQdrant()
    store = store_over(settings, client)

    built = World(
        session=model_session,
        storage=storage,
        store=store,
        provider=RecordingProvider(),
        settings=settings,
        acme=(
            acme_tenant.id,
            acme_user.id,
            AuthenticatedIdentity(
                user_id=acme_user.id, tenant_id=acme_tenant.id, role=UserRole.ADMIN
            ),
        ),
        globex=(
            globex_tenant.id,
            globex_user.id,
            AuthenticatedIdentity(
                user_id=globex_user.id, tenant_id=globex_tenant.id, role=UserRole.ADMIN
            ),
        ),
    )

    yield built

    store.close()


# ---------------------------------------------------------------------------
# Stages, individually
# ---------------------------------------------------------------------------


class TestStageRead:
    def test_it_reads_the_bytes_the_row_points_at(self, world: World) -> None:
        document = world.make_document(data=b"the stored bytes")

        assert stage_read(world.storage, document, max_bytes=1024) == b"the stored bytes"

    def test_it_refuses_a_file_larger_than_the_configured_limit(
        self, world: World
    ) -> None:
        """The bound is the configured limit, not the file's size.

        Reading at the document's own size would let the bound follow the thing it is
        supposed to constrain: a file that had somehow grown would be read at whatever
        size it now is.
        """
        document = world.make_document(data=b"x" * 100)

        with pytest.raises(FileTooLargeError):
            stage_read(world.storage, document, max_bytes=10)

    def test_a_missing_file_is_a_documented_error(self, world: World) -> None:
        document = world.make_document(stored=False)

        with pytest.raises(StoredObjectMissingError):
            stage_read(world.storage, document, max_bytes=1024)

    def test_the_missing_file_message_carries_no_path(self, world: World) -> None:
        """A message naming a path reveals the storage layout to an admin-visible column."""
        document = world.make_document(stored=False)

        with pytest.raises(StoredObjectMissingError) as caught:
            stage_read(world.storage, document, max_bytes=1024)

        message = str(caught.value)
        assert str(world.storage.root) not in message
        assert document.storage_key not in message

    def test_it_takes_no_path_argument_at_all(self) -> None:
        """The structural reason a client-supplied path is impossible.

        ``stage_read`` is handed a ``Document``, so the only path information available
        is the row's ``storage_key``. There is no parameter for a caller to put a path
        into, which is a stronger guarantee than a rule somebody has to remember.
        """
        import inspect

        parameters = inspect.signature(stage_read).parameters
        assert set(parameters) == {"storage", "document", "max_bytes"}
        assert "path" not in parameters
        assert "filename" not in parameters


class TestStageParse:
    def test_it_parses_using_the_stored_filename(self, world: World) -> None:
        document = world.make_document(
            data=b"First paragraph.\n\nSecond paragraph.", filename="handbook.txt"
        )

        parsed = stage_parse(document, b"First paragraph.\n\nSecond paragraph.")

        assert isinstance(parsed, ParsedDocument)
        assert "First paragraph." in parsed.full_text
        assert "Second paragraph." in parsed.full_text

    def test_the_filename_decides_the_parser_not_the_bytes(self, world: World) -> None:
        """A caller cannot choose the parser; the format the upload accepted did.

        ``.txt`` selects the text parser whatever the bytes look like, so bytes carrying
        another format's magic signature under a ``.txt`` name get the text parser and
        whatever that produces — not a parser the client picked.
        """
        document = world.make_document(filename="thing.txt", stored=False)

        parsed = stage_parse(document, b"%PDF-1.7 not really a pdf")

        assert [page.number for page in parsed.pages] == [None], (
            "a text document reported a page number"
        )
        assert "%PDF-1.7" in parsed.full_text

    def test_a_pdf_filename_selects_the_pdf_parser(self, world: World) -> None:
        pdf, pages = build_pdf_text(["Page one text.", "Page two text."])
        document = world.make_document(data=pdf, filename="scan.pdf", stored=False)

        parsed = stage_parse(document, pdf)
        assert pages == 2

        assert len(parsed.pages) == 2
        assert parsed.pages[1].number == 2


class TestStageChunk:
    def test_chunk_indexes_are_document_wide_across_pages(self) -> None:
        """The index must not restart per page.

        A per-page index would collide in :func:`point_id` — page 1 chunk 3 and page 2
        chunk 3 would derive the same id — and one would silently overwrite the other, so
        the document would return fewer chunks than it holds with nothing reporting it.
        """
        parsed = ParsedDocument(
            pages=(
                Page(number=1, text="Alpha. " * 60),
                Page(number=2, text="Beta. " * 60),
                Page(number=3, text="Gamma. " * 60),
            )
        )

        prepared = stage_chunk(parsed, _chunk_settings(chunk_size=200, overlap=20))

        indexes = [item.chunk_index for item in prepared]
        assert indexes == list(range(len(prepared))), "indexes restart, skip or repeat"
        assert len(prepared) > 3, "the fixture did not produce enough chunks per page"

    def test_each_chunk_carries_its_own_page(self) -> None:
        parsed = ParsedDocument(
            pages=(Page(number=1, text="A. " * 60), Page(number=2, text="B. " * 60))
        )

        prepared = stage_chunk(parsed, _chunk_settings(chunk_size=200, overlap=20))

        assert {item.page for item in prepared} == {1, 2}
        pages = [item.page for item in prepared]
        assert pages == sorted(page for page in pages if page is not None), (
            "pages are not in document order"
        )

    def test_a_blank_page_contributes_no_chunk(self) -> None:
        """Blank pages are ordinary; an empty chunk is not.

        The last page of a PDF is often whitespace and a slide may hold only a picture,
        so an empty chunk would put a point in the collection that no query can usefully
        match and no citation can quote.
        """
        parsed = ParsedDocument(
            pages=(
                Page(number=1, text="Real content."),
                Page(number=2, text="   \n\n\t  "),
                Page(number=3, text="More real content."),
            )
        )

        prepared = stage_chunk(parsed, _chunk_settings())

        assert [item.page for item in prepared] == [1, 3]
        assert all(item.text.strip() for item in prepared)

    def test_no_chunk_is_empty_or_whitespace(self) -> None:
        parsed = ParsedDocument(
            pages=(Page(number=1, text="Word. " * 200), Page(number=2, text="x"))
        )

        prepared = stage_chunk(parsed, _chunk_settings(chunk_size=90, overlap=10))

        assert prepared
        assert all(item.text.strip() for item in prepared)

    def test_the_text_is_cleaned(self) -> None:
        """Cleaning happens here, once, before chunking.

        A non-breaking space and a soft hyphen are the subjects: both are invisible, but
        both change where a chunk boundary falls, so a document whose only difference is
        those two characters must not chunk identically to the cleaned version.
        """
        parsed = ParsedDocument(
            pages=(Page(number=1, text="alpha\u00a0beta\u00adgamma"),)
        )

        prepared = stage_chunk(parsed, _chunk_settings())

        assert prepared[0].text == "alpha betagamma", (
            "a removed character was replaced by a space, which invents a word boundary"
        )

    def test_it_is_deterministic(self) -> None:
        """The property re-ingestion's idempotency rests on."""
        parsed = ParsedDocument(
            pages=(Page(number=1, text="Repeatable content. " * 30),)
        )
        settings = _chunk_settings(chunk_size=150, overlap=30)

        first = [(item.chunk_index, item.text) for item in stage_chunk(parsed, settings)]
        second = [(item.chunk_index, item.text) for item in stage_chunk(parsed, settings)]

        assert first == second

    def test_a_document_with_no_pages_produces_nothing(self) -> None:
        """This stage does not decide that is a failure; the orchestrator does."""
        assert stage_chunk(ParsedDocument(pages=()), _chunk_settings()) == []


def _chunk_settings(*, chunk_size: int = 120, overlap: int = 20) -> Any:
    return make_settings(
        embedding_dimension=WIDTH,
        embedding_provider="static",
        chunk_size_chars=chunk_size,
        chunk_overlap_chars=overlap,
    )


class TestPreparedChunk:
    """The carrier between the chunker and the payload.

    It holds a :class:`Chunk` plus the document-wide ordinal and the page, and it
    deliberately does **no validation of its own**: :class:`Chunk` already refuses an
    empty text, and :func:`~app.rag.vectors.payload_for` refuses an empty payload text and
    a non-positive page. Re-checking here would add a third place where the same rule
    lives, and the stages that actually use this type are where a bad value would do
    damage.
    """

    def test_text_is_the_chunks_text(self) -> None:
        prepared = PreparedChunk(
            chunk=Chunk(index=0, text="  hello  ", start=0, end=9), page=3, chunk_index=0
        )
        assert prepared.text == "  hello  ", "the carrier trimmed its chunk"

    def test_a_format_may_have_no_page(self) -> None:
        prepared = PreparedChunk(
            chunk=Chunk(index=0, text="hi", start=0, end=2), page=None, chunk_index=0
        )
        assert prepared.page is None

    def test_it_is_frozen(self) -> None:
        """It passes between four stages, and must not change in flight."""
        import dataclasses

        prepared = PreparedChunk(
            chunk=Chunk(index=0, text="hi", start=0, end=2), page=1, chunk_index=0
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            prepared.page = 2  # type: ignore[misc]

    def test_the_guards_belong_to_chunk_and_payload_not_here(self) -> None:
        """Stated as a positive test so the absence is deliberate and visible.

        Constructing one with a blank chunk is allowed here and refused at the payload
        boundary; a reader seeing this knows which layer owns the rule.
        """
        prepared = PreparedChunk(
            chunk=Chunk(index=0, text="   ", start=0, end=3), page=1, chunk_index=0
        )
        with pytest.raises(ValueError, match="requires text"):
            payload_for(
                tenant_id=TENANT_A,
                document_id=uuid.uuid4(),
                chunk_index=0,
                page=prepared.page,
                text=prepared.text,
                allowed_roles=None,
                filename="a.txt",
            )


class TestStageEmbed:
    def test_one_vector_per_chunk_in_order(self, provider: RecordingProvider) -> None:
        prepared = [
            _prepared(f"chunk {n}", n) for n in range(5)
        ]

        vectors = stage_embed(provider, prepared, batch_size=2)

        assert len(vectors) == 5
        assert all(len(vector) == WIDTH for vector in vectors)
        assert vectors == [vector_for(f"chunk {n}") for n in range(5)], (
            "a vector was paired with the wrong chunk"
        )

    def test_it_batches(self, provider: RecordingProvider) -> None:
        prepared = [_prepared(f"c{n}", n) for n in range(7)]

        stage_embed(provider, prepared, batch_size=3)

        assert [len(call) for call in provider.calls] == [3, 3, 1]

    def test_the_batch_size_is_clamped_to_the_provider_limit(
        self, provider: RecordingProvider
    ) -> None:
        """Raising the setting must not produce a request the provider refuses."""
        prepared = [
            _prepared(f"c{n}", n) for n in range(MAX_BATCH_SIZE + 20)
        ]

        stage_embed(provider, prepared, batch_size=MAX_BATCH_SIZE * 10)

        assert all(len(call) <= MAX_BATCH_SIZE for call in provider.calls)
        assert len(provider.calls) == 2

    def test_a_batch_size_below_one_is_raised_to_one(
        self, provider: RecordingProvider
    ) -> None:
        """A zero or negative batch size would otherwise never make progress."""
        prepared = [_prepared(f"c{n}", n) for n in range(3)]

        vectors = stage_embed(provider, prepared, batch_size=0)

        assert len(vectors) == 3
        assert [len(call) for call in provider.calls] == [1, 1, 1]

    def test_no_chunks_is_a_bug_guard_not_a_silent_empty_result(
        self, provider: RecordingProvider
    ) -> None:
        """An empty batch is a request that cannot succeed."""
        with pytest.raises(ValueError, match="at least one chunk"):
            stage_embed(provider, [], batch_size=4)
        assert provider.calls == []

    def test_a_provider_failure_propagates_and_is_not_retried(
        self, provider: RecordingProvider
    ) -> None:
        """Retry policy belongs to the caller, which will be Phase 5's worker.

        A silent retry here would end in a ``failed`` row with no indication that
        retrying was what would have helped.
        """
        provider.raise_with = EmbeddingError("provider is down")
        prepared = [_prepared(f"c{n}", n) for n in range(9)]

        with pytest.raises(EmbeddingError):
            stage_embed(provider, prepared, batch_size=3)
        assert len(provider.calls) == 1, "the provider was called more than once"


class TestStageBuild:
    def test_the_id_is_derived_from_the_row_and_the_index(self) -> None:
        document = _document_row(TENANT_A, uuid.uuid4())

        [point] = stage_build(
            document, [_prepared("text", 4, page=2)], [[0.1] * WIDTH]
        )

        assert point.id == point_id(
            tenant_id=TENANT_A, document_id=document.id, chunk_index=4
        )

    def test_the_payload_comes_from_the_row(self) -> None:
        """Filename and allowed roles are document facts, never request facts."""
        document = _document_row(
            TENANT_A, uuid.uuid4(), filename="plan.docx", roles=["admin"]
        )

        [point] = stage_build(
            document, [_prepared("the text", 0, page=7)], [[0.1] * WIDTH]
        )

        assert point.payload[PAYLOAD_DOCUMENT_ID] == str(document.id)
        assert point.payload[PAYLOAD_TENANT_ID] == str(TENANT_A)
        assert point.payload[PAYLOAD_TEXT] == "the text"
        assert point.payload["filename"] == "plan.docx"
        assert point.payload["allowed_roles"] == ["admin"]
        assert point.payload["page"] == 7

    def test_a_vector_count_mismatch_is_refused(self) -> None:
        """Pairing by position without checking would silently mis-pair.

        One vector short means the last chunk is paired with nothing and the
        second-last with the *wrong* vector — a document that retrieves plausible but
        incorrect neighbours, with nothing to indicate it.
        """
        document = _document_row(TENANT_A, uuid.uuid4())
        prepared = [_prepared(f"c{n}", n) for n in range(3)]

        with pytest.raises(ValueError, match="one to one"):
            stage_build(document, prepared, [[0.1] * WIDTH, [0.2] * WIDTH])

    def test_an_empty_document_and_no_vectors_is_accepted(self) -> None:
        """The stage is consistent; turning emptiness into a failure is the caller's job."""
        document = _document_row(TENANT_A, uuid.uuid4())
        assert stage_build(document, [], []) == []

    def test_building_the_same_document_twice_yields_the_same_points(self) -> None:
        """The whole of this phase's idempotency, at the point it is decided."""
        document = _document_row(TENANT_A, uuid.uuid4())
        prepared = [_prepared(f"c{n}", n) for n in range(3)]
        vectors = [vector_for("a"), vector_for("b"), vector_for("c")]

        assert stage_build(document, prepared, vectors) == stage_build(
            document, prepared, vectors
        )


class TestStageUpsert:
    def test_it_writes_every_point_and_counts_them(self, world: World) -> None:
        document = _document_row(world.tenant_a, world.acme[1])
        world.store.ensure_collection(document.tenant_id)

        assert stage_upsert(world.store, document, _points(document, count=4)) == 4

    def test_it_batches_a_large_point_set(self, world: World) -> None:
        """A ten-thousand-chunk document must not become one enormous request."""
        document = _document_row(world.tenant_a, world.acme[1])
        world.store.ensure_collection(document.tenant_id)
        count = VECTOR_WRITE_BATCH_SIZE + 5

        written = stage_upsert(
            world.store, document, _points(document, count=count), batch_size=256
        )

        assert written == count
        assert (
            world.store.count_points_for_document(document.tenant_id, document.id)
            == count
        ), "a batched write lost points"
        assert (
            len(
                world.store.scroll_document_points(
                    document.tenant_id, document.id, limit=count + 1
                )
            )
            == count
        )

    def test_it_writes_into_the_documents_own_tenant(self, world: World) -> None:
        document = _document_row(world.tenant_a, world.acme[1])
        world.store.ensure_collection(document.tenant_id)

        stage_upsert(world.store, document, _points(document, count=2))

        [call] = world.client.named("upsert")
        assert call["collection_name"] == collection_name_for(document.tenant_id)

    def test_a_failed_batch_names_the_batch_and_its_position(self, world: World) -> None:
        """"The write failed" is not actionable; "batch 1 of 3" is."""
        document = _document_row(world.tenant_a, world.acme[1])
        world.store.ensure_collection(document.tenant_id)
        world.client.fail_with["upsert"] = _server_error(503, "Service Unavailable")

        with pytest.raises(VectorStoreError, match="batch 1 of 3"):
            stage_upsert(world.store, document, _points(document, count=6), batch_size=2)

    def test_a_failed_batch_carries_no_document_content(self, world: World) -> None:
        """The message reaches ``documents.error``, which is admin-visible."""
        document = _document_row(world.tenant_a, world.acme[1])
        world.store.ensure_collection(document.tenant_id)
        world.client.fail_with["upsert"] = _server_error(
            503, "Service Unavailable", SECRET.encode()
        )

        with pytest.raises(VectorStoreError) as caught:
            stage_upsert(world.store, document, _points(document, count=1))

        assert SECRET not in str(caught.value)

    def test_a_non_positive_batch_size_is_refused(self, world: World) -> None:
        document = _document_row(world.tenant_a, world.acme[1])

        with pytest.raises(ValueError, match="at least 1"):
            stage_upsert(
                world.store, document, _points(document, count=2), batch_size=0
            )


class TestStagePrune:
    def test_it_removes_points_above_the_threshold(self, world: World) -> None:
        document = _document_row(world.tenant_a, world.acme[1])
        world.store.ensure_collection(document.tenant_id)
        world.store.upsert_points(document.tenant_id, _points(document, count=8))

        assert stage_prune(world.store, document, keep_below_index=3) == 3

    def test_it_returns_the_count_for_this_document_not_the_collection(
        self, world: World
    ) -> None:
        """The number is compared with the chunk count written, so it must be scoped."""
        first = _document_row(world.tenant_a, world.acme[1])
        second = _document_row(world.tenant_a, world.acme[1])
        world.store.ensure_collection(first.tenant_id)
        world.store.upsert_points(first.tenant_id, _points(first, count=4))
        world.store.upsert_points(first.tenant_id, _points(second, count=6))

        assert stage_prune(world.store, first, keep_below_index=4) == 4


class TestCheckProviderMatchesConfiguration:
    def test_matching_widths_are_accepted(self) -> None:
        check_provider_matches_configuration(
            _chunk_settings(), RecordingProvider(dimensions=WIDTH)
        )

    @pytest.mark.parametrize("declared", [4, WIDTH - 1, WIDTH + 1, 384, 1536])
    def test_a_disagreeing_width_is_refused(self, declared: int) -> None:
        with pytest.raises(IngestionError, match="cannot be resized"):
            check_provider_matches_configuration(
                _chunk_settings(), RecordingProvider(dimensions=declared)
            )

    def test_the_message_names_both_widths(self) -> None:
        """An operator's next action is the only thing this message can offer."""
        with pytest.raises(IngestionError) as caught:
            check_provider_matches_configuration(
                _chunk_settings(), RecordingProvider(dimensions=1536)
            )

        message = str(caught.value)
        assert "1536-dimensional" in message
        assert f"is {WIDTH}" in message
        assert "before any document is ingested" in message


# ---------------------------------------------------------------------------
# The failure message
# ---------------------------------------------------------------------------


class TestFailureMessage:
    """What ``documents.error`` is allowed to contain.

    ``error`` is admin-visible through ``GET /admin/documents/{id}/status``, so it is a
    place tenant content must not reach. Two categories of exception get their detail
    recorded and the rest do not, and the boundary between them is worth stating: a
    message this project *builds* from format names, byte limits, configured values and
    counts contains no tenant data by construction, while a message a third party builds
    may contain anything it was sent.
    """

    def test_it_names_the_stage(self) -> None:
        message = _failure_message(IngestionStage.PARSE, ValueError("boom"))
        assert "parse" in message

    def test_it_names_the_exception_class(self) -> None:
        """"ValueError" separates conditions needing opposite responses."""
        message = _failure_message(IngestionStage.CHUNK, ValueError("boom"))
        assert "ValueError" in message

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            (IngestionStage.RESOLVE, "resolve"),
            (IngestionStage.READ, "read"),
            (IngestionStage.PARSE, "parse"),
            (IngestionStage.CHUNK, "chunk"),
            (IngestionStage.EMBED, "embed"),
            (IngestionStage.BUILD, "build"),
            (IngestionStage.COLLECTION, "collection"),
            (IngestionStage.UPSERT, "upsert"),
            (IngestionStage.PRUNE, "prune"),
        ],
    )
    def test_every_stage_is_named_readably(self, stage: IngestionStage, expected: str) -> None:
        assert expected in _failure_message(stage, ValueError())

    def test_an_embedding_error_records_the_class_name_alone(self) -> None:
        """The exclusion this project promised, pinned by a test.

        A provider's own error text may echo the request, and the request is a chunk of a
        tenant's document. Nothing can bound what a third party puts in an error body, so
        the detail is dropped and the class name kept.
        """
        provider_said = f"batch failed: {SECRET}"

        message = _failure_message(
            IngestionStage.EMBED, EmbeddingError(provider_said)
        )

        assert "EmbeddingError" in message
        assert SECRET not in message
        assert provider_said not in message

    def test_a_parse_error_records_its_detail(self) -> None:
        """:class:`DocumentParseError` messages are built from format names and limits."""
        from app.rag.parsers import DocumentParseError

        message = _failure_message(
            IngestionStage.PARSE, DocumentParseError("pdf: no pages found")
        )
        assert "no pages found" in message

    def test_a_store_error_records_its_detail(self) -> None:
        """These name an operation, a count and an exception class — never a payload."""
        message = _failure_message(
            IngestionStage.UPSERT,
            VectorStoreError("3 vector point(s) could not be written"),
        )
        assert "3 vector point(s)" in message

    def test_no_extractable_text_records_its_explanation(self) -> None:
        """The one message an operator genuinely needs in full.

        It is constructed by this project from content-free material, and it is the
        difference between "something went wrong" and "this is a scanned PDF and OCR is
        not part of this phase".
        """
        message = _failure_message(
            IngestionStage.CHUNK, NoExtractableTextError("it contains only images")
        )
        assert "only images" in message

    def test_a_stored_object_error_records_its_detail(self) -> None:
        message = _failure_message(
            IngestionStage.READ, StoredObjectMissingError("not found")
        )
        assert "not found" in message

    def test_a_generic_exception_records_no_detail(self) -> None:
        """An exception nobody anticipated is a bug; its message is not a diagnosis."""
        message = _failure_message(
            IngestionStage.EMBED, RuntimeError(f"unexpected {SECRET}")
        )
        assert SECRET not in message

    def test_the_message_is_truncated_to_the_column_bound(self) -> None:
        from app.rag.parsers import DocumentParseError

        message = _failure_message(
            IngestionStage.PARSE, DocumentParseError("q" * (MAX_ERROR_CHARS * 3))
        )
        assert len(message) == MAX_ERROR_CHARS

    def test_the_bound_is_within_the_database_column(self) -> None:
        """Truncating here rather than at flush time is the point of the constant."""
        from sqlalchemy import String

        column = Document.__table__.c.error
        assert isinstance(column.type, String), (
            f"documents.error is {column.type!r}, not a bounded string"
        )
        assert column.type.length is not None, "documents.error has no length bound"
        assert MAX_ERROR_CHARS <= column.type.length


# ---------------------------------------------------------------------------
# Orchestration: the happy path
# ---------------------------------------------------------------------------


class TestIngestHappyPath:
    def test_it_records_ready_with_the_chunk_count(self, world: World) -> None:
        document = world.make_document()

        result = world.ingest(document.id)

        assert result.status is DocumentStatus.READY
        assert result.succeeded is True
        assert result.chunk_count is not None
        assert result.chunk_count > 1, "the fixture produced too few chunks"
        assert result.error is None

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.READY
        assert row.chunk_count == result.chunk_count
        assert row.error is None

    def test_the_recorded_count_matches_the_points_in_the_store(
        self, world: World
    ) -> None:
        """``chunk_count`` must describe reality, not just this run's output."""
        document = world.make_document()

        result = world.ingest(document.id)

        stored = world.store.scroll_document_points(world.tenant_a, document.id)
        assert len(stored) == result.chunk_count
        assert world.reloaded(document.id).chunk_count == len(stored)

    def test_it_creates_the_tenants_collection(self, world: World) -> None:
        document = world.make_document()

        result = world.ingest(document.id)

        assert result.collection == collection_name_for(world.tenant_a)
        assert world.store.collection_exists(world.tenant_a) is True

    def test_it_never_creates_another_tenants_collection(self, world: World) -> None:
        """There is no argument by which the pipeline could.

        The tenant comes from the identity, so no second tenant is even reachable — this
        asserts the *absence* of the collection rather than the presence of the right one.
        """
        document = world.make_document()

        world.ingest(document.id)

        assert world.store.collection_exists(world.tenant_b) is False

    def test_the_status_is_processing_while_it_runs(self, world: World) -> None:
        """Observed from inside the embedding call, so it cannot be a race.

        The provider is asked for vectors while the row is in flight; reading it back at
        that moment proves the transition happened *before* the work rather than being a
        side effect of writing the outcome.
        """
        document = world.make_document()
        observed: list[str] = []

        class Observing(RecordingProvider):
            def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
                observed.append(world.reloaded(document.id).status.value)
                return super().embed_texts(texts)

        world.ingest(document.id, provider=Observing())

        assert observed, "the provider was never called, so nothing was observed"
        assert set(observed) == {DocumentStatus.PROCESSING.value}, (
            "the row was not marked processing while the work was in flight"
        )

    def test_an_already_ready_document_can_be_re_ingested(self, world: World) -> None:
        """Re-ingestion is a supported operation, not an error.

        A guard refusing a second ingestion would be defensible, but the requirement is
        idempotency, which is only meaningful if the second run is allowed to happen.
        """
        document = world.make_document()
        world.ingest(document.id)

        second = world.ingest(document.id)

        assert second.status is DocumentStatus.READY
        assert world.reloaded(document.id).status is DocumentStatus.READY

    def test_a_restricted_document_carries_its_roles_into_every_point(
        self, world: World
    ) -> None:
        """The restriction must reach the payload for Phase 2.6's filter to have
        anything to filter on."""
        document = world.make_document(allowed_roles=["admin", "editor"])

        world.ingest(document.id)

        stored = world.store.scroll_document_points(world.tenant_a, document.id)
        assert stored
        assert all(
            payload_key(record, "allowed_roles") == ["admin", "editor"]
            for record in stored
        )

    def test_an_unrestricted_document_carries_none(self, world: World) -> None:
        document = world.make_document(allowed_roles=None)

        world.ingest(document.id)

        stored = world.store.scroll_document_points(world.tenant_a, document.id)
        assert all(
            payload_key(record, "allowed_roles") is None for record in stored
        )

    def test_the_filename_reaches_the_payload_for_citation(self, world: World) -> None:
        document = world.make_document(filename="leave-policy.txt")

        world.ingest(document.id)

        stored = world.store.scroll_document_points(world.tenant_a, document.id)
        assert all(
            payload_key(record, "filename") == "leave-policy.txt" for record in stored
        )


# ---------------------------------------------------------------------------
# Orchestration: idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_re_ingesting_does_not_multiply_chunks(self, world: World) -> None:
        """PMP §4.1's chunk count, and the reason point ids are deterministic.

        If ingestion appended rather than overwrote, the count would grow on every
        retry and a document re-uploaded twice would return each chunk twice.
        """
        document = world.make_document()

        first = world.ingest(document.id)
        second = world.ingest(document.id)
        third = world.ingest(document.id)

        assert first.chunk_count == second.chunk_count == third.chunk_count
        stored = world.store.scroll_document_points(world.tenant_a, document.id)
        assert len(stored) == first.chunk_count
        assert world.reloaded(document.id).chunk_count == first.chunk_count

    def test_re_ingesting_replaces_the_text_rather_than_keeping_the_old(
        self, world: World
    ) -> None:
        """The points are overwritten, not merely deduplicated by count.

        Rewriting the bytes under the same storage key is the version this tests, since
        document versioning is explicitly out of scope for this phase.
        """
        document = world.make_document()
        world.ingest(document.id)
        first_texts = [
            payload_key(record, PAYLOAD_TEXT)
            for record in world.store.scroll_document_points(world.tenant_a, document.id)
        ]

        world.storage.path_for(world.tenant_a, document.storage_key).write_bytes(
            build_txt(SHORTENED_TEXT)
        )
        result = world.ingest(document.id)

        second_texts = [
            payload_key(record, PAYLOAD_TEXT)
            for record in world.store.scroll_document_points(world.tenant_a, document.id)
        ]
        assert second_texts != first_texts
        assert all(SHORTENED_TEXT[:20] in text for text in second_texts)
        assert result.chunk_count == len(second_texts)

    def test_a_shorter_re_ingest_prunes_the_surplus(self, world: World) -> None:
        """The case :meth:`delete_stale_points` exists for.

        A document that indexed to ten chunks and then to three must end up with three
        points, not ten — the surplus would otherwise be quoted by a query as if it were
        part of the document.
        """
        document = world.make_document()
        world.ingest(document.id)
        before = len(
            world.store.scroll_document_points(world.tenant_a, document.id)
        )
        assert before > 3

        world.storage.path_for(world.tenant_a, document.storage_key).write_bytes(
            build_txt(SHORTENED_TEXT)
        )
        result = world.ingest(document.id)

        assert result.chunk_count == 1
        stored = world.store.scroll_document_points(world.tenant_a, document.id)
        assert [payload_key(record, PAYLOAD_CHUNK_INDEX) for record in stored] == [0]

    def test_an_unchanged_document_produces_identical_points(self, world: World) -> None:
        """The purity of the derivation, observed at the store."""
        document = world.make_document()
        world.ingest(document.id)
        before = [
            (record.id, payload_of(record))
            for record in world.store.scroll_document_points(world.tenant_a, document.id)
        ]

        world.ingest(document.id)
        after = [
            (record.id, payload_of(record))
            for record in world.store.scroll_document_points(world.tenant_a, document.id)
        ]

        assert after == before

    def test_the_point_ids_are_the_derived_ones(self, world: World) -> None:
        """Not merely *some* stable ids — the ones :func:`point_id` produces."""
        document = world.make_document()

        result = world.ingest(document.id)

        stored = world.store.scroll_document_points(world.tenant_a, document.id)
        assert {record.id for record in stored} == {
            point_id(
                tenant_id=world.tenant_a, document_id=document.id, chunk_index=index
            )
            for index in range(result.chunk_count)
        }


SHORTENED_TEXT = "Only one short section remains after the document was rewritten.\n"


# ---------------------------------------------------------------------------
# Orchestration: tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_a_tenant_can_ingest_its_own_document(self, world: World) -> None:
        document = world.make_document(tenant=world.tenant_a)

        result = world.ingest(document.id, identity=world.identity_a)

        assert result.status is DocumentStatus.READY

    def test_one_tenant_cannot_ingest_another_tenants_document(self, world: World) -> None:
        """The central isolation property.

        Tenant B naming Tenant A's document id must get ``DocumentNotFoundError``, and
        Tenant A's document must be entirely untouched — not failed, not processing. A
        403 would also have revealed that the document exists.
        """
        document = world.make_document(tenant=world.tenant_a)

        with pytest.raises(DocumentNotFoundError):
            world.ingest(document.id, identity=world.identity_b)

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.PROCESSING, "the victim's row was touched"
        assert row.chunk_count == 0

    def test_a_cross_tenant_probe_creates_no_collection(self, world: World) -> None:
        """Nothing may be created for a tenant who was refused."""
        document = world.make_document(tenant=world.tenant_a)

        with pytest.raises(DocumentNotFoundError):
            world.ingest(document.id, identity=world.identity_b)

        assert world.store.collection_exists(world.tenant_b) is False
        assert world.store.collection_exists(world.tenant_a) is False

    def test_a_cross_tenant_probe_writes_no_points(self, world: World) -> None:
        """Even if a collection did exist for B, A's document must not be in it."""
        document = world.make_document(tenant=world.tenant_a)
        world.store.ensure_collection(world.tenant_b)

        with pytest.raises(DocumentNotFoundError):
            world.ingest(document.id, identity=world.identity_b)

        assert world.store.scroll_document_points(world.tenant_b, document.id) == []

    def test_a_cross_tenant_probe_never_embeds(self, world: World) -> None:
        """The refusal happens at the lookup, so nothing downstream runs at all."""
        document = world.make_document(tenant=world.tenant_a)

        with pytest.raises(DocumentNotFoundError):
            world.ingest(document.id, identity=world.identity_b)

        assert world.provider.calls == []

    def test_an_editor_may_ingest_a_document_they_did_not_upload(
        self, world: World
    ) -> None:
        """The lookup is tenant-scoped but **not** role-scoped.

        Ingestion is a system job triggered by an upload, and restricting it to the
        uploader would break a retry by an operator. What matters is that the editor is
        in the right tenant — which the previous tests establish.
        """
        document = world.make_document(tenant=world.tenant_a)
        editor = AuthenticatedIdentity(
            user_id=uuid.uuid4(), tenant_id=world.tenant_a, role=UserRole.EDITOR
        )

        result = world.ingest(document.id, identity=editor)

        assert result.status is DocumentStatus.READY

    def test_an_unknown_document_is_not_found_for_everyone(
        self, world: World
    ) -> None:
        with pytest.raises(DocumentNotFoundError):
            world.ingest(uuid.uuid4())

    def test_two_tenants_documents_never_share_a_point_id(self, world: World) -> None:
        """Same filename, same content, same chunk indexes, different tenants."""
        first = world.make_document(tenant=world.tenant_a, filename="policy.txt")
        second = world.make_document(tenant=world.tenant_b, filename="policy.txt")

        world.ingest(first.id, identity=world.identity_a)
        world.ingest(second.id, identity=world.identity_b)

        ids_a = {record.id for record in world.store.scroll_document_points(world.tenant_a, first.id)}
        ids_b = {record.id for record in world.store.scroll_document_points(world.tenant_b, second.id)}
        assert ids_a
        assert ids_b
        assert not (ids_a & ids_b), "two tenants produced a shared point id"


# ---------------------------------------------------------------------------
# Orchestration: failure at each stage
# ---------------------------------------------------------------------------


class TestFailuresAreRecorded:
    """The central failure contract, once per stage.

    Four assertions run against every case, because each is a separate requirement and a
    failure of one is easy to mistake for a failure of another:

    * the row is ``failed`` — never ``ready``;
    * the error names the stage and the exception class;
    * no document content reached the error;
    * the original exception is not swallowed, only recorded.
    """

    @staticmethod
    def _assert_failed(world: World, document: Document, result: Any, stage: str) -> None:
        assert result.status is DocumentStatus.FAILED
        assert result.succeeded is False
        assert result.error is not None
        assert stage in result.error

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.FAILED, "a failure was recorded as ready"
        assert row.status is not DocumentStatus.READY
        assert row.error == result.error
        assert SECRET not in (row.error or "")

    def test_a_missing_file_fails_the_read_stage(self, world: World) -> None:
        document = world.make_document(stored=False)

        result = world.ingest(document.id)

        self._assert_failed(world, document, result, "read")
        assert "StoredObjectMissingError" in result.error

    def test_an_unparseable_file_fails_the_parse_stage(self, world: World) -> None:
        document = world.make_document(
            data=b"%PDF-1.7 truncated to nothing", filename="broken.pdf"
        )

        result = world.ingest(document.id)

        self._assert_failed(world, document, result, "parse")
        assert world.provider.calls == [], "embedding ran after a parse failure"

    def test_a_document_with_no_text_fails_at_the_chunk_stage(
        self, world: World
    ) -> None:
        """Whitespace only, images only, or an empty file.

        Recorded as a *failure* rather than a success with zero chunks, because a
        ``ready`` document with nothing in the collection answers every future query
        with nothing and reports that it indexed fine.
        """
        document = world.make_document(data=build_txt("   \n\n\t  \n"))

        result = world.ingest(document.id)

        self._assert_failed(world, document, result, "chunk")
        assert "NoExtractableTextError" in result.error
        assert "OCR" in result.error, "the recorded error does not explain the situation"
        assert world.store.collection_exists(world.tenant_a) is False

    def test_bytes_truncated_to_nothing_after_upload_fail_at_the_chunk_stage(
        self, world: World
    ) -> None:
        """An empty *upload* is refused by Phase 2.4, so this reaches it differently.

        ``ck_documents_size_bytes_positive`` means a committed row can never record a
        zero size, and the upload endpoint refuses an empty body. What can happen is the
        stored file being emptied afterwards — by a bad script, a full disk, or an
        operator — and the pipeline must notice rather than record a ready document with
        nothing behind it.
        """
        document = world.make_document()
        world.storage.path_for(world.tenant_a, document.storage_key).write_bytes(b"")

        result = world.ingest(document.id)

        self._assert_failed(world, document, result, "chunk")
        assert "NoExtractableTextError" in result.error

    def test_a_width_mismatch_fails_before_anything_is_written(
        self, world: World
    ) -> None:
        document = world.make_document()
        world.client.add_collection(collection_name_for(world.tenant_a), size=384)

        result = world.ingest(document.id)

        self._assert_failed(world, document, result, "collection")
        assert "VectorDimensionMismatch" in result.error
        assert world.provider.calls, "the document was embedded despite the mismatch"
        assert world.store.scroll_document_points(world.tenant_a, document.id) == []


class TestRetryableFailuresPropagate:
    """The three transient infrastructure failures leave the pipeline as exceptions.

    The phase-2 contract was "record every documented failure". Step 4A changes
    it for exactly three conditions — an embedding refusal, a vector-store
    refusal, a filesystem error — because those are worth trying again, and the
    worker (Phase 5's caller) is the only place with a retry budget. Everything
    the worker needs travels in :class:`TransientIngestionError`: the stage,
    the message this module would have recorded, and the original exception as
    ``__cause__``. The row is left ``processing``, so a retry has nothing to
    undo. Permanent and unanticipated failures are unchanged: they are still
    recorded or propagate as bugs.
    """

    def test_an_embedding_failure_propagates_with_stage_and_safe_message(
        self, world: World
    ) -> None:
        """The message carried is the one that would have been recorded — safe."""
        document = world.make_document()
        provider_said = f"provider echoed {SECRET}"
        world.provider.raise_with = EmbeddingError(provider_said)

        with pytest.raises(TransientIngestionError) as caught:
            world.ingest(document.id)

        transient = caught.value
        assert transient.stage is IngestionStage.EMBED
        assert isinstance(transient.__cause__, EmbeddingError)
        assert "EmbeddingError" in transient.message
        assert "embed" in transient.message
        assert provider_said not in transient.message
        assert SECRET not in transient.message

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.PROCESSING, (
            "a retryable failure must leave the row processing, not failed"
        )
        assert row.error is None
        assert world.store.collection_exists(world.tenant_a) is False

    def test_a_store_write_failure_propagates_with_its_stage(
        self, world: World
    ) -> None:
        document = world.make_document()
        world.client.fail_with["upsert"] = _server_error(
            503, "Service Unavailable", SECRET.encode()
        )

        with pytest.raises(TransientIngestionError) as caught:
            world.ingest(document.id)

        transient = caught.value
        assert transient.stage is IngestionStage.UPSERT
        assert isinstance(transient.__cause__, VectorStoreError)
        assert "VectorStoreError" in transient.message
        assert SECRET not in transient.message

    def test_a_prune_failure_propagates_with_its_stage(self, world: World) -> None:
        document = world.make_document()
        world.client.fail_with["delete"] = _server_error(500, "Internal Server Error")

        with pytest.raises(TransientIngestionError) as caught:
            world.ingest(document.id)

        assert caught.value.stage is IngestionStage.PRUNE

    def test_a_collection_creation_failure_propagates_with_its_stage(
        self, world: World
    ) -> None:
        document = world.make_document()
        world.client.fail_with["create_collection"] = _server_error(
            500, "Internal Error"
        )

        with pytest.raises(TransientIngestionError) as caught:
            world.ingest(document.id)

        transient = caught.value
        assert transient.stage is IngestionStage.COLLECTION
        assert isinstance(transient.__cause__, VectorStoreError)

    def test_an_os_error_propagates_as_transient(
        self, monkeypatch: pytest.MonkeyPatch, world: World
    ) -> None:
        """A filesystem hiccup is in the retryable set, not a permanent record."""
        document = world.make_document()

        def hiccup(*args: object, **kwargs: object) -> None:
            raise OSError("disk full while writing")

        monkeypatch.setattr(world.storage, "read_object", hiccup)

        with pytest.raises(TransientIngestionError) as caught:
            world.ingest(document.id)

        assert caught.value.stage is IngestionStage.READ
        assert isinstance(caught.value.__cause__, OSError)


class TestNoDocumentEverBecomesReadyAfterAFailure:
    def test_the_chunk_count_is_left_alone_on_failure(self, world: World) -> None:
        """Deliberate, and the reason is worth stating.

        ``chunk_count`` is ``NOT NULL DEFAULT 0``, so zero means "not indexed yet".
        Overwriting a document's previous count with zero on a failed re-ingest would
        erase the only record of what is still sitting in the collection. A stale count
        during a failed re-ingest is the *accurate* count; ``status = failed`` is what
        says it cannot be trusted.
        """
        document = world.make_document()
        world.ingest(document.id)
        good_count = world.reloaded(document.id).chunk_count
        assert good_count > 0

        # Whitespace only: the text parser succeeds and finds nothing, which is exactly
        # the condition under test. (PDF-looking bytes in a `.txt` file would *not* fail —
        # the filename chose the parser, so it would be read as text and ingest fine.)
        world.storage.path_for(world.tenant_a, document.storage_key).write_bytes(
            b"   \n\n\t  \n"
        )
        result = world.ingest(document.id)

        assert result.status is DocumentStatus.FAILED, (
            "the rewrite did not actually produce an unparseable document"
        )
        assert world.reloaded(document.id).chunk_count == good_count

    def test_a_failure_is_recoverable_by_re_ingesting(self, world: World) -> None:
        """The recovery story the phase has to document, tested rather than asserted.

        Restore the bytes, ingest again, and the row reaches ``ready``. Nothing about the
        first failure is permanent, which is what makes "re-run the ingestion" a
        workable instruction.
        """
        document = world.make_document(stored=False)
        failed = world.ingest(document.id)
        assert failed.status is DocumentStatus.FAILED

        world.storage.path_for(world.tenant_a, document.storage_key).parent.mkdir(
            parents=True, exist_ok=True
        )
        world.storage.path_for(world.tenant_a, document.storage_key).write_bytes(
            build_txt(LEASE_TEXT)
        )
        recovered = world.ingest(document.id)

        assert recovered.status is DocumentStatus.READY
        row = world.reloaded(document.id)
        assert row.chunk_count == recovered.chunk_count
        assert row.error is None, "the earlier failure was not cleared"

    def test_a_failure_leaves_the_earlier_points_in_place(self, world: World) -> None:
        """No partial vector state where it can be avoided.

        The points from a successful run are not deleted by a later failed run — the
        pipeline never removes points it did not write, and pruning is the only thing
        that deletes.
        """
        document = world.make_document()
        world.ingest(document.id)
        good = len(world.store.scroll_document_points(world.tenant_a, document.id))
        assert good > 0

        world.storage.path_for(world.tenant_a, document.storage_key).write_bytes(b"")
        result = world.ingest(document.id)

        assert result.status is DocumentStatus.FAILED
        assert (
            len(world.store.scroll_document_points(world.tenant_a, document.id)) == good
        )


class TestBugsAreNotTurnedIntoFailures:
    def test_an_unexpected_exception_propagates(self, world: World) -> None:
        """An ``AttributeError`` is a bug in this module, not a document's problem.

        Recording it would leave a tenant's file permanently ``failed`` with a message
        naming an exception class nobody can act on — and would hide the bug from every
        log that only reports recorded outcomes. It propagates instead, and the row stays
        ``processing``, which is at least visibly not a completed ingestion.
        """
        document = world.make_document()

        class Buggy(RecordingProvider):
            def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
                raise AttributeError("'NoneType' object has no attribute 'id'")

        with pytest.raises(AttributeError):
            world.ingest(document.id, provider=Buggy())

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.PROCESSING, "a bug became a recorded failure"
        assert row.error is None

    def test_a_type_error_propagates_too(self, world: World) -> None:
        document = world.make_document()

        class Buggy(RecordingProvider):
            def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
                raise TypeError("unexpected keyword argument")

        with pytest.raises(TypeError):
            world.ingest(document.id, provider=Buggy())

        assert world.reloaded(document.id).error is None

    def test_the_recorded_failure_tuple_is_not_exception(self) -> None:
        """The structural version of the same claim."""
        from app.services.ingestion import PIPELINE_FAILURES

        assert Exception not in PIPELINE_FAILURES
        assert all(
            issubclass(failure, BaseException) for failure in PIPELINE_FAILURES
        )


class TestTheConfigurationCheckRunsFirst:
    def test_a_width_mismatch_is_an_error_not_a_recorded_failure(
        self, world: World
    ) -> None:
        """A configuration fault must not be blamed on a tenant's document.

        The row is untouched rather than marked ``failed``: the document is perfectly
        fine, and marking it failed would tell the tenant its file is bad.
        """
        document = world.make_document()

        with pytest.raises(IngestionError):
            world.ingest(document.id, provider=RecordingProvider(dimensions=1536))

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.PROCESSING
        assert row.error is None

    def test_nothing_is_embedded_or_written(self, world: World) -> None:
        """Checked before any work, which is the entire point."""
        document = world.make_document()

        with pytest.raises(IngestionError):
            world.ingest(document.id, provider=RecordingProvider(dimensions=1536))

        assert world.provider.calls == []
        assert world.client.named("upsert") == []
        assert world.store.collection_exists(world.tenant_a) is False

    def test_the_row_is_not_even_marked_processing(self, world: World) -> None:
        """The check precedes the lookup, so nothing is written at all."""
        document = world.make_document()
        from sqlalchemy import update

        world.session.execute(
            update(Document)
            .where(Document.id == document.id)
            .values(status=DocumentStatus.PROCESSING.value)
        )
        world.session.commit()

        with pytest.raises(IngestionError):
            world.ingest(document.id, provider=RecordingProvider(dimensions=1536))

        row = world.reloaded(document.id)
        assert row.status is DocumentStatus.PROCESSING
        assert row.error is None
        assert row.chunk_count == 0


class TestPruningConvergence:
    def test_a_count_mismatch_refuses_to_record_ready(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The orchestrator verifies rather than assuming.

        A store that reported a different number of remaining points than were written
        means Qdrant holds points this run did not write — a real condition an operator
        needs to see, not a number to paper over by writing the row anyway.
        """
        document = world.make_document()
        world.client.add_collection(collection_name_for(world.tenant_a))
        from qdrant_client import models

        world.client.collections[collection_name_for(world.tenant_a)].indexes = {
            PAYLOAD_DOCUMENT_ID: models.PayloadSchemaType.KEYWORD,
            PAYLOAD_TENANT_ID: models.PayloadSchemaType.KEYWORD,
            PAYLOAD_CHUNK_INDEX: models.PayloadSchemaType.INTEGER,
        }

        # The seam is the store's own method: `delete_stale_points` returns the result
        # of `count_points_for_document`, so inflating that is exactly "the store reported
        # a number this run did not produce".
        original_count = world.store.count_points_for_document

        def wrong_count(tenant_id: uuid.UUID, document_id: uuid.UUID) -> int:
            return original_count(tenant_id, document_id) + 3

        monkeypatch.setattr(
            world.store, "count_points_for_document", wrong_count, raising=True
        )

        with pytest.raises(IngestionError, match="did not converge"):
            world.ingest(document.id)

        row = world.reloaded(document.id)
        assert row.status is not DocumentStatus.READY


# ---------------------------------------------------------------------------
# Orchestration: signature and imports
# ---------------------------------------------------------------------------


class TestTheSignatureRulesOutCrossTenantIngestion:
    def test_it_takes_no_tenant_id(self) -> None:
        """The most important signature decision in the module.

        No tenant argument means a caller cannot name a tenant, so it cannot ingest into
        another tenant's collection even by mistake — and the tenant used for the
        database lookup, the storage path and the collection name is one value
        throughout.
        """
        import inspect

        parameters = inspect.signature(ingest_document).parameters
        assert set(parameters) == {
            "session",
            "identity",
            "document_id",
            "settings",
            "storage",
            "store",
            "provider",
        }
        assert "tenant_id" not in parameters
        assert "collection" not in parameters

    def test_tenant_id_is_not_a_keyword_argument_anywhere(self) -> None:
        """And not through ``**kwargs`` either."""
        import inspect

        for parameter in inspect.signature(ingest_document).parameters.values():
            assert parameter.kind is not inspect.Parameter.VAR_KEYWORD

    def test_the_module_imports_nothing_from_fastapi(self) -> None:
        """So the pipeline is callable from Celery, a CLI, or a test.

        A ``Request`` or ``Depends`` anywhere in the signature chain would make it
        impossible to call outside an HTTP request, and the next phase's whole plan is to
        call it from a worker.
        """
        import ast
        from pathlib import Path as _Path

        import app.services.ingestion as module

        tree = ast.parse(_Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        for forbidden in ("fastapi", "starlette", "qdrant_client", "celery", "redis"):
            assert forbidden not in imported, (
                f"app/services/ingestion.py imports {forbidden!r}; the pipeline must be "
                "callable from a worker, so it may not depend on a web framework"
            )

    def test_every_stage_is_separately_callable(self) -> None:
        """No giant function: each stage is a module-level function taking plain values.

        Independently testable is the requirement, and it is checked here by asserting the
        stages exist at all with the signatures they claim.
        """
        import inspect

        for stage in (
            stage_read,
            stage_parse,
            stage_chunk,
            stage_embed,
            stage_build,
            stage_upsert,
            stage_prune,
        ):
            parameters = list(inspect.signature(stage).parameters)
            assert parameters, f"{stage.__name__} takes nothing"
            assert all(
                parameter not in ("request", "response", "session")
                for parameter in parameters
            ), f"{stage.__name__} takes a framework object"

    def test_the_module_declares_its_both_failure_tuples(self) -> None:
        """Everything the pipeline raises as a fault is *covered* by the tuple.

        Asserted with :func:`issubclass` rather than ``in``: ``NoExtractableTextError``
        is a subclass of ``IngestionError`` rather than a member of the tuple, so a
        membership test would describe the shape of the declaration instead of the
        property that matters — a pipeline fault that escapes the ``except`` would leave
        a tenant's document permanently ``processing``.
        """
        from app.services.ingestion import (
            PERMANENT_RETRYABLE_SUBCLASSES,
            PIPELINE_FAILURES,
            RECORDING_FAILURES,
            RETRYABLE_INGESTION_FAILURES,
        )

        for covered in (
            NoExtractableTextError,
            EmbeddingError,
            VectorStoreError,
            StoredObjectMissingError,
            FileTooLargeError,
        ):
            assert any(
                issubclass(covered, failure) for failure in PIPELINE_FAILURES
            ), f"{covered.__name__} is not covered by PIPELINE_FAILURES"

        assert RETRYABLE_INGESTION_FAILURES == (EmbeddingError, VectorStoreError, OSError), (
            "the retryable set is exactly the three transient infrastructure types"
        )
        for retryable in RETRYABLE_INGESTION_FAILURES:
            assert any(
                issubclass(retryable, failure) for failure in PIPELINE_FAILURES
            ), f"{retryable.__name__} escaped the pipeline's failure tuple"

        # The permanent subclasses would wrongly be retried if only the retryable
        # isinstance check ran; the exclude-tuple is what keeps a width mismatch a
        # recorded failure. They must be members of the retryable set by inheritance.
        for permanent in PERMANENT_RETRYABLE_SUBCLASSES:
            assert any(
                issubclass(permanent, retryable)
                for retryable in RETRYABLE_INGESTION_FAILURES
            ), f"{permanent.__name__} is not a subclass of the retryable set"

        assert Exception not in RECORDING_FAILURES, (
            "a blank Exception catch here would swallow a real bug into a recorded failure"
        )

    def test_every_stage_value_is_distinct_and_readable(self) -> None:
        values = [stage.value for stage in IngestionStage]
        assert len(values) == len(set(values)), "two stages share a value"
        assert all(value.islower() for value in values), values


class TestElapsedSince:
    def test_it_is_non_negative(self) -> None:
        import time

        assert elapsed_since(time.monotonic()) >= 0.0

    def test_it_grows(self) -> None:
        import time

        started = time.monotonic()
        time.sleep(0.01)
        assert elapsed_since(started) >= 0.005

    def test_the_recorded_elapsed_is_a_real_duration(self, world: World) -> None:
        document = world.make_document()

        result = world.ingest(document.id)

        assert result.elapsed_seconds >= 0.0


def _schema_names(node: Any) -> set[str]:
    """Every JSON key appearing anywhere in ``node``, recursively.

    Module-level so the two schema guards below cannot drift into reading the document
    differently from one another — which would make the second one unable to notice
    anything the first had classified.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        found.update(str(key) for key in node)
        for value in node.values():
            found.update(_schema_names(value))
    elif isinstance(node, list):
        for value in node:
            found.update(_schema_names(value))
    return found


class TestNoNewEndpointWasAdded:
    """The path set is a closed set, and each widening is a decision someone made.

    PMP §4.1 adds ingestion but no route: it is a system job, not an API call.

    The path set is written out in full rather than compared against a stored baseline,
    because a baseline that had been regenerated from the code under test would prove
    nothing. A reader can see exactly which routes exist and check them against the
    execution log.

    Two widenings have happened across phases, and the set is asserted by equality
    rather than as a subset precisely so that another one cannot appear without someone
    editing this literal and explaining why:

    * Phase 2.4 added the three ``/api/v1/users`` routes and
      ``/api/v1/admin/documents/{document_id}/status``.
    * PMP Phase 4 added ``POST /api/v1/query``. That one *is* a PMP §15 endpoint rather
      than an invented one, and it is the only read route that returns document content,
      so it is the first time a response schema carries anything derived from a chunk —
      which is why :meth:`TestNoNewEndpointWasAdded.test_no_schema_property_exposes_a_vector_or_an_embedding`
      needed widening at the same moment as this set.
    * PMP Phase 7 added the rest of PMP §15's ``Query`` group —
      ``GET /query/history`` and ``POST /query/{query_id}/feedback`` (backed by the
      new ``query_logs`` table) — plus ``POST /query/stream`` (SSE lifecycle events,
      PMP §25 success criterion 9) and ``GET /admin/usage`` (usage analytics for the
      ``Admin`` role). None of them exposes a pipeline internal, which keeps the
      no-leak assertions below meaningful for the widened surface.
    """

    #: Every route the application serves, as of the end of PMP Phase 7.
    EXPECTED_PATHS = frozenset(
        {
            "/health",
            "/health/ready",
            "/api/v1/auth/login",
            "/api/v1/auth/logout",
            "/api/v1/auth/refresh",
            "/api/v1/auth/register-tenant",
            "/api/v1/documents",
            "/api/v1/documents/{document_id}",
            "/api/v1/admin/documents/{document_id}/status",
            "/api/v1/admin/usage",
            "/api/v1/query",
            "/api/v1/query/history",
            "/api/v1/query/stream",
            "/api/v1/query/{query_id}/feedback",
            "/api/v1/users",
            "/api/v1/users/invite",
            "/api/v1/users/{user_id}",
            "/api/v1/users/{user_id}/role",
        }
    )

    @staticmethod
    def _paths() -> set[str]:
        from app.main import create_app

        return set(create_app().openapi()["paths"])

    def test_the_openapi_path_set_is_unchanged(self) -> None:
        paths = self._paths()
        assert paths == set(self.EXPECTED_PATHS), (
            f"added: {sorted(paths - self.EXPECTED_PATHS)}; "
            f"removed: {sorted(self.EXPECTED_PATHS - paths)}"
        )

    def test_no_path_mentions_the_pipeline(self) -> None:
        for path in self._paths():
            lowered = path.lower()
            for forbidden in ("ingest", "embed", "vector", "qdrant", "chunk"):
                assert forbidden not in lowered, f"{path!r} exposes pipeline internals"

    def test_no_schema_property_exposes_a_vector_or_an_embedding(self) -> None:
        """The internals stay out of the API, not merely out of the paths.

        Read over the *names* in the schema rather than the whole document as text:
        ``/health/ready``'s description deliberately names qdrant to explain why it is
        not probed, and a substring search over prose would fail on an honest explanation
        while passing on a genuinely leaked field called ``embedding_model``.

        ``chunk_index`` left the forbidden set at PMP Phase 4, with the restriction that
        replaced it stated below rather than simply dropped. It was forbidden because
        before a query endpoint existed, a ``chunk_index`` anywhere in the public schema
        could only have meant an ingestion detail had leaked — which is exactly what it
        means for ``DocumentResponse``. Now that :class:`app.schemas.query.CitationResponse`
        exists, ``chunk_index`` is the citation's *content*: PMP §7 FR-13 requires a
        response to reference its source, and "which part of the document" is the
        difference between a citation a user can follow and a filename they have to search
        for. So the name is permitted in that one schema and nowhere else, which is a
        stronger statement than the one it replaced.
        """
        from app.main import create_app

        schema = create_app().openapi()

        forbidden = {"embedding", "embeddings", "vector", "vectors",
                     "qdrant", "collection", "collection_name", "point_id", "model_name"}
        assert not (_schema_names(schema.get("components", {})) & forbidden), (
            "a component schema exposes pipeline internals"
        )

    def test_chunk_index_appears_only_as_citation_provenance(self) -> None:
        """The one place ``chunk_index`` may appear, asserted rather than permitted.

        :meth:`test_no_schema_property_exposes_a_vector_or_an_embedding` removes
        ``chunk_index`` from its forbidden set, which on its own would allow it to appear
        in any schema a later change invented. This closes that: it may appear in
        ``CitationResponse`` and in no other component, so a document or user response
        that grew a chunk index would fail here even though the other test passed.
        """
        from app.main import create_app

        schemas = create_app().openapi().get("components", {}).get("schemas", {})
        carriers = {
            name
            for name, definition in schemas.items()
            if "chunk_index" in _schema_names(definition)
        }
        assert carriers == {"CitationResponse"}, sorted(carriers)

    def test_no_operation_carries_an_ingestion_operation_id(self) -> None:
        """Belt and braces: an operation is named even if a path is not."""
        from app.main import create_app

        operations = create_app().openapi()["paths"]
        for path, methods in operations.items():
            for method, specification in methods.items():
                operation_id = str(specification.get("operationId", "")).lower()
                assert "ingest" not in operation_id, f"{method.upper()} {path}"
                assert "embed" not in operation_id, f"{method.upper()} {path}"


# ---------------------------------------------------------------------------
# The same orchestrator, against a real Qdrant
# ---------------------------------------------------------------------------


@REQUIRES_QDRANT
class TestAgainstRealQdrant:
    """The pipeline end to end into a real vector store.

    Everything above substitutes the store, which means it proves the orchestrator calls
    the store correctly and nothing about what the server does with it. These tests close
    that gap for the chain that matters most: document bytes → chunks → vectors → points
    actually present in an index, with two tenants in the same server.
    """

    @pytest.fixture
    def live(self, world: World) -> Iterator[World]:
        """The same world, with the store pointed at a real server.

        Reuses ``world`` rather than rebuilding it, so the tenants, rows and settings are
        the same objects the fake-client tests use — the only difference is the client.
        """
        from qdrant_client import QdrantClient

        url = os.environ[QDRANT_URL_ENV_VAR]
        client = QdrantClient(url=url, timeout=15)
        world.store.close()
        world.store = QdrantVectorStore(world.settings, client=client)
        yield world
        for tenant in (world.tenant_a, world.tenant_b):
            try:
                client.delete_collection(collection_name_for(tenant))
            except Exception:  # noqa: BLE001, S110 - teardown only; see below
                # A cleanup failure says something about the server, not about the test
                # that just passed. Raising here would replace a green run with a red one
                # pointing at the wrong line.
                pass
        world.store.close()

    def test_the_full_chain_lands_in_a_real_index(self, live: World) -> None:
        """Every point the pipeline intended is a point the server holds.

        Compared field by field against what the stages produced, not just counted: a
        matching count with a mismatched payload would satisfy a count-only assertion and
        ship a document that quotes the wrong text.
        """
        document = live.make_document()

        result = live.ingest(document.id)

        expected_texts = [call for batch in live.provider.calls for call in batch]
        stored = live.store.scroll_document_points(live.tenant_a, document.id)
        assert len(stored) == len(expected_texts) == result.chunk_count

        for record, text in zip(stored, expected_texts, strict=True):
            # Not `.strip()`ed on either side: the payload text is the chunk text, byte
            # for byte, including its trailing newline. Comparing stripped forms would
            # hide a payload that was normalised differently from what was embedded —
            # which is precisely the defect this assertion exists to catch.
            assert payload_key(record, PAYLOAD_TEXT) == text
            assert payload_key(record, PAYLOAD_DOCUMENT_ID) == str(document.id)
            assert payload_key(record, PAYLOAD_TENANT_ID) == str(live.tenant_a)
            assert payload_key(record, "filename") == "policy.txt"

    def test_every_stored_vector_is_the_providers(
        self, live: World
    ) -> None:
        """Compared by direction, because a Cosine collection normalises on write."""
        document = live.make_document()

        live.ingest(document.id)

        stored = live.store.scroll_document_points(live.tenant_a, document.id)
        sent = [
            vector_for(call) for batch in live.provider.calls for call in batch
        ]
        for record, expected in zip(stored, sent, strict=True):
            actual = vector_of(record)
            scale = max(abs(component) for component in expected)
            unit = [component / scale for component in expected]
            length = sum(component**2 for component in unit) ** 0.5
            normalised = [component / length for component in unit]
            assert all(
                abs(a - b) < 1e-5 for a, b in zip(actual, normalised, strict=True)
            ), "the stored vector is not the provider's, up to normalisation"

    def test_two_tenants_round_trip_through_real_collections(
        self, live: World
    ) -> None:
        """Tenant A and Tenant B, same filename, same content, real server.

        The adversarial version: identical documents, so any id or payload derived from
        anything but the tenant would collide and one tenant would read the other's text.
        """
        first = live.make_document(tenant=live.tenant_a, filename="policy.txt")
        second = live.make_document(tenant=live.tenant_b, filename="policy.txt")

        live.ingest(first.id, identity=live.identity_a)
        live.ingest(second.id, identity=live.identity_b)

        texts_a = [
            payload_key(record, PAYLOAD_TEXT)
            for record in live.store.scroll_document_points(live.tenant_a, first.id)
        ]
        texts_b = [
            payload_key(record, PAYLOAD_TEXT)
            for record in live.store.scroll_document_points(live.tenant_b, second.id)
        ]
        assert texts_a and texts_a == texts_b, "the fixture documents are not identical"
        assert live.store.payload_tenant_id_is_exhaustive(live.tenant_a) is True
        assert live.store.payload_tenant_id_is_exhaustive(live.tenant_b) is True

    def test_a_cross_tenant_probe_leaves_the_real_collection_empty(
        self, live: World
    ) -> None:
        document = live.make_document(tenant=live.tenant_a)

        with pytest.raises(DocumentNotFoundError):
            live.ingest(document.id, identity=live.identity_b)

        assert (
            live.store.scroll_document_points(live.tenant_b, document.id) == []
        )

    def test_re_ingestion_does_not_multiply_real_points(self, live: World) -> None:
        document = live.make_document()

        first = live.ingest(document.id)
        second = live.ingest(document.id)

        assert first.chunk_count == second.chunk_count
        assert (
            live.store.count_points_for_document(live.tenant_a, document.id)
            == first.chunk_count
        )
        assert live.reloaded(document.id).chunk_count == first.chunk_count

    def test_a_failed_ingestion_records_failed_and_never_ready(
        self, live: World
    ) -> None:
        document = live.make_document(data=build_txt("   \n \n"))

        result = live.ingest(document.id)

        assert result.status is DocumentStatus.FAILED
        row = live.reloaded(document.id)
        assert row.status is DocumentStatus.FAILED
        assert row.status is not DocumentStatus.READY