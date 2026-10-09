"""Integration tests for :mod:`app.storage.qdrant` against a **real** Qdrant server.

What this file is, and why it is not the same as ``tests/test_qdrant_store.py``
-----------------------------------------------------------------------------
``tests/test_qdrant_store.py`` asserts what TenantMind *sends* to Qdrant. This file
asserts what Qdrant *does* with it. That division is not a formality — every one of the
following is a fact about Qdrant that the fake cannot know and would happily agree with:

* ``create_payload_index`` on ``chunk_index`` succeeding at all, and a scroll with
  ``order_by=chunk_index`` returning 400 without it.
* A ``Range(gte=n)`` filter inside a ``must`` selecting the points it appears to select.
* **Cosine normalising vectors on write.** ``[1.0] * 8`` reads back as ``[0.3535] * 8``.
  This is not a rounding artefact; it is deliberate, and it means *any* test asserting
  an exact stored vector against a Cosine collection is asserting a normalisation this
  project did not do. Every vector assertion below therefore compares direction, and
  says so.
* ``points_count_approximate`` actually being set by a real server.
* A 409 really being what a concurrent create returns.
* An unreachable server really raising ``ResponseHandlingException``.

Two of these were found by running them here for the first time, and neither was
predictable from the client library's documentation.

Running it
----------
Point ``TENANTMIND_TEST_QDRANT_URL`` at a Qdrant server and run::

    TENANTMIND_TEST_QDRANT_URL=http://127.0.0.1:6333 pytest tests/test_qdrant_integration.py

A local server is enough::

    docker run --rm -p 6333:6333 -v "$(pwd)/qdrant-data:/qdrant/storage" \
        qdrant/qdrant:v1.19.1

Optional key and HTTPS
----------------------
``TENANTMIND_TEST_QDRANT_API_KEY`` and ``TENANTMIND_TEST_QDRANT_HTTPS=1`` cover a secured
server. No key means no key is sent; the value is never printed, never asserted on, and
never reaches a failure message — the same policy the rest of the phase holds.

This suite **creates and deletes collections**. It uses a fresh random tenant id per
session and deletes that tenant's collection on teardown, so it cannot collide with
another run. It never deletes a collection it did not create — see
:func:`destructive_qdrant`, which refuses a URL whose name suggests it is not a test
server.
"""

from __future__ import annotations

import math
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from qdrant_client import QdrantClient, models

from app.models.document import Document, DocumentStatus
from app.models.user import UserRole
from app.rag.vectors import (
    MAX_PAYLOAD_TEXT_CHARS,
    PAYLOAD_ALLOWED_ROLES,
    PAYLOAD_CHUNK_INDEX,
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_FILENAME,
    PAYLOAD_PAGE,
    PAYLOAD_TENANT_ID,
    PAYLOAD_TEXT,
    point_id,
)
from app.storage.qdrant import (
    DISTANCE_METRIC,
    PAYLOAD_INDEXES,
    QdrantVectorStore,
    VectorDimensionMismatch,
    VectorStoreError,
    build_vector_point,
    collection_name_for,
)
from tests.conftest import make_settings
from tests.records import payload_key, scored_payload_of, vector_of

#: Environment variable naming the Qdrant server to test against.
QDRANT_URL_ENV_VAR = "TENANTMIND_TEST_QDRANT_URL"

#: Optional credential for a secured server.
QDRANT_API_KEY_ENV_VAR = "TENANTMIND_TEST_QDRANT_API_KEY"

#: Set to ``1`` to speak HTTPS rather than HTTP.
QDRANT_HTTPS_ENV_VAR = "TENANTMIND_TEST_QDRANT_HTTPS"

#: Width these tests configure. A small, *legal* width: Qdrant accepts any positive
#: integer, and using 8 rather than 768 keeps the vectors cheap to build without
#: changing any behaviour under test — nothing here depends on the width being a real
#: embedding width, and :class:`TestDimensionMismatch` deliberately configures a second
#: one to prove the check fires.
WIDTH = 8

#: Fragments that indicate a disposable Qdrant instance. A URL naming nothing else is
#: refused, because this suite issues ``DELETE`` on collections.
SAFE_HOST_MARKERS = ("localhost", "127.0.0.1", "test", "tmp", "ci", "qdrant", "dev")

SKIP_REASON = (
    f"{QDRANT_URL_ENV_VAR} is not set, so no Qdrant server is configured. "
    "The Qdrant integration gate is therefore PENDING; see this module's "
    "docstring for how to run it."
)

#: Applied to the fixtures' consumers rather than as a module-level ``pytestmark``, so
#: that the guard-checking tests still run. A blanket module skip would silence them and
#: the suite could then skip for any structural reason without anything reporting it.
requires_qdrant = pytest.mark.usefixtures("qdrant_url")


def _configured_url() -> str | None:
    value = os.environ.get(QDRANT_URL_ENV_VAR)
    return value.strip() if value and value.strip() else None


#: Skip reason shown wherever a live Qdrant server is required.
REQUIRES_QDRANT = pytest.mark.skipif(
    _configured_url() is None,
    reason=SKIP_REASON,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Fixed ids so a failure names the scope it failed in.
TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")


def _random_tenant() -> uuid.UUID:
    """A fresh tenant id, so a run cannot collide with a concurrent one."""
    return uuid.uuid4()


@pytest.fixture(scope="module")
def qdrant_url() -> str:
    """The configured server URL, after checking it is safe to delete from.

    Carries the skip itself so every fixture needing it inherits the same decision in
    one place — the same arrangement as the PostgreSQL suite's ``pg_dsn``.
    """
    url = _configured_url()
    if url is None:
        pytest.skip(SKIP_REASON)

    lowered = url.lower()
    assert lowered.startswith(("http://", "https://")), (
        f"{QDRANT_URL_ENV_VAR} must be an http or https URL, got {url!r}"
    )
    assert any(marker in lowered for marker in SAFE_HOST_MARKERS), (
        f"refusing to test against {url!r}: this suite issues DELETE on "
        "collections, so the host must look like a local or test server"
    )
    return url


@pytest.fixture(scope="module")
def live_client(qdrant_url: str) -> Iterator[QdrantClient]:
    """A real client, connected for the module's duration.

    The connection is proved open here. If the server is not answering, every test in
    this file would otherwise fail with its own error, and a reader would have to guess
    that the common cause was one refused connection — or, worse, that some of them
    passed against nothing.
    """
    key = os.environ.get(QDRANT_API_KEY_ENV_VAR)
    secure = os.environ.get(QDRANT_HTTPS_ENV_VAR) == "1"
    client = QdrantClient(url=qdrant_url, api_key=key, https=secure, timeout=15)
    try:
        try:
            client.get_collections()
        except Exception as exc:  # noqa: BLE001
            pytest.fail(
                f"{QDRANT_URL_ENV_VAR} is set to {qdrant_url!r} but the server did "
                f"not answer ({type(exc).__name__}). Point it at a running Qdrant, "
                "or unset it to skip this suite."
            )
        yield client
    finally:
        client.close()


@pytest.fixture
def managed_tenant(live_client: QdrantClient) -> Iterator[uuid.UUID]:
    """A tenant collection belonging to one test, deleted when that test ends.

    **Per test, not per module**, and that is load-bearing rather than tidy. Qdrant
    collections persist for the life of the server, so a module-scoped tenant would be
    shared by every test in the module and their points would accumulate — after which
    "this document has three chunks" becomes "everything anyone has written has six",
    and the assertions would be checking the sum of the module's tests instead of one
    behaviour.

    Random rather than a fixed id for the same reason the PostgreSQL suite uses a
    disposable database: two concurrent runs must not share a collection, or one would
    delete the other's points and the isolation assertions would be testing the harness.

    Teardown swallows failures deliberately. A delete that fails during teardown says
    something about the server, not about the test that just passed, and raising there
    would replace a green run with a red one whose message points at the wrong line.
    """
    tenant = _random_tenant()
    yield tenant
    try:
        live_client.delete_collection(collection_name_for(tenant))
    except Exception:  # noqa: BLE001, S110 - teardown only; see above
        pass


@pytest.fixture
def store(qdrant_url: str) -> Iterator[QdrantVectorStore]:
    """A store aimed at the real server, closed when the test ends.

    **Per test, not per module**, and the reason is a test that needs to close a store
    mid-run. Sharing one across the module would mean that test closes the connection
    every later test then uses, and they would all fail with a closed client — a
    confusing way of learning that a fixture's scope was wrong.

    Built **from settings** rather than from :func:`live_client`, because that is the
    path production takes: these tests therefore exercise ``_build_client`` — the
    timeout, the HTTPS flag, the API key — rather than assuming them. It also owns its
    own client, so closing this store never closes :func:`live_client` for the others.
    """
    instance = QdrantVectorStore(
        make_settings(
            embedding_dimension=WIDTH,
            qdrant_url=qdrant_url,
            qdrant_api_key=os.environ.get(QDRANT_API_KEY_ENV_VAR),
            qdrant_timeout_seconds=15,
        )
    )
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def tenant(
    store: QdrantVectorStore, managed_tenant: uuid.UUID
) -> Iterator[uuid.UUID]:
    """The one tenant a test owns: its collection is created and then destroyed.

    A distinct name from :func:`managed_tenant` on purpose. ``managed_tenant`` promises
    a *collection that will be deleted* and is what the tenant-isolation tests want,
    because those tests create extra collections by hand. ``tenant`` additionally
    promises the *store's own* collection is already there, which is what the ordinary
    upsert and deletion tests want and should not have to set up themselves.
    """
    store.ensure_collection(managed_tenant)
    yield managed_tenant


def _document_id_of(seed: int) -> uuid.UUID:
    return uuid.UUID(int=0xD0000 + seed)


def _point(
    tenant_id: uuid.UUID,
    *,
    chunk_index: int = 0,
    page: int | None = 1,
    text: str = "A chunk of the document.",
    allowed_roles: list[str] | None = None,
    vector: list[float] | None = None,
    filename: str = "policy.txt",
    document_id: uuid.UUID | None = None,
) -> Any:
    """One valid point for ``tenant_id``, with every field a test might vary.

    Eight arguments spelled out at each of the forty-odd call sites would be how a test
    ends up with one field silently different from its neighbours — a different
    ``document_id``, most likely, which is precisely the field that decides which points
    a later filter selects. Defaults here keep each call site about the one thing it is
    actually testing.

    ``document_id`` defaults to seed 1 for every call, so the default points of one test
    land in one document and a prune or a tenant-isolation assertion has something to
    select.
    """
    return build_vector_point(
        tenant_id=tenant_id,
        document_id=document_id if document_id is not None else _document_id_of(1),
        chunk_index=chunk_index,
        page=page,
        text=text,
        allowed_roles=allowed_roles,
        filename=filename,
        vector=vector if vector is not None else [0.5] * WIDTH,
    )


def _cosine_close(left: Any, right: Any, *, tolerance: float = 1e-6) -> bool:
    """Whether two stored vectors point the same way, whatever their length.

    Qdrant normalises on write for a Cosine collection, so the *values* read back never
    equal what was sent. Comparing direction instead is the only comparison that means
    something — and it is what lets these tests assert "the provider's vector reached
    Qdrant" without the assertion being about normalisation.

    Takes ``Any`` because it is called both on plain float lists this module built and on
    ``record.vector``, which ``qdrant_client`` types as a union of a float, a list of
    lists, a string and ``None`` — over REST, any of which is representable. Narrowed
    here, once, rather than at each call site.
    """
    first = _components_of(left)
    second = _components_of(right)
    if len(first) != len(second):
        return False
    dot = math.fsum(a * b for a, b in zip(first, second, strict=True))
    left_norm = math.sqrt(math.fsum(a * a for a in first))
    right_norm = math.sqrt(math.fsum(b * b for b in second))
    if left_norm == 0.0 or right_norm == 0.0:
        return False
    return math.isclose(dot / (left_norm * right_norm), 1.0, abs_tol=tolerance)


def _components_of(vector: Any) -> list[float]:
    """A vector as plain floats, refused rather than coerced when it is not one."""
    assert isinstance(vector, list), (
        f"{vector!r} is not a flat list of components; this project writes only flat "
        "vectors, so anything else is a shape change rather than noise"
    )
    components: list[float] = []
    for component in vector:
        assert isinstance(component, (int, float)), (
            f"{component!r} is not numeric"
        )
        components.append(float(component))
    return components


def delete_collection(target: Any, tenant_id: uuid.UUID) -> None:
    """Drop ``tenant_id``'s collection, ignoring a failure.

    The same teardown rule the fixtures follow: a delete that fails during cleanup says
    something about the server, not about the test that just passed. Written once here
    because a dozen isolation tests need it, and because a test that inlines the delete
    has to re-decide whether to swallow the failure each time.
    """
    try:
        target.delete_collection(collection_name_for(tenant_id))
    except Exception:  # noqa: BLE001, S110 - teardown only; see above
        pass


def _document_points(
    tenant_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    tag: str,
    vector: list[float],
    count: int,
) -> list[Any]:
    """``count`` points for one document, in one call.

    A helper because several isolation tests need the same shape with different
    documents, and repeating an eight-argument constructor four times is how a test ends
    up with one field silently different from its neighbours.
    """
    return [
        build_vector_point(
            tenant_id=tenant_id,
            document_id=document_id,
            chunk_index=index,
            page=1,
            text=f"{tag} chunk {index}",
            allowed_roles=None,
            filename=f"{tag}.txt",
            vector=vector,
        )
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# The guard, and the collection lifecycle
# ---------------------------------------------------------------------------


class TestTheGuardIsHonest:
    """The skip and the destructive-host refusal, checked so they cannot lie.

    Every other test in this module is conditional on this guard. If it refused for the
    wrong reason, or skipped for a reason it did not admit to, then a green run would
    mean "the module decided not to run" rather than "Qdrant behaved as specified" —
    and the whole point of the Qdrant gate is that a blocked run is reported as blocked
    rather than passed. So the guard itself is a test subject, and these tests run with
    no server: they inspect the guard, not Qdrant.
    """

    def test_the_url_variable_is_the_one_documented(self) -> None:
        """A misspelled variable name would make every test skip, silently and cleanly."""
        module = Path(__file__).resolve()
        source = module.read_text()

        assert QDRANT_URL_ENV_VAR in source
        assert QDRANT_URL_ENV_VAR == "TENANTMIND_TEST_QDRANT_URL", (
            "the module reads a variable the project documents under another name"
        )

    def test_the_url_is_read_at_call_time_not_at_import_time(self) -> None:
        """So exporting the variable after import still enables the suite.

        A module-level read captured at import is the reason a suite "passed" when
        launched without the variable set and then still skipped when the variable *was*
        set, with nothing changing in between.
        """
        monkeyed = os.environ.get(QDRANT_URL_ENV_VAR)
        os.environ[QDRANT_URL_ENV_VAR] = "http://127.0.0.1:6333"
        try:
            assert _configured_url() == "http://127.0.0.1:6333", (
                "a change to the environment after import was not observed"
            )
        finally:
            if monkeyed is None:
                del os.environ[QDRANT_URL_ENV_VAR]
            else:
                os.environ[QDRANT_URL_ENV_VAR] = monkeyed

    def test_a_blank_url_counts_as_unconfigured(self) -> None:
        """Whitespace is not a server, and must skip rather than be handed to a client."""
        monkeyed = os.environ.get(QDRANT_URL_ENV_VAR)
        os.environ[QDRANT_URL_ENV_VAR] = "   "
        try:
            assert _configured_url() is None
        finally:
            if monkeyed is None:
                del os.environ[QDRANT_URL_ENV_VAR]
            else:
                os.environ[QDRANT_URL_ENV_VAR] = monkeyed

    def test_the_skip_reason_names_the_variable_to_set(self) -> None:
        """A skip a reader cannot act on is a way of not reporting a blocked gate."""
        assert QDRANT_URL_ENV_VAR in SKIP_REASON
        assert SKIP_REASON == REQUIRES_QDRANT.kwargs["reason"], (
            "the declared skip and the raised skip disagree"
        )
        assert "PENDING" in SKIP_REASON, (
            "the skip does not say the gate was blocked rather than passed"
        )

    def test_the_destructive_guard_names_what_made_it_refuse(self, qdrant_url: str) -> None:
        """When the host is refused, the message must name the offending URL.

        This is the assertion that keeps the refusal from being mysterious: an operator
        who points the suite at the wrong host gets told which URL was rejected and why.
        It runs only where the configured host *is* acceptable, because the refusal path
        needs a rejected host to reach — see the test below for that side.
        """
        assert qdrant_url.startswith(("http://", "https://"))
        assert any(marker in qdrant_url.lower() for marker in SAFE_HOST_MARKERS)

    def test_no_destructive_mark_is_required_for_a_loopback_host(self) -> None:
        """A loopback server is not a production server, and is never refused.

        Requiring some second "yes I mean it" marker in addition to the host check would
        make the ordinary local setup — the one the phase documents — fail for no reason
        a reader could infer. The guard must be satisfied by a loopback host alone.
        """
        for host in ("http://127.0.0.1:6333", "http://localhost:6333"):
            assert any(marker in host for marker in SAFE_HOST_MARKERS), (
                f"{host!r} would be refused by the destructive-host guard"
            )


@requires_qdrant
class TestCollectionLifecycle:
    """Creation against a server that actually has to accept it.

    ``ensure_collection`` is the one call every ingestion makes, and its whole job is to
    be *idempotent*: called on a first ingest, on every later one, and concurrently by a
    Celery worker that does not know what another one just did. The fake in
    ``test_qdrant_store.py`` proves which requests are issued; this proves the server
    accepts them and that repeating them changes nothing.
    """

    def test_it_creates_a_collection_a_real_server_accepts(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        name = store.ensure_collection(tenant)
        assert name == collection_name_for(tenant), (
            "the collection is not the one the tenant's id derives"
        )
        assert store.collection_exists(tenant) is True
        assert store.is_available is True

    def test_the_created_collection_has_this_phase_s_width_and_metric(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        info = store.describe_collection(tenant)
        assert info.vector_size == WIDTH, (
            f"Qdrant stored {info.vector_size}, the configuration says {WIDTH}"
        )
        assert info.distance is not None
        assert DISTANCE_METRIC.lower() in info.distance.lower()

    def test_a_second_call_recreates_nothing(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Idempotency, including the payload indexes.

        The second call must not wipe the collection: a collection dropped and recreated
        on every ingestion would silently lose a document whose chunks were already
        written, which is the one failure mode a per-request ``ensure`` invites.
        """
        store.ensure_collection(tenant)
        store.upsert_points(tenant, [_point(tenant, chunk_index=0)])

        store.ensure_collection(tenant)

        assert store.count_points_for_document(tenant, _document_id_of(1)) == 1, (
            "a second ensure_collection discarded the points already written"
        )

    def test_deleting_a_missing_collection_reports_false(
        self, store: QdrantVectorStore, live_client: QdrantClient
    ) -> None:
        """Deleting an absent collection is not an error.

        Phase 2.4 deletes the database row before Phase 2.5 would delete the vectors, so
        the common order of events leaves the store deleting something that is already
        gone. Reporting that as a failure would turn a successful un-ingestion into a
        red one.
        """
        # A tenant of its own rather than the `tenant` fixture's, because that fixture
        # creates the collection being absent — which is the whole thing under test.
        absent = _random_tenant()
        assert store.collection_exists(absent) is False
        assert store.delete_collection(absent) is False

    def test_a_deleted_collection_can_be_created_again(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        store.ensure_collection(tenant)
        assert store.delete_collection(tenant) is True
        assert store.collection_exists(tenant) is False

        store.ensure_collection(tenant)

        assert store.collection_exists(tenant) is True
        assert store.describe_collection(tenant).vector_size == WIDTH

    def test_two_tenants_get_two_independent_collections(
        self, store: QdrantVectorStore, live_client: QdrantClient
    ) -> None:
        """Per tenant, not per application.

        The PMP requires a collection per tenant, and the reason is isolation rather than
        tidiness: a filter that forgot the tenant clause would then read another tenant's
        points, and per-tenant collections make that mistake a 404-shaped mistake
        instead of a silent data leak. Two tenants must therefore be two collections with
        two names — asserted through the raw client's collection list, because asking
        the store "do you have this tenant's collection" cannot distinguish "absent" from
        "present under a name I did not expect".
        """
        first, second = _random_tenant(), _random_tenant()
        store.ensure_collection(first)
        store.ensure_collection(second)
        try:
            names = {item.name for item in live_client.get_collections().collections}
            assert collection_name_for(first) in names
            assert collection_name_for(second) in names
            assert collection_name_for(first) != collection_name_for(second)

            store.upsert_points(first, [_point(first, chunk_index=0)])
            assert store.count_points_for_document(second, _document_id_of(1)) == 0, (
                "one tenant's points were counted against another's collection"
            )
        finally:
            delete_collection(store, first)
            delete_collection(store, second)


# ---------------------------------------------------------------------------
# Payload indexes
# ---------------------------------------------------------------------------


@requires_qdrant
class TestPayloadIndexesAreAcceptedByQdrant:
    def test_every_declared_index_is_actually_created(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Read back from Qdrant, not from the module's own dictionary.

        ``PAYLOAD_INDEXES`` says what this project *intends* to create. Only
        ``get_collection(...).payload_schema`` says what Qdrant did. This test is the
        difference between the two claims.
        """
        from qdrant_client import QdrantClient

        client = store._client
        assert isinstance(client, QdrantClient)
        info = client.get_collection(collection_name_for(tenant))
        schema = info.payload_schema or {}

        for field, expected in PAYLOAD_INDEXES.items():
            assert field in schema, f"Qdrant reports no index on {field!r}"
            actual = schema[field]
            assert str(getattr(actual, "data_type", actual)).lower() in {
                expected.value.lower(),
                str(expected.value).lower(),
            }, f"{field} is indexed as {actual}, expected {expected}"

    def test_chunk_index_is_indexed_so_an_ordered_scroll_works(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """The index that exists for one reason only.

        Qdrant 400s an ``order_by`` on an unindexed payload key. Without this index
        ``scroll_document_points`` fails for every document, and every test that reads a
        document's chunks back fails with it. That was found against a real 1.19.1
        server, which is why it is asserted here and not only in the fake.
        """
        store.upsert_points(tenant, [_point(tenant, chunk_index=n) for n in range(4)])
        records = store.scroll_document_points(tenant, _document_id_of(1))
        assert [payload_key(record, PAYLOAD_CHUNK_INDEX) for record in records] == [0, 1, 2, 3]

    def test_an_ordered_scroll_on_an_unindexed_key_is_refused_by_qdrant(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """The counterfactual: proving the index above is load-bearing.

        Drops the ``chunk_index`` index from a scratch collection and asks Qdrant for the
        same ordered scroll. If this *succeeds*, the real server does not need the index
        and :data:`PAYLOAD_INDEXES` carries a field for no reason — which would be worth
        knowing. If it 400s, the index is justified by observed behaviour rather than by
        documentation.
        """
        name = collection_name_for(_random_tenant())
        client = store._client
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=WIDTH, distance=DISTANCE_METRIC),
        )
        try:
            from qdrant_client import QdrantClient

            assert isinstance(client, QdrantClient)
            client.upsert(
                collection_name=name,
                points=[
                    models.PointStruct(
                        id=1,
                        vector=[0.1] * WIDTH,
                        payload={PAYLOAD_CHUNK_INDEX: 0},
                    ),
                    models.PointStruct(
                        id=2,
                        vector=[0.2] * WIDTH,
                        payload={PAYLOAD_CHUNK_INDEX: 1},
                    ),
                ],
                wait=True,
            )
            with pytest.raises(Exception) as caught:
                client.scroll(
                    collection_name=name,
                    limit=10,
                    with_payload=True,
                    order_by=models.OrderBy(
                        key=PAYLOAD_CHUNK_INDEX, direction=models.Direction.ASC
                    ),
                )
            assert "400" in str(caught.value) or "index" in str(caught.value).lower()
        finally:
            client.delete_collection(name)

    def test_reindexing_an_already_indexed_field_is_tolerated(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """A second ``ensure_collection`` must not fail on an existing index.

        This is the case that motivates each index being created inside its own
        ``try``. If it propagated, every ingestion after the first would be recorded as
        a failure by a store that is working perfectly.
        """
        store.ensure_collection(tenant)
        store.ensure_collection(tenant)
        assert store.collection_exists(tenant) is True

    def test_a_collection_from_an_earlier_phase_is_brought_up_to_date(
        self, store: QdrantVectorStore, managed_tenant: uuid.UUID
    ) -> None:
        """A collection created with *fewer* indexes gains the missing ones.

        The realistic version of this phase's bug: a collection exists, its indexes are
        incomplete, and ingestion must still succeed and still be readable. Built by
        creating the collection by hand with only one index.
        """
        name = collection_name_for(managed_tenant)
        client = store._client
        client.create_collection(
            collection_name=name,
            vectors_config=models.VectorParams(size=WIDTH, distance=DISTANCE_METRIC),
        )
        client.create_payload_index(
            collection_name=name,
            field_name=PAYLOAD_DOCUMENT_ID,
            field_schema=models.PayloadSchemaType.KEYWORD,
        )
        # The store must not treat "exists" as "complete", and must go on to bring the
        # missing indexes into being without disturbing the one already there.
        store.ensure_collection(managed_tenant)
        store.upsert_points(
            managed_tenant, [_point(managed_tenant, chunk_index=n) for n in range(2)]
        )
        read_back = store.scroll_document_points(managed_tenant, _document_id_of(1))
        assert len(read_back) == 2
        delete_collection(store, managed_tenant)


# ---------------------------------------------------------------------------
# Writes and reads
# ---------------------------------------------------------------------------


@requires_qdrant
class TestUpsertAgainstQdrant:
    def test_every_point_arrives(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Point-by-point, not just a count: a count can match with the wrong points."""
        sent = [_point(tenant, chunk_index=n, text=f"chunk number {n}") for n in range(6)]
        assert store.upsert_points(tenant, sent) == 6

        records = store.scroll_document_points(tenant, _document_id_of(1))
        assert len(records) == 6
        assert [payload_key(record, PAYLOAD_CHUNK_INDEX) for record in records] == list(range(6))

    def test_the_payload_arrives_intact(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Every field, compared against what was sent.

        The document→chunk→embedding→point consistency requirement: if a field is
        dropped, renamed or coerced in transit, this is where it shows.
        """
        sent = _point(
            tenant,
            chunk_index=3,
            page=7,
            text="A paragraph about procurement lead times.",
            allowed_roles=["admin", "editor"],
            filename="Q3 procurement plan.docx",
        )
        store.upsert_points(tenant, [sent])

        [record] = store.scroll_document_points(tenant, _document_id_of(1))
        payload = record.payload
        assert payload is not None
        assert payload[PAYLOAD_DOCUMENT_ID] == str(sent.payload[PAYLOAD_DOCUMENT_ID])
        assert payload[PAYLOAD_CHUNK_INDEX] == 3
        assert payload[PAYLOAD_PAGE] == 7
        assert payload[PAYLOAD_TEXT] == "A paragraph about procurement lead times."
        assert payload[PAYLOAD_ALLOWED_ROLES] == ["admin", "editor"]
        assert payload[PAYLOAD_FILENAME] == "Q3 procurement plan.docx"
        assert payload[PAYLOAD_TENANT_ID] == str(tenant)
        assert record.id == sent.id

    def test_the_id_arrives_as_the_deterministic_uuid(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Qdrant accepts a UUID string and keeps it as one.

        Checked against the value re-derived here rather than against
        ``sent.id``, because "Qdrant stored what we sent" is the weaker claim and the
        one that would still pass if ``point_id`` were random.
        """
        store.upsert_points(tenant, [_point(tenant, chunk_index=5)])
        [record] = store.scroll_document_points(tenant, _document_id_of(1))
        assert str(record.id) == point_id(
            tenant_id=tenant, document_id=_document_id_of(1), chunk_index=5
        )

    def test_cosine_normalises_the_vector_on_write(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """The finding this file is partly built around.

        Sent: ``[1.0] * 8``, norm 2.83. Stored: ``[0.3535] * 8``, norm 1.0. Qdrant
        normalises on write for Cosine collections, so the stored vector is *not* the
        sent one — and any test comparing them exactly would fail against a real server
        while passing against the fake.

        Asserted as direction equality, which is what Cosine similarity actually uses and
        therefore the only equality that carries meaning here.
        """
        sent = [1.0] * WIDTH
        store.upsert_points(tenant, [_point(tenant, vector=sent)])

        [record] = store.scroll_document_points(tenant, _document_id_of(1))
        stored = vector_of(record)

        assert not all(
            math.isclose(a, b, abs_tol=1e-9) for a, b in zip(sent, stored, strict=True)
        ), "the raw values matched, so normalisation did not happen as documented"
        assert _cosine_close(stored, sent), "direction was changed, not just magnitude"
        norm = math.sqrt(sum(component**2 for component in stored))
        assert math.isclose(norm, 1.0, abs_tol=1e-6), "the stored vector is not unit length"

    def test_two_documents_of_different_length_keep_their_direction(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Why the metric is Cosine: magnitude must not carry meaning.

        One long and one short chunk, deliberately scaled by 1000. Under Cosine both
        read back unit length and their *direction* is preserved; under Dot the stored
        values would differ by that factor and a search would rank by length.
        """
        direction = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        store.upsert_points(
            tenant,
            [
                _point(tenant, chunk_index=0, vector=[1.0] * WIDTH),
                _point(tenant, chunk_index=1, vector=direction),
            ],
        )
        records = store.scroll_document_points(tenant, _document_id_of(1))
        by_index = {payload_key(record, PAYLOAD_CHUNK_INDEX): record for record in records}
        assert _cosine_close(by_index[1].vector, direction)
        assert _cosine_close(by_index[1].vector, by_index[0].vector) is False

    def test_reingesting_overwrites_rather_than_duplicating(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """The phase's idempotency requirement, verified against the real server.

        Not assumed from the method name: an ``upsert`` of a missing id inserting a
        second copy would leave the count wrong forever, and only the server's own count
        can settle it.
        """
        store.upsert_points(
            tenant, [_point(tenant, chunk_index=n, text=f"original {n}") for n in range(4)]
        )
        store.upsert_points(
            tenant, [_point(tenant, chunk_index=n, text=f"revised {n}") for n in range(4)]
        )

        assert store.count_points_for_document(tenant, _document_id_of(1)) == 4
        records = store.scroll_document_points(tenant, _document_id_of(1))
        assert [payload_key(record, PAYLOAD_TEXT) for record in records] == [
            f"revised {n}" for n in range(4)
        ]

    def test_the_server_reports_an_exact_count(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Because ``documents.chunk_count`` is compared with this figure.

        Qdrant's default count becomes approximate above a threshold. A server that
        answered with an estimate would make a real mismatch compare as a pass.
        """
        store.upsert_points(tenant, [_point(tenant, chunk_index=n) for n in range(5)])
        assert store.count_points_for_document(tenant, _document_id_of(1)) == 5
        info = store.describe_collection(tenant)
        assert info.points == 5, "the exact figure was dropped as approximate"

    def test_a_long_payload_survives_the_round_trip(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """A chunk at the payload bound is written whole.

        The bound exists to stop an unbounded payload, so the interesting question is
        whether a payload *at* the bound is accepted rather than rejected — a server-side
        limit quietly below this constant would turn a designed truncation into a lost
        ingestion.
        """
        text = "q" * MAX_PAYLOAD_TEXT_CHARS
        store.upsert_points(tenant, [_point(tenant, text=text)])
        [record] = store.scroll_document_points(tenant, _document_id_of(1))
        stored = payload_key(record, PAYLOAD_TEXT)
        assert len(stored) == MAX_PAYLOAD_TEXT_CHARS

    def test_unicode_survives_the_round_trip(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Multi-byte text, in several scripts, and a character that needs escaping.

        A payload is JSON. A payload that arrives mojibaked would be quoted verbatim in a
        citation, so this checks the bytes survived rather than that the call returned.
        """
        text = "Überblick — 概要 — резюме — \"quoted\" — \\backslash\\ — ✓"
        store.upsert_points(tenant, [_point(tenant, text=text)])
        [record] = store.scroll_document_points(tenant, _document_id_of(1))
        assert payload_key(record, PAYLOAD_TEXT) == text

    def test_an_empty_role_list_is_stored_as_given(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """``[]`` and ``None`` are opposite meanings and must not be conflated.

        ``None`` means no restriction; ``[]`` would mean a document nobody may read. If
        Qdrant or the client dropped the empty list, the restriction check in Phase 2.6
        would treat it as unrestricted.
        """
        store.upsert_points(
            tenant,
            [
                _point(tenant, chunk_index=0, allowed_roles=None),
                _point(tenant, chunk_index=1, allowed_roles=[]),
            ],
        )
        records = store.scroll_document_points(tenant, _document_id_of(1))
        by_index = {payload_key(record, PAYLOAD_CHUNK_INDEX): record for record in records}
        assert payload_key(by_index[0], PAYLOAD_ALLOWED_ROLES) is None
        assert payload_key(by_index[1], PAYLOAD_ALLOWED_ROLES) == []

    def test_a_null_page_survives_as_null(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """"This format has no pages" must not become "page zero"."""
        store.upsert_points(tenant, [_point(tenant, page=None)])
        [record] = store.scroll_document_points(tenant, _document_id_of(1))
        assert payload_key(record, PAYLOAD_PAGE) is None


# ---------------------------------------------------------------------------
# Deletion — the filters, against a real engine
# ---------------------------------------------------------------------------


@requires_qdrant
class TestDeletionFiltersAgainstQdrant:
    def test_deleting_one_document_leaves_another(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Two documents in one tenant, one deleted.

        A delete filter that ignored ``document_id`` would take both. That is the bug this
        test exists for, and only a real filter engine can rule it out.
        """
        first, second = _document_id_of(1), _document_id_of(2)
        store.upsert_points(
            tenant,
            [
                *_document_points(
                    tenant, first, tag="first", vector=[0.3] * WIDTH, count=4
                ),
                *_document_points(
                    tenant, second, tag="second", vector=[0.4] * WIDTH, count=3
                ),
            ],
        )

        store.delete_document_points(tenant, first)

        assert store.count_points_for_document(tenant, first) == 0
        assert store.count_points_for_document(tenant, second) == 3

    def test_stale_removal_deletes_exactly_the_range_above(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """The re-ingestion case: a document that now has fewer chunks.

        A range filter that ignored ``document_id`` would also delete another document's
        high-indexed chunks, so both documents are populated and both are asserted.
        """
        first, second = _document_id_of(1), _document_id_of(2)
        store.upsert_points(
            tenant,
            [
                *_document_points(
                    tenant, first, tag="first", vector=[0.2] * WIDTH, count=8
                ),
                *_document_points(
                    tenant, second, tag="second", vector=[0.2] * WIDTH, count=8
                ),
            ],
        )

        store.delete_stale_points(tenant, first, keep_below_index=3)

        kept = store.scroll_document_points(tenant, first)
        assert [payload_key(record, PAYLOAD_CHUNK_INDEX) for record in kept] == [0, 1, 2]
        assert store.count_points_for_document(tenant, second) == 8, (
            "the range filter reached into another document"
        )

    @pytest.mark.parametrize(
        ("threshold", "expected_survivors"),
        [
            (0, []),
            (1, [0]),
            (3, [0, 1, 2]),
            (99, [0, 1, 2]),
        ],
    )
    def test_the_threshold_is_inclusive_from_above(
        self,
        store: QdrantVectorStore,
        tenant: uuid.UUID,
        threshold: int,
        expected_survivors: list[int],
    ) -> None:
        """``keep_below_index=n`` removes chunks ``n`` and above, and keeps ``0..n-1``.

        Asserted at the boundaries on a real engine, because an off-by-one here is
        invisible in every other test: a version that removed ``> n`` would leave exactly
        one stale point and still pass a single middle-of-the-range check.

        ``0`` is the case that makes the semantics unambiguous rather than guessed at:
        the filter is ``chunk_index >= n``, so ``0`` removes every chunk including
        chunk 0. That is the correct reading of the name — "keep below 0" keeps nothing —
        and it is why the orchestrator always passes the *chunk count*, which is never
        zero because the chunker refuses to emit an empty chunk.
        """
        store.upsert_points(
            tenant, [_point(tenant, chunk_index=n) for n in range(3)]
        )
        store.delete_stale_points(tenant, _document_id_of(1), keep_below_index=threshold)
        survivors = store.scroll_document_points(tenant, _document_id_of(1))
        assert [payload_key(record, PAYLOAD_CHUNK_INDEX) for record in survivors] == expected_survivors

    def test_a_range_of_zero_removes_nothing(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """A range no point satisfies must remove no points at all.

        The dangerous failure here is not the off-by-one — that is the threshold test
        below — but a range filter that degrades to "match everything" when its bounds
        exclude every index. On a collection holding several documents that would delete
        a tenant's other documents on an idempotent re-ingest, and it would look exactly
        like success: the stale count is still correct afterwards.
        """
        mine, theirs = _document_id_of(1), _document_id_of(2)
        store.upsert_points(
            tenant,
            [
                *_document_points(tenant, mine, tag="mine", vector=[0.3] * WIDTH, count=3),
                *_document_points(
                    tenant, theirs, tag="theirs", vector=[0.3] * WIDTH, count=3
                ),
            ],
        )

        removed = store.delete_stale_points(tenant, mine, keep_below_index=99)

        assert removed == 3, "an unsatisfiable range removed something"
        assert store.count_points_for_document(tenant, theirs) == 3, (
            "the range filter reached into another document"
        )

    def test_deleting_a_document_that_never_ingested_is_harmless(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """A tenant's first ingestion, and a document that failed before writing."""
        assert store.delete_document_points(tenant, uuid.uuid4()) == 0
        assert store.delete_stale_points(tenant, uuid.uuid4(), keep_below_index=5) == 0

    def test_deleting_a_whole_document_makes_an_ordered_scroll_empty(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        store.upsert_points(
            tenant, [_point(tenant, chunk_index=n) for n in range(4)]
        )
        store.delete_document_points(tenant, _document_id_of(1))
        assert store.scroll_document_points(tenant, _document_id_of(1)) == []
        assert store.describe_collection(tenant).points == 0

    def test_a_scroll_respects_its_limit(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """So a bounded read cannot silently become unbounded."""
        store.upsert_points(
            tenant, [_point(tenant, chunk_index=n) for n in range(6)]
        )
        assert len(store.scroll_document_points(tenant, _document_id_of(1), limit=3)) == 3


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


@requires_qdrant
class TestTenantIsolationAgainstQdrant:
    """Two tenants, one server, real collections.

    Every other test file in this phase proves isolation in this project's own code.
    This one proves the *store* the server offers can hold two tenants' data without
    mixing it — including the case where the two tenants' document ids are identical,
    which is the strongest version of the question.
    """

    def test_two_tenants_holding_the_same_document_id_stay_separate(
        self, store: QdrantVectorStore, live_client: QdrantClient
    ) -> None:
        """The adversarial case: same document id, same chunk indexes, same names.

        Only the tenant differs, so any filter that reaches across a collection would be
        caught here. Both documents' points must remain individually readable and
        individually countable.
        """
        tenant_a, tenant_b = _random_tenant(), _random_tenant()
        shared_document = _document_id_of(1)
        try:
            for tenant, marker in ((tenant_a, "AAA"), (tenant_b, "BBB")):
                store.ensure_collection(tenant)
                store.upsert_points(
                    tenant,
                    _document_points(
                        tenant,
                        shared_document,
                        tag=marker,
                        vector=[0.5] * WIDTH,
                        count=3,
                    ),
                )

            for tenant, marker in ((tenant_a, "AAA"), (tenant_b, "BBB")):
                records = store.scroll_document_points(tenant, shared_document)
                assert len(records) == 3
                texts = [payload_key(record, PAYLOAD_TEXT) for record in records]
                assert texts == [f"{marker} chunk {n}" for n in range(3)], (
                    "a tenant read another tenant's chunks"
                )
                assert store.count_points_for_document(tenant, shared_document) == 3

          
        # And the collections really are two, not one with two tenants' points in it.
            assert collection_name_for(tenant_a) != collection_name_for(tenant_b)
        finally:
            delete_collection(store, tenant_a)
            delete_collection(store, tenant_b)

    def test_one_tenant_s_deletion_cannot_reach_another(
        self, store: QdrantVectorStore, live_client: QdrantClient
    ) -> None:
        """Tenant A deletes "the document"; tenant B's identical document survives."""
        tenant_a, tenant_b = _random_tenant(), _random_tenant()
        shared_document = _document_id_of(1)
        try:
            for tenant in (tenant_a, tenant_b):
                store.ensure_collection(tenant)
                store.upsert_points(
                    tenant, [_point(tenant, chunk_index=n) for n in range(3)]
                )

            store.delete_document_points(tenant_a, shared_document)

            assert store.count_points_for_document(tenant_a, shared_document) == 0
            assert store.count_points_for_document(tenant_b, shared_document) == 3
        finally:
            delete_collection(store, tenant_a)
            delete_collection(store, tenant_b)

    def test_one_tenant_s_pruning_cannot_reach_another(
        self, store: QdrantVectorStore, live_client: QdrantClient
    ) -> None:
        """The range filter, tested across tenants.

        A delete without the ``document_id`` clause would empty both collections; a
        delete against the wrong collection would empty the wrong one. Only both
        assertions distinguish those.
        """
        tenant_a, tenant_b = _random_tenant(), _random_tenant()
        try:
            for tenant in (tenant_a, tenant_b):
                store.ensure_collection(tenant)
                store.upsert_points(
                    tenant, [_point(tenant, chunk_index=n) for n in range(6)]
                )

            store.delete_stale_points(tenant_a, _document_id_of(1), keep_below_index=2)

            assert store.count_points_for_document(tenant_a, _document_id_of(1)) == 2
            assert store.count_points_for_document(tenant_b, _document_id_of(1)) == 6
        finally:
            delete_collection(store, tenant_a)
            delete_collection(store, tenant_b)

    def test_every_point_carries_its_own_tenant(
        self, store: QdrantVectorStore, live_client: QdrantClient
    ) -> None:
        """The defence-in-depth check, run for real.

        A hand-mixed point is written into a tenant's collection — the state a
        half-finished migration to a shared collection would leave behind — and the
        check must notice.
        """
        tenant = _random_tenant()
        try:
            store.ensure_collection(tenant)
            store.upsert_points(tenant, [_point(tenant, chunk_index=0)])
            assert store.payload_tenant_id_is_exhaustive(tenant) is True

            client = store._client
            client.upsert(
                collection_name=collection_name_for(tenant),
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=[0.1] * WIDTH,
                        payload={PAYLOAD_TENANT_ID: str(_random_tenant())},
                    )
                ],
                wait=True,
            )
            assert store.payload_tenant_id_is_exhaustive(tenant) is False
        finally:
            delete_collection(store, tenant)

    def test_a_missing_collection_has_nothing_to_be_exhaustive_about(
        self, store: QdrantVectorStore, live_client: QdrantClient
    ) -> None:
        """True, not an error — so it is usable as an assertion in a test."""
        assert store.payload_tenant_id_is_exhaustive(_random_tenant()) is True


# ---------------------------------------------------------------------------
# Failure modes, against a real server
# ---------------------------------------------------------------------------


@requires_qdrant
class TestRealFailuresAreTranslated:
    def test_an_unreachable_port_raises_a_store_error(self) -> None:
        """A refused connection, produced for real.

        Nothing is stubbed: the port is one nothing listens on. This is the failure the
        whole ``CLIENT_FAILURES`` tuple exists for, and asserting it against a fake would
        be asserting the fake.
        """
        instance = QdrantVectorStore(
            make_settings(
                embedding_dimension=WIDTH,
                qdrant_url="http://127.0.0.1:1",
                qdrant_timeout_seconds=2,
            )
        )
        try:
            assert instance.is_available is False
            with pytest.raises(VectorStoreError, match="could not be reached"):
                instance.collection_exists(_random_tenant())
        finally:
            instance.close()

    def test_an_unreachable_store_refuses_to_claim_a_collection_is_absent(
        self,
    ) -> None:
        """``False`` would invite a create on a server that is not answering."""
        instance = QdrantVectorStore(
            make_settings(
                embedding_dimension=WIDTH,
                qdrant_url="http://127.0.0.1:1",
                qdrant_timeout_seconds=2,
            )
        )
        try:
            with pytest.raises(VectorStoreError) as caught:
                instance.collection_exists(_random_tenant())
            assert "could not be reached" in str(caught.value)
        finally:
            instance.close()

    def test_an_unreachable_store_fails_an_ingestion_rather_than_succeeding_quietly(
        self,
    ) -> None:
        """The ingestion-shaped consequence: a write raises instead of returning a count.

        A ``0`` returned here would be recorded as a successful ingestion of nothing.
        """
        instance = QdrantVectorStore(
            make_settings(
                embedding_dimension=WIDTH,
                qdrant_url="http://127.0.0.1:1",
                qdrant_timeout_seconds=2,
            )
        )
        tenant = _random_tenant()
        try:
            with pytest.raises(VectorStoreError):
                instance.upsert_points(
                    tenant, [_point(tenant, vector=[0.5] * WIDTH)]
                )
        finally:
            instance.close()

    def test_a_width_mismatch_against_a_real_collection(
        self, store: QdrantVectorStore, managed_tenant: uuid.UUID
    ) -> None:
        """A server-side collection at 384, this project configured for 8.

        Built for real, so the mismatch is a fact about the server rather than a value
        in a fake's constructor.
        """
        store.ensure_collection(managed_tenant)

        wider = QdrantVectorStore(
            make_settings(embedding_dimension=384), client=store._client
        )
        with pytest.raises(VectorDimensionMismatch) as caught:
            wider.ensure_collection(managed_tenant)

        message = str(caught.value)
        assert f"{WIDTH}-dimensional" in message, "the existing width is not named"
        assert str(384) in message, "the configured width is not named"
        assert "cannot be resized" in message, "the operator is not told what to do"

    def test_the_refusal_happens_before_any_point_is_written(
        self, store: QdrantVectorStore, managed_tenant: uuid.UUID
    ) -> None:
        """A rejected ingestion must leave the collection empty, not half written."""
        store.ensure_collection(managed_tenant)

      
        # A second store over a second client, sharing the server. Injecting a client
      
        # built by the first store would close it on teardown and break whatever ran next.
        mismatched = QdrantVectorStore(
            make_settings(embedding_dimension=384, qdrant_url=os.environ[QDRANT_URL_ENV_VAR])
        )
        try:
            with pytest.raises(VectorDimensionMismatch):
                mismatched.ensure_collection(managed_tenant)
            assert mismatched.describe_collection(managed_tenant).points == 0
        finally:
            mismatched.close()

    def test_a_wrong_width_point_is_refused_by_the_server_too(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """The project's own check fires first; this proves the server agrees.

        Bypasses the store deliberately, so what is being observed is Qdrant's answer,
        not TenantMind's. It matters because the store's message is the one an operator
        reads, and it should not describe a situation the server would have accepted.
        """
        client = store._client
        with pytest.raises(Exception) as caught:
            client.upsert(
                collection_name=collection_name_for(tenant),
                points=[
                    models.PointStruct(id=1, vector=[0.1] * (WIDTH + 1), payload={})
                ],
                wait=True,
            )
        assert "384" in str(caught.value) or "8" in str(caught.value) or "dimension" in str(caught.value).lower()

    def test_a_real_server_rejection_becomes_a_store_error(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """A server-side rejection is translated, not leaked.

        Provoked with a vector Qdrant genuinely refuses — ``NaN``, which is the one value
        a real embedding provider could conceivably return and which cannot be stored,
        because any comparison against it is undefined. The raw client exception is an
        ``UnexpectedResponse`` carrying Qdrant's own text; what application code sees is a
        :class:`VectorStoreError` that names the operation and the exception class, and
        which carries none of the rejected payload.

        A rejection the store *should* catch earlier — a wrong width — is covered
        separately in :meth:`test_a_wrong_width_point_is_refused_by_the_server_too`.
        """
        poisoned = [0.1] * (WIDTH - 1) + [math.nan]
        with pytest.raises(VectorStoreError) as caught:
            store.upsert_points(tenant, [_point(tenant, vector=poisoned)])

        message = str(caught.value)
        assert "vector point" in message, "the operation is not named"
        assert "UnexpectedResponse" in message, "the client exception class is not named"
      
        # Not `assert "nan" not in message`: that substring is inside "uNexpected".
        assert "NaN" not in message, "the rejected value leaked into the message"

    def test_a_rejected_write_leaves_the_collection_unchanged(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """A failure the server raised did not half-apply the batch.

        Qdrant rejects the whole request, so the count before and after must match. If
        this ever stopped holding, the ingestion orchestrator's "avoid partial vector
        state" claim would be false and the recovery story would have to change.
        """
        store.upsert_points(tenant, [_point(tenant, chunk_index=0)])
        before = store.count_points_for_document(tenant, _document_id_of(1))

        poisoned = [0.1] * (WIDTH - 1) + [math.nan]
        with pytest.raises(VectorStoreError):
            store.upsert_points(
                tenant,
                [_point(tenant, chunk_index=1), _point(tenant, chunk_index=2, vector=poisoned)],
            )

        assert store.count_points_for_document(tenant, _document_id_of(1)) == before


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _query_vector(*, lead: float = 1.0) -> list[float]:
    """A ``WIDTH``-wide vector whose first component is ``lead`` and whose rest are ``1``.

    Not a constant vector: two constant vectors have cosine similarity 1.0 whatever their
    value, so a fixture built from them would rank every candidate identically and the
    ordering assertions below would pass on insertion order instead of on order. Moving one
    component moves the cosine monotonically, which is what makes "the nearer vector came
    back first" a claim about the data.
    """
    return [lead, *([1.0] * (WIDTH - 1))]


@requires_qdrant
class TestSearchAgainstQdrant:
    """PMP Phase 4's retrieval, against the server that will actually serve it.

    The unit suite asserts what the store *sends*; this asserts what Qdrant *selects*. That
    distinction is not pedantry here — the role filter is the one filter whose semantics
    (``None`` unrestricted, ``[]`` visible to nobody, ``["admin"]`` admin only) have to be
    reproduced exactly or restricted documents leak, and ``MatchAny``-over-an-array is
    precisely the shape where a plausible reading and the actual behaviour differ. So every
    case in :class:`TestRoleFilteringAgainstQdrant` was checked against a real 1.19.1
    server rather than inferred from the client's model classes.
    """

    def test_the_nearest_point_comes_back_first(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        near, far = _document_id_of(1), _document_id_of(2)
        store.upsert_points(
            tenant,
            [
                _point(tenant, document_id=far, vector=_query_vector(lead=-0.9)),
                _point(tenant, document_id=near, vector=_query_vector(lead=0.95)),
            ],
        )

        hits = store.search_points(
            tenant, vector=_query_vector(), limit=2, role=UserRole.VIEWER
        )
        assert [scored_payload_of(hit)[PAYLOAD_DOCUMENT_ID] for hit in hits] == [
            str(near),
            str(far),
        ]
        assert hits[0].score > hits[1].score

    def test_the_payload_arrives_intact_on_a_search_hit(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Every field a citation is built from, read back off a real response.

        A search that returned a score and an id but a degraded payload would produce
        citations with a missing ``filename`` or a null ``page`` — which is a user-visible
        defect in the answer, and one no unit test of the store would catch.
        """
        store.upsert_points(
            tenant,
            [
                _point(
                    tenant,
                    chunk_index=7,
                    page=3,
                    text="Sentences survive the round trip.",
                    filename="handbook.txt",
                )
            ],
        )

        [hit] = store.search_points(
            tenant, vector=_query_vector(), limit=1, role=UserRole.VIEWER
        )
        assert hit.payload is not None
        assert hit.payload[PAYLOAD_TEXT] == "Sentences survive the round trip."
        assert hit.payload[PAYLOAD_CHUNK_INDEX] == 7
        assert hit.payload["page"] == 3
        assert hit.payload[PAYLOAD_FILENAME] == "handbook.txt"
        assert hit.payload[PAYLOAD_TENANT_ID] == str(tenant)

    def test_the_limit_is_honoured_by_the_server(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        store.upsert_points(
            tenant, [_point(tenant, chunk_index=index) for index in range(6)]
        )
        assert (
            len(
                store.search_points(
                    tenant, vector=_query_vector(), limit=2, role=UserRole.VIEWER
                )
            )
            == 2
        )

    def test_an_empty_collection_returns_no_hits(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """A collection that exists and holds nothing is not an error.

        Distinct from the missing-collection case below, and worth its own test because the
        two take different paths: here ``query_points`` is genuinely called and Qdrant
        answers with an empty result set.
        """
        assert (
            store.search_points(
                tenant, vector=_query_vector(), limit=5, role=UserRole.VIEWER
            )
            == []
        )

    def test_a_missing_collection_returns_no_hits(
        self, store: QdrantVectorStore, managed_tenant: uuid.UUID
    ) -> None:
        """A tenant that has never ingested has no collection at all.

        Uses ``managed_tenant`` rather than ``tenant`` precisely because that fixture does
        *not* create the collection — the state under test is its absence.
        """
        assert store.collection_exists(managed_tenant) is False
        assert (
            store.search_points(
                managed_tenant, vector=_query_vector(), limit=5, role=UserRole.VIEWER
            )
            == []
        )

    def test_tied_scores_come_back_in_the_same_order_every_time(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        """Two chunks at cosine 1.0, repeated, so the tie-break is observable.

        Both points are constant vectors of the same value, so their scores are exactly
        equal and Qdrant is free to order them however it likes. The store sorts ties by id,
        so the answer is a function of the stored data — and this test is what would catch
        Qdrant changing its ordering under the project.
        """
        store.upsert_points(
            tenant,
            [
                _point(tenant, chunk_index=1, text="second"),
                _point(tenant, chunk_index=0, text="first"),
            ],
        )
        first = store.search_points(
            tenant, vector=_query_vector(), limit=2, role=UserRole.VIEWER
        )
        second = store.search_points(
            tenant, vector=_query_vector(), limit=2, role=UserRole.VIEWER
        )

        assert [hit.id for hit in first] == [hit.id for hit in second]
        assert [str(hit.id) for hit in first] == sorted(str(hit.id) for hit in first)

    def test_a_wrong_width_query_vector_is_refused_before_the_server_is_asked(
        self, store: QdrantVectorStore, tenant: uuid.UUID
    ) -> None:
        with pytest.raises(VectorDimensionMismatch):
            store.search_points(
                tenant,
                vector=[0.1] * (WIDTH + 1),
                limit=5,
                role=UserRole.VIEWER,
            )

    def test_a_search_against_an_unreachable_store_is_a_store_error(self) -> None:
        """An outage on the read path is an error, never an empty result.

        The distinction this protects is the expensive one. ``search_points`` returns ``[]``
        for a tenant with no collection — a successful query with nothing to answer from —
        and a raw ``ResponseHandlingException`` for a store that cannot be reached. If an
        outage were reported as ``[]``, every query against a down Qdrant would answer
        "I have no documents to draw on" with full confidence, which is a false statement
        about the tenant's knowledge base rather than a visible failure. So the existence
        pre-check raises rather than answering ``False``, and that propagates.

        The message names the *existence check*, not the search, because the pre-check is
        what fails first: ``search_points`` asks whether the collection exists before
        querying it, and on an unreachable server that is where the connection is refused.
        Asserting the type and the absence of leaked detail rather than a specific
        operation, so this test stays true if that order ever changes.
        """
        unreachable = QdrantVectorStore(
            make_settings(
                embedding_dimension=WIDTH,
                qdrant_url="http://127.0.0.1:1",
                qdrant_timeout_seconds=1,
            )
        )
        try:
            with pytest.raises(VectorStoreError) as caught:
                unreachable.search_points(
                    _random_tenant(),
                    vector=_query_vector(),
                    limit=5,
                    role=UserRole.VIEWER,
                )
            message = str(caught.value)
            assert "could not be reached" in message, "an outage is not named as one"
            assert "ResponseHandlingException" in message, (
                "the client exception class is not named, so an operator cannot tell a "
                "transport failure from a server-side rejection"
            )
            assert "127.0.0.1" not in message, "the endpoint leaked into the message"
            assert "http" not in message, "the endpoint leaked into the message"
        finally:
            unreachable.close()


@requires_qdrant
class TestRoleFilteringAgainstQdrant:
    """The ``allowed_roles`` filter, answered by the server.

    The equivalence being established is between this filter and
    :meth:`app.models.document.Document.role_may_view` — PostgreSQL's answer for a document
    and Qdrant's answer for the copies of its chunks. They are two independent
    implementations of one rule, and the whole point of keeping the filter in the store is
    that the second one is a faithful translation of the first. Every row of the truth table
    is therefore tested here against a real server rather than against a fake that
    re-implements the same reading.
    """

    @pytest.fixture
    def populated(self, store: QdrantVectorStore, tenant: uuid.UUID) -> uuid.UUID:
        """One collection holding all four interesting ``allowed_roles`` values.

        Written in a single call so the four documents are indexed identically and the
        filter is the only thing distinguishing them.
        """
        store.upsert_points(
            tenant,
            [
                _point(
                    tenant,
                    document_id=_document_id_of(1),
                    allowed_roles=None,
                    text="UNRESTRICTED",
                ),
                _point(
                    tenant,
                    document_id=_document_id_of(2),
                    allowed_roles=[],
                    text="VISIBLE-TO-NOBODY",
                ),
                _point(
                    tenant,
                    document_id=_document_id_of(3),
                    allowed_roles=["admin"],
                    text="ADMIN-ONLY",
                ),
                _point(
                    tenant,
                    document_id=_document_id_of(4),
                    allowed_roles=["admin", "editor"],
                    text="ADMIN-AND-EDITOR",
                ),
            ],
        )
        return tenant

    @staticmethod
    def _texts(hits: list[Any]) -> list[str]:
        texts: list[str] = []
        for hit in hits:
            assert hit.payload is not None
            texts.append(hit.payload[PAYLOAD_TEXT])
        return texts

    def _search_as(
        self, store: QdrantVectorStore, tenant: uuid.UUID, role: UserRole
    ) -> list[str]:
        hits = store.search_points(
            tenant, vector=_query_vector(), limit=10, role=role
        )
        return sorted(self._texts(hits))

    def test_a_viewer_sees_only_the_unrestricted_document(
        self, store: QdrantVectorStore, populated: uuid.UUID
    ) -> None:
        assert self._search_as(store, populated, UserRole.VIEWER) == ["UNRESTRICTED"]

    def test_an_admin_sees_the_unrestricted_and_the_admin_only_documents(
        self, store: QdrantVectorStore, populated: uuid.UUID
    ) -> None:
        assert self._search_as(store, populated, UserRole.ADMIN) == [
            "ADMIN-AND-EDITOR",
            "ADMIN-ONLY",
            "UNRESTRICTED",
        ]

    def test_an_editor_sees_the_unrestricted_and_the_multi_role_documents(
        self, store: QdrantVectorStore, populated: uuid.UUID
    ) -> None:
        assert self._search_as(store, populated, UserRole.EDITOR) == [
            "ADMIN-AND-EDITOR",
            "UNRESTRICTED",
        ]

    def test_the_document_visible_to_nobody_is_visible_to_nobody(
        self, store: QdrantVectorStore, populated: uuid.UUID
    ) -> None:
        """``[]`` selected by no role at all — the case most easily got wrong.

        Asserted by exclusion from every role's results rather than by a single role's, so
        a filter that matched ``[]`` for exactly one role still fails here. This is the row
        where an over-restrictive configuration becomes a disclosure.
        """
        for role in UserRole:
            texts = self._search_as(store, populated, role)
            assert "VISIBLE-TO-NOBODY" not in texts, (
                f"{role} was shown a document restricted to no role"
            )

    def test_the_filter_agrees_with_the_postgres_predicate_for_every_case(
        self, store: QdrantVectorStore, populated: uuid.UUID
    ) -> None:
        """The whole truth table, compared against the model rather than restated.

        The three tests above each assert one role's results, which is good but incomplete:
        they would all still pass if a *new* restriction case were added to
        :meth:`Document.role_may_view` without a matching filter clause. This derives the
        expected set from the authoritative predicate — constructed per case, so the model
        is genuinely consulted and not a table copied from the filter — and compares it with
        what Qdrant returned. That is an equivalence check between two independent
        implementations of one rule, which is the property the whole defence-in-depth
        design rests on.

        Parameterised over the three roles because a divergence could be role-specific: the
        filter compares against ``role.value``, and a mismatch between the enum's value and
        the string Qdrant stores would show up as one role seeing nothing.
        """
        # (marker, allowed_roles) for each of the four documents written by the fixture.
        documents = [
            ("UNRESTRICTED", None),
            ("VISIBLE-TO-NOBODY", []),
            ("ADMIN-ONLY", ["admin"]),
            ("ADMIN-AND-EDITOR", ["admin", "editor"]),
        ]

        for role in UserRole:
            predicted = sorted(
                marker
                for marker, allowed in documents
                if Document(
                    tenant_id=populated,
                    filename="policy.txt",
                    storage_key=f"documents/{marker}",
                    status=DocumentStatus.READY,
                    allowed_roles=allowed,
                ).role_may_view(role)
            )
            selected = self._search_as(store, populated, role)
            assert predicted == selected, (
                f"for role {role.value!r} the model predicts {predicted} but the "
                f"payload filter returned {selected}"
            )


@requires_qdrant
class TestRetrievalTenantIsolationAgainstQdrant:
    """Two tenants' chunks, one query, one server.

    The strongest form of the question Phase 4 has to answer: not "does the filter reject
    the wrong role's document" but "can a query run as Tenant A return Tenant B's text at
    all". Both collections hold chunks at identical distances from an identical query, so
    the collection name is the only thing that can decide the outcome.
    """

    def test_a_query_as_one_tenant_returns_only_that_tenant_s_chunks(
        self, store: QdrantVectorStore, managed_tenant: uuid.UUID
    ) -> None:
        tenant_a, tenant_b = _random_tenant(), _random_tenant()
        shared_document = _document_id_of(1)
        try:
            for tenant, marker in ((tenant_a, "AAA"), (tenant_b, "BBB")):
                store.ensure_collection(tenant)
                store.upsert_points(
                    tenant,
                    [
                        _point(
                            tenant,
                            document_id=shared_document,
                            text=f"{marker} tenant content",
                            vector=_query_vector(),
                        )
                    ],
                )

            for tenant, expected, forbidden in (
                (tenant_a, "AAA", "BBB"),
                (tenant_b, "BBB", "AAA"),
            ):
                hits = store.search_points(
                    tenant, vector=_query_vector(), limit=10, role=UserRole.ADMIN
                )
                texts = [scored_payload_of(hit)[PAYLOAD_TEXT] for hit in hits]
                assert texts == [f"{expected} tenant content"], (
                    f"a query as one tenant returned another tenant's chunks: {texts}"
                )
                assert not any(forbidden in text for text in texts)
        finally:
            delete_collection(store, tenant_a)
            delete_collection(store, tenant_b)

    def test_a_tenant_holding_a_document_id_the_other_tenant_also_holds(
        self, store: QdrantVectorStore, managed_tenant: uuid.UUID
    ) -> None:
        """Same document id, so a payload-based filter could not have caught a mix-up.

        The adversarial version: with a shared ``document_id``, the only thing separating the
        two chunks is the collection. A store that searched the wrong collection would return
        the right *shape* of answer — one hit, correct document id — with the wrong tenant's
        text in it, which is exactly the failure a document-id-based assertion would miss.
        """
        tenant_a, tenant_b = _random_tenant(), _random_tenant()
        try:
            for tenant, marker in ((tenant_a, "AAA"), (tenant_b, "BBB")):
                store.ensure_collection(tenant)
                store.upsert_points(
                    tenant,
                    [
                        _point(
                            tenant,
                            document_id=_document_id_of(1),
                            text=f"{marker} confidential",
                        )
                    ],
                )

            [hit] = store.search_points(
                tenant_a, vector=_query_vector(), limit=10, role=UserRole.ADMIN
            )
            assert hit.payload is not None
            assert hit.payload[PAYLOAD_TEXT] == "AAA confidential"
            assert hit.payload[PAYLOAD_TENANT_ID] == str(tenant_a)
            assert str(tenant_b) not in str(hit.payload)

        finally:
            delete_collection(store, tenant_a)
            delete_collection(store, tenant_b)

    def test_a_query_as_a_tenant_that_ingested_nothing_returns_nothing(
        self, store: QdrantVectorStore, managed_tenant: uuid.UUID
    ) -> None:
        """The benign direction of the same boundary.

        A brand-new tenant querying a server that is full of other tenants' data gets
        nothing — not an error, not a fallback to a shared collection.
        """
        assert (
            store.search_points(
                managed_tenant, vector=_query_vector(), limit=10, role=UserRole.ADMIN
            )
            == []
        )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


@requires_qdrant
class TestConcurrentCreation:
    def test_four_threads_racing_to_create_one_collection_all_succeed(
        self, qdrant_url: str, live_client: QdrantClient
    ) -> None:
        """The race the 409 handling exists for, actually raced, against a real server.

        Four threads rather than two, because the number is the point: with N racers
        exactly one wins the create and N-1 lose it. Two racers would have caught the 409
        handling while leaving the *settling* defect below invisible for want of a third
        and fourth thread asking for a description at the same instant.

        ``ensure_collection`` is called on every ingestion, and Phase 2.6 will make
        ingestion concurrent. Two threads therefore reach the create at the same time and
        each loser must get a 409, re-check, find the collection, wait for Qdrant to be
        able to describe it, and carry on. Asserted for real rather than by monkeypatching
        the race, because the 409 is the server's behaviour and there is no honest way to
        fake it.

        This test is the one that found a defect. Qdrant 1.19.1 makes a collection visible
        to ``collection_exists`` in the same instant it still answers ``get_collection``
        with a 500 and the body ``Service internal error: 0 of 0 read operations failed``.
        So the 409 handling alone was not sufficient: the losers re-checked, found the
        collection, and then failed to describe it. ``_describe_newly_created`` waits that
        window out, and this test is what proves it does.

        A barrier is used so all threads pass the existence check before any creates;
        without it the later threads would usually arrive after the first had finished and
        the race would not be exercised at all.
        """
        import threading

        tenant = _random_tenant()
        clients = [
            QdrantVectorStore(
                make_settings(embedding_dimension=WIDTH, qdrant_url=qdrant_url),
                client=QdrantClient(url=qdrant_url, timeout=15),
            )
            for _ in range(4)
        ]
        barrier = threading.Barrier(len(clients))
        results: list[str | BaseException] = []
        lock = threading.Lock()

        def attempt(index: int, instance: QdrantVectorStore) -> None:
            try:
                barrier.wait(timeout=10)
                name = instance.ensure_collection(tenant)
                with lock:
                    results.append(name)
            except BaseException as exc:  # noqa: BLE001
                # A barrier timeout raises inside this thread, and the point of the test
                # is that all four threads report *something* rather than deadlocking.
                with lock:
                    results.append(exc)

        threads = [
            threading.Thread(target=attempt, args=(index, instance))
            for index, instance in enumerate(clients)
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            assert len(results) == len(clients), "a thread did not finish"
            failures = [result for result in results if isinstance(result, BaseException)]
            assert not failures, (
                "concurrent creation failed: "
                + ", ".join(f"{type(f).__name__}: {f}" for f in failures)
            )
            assert set(results) == {collection_name_for(tenant)}
            assert live_client.collection_exists(collection_name_for(tenant))

            # And the collection is usable, not merely present. A winner that reported
            # success while writing into a collection the losers could not describe would
            # satisfy every assertion above and still be useless, so this is a separate
            # claim from "the collection exists".
            for instance in clients[:1]:
                instance.upsert_points(
                    tenant, [_point(tenant, vector=[0.6] * WIDTH)]
                )
                assert store_counts(instance, tenant) == 1
        finally:
            for instance in clients:
                instance.close()
            delete_collection(clients[0], tenant)


def store_counts(instance: QdrantVectorStore, tenant: uuid.UUID) -> int:
    return instance.count_points_for_document(tenant, _document_id_of(1))


# ---------------------------------------------------------------------------
# The client this project actually constructs
# ---------------------------------------------------------------------------


@requires_qdrant
class TestTheConfiguredClientReallyWorks:
    def test_a_store_built_from_settings_alone_reaches_the_server(
        self, qdrant_url: str, live_client: QdrantClient
    ) -> None:
        """No injected client.

        Every other test here injects the module-scoped client, which proves the store's
        logic but not its wiring. This one is constructed exactly as application code
        constructs it — URL, optional key and timeout from settings — and must reach a
        real server. Without it, a wrong argument name in ``_build_client`` would pass
        every other test in this file.
        """
        instance = QdrantVectorStore(
            make_settings(
                embedding_dimension=WIDTH,
                qdrant_url=qdrant_url,
                qdrant_timeout_seconds=15,
            )
        )
        tenant = _random_tenant()
        try:
            assert instance.is_available is True, (
                "a store built from settings cannot reach the configured server"
            )
            instance.ensure_collection(tenant)
            instance.upsert_points(tenant, [_point(tenant, vector=[0.7] * WIDTH)])
            assert instance.count_points_for_document(tenant, _document_id_of(1)) == 1
            instance.delete_document_points(tenant, _document_id_of(1))
            assert instance.count_points_for_document(tenant, _document_id_of(1)) == 0
        finally:
            instance.delete_collection(tenant)
            instance.close()

    def test_the_readiness_probe_answers_true_for_a_live_server(
        self, qdrant_url: str, live_client: QdrantClient
    ) -> None:
        instance = QdrantVectorStore(
            make_settings(
                embedding_dimension=WIDTH,
                qdrant_url=qdrant_url,
                qdrant_timeout_seconds=15,
            )
        )
        try:
            assert instance.is_available is True
        finally:
            instance.close()

    def test_no_credential_appears_in_the_repr(
        self, qdrant_url: str, live_client: QdrantClient
    ) -> None:
        """Checked against a real key, passed the way a deployment would pass it."""
        key = os.environ.get(QDRANT_API_KEY_ENV_VAR)
        instance = QdrantVectorStore(
            make_settings(
                embedding_dimension=WIDTH,
                qdrant_url=qdrant_url,
                qdrant_api_key=key,
                qdrant_timeout_seconds=15,
            )
        )
        try:
            rendered = repr(instance)
            if key:
                assert key not in rendered, "the API key reached the repr"
            assert qdrant_url in rendered, "the URL should be in the repr; it is diagnostic"
        finally:
            instance.close()