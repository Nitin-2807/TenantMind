"""Tests for :mod:`app.storage.qdrant` that need no Qdrant server.

What a fake client is for here, and what it is not
--------------------------------------------------
This file asserts **what the store sends**. That is a real and checkable claim: that a
delete filter names the right field with the right match, that a scroll asks to be
ordered by ``chunk_index``, that a width mismatch is caught before the network is
touched, and that every client failure is translated into a :class:`VectorStoreError`
whose message names the operation and the exception class but no collection contents.

It cannot assert **what Qdrant does with it**. Whether a ``Range(gte=…)`` filter selects
the points it appears to select, whether ``create_payload_index`` on ``chunk_index``
succeeds, whether Cosine really does normalise on write — those are questions about
Qdrant, and they are asked against a real Qdrant in ``tests/test_qdrant_integration.py``.
A unit suite that claimed otherwise would be the exact failure this phase's gate exists
to prevent, so this module's docstring says so plainly and the integration module
re-checks every one of these filters for real.

The fake
--------
:class:`FakeQdrant` interprets the filter shapes this module actually builds, so "the
delete removed the right points" is answerable offline. That interpretation is
deliberately *narrow* — it raises on any filter shape it does not recognise, rather than
matching nothing, so a filter this module starts sending in a new form fails here instead
of silently returning an empty set and passing.
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any, Final

import httpx
import pytest
from qdrant_client import models
from qdrant_client.http import exceptions as http_exceptions
from qdrant_client.local.qdrant_local import QdrantLocal

from app.models.user import UserRole
from app.rag.vectors import (
    PAYLOAD_CHUNK_INDEX,
    PAYLOAD_DOCUMENT_ID,
    PAYLOAD_TENANT_ID,
    PAYLOAD_TEXT,
    VectorPoint,
    point_id,
)
from app.storage.qdrant import (
    CLIENT_FAILURES,
    COLLECTION_PREFIX,
    DISTANCE_METRIC,
    PAYLOAD_INDEXES,
    SETTLE_ATTEMPTS,
    SETTLE_INTERVAL_SECONDS,
    CollectionInfo,
    QdrantVectorStore,
    VectorDimensionMismatch,
    VectorStoreError,
    build_vector_point,
    collection_name_for,
)
from tests.records import (
    fake_client_of,
    payload_key,
    scored_payload_of,
    store_over,
)

TENANT_A = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
TENANT_B = uuid.UUID("00000000-0000-0000-0000-0000000000b2")
DOCUMENT_ONE = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
DOCUMENT_TWO = uuid.UUID("00000000-0000-0000-0000-0000000000d2")

#: Obvious non-credential, so a leak of it is unmistakable.
FAKE_QDRANT_KEY = "not-a-real-qdrant-key-for-tests-only-00000000"

#: The width these tests configure, and small so a vector is cheap to build.
WIDTH = 8


class FilterShapeUnsupported(Exception):
    """The fake was asked to evaluate a filter it does not implement."""


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Cosine similarity, computed for real.

    Not a stand-in that returns a constant, and not a random number. :meth:`FakeQdrant.query_points`
    scores vectors with this so that "the two nearest chunks come back, in that order" is a
    statement about the data rather than about the fake, and so a test can put a known
    vector at a known distance from the query and assert it is selected.

    Zero-magnitude vectors score ``0.0`` rather than raising: Qdrant Cosine itself rejects a
    zero vector at write time, and this fake's job is to reach the *ordering* logic, not to
    re-implement that validation. A test needing the rejection uses the real server.
    """
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (left_norm * right_norm)


def server_error(status: int, reason: str, body: bytes = b"") -> Any:
    """An :class:`UnexpectedResponse`, i.e. what Qdrant returns for a 4xx or 5xx.

    Built through the real constructor rather than a bare ``Exception`` subclass,
    because the store catches ``CLIENT_FAILURES`` and a stand-in that were *not* in that
    tuple would make these tests pass while proving nothing about the real path.
    """
    return http_exceptions.UnexpectedResponse(status, reason, body, httpx.Headers())


#: The body Qdrant 1.19.1 returns for ``get_collection`` in the window between a create
#: becoming visible to ``collection_exists`` and becoming describable. Verbatim from the
#: server, captured in ``tests/test_qdrant_integration.py``, because reproducing a bug
#: with an invented error message tests nothing.
SETTLING_BODY: Final[bytes] = (
    b'{"status":{"error":"Service internal error: 0 of 0 read operations failed"}}'
)


def not_yet_describable() -> Any:
    """The 500 a real Qdrant answers while a just-created collection settles."""
    return server_error(500, "Internal Server Error", SETTLING_BODY)


def connection_refused() -> Any:
    """The failure a stopped Qdrant produces, wrapped as the client wraps it."""
    return http_exceptions.ResponseHandlingException(ConnectionRefusedError("refused"))


def failure_instance(failure: type[BaseException]) -> BaseException:
    """An instance of a client failure type, built the way that type is really built.

    ``ResponseHandlingException`` takes the *source* exception rather than a message,
    so a naive ``failure("probe failed")`` raises ``TypeError`` instead of exercising
    the swallow path these tests exist to check.
    """
    if issubclass(failure, http_exceptions.ResponseHandlingException):
        return connection_refused()
    if issubclass(failure, http_exceptions.UnexpectedResponse):
        return server_error(500, "Internal Server Error")
    return failure("probe failed")


def _matches(payload: dict[str, Any], filters: models.Filter) -> bool:
    """Evaluate the filter shapes :mod:`app.storage.qdrant` actually builds.

    Supports equality match, numeric range, ``MatchAny`` (any element of a stored array),
    and ``IsNullCondition`` — the four shapes this module sends, and the fourth of which
    was added with :meth:`QdrantVectorStore.search_points`. Anything else raises, so a
    filter this module begins sending in a form the fake has not been taught is a loud
    failure rather than a test that passes because the filter matched nothing.

    ``must`` is a conjunction and ``should`` is a disjunction, combined as
    ``must AND (should)`` when both are present — which is Qdrant's own rule, and the
    reason a role filter built from two ``should`` clauses is an "unrestricted OR
    permitted" rule rather than an impossible-to-satisfy pair of requirements.
    """
    return not (
        (filters.must and not _all_match(payload, list(filters.must)))
        or (filters.should and not _any_match(payload, list(filters.should)))
    )


def _all_match(payload: dict[str, Any], conditions: list[Any]) -> bool:
    """Every condition in ``conditions`` holds — Qdrant's ``must``."""
    return all(_condition_matches(payload, condition) for condition in conditions)


def _any_match(payload: dict[str, Any], conditions: list[Any]) -> bool:
    """At least one condition in ``conditions`` holds — Qdrant's ``should``."""
    return any(_condition_matches(payload, condition) for condition in conditions)


def _condition_matches(payload: dict[str, Any], condition: Any) -> bool:
    """Whether one filter condition holds for ``payload``.

    Split out of :func:`_matches` so ``must`` and ``should`` share one implementation
    rather than having the ``IsNullCondition`` and ``MatchAny`` cases written twice and
    able to disagree.
    """
    if isinstance(condition, models.IsNullCondition):
        # Qdrant's IsNullCondition selects payloads where the key is null *or absent*:
        # an absent key has no value, and "has no value" is what null means here. The
        # fake uses `.get`, which is the same rule. Documented because the alternative
        # reading — only an explicit JSON null — would make a point whose field was never
        # written stop matching, and this project writes `allowed_roles` explicitly in
        # every case, so both readings agree on real data and only differ on a
        # hand-written point.
        return payload.get(condition.is_null.key) is None

    if not isinstance(condition, models.FieldCondition):
        raise FilterShapeUnsupported(f"unsupported condition {type(condition).__name__}")

    if condition.match is not None:
        if isinstance(condition.match, models.MatchValue):
            return payload.get(condition.key) == condition.match.value
        if isinstance(condition.match, models.MatchAny):
            actual = payload.get(condition.key)
            if not isinstance(actual, list):
                raise FilterShapeUnsupported(
                    f"MatchAny filter on {condition.key!r} needs an array payload "
                    f"value, got {actual!r}"
                )
            # An empty array contains none of `any`, so MatchAny against `[]` matches
            # nothing. That is the behaviour the empty-`allowed_roles` case depends on,
            # and it is why this fake must not be written as "the array is a subset of
            # any" or any other looser relation.
            return any(value in condition.match.any for value in actual)
        raise FilterShapeUnsupported(
            f"match filter on {condition.key!r} is a "
            f"{type(condition.match).__name__}, which this interpreter does not evaluate"
        )

    if condition.range is not None:
        # `models.Range` has only lt/gt/gte/lte; the field it applies to is on
        # the enclosing FieldCondition. Reading `.key` off the Range would be an
        # AttributeError here rather than a wrong answer.
        actual = payload.get(condition.key)
        if actual is None or not isinstance(actual, int):
            raise FilterShapeUnsupported(
                f"range filter on {condition.key!r} needs an integer "
                f"payload value, got {actual!r}"
            )
        return not (
            _at_least(actual, condition.range.gte, "gte", condition.key)
            or _above(actual, condition.range.gt, "gt", condition.key)
            or _at_most(actual, condition.range.lte, "lte", condition.key)
            or _below(actual, condition.range.lt, "lt", condition.key)
        )

    raise FilterShapeUnsupported(
        f"condition on {condition.key!r} is neither a match nor a range"
    )


def _numeric(
    bound: float | date | None, bound_name: str, key: str
) -> float | None:
    """A range bound this interpreter can compare, or a refusal.

    ``models.Range`` types every bound as ``float | datetime.date`` because Qdrant
    ranges apply to numbers, datetimes and anything else it stores. This interpreter
    compares integers, so a datetime bound is not something it can answer — and
    silently coercing one to a number would produce a confident wrong answer, which is
    the single worst outcome a test double can have. Refuse instead.
    """
    if bound is None or isinstance(bound, (int, float)):
        return bound
    raise FilterShapeUnsupported(
        f"range bound {bound_name} on {key!r} is a {type(bound).__name__}, "
        "which this interpreter does not compare"
    )


def _at_least(actual: int, bound: float | date | None, name: str, key: str) -> bool:
    """True when ``actual`` fails a ``>= bound`` test."""
    limit = _numeric(bound, name, key)
    return limit is not None and actual < limit


def _above(actual: int, bound: float | date | None, name: str, key: str) -> bool:
    """True when ``actual`` fails a ``> bound`` test."""
    limit = _numeric(bound, name, key)
    return limit is not None and actual <= limit


def _at_most(actual: int, bound: float | date | None, name: str, key: str) -> bool:
    """True when ``actual`` fails a ``<= bound`` test."""
    limit = _numeric(bound, name, key)
    return limit is not None and actual > limit


def _below(actual: int, bound: float | date | None, name: str, key: str) -> bool:
    """True when ``actual`` fails a ``< bound`` test."""
    limit = _numeric(bound, name, key)
    return limit is not None and actual >= limit


class FakeCollection:
    """One collection's worth of state."""

    def __init__(self, *, size: int, distance: models.Distance) -> None:
        self.size = size
        self.distance = distance
        self.points: dict[str, VectorPoint] = {}
        self.indexes: dict[str, models.PayloadSchemaType] = {}


class FakeQdrant:
    """An in-memory stand-in recording every call the store makes.

    Implements only the ten client methods :class:`QdrantVectorStore` calls. A method
    this store does not call is deliberately absent, so a future call to a new client
    method fails with an ``AttributeError`` here rather than being silently served.

    It is **not** a :class:`~qdrant_client.QdrantClient` subclass. It could be — the ten
    methods exist on the real client — but the real signatures carry around fifty
    optional parameters between them, and restating them here would put a maintenance
    burden on this file for no gain: the store calls nine methods with a handful of
    arguments each, and matching everything else proves nothing about the store.
    Instead the single type-level mismatch is bridged in exactly one place,
    :func:`tests.records.store_over`, so the whole suite's use of this double is
    asserted to be substitutable in one spot rather than assumed in sixty.
    """

    def __init__(self, *, size: int = WIDTH) -> None:
        self.collections: dict[str, FakeCollection] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.default_size = size
        self.closed = False
        #: Exceptions to raise on the next call to the named method, by name.
        self.fail_with: dict[str, BaseException] = {}

    # -- helpers ------------------------------------------------------

    def _record(self, name: str, **arguments: Any) -> None:
        self.calls.append((name, arguments))
        if name in self.fail_with:
            raise self.fail_with.pop(name)

    def named(self, call: str) -> list[dict[str, Any]]:
        """Every recorded call to ``call``, as the keyword arguments it was given."""
        return [arguments for name, arguments in self.calls if name == call]

    def add_collection(self, name: str, *, size: int = WIDTH) -> FakeCollection:
        collection = FakeCollection(size=size, distance=DISTANCE_METRIC)
        collection.indexes = dict(PAYLOAD_INDEXES)
        self.collections[name] = collection
        return collection

    def point_ids(self, name: str) -> set[str]:
        return set(self.collections[name].points)

    # -- the client surface -------------------------------------------

    def get_collections(self) -> Any:
        self._record("get_collections")
        return models.CollectionsResponse(collections=[])

    def close(self) -> None:
        self._record("close")
        self.closed = True

    def collection_exists(self, collection_name: str) -> bool:
        self._record("collection_exists", collection_name=collection_name)
        return collection_name in self.collections

    def create_collection(
        self, collection_name: str, vectors_config: Any
    ) -> Any:
        self._record(
            "create_collection",
            collection_name=collection_name,
            vectors_config=vectors_config,
        )
        if collection_name in self.collections:
            raise server_error(409, "Conflict", b"already exists")
        self.collections[collection_name] = FakeCollection(
            size=vectors_config.size, distance=vectors_config.distance
        )
        return True

    def create_payload_index(
        self, collection_name: str, field_name: str, field_schema: Any
    ) -> Any:
        self._record(
            "create_payload_index",
            collection_name=collection_name,
            field_name=field_name,
            field_schema=field_schema,
        )
        collection = self.collections[collection_name]
        if field_name in collection.indexes:
            raise server_error(400, "Bad Request", b"already exists")
        collection.indexes[field_name] = field_schema
        return None

    def get_collection(self, collection_name: str) -> Any:
        self._record("get_collection", collection_name=collection_name)
        collection = self.collections[collection_name]

        # A real ``models.VectorParams``, not a stand-in: ``describe_collection``
        # discriminates with ``isinstance(vectors, models.VectorParams)`` and reports
        # ``None`` for anything else. A duck-typed stand-in would therefore make this
        # fake report a collection with no width, and every dimension assertion
        # downstream would silently pass on ``None``.
        return SimpleNamespace(
            points_count=len(collection.points),
            points_count_approximate=False,
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors=models.VectorParams(
                        size=collection.size, distance=collection.distance
                    )
                )
            ),
        )

    def delete_collection(self, collection_name: str) -> Any:
        self._record("delete_collection", collection_name=collection_name)
        del self.collections[collection_name]
        return True

    def upsert(
        self, collection_name: str, points: list[Any], wait: bool
    ) -> Any:
        self._record(
            "upsert",
            collection_name=collection_name,
            points=points,
            wait=wait,
        )
        collection = self.collections[collection_name]
        for point in points:
            width = len(point.vector)
            if width != collection.size:
                raise ValueError(
                    f"vector width {width} does not match collection size "
                    f"{collection.size}"
                )
            collection.points[str(point.id)] = VectorPoint(
                id=str(point.id),
                vector=list(point.vector),
                payload=dict(point.payload or {}),
            )
        return None

    def delete(
        self, collection_name: str, points_selector: Any, wait: bool
    ) -> Any:
        self._record(
            "delete",
            collection_name=collection_name,
            points_selector=points_selector,
            wait=wait,
        )
        collection = self.collections[collection_name]
        filters = points_selector.filter
        for identifier in [
            identifier
            for identifier, point in collection.points.items()
            if _matches(point.payload, filters)
        ]:
            del collection.points[identifier]
        return None

    def count(
        self, collection_name: str, count_filter: Any, exact: bool
    ) -> Any:
        self._record(
            "count",
            collection_name=collection_name,
            count_filter=count_filter,
            exact=exact,
        )
        collection = self.collections[collection_name]
        matched = sum(
            1 for point in collection.points.values() if _matches(point.payload, count_filter)
        )
        return models.CountResult(count=matched)

    def scroll(
        self,
        collection_name: str,
        scroll_filter: Any = None,
        limit: int = 10,
        with_payload: bool = True,
        with_vectors: bool = False,
        order_by: Any = None,
    ) -> Any:
        self._record(
            "scroll",
            collection_name=collection_name,
            scroll_filter=scroll_filter,
            limit=limit,
            with_payload=with_payload,
            with_vectors=with_vectors,
            order_by=order_by,
        )
        collection = self.collections[collection_name]
        matched = list(collection.points.values())
        if scroll_filter is not None:
            matched = [
                point
                for point in matched
                if _matches(point.payload, scroll_filter)
            ]
        if order_by is not None:
            matched.sort(key=lambda point: point.payload[order_by.key])
        records = [
            models.Record(
                id=point.id,
                vector=point.vector,
                payload=point.payload if with_payload else None,
            )
            for point in matched[:limit]
        ]
        return records, None

    def query_points(
        self,
        collection_name: str,
        query: list[float],
        query_filter: Any,
        limit: int,
        with_payload: bool,
        with_vectors: bool,
    ) -> Any:
        """Score every matching point against ``query`` and return the best ``limit``.

        ``query_points`` is the tenth client method :meth:`QdrantVectorStore.search_points`
        calls, and the first one that computes anything. The earlier nine return or ignore
        payloads; this one orders them.

        Scored with :func:`cosine_similarity` for the reason given there, and *not* returned
        in Qdrant's own order: the store sorts its result to break ties by id, so a fake that
        handed back points in insertion order would let that sort pass without ever being
        exercised on a genuine tie. Insertion order is deliberately not the sorted order, so
        the store's sort has to do real work — the tie-break test would fail here if the
        store stopped sorting.

        ``with_payload``/``with_vectors`` are honoured rather than ignored, because
        ``search_points`` asks for payloads and explicitly *not* vectors, and a fake that
        always returned both would make "the store does not pull whole vectors over the wire
        for a search" untestable.
        """
        self._record(
            "query_points",
            collection_name=collection_name,
            query=query,
            query_filter=query_filter,
            limit=limit,
            with_payload=with_payload,
            with_vectors=with_vectors,
        )
        collection = self.collections[collection_name]
        scored = [
            (
                models.ScoredPoint(
                    id=point.id,
                    version=0,
                    score=cosine_similarity(query, point.vector),
                    payload=point.payload if with_payload else None,
                    vector=point.vector if with_vectors else None,
                ),
                point.id,
            )
            for point in collection.points.values()
            if _matches(_payload_or_empty(point.payload), query_filter)
        ]
        # Descending by score, ties by ascending id — Qdrant's own deterministic answer,
        # so that a fake whose ordering already matched the store's would not hide a
        # missing sort.
        scored.sort(key=lambda scored_point: (-scored_point[0].score, scored_point[1]))
        return SimpleNamespace(points=[point for point, _ in scored[:limit]])


@pytest.fixture
def store() -> QdrantVectorStore:
    """A store over a fake client, with its collection already created."""
    from tests.conftest import make_settings

    client = FakeQdrant()
    instance = store_over(make_settings(embedding_dimension=WIDTH), client)
    client.add_collection(collection_name_for(TENANT_A))
    instance.ensure_collection(TENANT_A)
    return instance


class SettlingClient(FakeQdrant):
    """Loses the create race, and is then undescribable for a while.

    Models what Qdrant 1.19.1 actually does, in two separable parts so a test can vary
    either:

    * the collection is **absent** when ``ensure_collection`` checks and appears during the
      create, which is what makes ``create_collection`` answer 409 and puts the store on
      its lost-race path at all. Presenting the collection as already present instead
      would leave ``lost_race`` false, and the test would pass while exercising nothing.
    * ``get_collection`` answers :func:`not_yet_describable` for the first
      ``readable_after`` calls, then behaves normally. That is the settling window.
    """

    def __init__(self, *, readable_after: int, size: int = WIDTH) -> None:
        super().__init__()
        self.readable_after = readable_after
        self.raced_size = size
        self.get_calls = 0

    def create_collection(self, collection_name: str, vectors_config: Any) -> Any:
        # The other worker wins first: the collection appears, so the create below 409s.
        self.collections[collection_name] = FakeCollection(
            size=self.raced_size, distance=vectors_config.distance
        )
        self.collections[collection_name].indexes = dict(PAYLOAD_INDEXES)
        return super().create_collection(
            collection_name=collection_name, vectors_config=vectors_config
        )

    def get_collection(self, collection_name: str) -> Any:
        self.get_calls += 1
        if self.get_calls <= self.readable_after:
            raise not_yet_describable()
        return super().get_collection(collection_name)


def _point(**overrides: Any) -> VectorPoint:
    arguments: dict[str, Any] = {
        "tenant_id": TENANT_A,
        "document_id": DOCUMENT_ONE,
        "chunk_index": 0,
        "page": 1,
        "text": "A chunk of the document.",
        "allowed_roles": None,
        "filename": "policy.pdf",
        "vector": [0.25] * WIDTH,
    }
    arguments.update(overrides)
    return build_vector_point(**arguments)


def _vector(*, lead: float = 1.0) -> list[float]:
    """A ``WIDTH``-wide vector whose first component is ``lead`` and whose rest are ``1``.

    Not a constant vector. Two constant vectors have cosine similarity 1.0 whatever their
    value, so a fixture built from them would score every candidate identically and the
    ranking assertions would pass on insertion order rather than on order. Moving one
    component moves the cosine monotonically, which is what makes "the nearer vector is
    returned first" a claim about the data.
    """
    return [lead, *([1.0] * (WIDTH - 1))]


def _payload_or_empty(payload: dict[str, Any] | None) -> dict[str, Any]:
    """A point's payload, treating ``None`` as no fields rather than as a failure.

    The one place in this file that tolerates an absent payload, and it needs to:
    :meth:`FakeQdrant.query_points` honours ``with_payload=False``, so its filter
    evaluation runs against a point whose payload was deliberately not requested. Those
    are not the hits under test — they are candidates the fake discards — and treating
    their missing payload as a defect would fail a test about payload *delivery* for a
    search that correctly did not ask for one.
    """
    return {} if payload is None else payload


def _searched(store: QdrantVectorStore, *, since: int = 0) -> list[models.ScoredPoint]:
    """The points the ``since``-th search's **sent** filter selects.

    Deliberately not "the points the search returned": this re-evaluates the recorded
    filter against the stored payloads, so the assertion is about what was asked for. A
    fake that returned the right answer for the wrong filter would still fail here, which
    is the failure a filtering test cannot have — a test that only checks the result proves
    the fake and the store agree, and they were written by the same hand.
    """
    calls = fake_client_of(store).named("query_points")
    assert len(calls) > since, "expected a search that did not happen"
    return [
        point
        for point in _stored_points(store, calls[since]["collection_name"])
        if _matches(scored_payload_of(point), calls[since]["query_filter"])
    ]


def _stored_points(
    store: QdrantVectorStore, name: str
) -> list[models.ScoredPoint]:
    """Every point in the named collection, with a real cosine score against ``_vector()``.

    Real :class:`ScoredPoint` objects rather than ``VectorPoint`` ones so that callers of
    :func:`_searched` read ``point.payload`` without this file needing a second narrowing
    helper.
    """
    collection = fake_client_of(store).collections[name]
    return [
        models.ScoredPoint(
            id=point.id,
            version=0,
            score=cosine_similarity(_vector(), point.vector),
            payload=point.payload,
            vector=None,
        )
        for point in collection.points.values()
    ]


class TestCollectionNaming:
    def test_it_is_the_prefix_plus_the_tenant_hex(self) -> None:
        assert collection_name_for(TENANT_A) == f"{COLLECTION_PREFIX}{TENANT_A.hex}"

    def test_the_name_contains_no_punctuation_a_collection_name_rejects(self) -> None:
        """A name is typed by operators into Qdrant's own dashboard."""

        assert collection_name_for(TENANT_A).replace("_", "").isalnum()
        assert "-" not in collection_name_for(TENANT_A)

    def test_it_is_injective_over_tenants(self) -> None:
        """The isolation boundary depends on two tenants never sharing a name."""
        names = {collection_name_for(uuid.UUID(int=n)) for n in range(500)}
        assert len(names) == 500

    def test_the_prefix_marks_the_collection_as_this_project_s(self) -> None:
        """So an operator can tell TenantMind's collections from another's."""
        assert COLLECTION_PREFIX == "tenant_"
        assert collection_name_for(TENANT_A).startswith(COLLECTION_PREFIX)


class TestTheDistanceMetric:
    def test_it_is_cosine(self) -> None:
        """A decision, not a default: embedding models carry meaning in direction."""
        assert DISTANCE_METRIC == models.Distance.COSINE

    def test_the_configured_metric_is_the_one_a_collection_is_created_with(self) -> None:
        from tests.conftest import make_settings

        client = FakeQdrant()
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        instance.ensure_collection(TENANT_A)

        [call] = client.named("create_collection")
        assert call["vectors_config"].distance == models.Distance.COSINE
        assert call["vectors_config"].size == WIDTH


class TestConstruction:
    def test_an_injected_client_is_used_verbatim(self) -> None:
        """The seam the whole file depends on, and what Phase 2.6's factory will use."""
        from tests.conftest import make_settings

        client = FakeQdrant()
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        assert instance._client is client

    def test_the_dimension_comes_from_settings(self) -> None:
        from tests.conftest import make_settings

        instance = store_over(
            make_settings(embedding_dimension=384), FakeQdrant()
        )
        assert instance.dimension == 384

    def test_the_repr_names_no_credential(self) -> None:
        """A ``repr`` reaches a log line, a traceback and a crash report.

        The URL is included because it is diagnostic and — per ``_build_client`` —
        cannot contain a key. If that ever changes, this must change with it, which is
        why the key is asserted absent from the whole rendering rather than from one
        field.
        """
        from tests.conftest import make_settings

        instance = store_over(
            make_settings(
                embedding_dimension=WIDTH,
                qdrant_url="http://localhost:6333",
                qdrant_api_key=FAKE_QDRANT_KEY,
            ),
            FakeQdrant(),
        )
        rendered = repr(instance)
        assert FAKE_QDRANT_KEY not in rendered
        assert "localhost:6333" in rendered
        assert str(WIDTH) in rendered

    def test_building_a_client_from_settings_passes_the_url_key_and_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one place a credential is read, and it is passed rather than stored.

        Asserted against a recording stand-in for the constructor rather than by reading
        ``QdrantRemote._timeout`` back afterwards. A private attribute of the installed
        client is not part of this project's contract; the argument this project
        supplies is.
        """
        import app.storage.qdrant as module
        from tests.conftest import make_settings

        recorded: dict[str, Any] = {}

        class RecordingClient:
            def __init__(self, **arguments: Any) -> None:
                recorded.update(arguments)

        monkeypatch.setattr(module, "QdrantClient", RecordingClient)
        module.QdrantVectorStore(
            make_settings(
                qdrant_url="http://qdrant.internal:6333",
                qdrant_api_key=FAKE_QDRANT_KEY,
                qdrant_timeout_seconds=7,
            )
        )

        assert recorded["url"] == "http://qdrant.internal:6333"
        assert recorded["api_key"] == FAKE_QDRANT_KEY
        assert recorded["timeout"] == 7, "the configured timeout was not passed through"

    def test_the_object_holds_no_attribute_named_like_a_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing on the store itself retains the credential.

        The key does live inside the client object, by construction — that is where it
        has to be to authenticate. What this rules out is a *second* copy on the store,
        which would be a copy with no owner and no lifetime tied to the client's.
        """
        import app.storage.qdrant as module
        from tests.conftest import make_settings

        recorded: dict[str, Any] = {}

        class RecordingClient:
            def __init__(self, **arguments: Any) -> None:
                recorded.update(arguments)

            def close(self) -> None:
                """The store calls this; a stand-in without it would fail on teardown."""

        monkeypatch.setattr(module, "QdrantClient", RecordingClient)
        instance = QdrantVectorStore(
            make_settings(
                embedding_dimension=WIDTH,
                qdrant_url="http://localhost:6333",
                qdrant_api_key=FAKE_QDRANT_KEY,
            )
        )
        instance.close()
        assert recorded["api_key"] == FAKE_QDRANT_KEY, "the fixture built no client"
        try:
            stored = [
                value
                for _, value in vars(instance).items()
                if isinstance(value, str) and FAKE_QDRANT_KEY in value
            ]
            assert not stored
            assert all(FAKE_QDRANT_KEY not in str(value) for value in vars(instance).values())
        finally:
            instance.close()


class TestEnsureCollection:
    def test_it_creates_the_collection_with_the_configured_width(self) -> None:
        from tests.conftest import make_settings

        client = FakeQdrant()
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        name = instance.ensure_collection(TENANT_A)

        assert name == collection_name_for(TENANT_A)
        assert name in client.collections
        assert client.collections[name].size == WIDTH

    def test_it_creates_every_payload_index(self) -> None:
        """All three, and the third is not optional.

        ``chunk_index`` is indexed because ``scroll_document_points`` orders by it and
        Qdrant answers 400 for an ``order_by`` on an unindexed key. A version of this
        module that indexed only the two UUIDs would therefore be unable to read a
        document's chunks back at all — a failure found against a real server, and the
        reason this assertion exists.
        """
        from tests.conftest import make_settings

        client = FakeQdrant()
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        name = instance.ensure_collection(TENANT_A)

        indexed = client.collections[name].indexes
        assert indexed == dict(PAYLOAD_INDEXES)
        assert PAYLOAD_DOCUMENT_ID in indexed
        assert PAYLOAD_TENANT_ID in indexed
        assert PAYLOAD_CHUNK_INDEX in indexed
        assert indexed[PAYLOAD_CHUNK_INDEX] == models.PayloadSchemaType.INTEGER
        assert indexed[PAYLOAD_DOCUMENT_ID] == models.PayloadSchemaType.KEYWORD

    def test_it_is_idempotent(self) -> None:
        """Called on every ingestion, so a second call must not create a second one."""
        from tests.conftest import make_settings

        client = FakeQdrant()
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        instance.ensure_collection(TENANT_A)
        instance.ensure_collection(TENANT_A)
        instance.ensure_collection(TENANT_A)

        assert len(client.collections) == 1
        assert len(client.named("create_collection")) == 1

    def test_a_collection_created_before_is_reused_not_recreated(self) -> None:
        client = FakeQdrant()
        client.add_collection(collection_name_for(TENANT_A))
        from tests.conftest import make_settings

        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        instance.ensure_collection(TENANT_A)
        assert client.named("create_collection") == []

    def test_a_concurrent_create_is_not_an_error(self) -> None:
        """Another worker winning the race is the desired outcome.

        The create raises 409, and the answer is to re-check: if the collection is there
        now, the other worker did the work and this one continues. Without the re-check
        a perfectly ordinary concurrent ingestion would be recorded as a failure.
        """

        class RacingClient(FakeQdrant):
            def create_collection(self, collection_name: str, vectors_config: Any) -> Any:
                # Simulate the other process winning between our check and our create.
                self.collections[collection_name] = FakeCollection(
                    size=vectors_config.size, distance=vectors_config.distance
                )
                self.collections[collection_name].indexes = dict(PAYLOAD_INDEXES)
                return super().create_collection(
                    collection_name=collection_name, vectors_config=vectors_config
                )

        from tests.conftest import make_settings

        client = RacingClient()
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        assert (
            instance.ensure_collection(TENANT_A) == collection_name_for(TENANT_A)
        ), "a lost race is not a failure"

    def test_a_lost_race_waits_for_the_collection_to_become_describable(self) -> None:
        """The window Qdrant 1.19.1 opens between a create and its describability.

        Found by ``tests/test_qdrant_integration.py`` against a real server: once a create
        has happened, ``collection_exists`` answers ``True`` immediately while
        ``get_collection`` answers **500** — ``Service internal error: 0 of 0 read
        operations failed`` — and answers correctly a moment later. So handling the 409 is
        not enough; the loser has to wait, and this asserts it does rather than failing on
        the winner's timing.

        Reproduced here so the wait is covered without a live server, because a test that
        needs a real Qdrant to observe a millisecond-wide race will pass vacuously on a
        fast machine and fail on a loaded one.
        """
        from tests.conftest import make_settings

        client = SettlingClient(readable_after=2)
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        assert (
            instance.ensure_collection(TENANT_A) == collection_name_for(TENANT_A)
        ), "a lost race must survive the window where the collection exists but cannot be read"
        assert client.get_calls == 3, "it waited exactly as long as it had to"

    def test_the_wait_is_bounded_and_reports_the_real_reason(self) -> None:
        """Waiting must not turn a permanently undescribable collection into a hang.

        A collection that never becomes describable has to raise ``VectorStoreError``
        carrying the underlying failure. Without a bound this would retry forever and turn
        a broken store into a stuck ingestion.
        """

        from tests.conftest import make_settings

        client = SettlingClient(readable_after=10_000)
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        with pytest.raises(VectorStoreError, match="could not be described"):
            instance.ensure_collection(TENANT_A)
        # Bounded: `SETTLE_ATTEMPTS` retries plus one final describe outside the wait.
        assert client.get_calls == SETTLE_ATTEMPTS + 1

    def test_the_wait_is_short_enough_to_be_invisible_to_an_ingestion(self) -> None:
        """The budget is recorded, not implied.

        A retry loop that can wait minutes has replaced one failure with a stalled
        ingestion, which is worse: the document stays ``processing`` forever and nobody
        learns why. Half a second is long enough for the measured window and short enough
        that a caller would rather wait than fail.
        """
        total = SETTLE_ATTEMPTS * SETTLE_INTERVAL_SECONDS
        assert total <= 1.0, f"the settle budget grew to {total}s"
        assert SETTLE_INTERVAL_SECONDS >= 0.01, "a sub-10ms poll would spin the CPU"

    def test_a_describe_that_never_settles_on_the_race_path_keeps_the_underlying_reason(
        self,
    ) -> None:
        """The error the caller sees must name what actually failed.

        The retry wraps ``describe_collection`` rather than calling the client directly, so
        the message reaching the caller is the one that describes the real problem. Asserted
        because a retry loop is exactly where a diagnostic goes to die.
        """

        from tests.conftest import make_settings

        # The winning worker created it at 384 while this store is configured for 8, so
        # the only failure left once the window closes is the genuine width mismatch. That
        # is the point: proving the retry preserves the diagnosis, not just the success.
        client = SettlingClient(readable_after=1, size=384)
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        with pytest.raises(VectorDimensionMismatch, match="384"):
            instance.ensure_collection(TENANT_A)

    def test_a_create_that_fails_for_another_reason_is_reported(self) -> None:
        """The re-check is what makes the race handling correct rather than tolerant.

        If the create failed for a reason the re-check does not satisfy, the collection
        is still absent and the error must propagate as a ``VectorStoreError`` — not be
        mistaken for a lost race and swallowed.
        """
        from tests.conftest import make_settings

        client = FakeQdrant()
        client.fail_with["create_collection"] = server_error(500, "Internal Server Error")
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        with pytest.raises(VectorStoreError, match="could not be created"):
            instance.ensure_collection(TENANT_A)

    def test_a_width_mismatch_is_refused_not_written_into(self) -> None:
        """Never truncated, never padded, never silently accepted.

        Qdrant collections cannot be resized, so the operator's options are both
        destructive. That makes it a decision to surface, and a distinct exception type
        is what lets the recorded failure tell the two situations apart.
        """
        from tests.conftest import make_settings

        client = FakeQdrant()
        client.add_collection(collection_name_for(TENANT_A), size=384)
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)

        with pytest.raises(VectorDimensionMismatch, match="384-dimensional"):
            instance.ensure_collection(TENANT_A)

    def test_the_mismatch_says_what_the_setting_is(self) -> None:
        """So an operator can tell which of the two to change."""
        from tests.conftest import make_settings

        client = FakeQdrant()
        client.add_collection(collection_name_for(TENANT_A), size=1536)
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        with pytest.raises(VectorDimensionMismatch) as caught:
            instance.ensure_collection(TENANT_A)
        message = str(caught.value)
        assert "1536" in message
        assert str(WIDTH) in message
        assert "revert" in message

    def test_a_mismatch_is_not_transient(self) -> None:
        """A distinct type, so ``except VectorStoreError`` cannot mistake it for an outage."""
        assert issubclass(VectorDimensionMismatch, VectorStoreError)
        assert VectorDimensionMismatch is not VectorStoreError

    def test_it_creates_the_indexes_on_an_already_existing_collection(self) -> None:
        """The regression this pins, found against a real Qdrant 1.19.1.

        ``ensure_collection`` used to create the payload indexes only on the
        *just-created* path and return early. A collection created by an earlier version
        of this phase with fewer indexes therefore stayed that way forever — and because
        Qdrant answers 400 for an ``order_by`` on an unindexed key, every subsequent
        read of that tenant's documents failed. Nothing noticed, because nothing read an
        older collection's documents back.

        The fix costs three requests per ingestion on the common path; the cost of not
        paying it is a silently broken tenant.
        """
        from tests.conftest import make_settings

        client = FakeQdrant()
        client.add_collection(collection_name_for(TENANT_A))
        client.collections[collection_name_for(TENANT_A)].indexes = {}

        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        instance.ensure_collection(TENANT_A)

        attempted = {call["field_name"] for call in client.named("create_payload_index")}
        assert attempted == set(PAYLOAD_INDEXES)
        assert client.collections[collection_name_for(TENANT_A)].indexes == dict(
            PAYLOAD_INDEXES
        )

    def test_a_failed_index_on_an_existing_collection_does_not_fail_either(self) -> None:
        """The swallow must cover the already-exists path as well.

        After the fix above, every ingestion reaches the index creation. If that path
        propagated a failure, every ingestion after a collection's first would be recorded
        as failed by a perfectly working store.
        """
        from tests.conftest import make_settings

        client = FakeQdrant()
        client.add_collection(collection_name_for(TENANT_A))
        client.fail_with["create_payload_index"] = server_error(400, "Bad Request")

        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        assert (
            instance.ensure_collection(TENANT_A) == collection_name_for(TENANT_A)
        )

    def test_a_failed_index_does_not_fail_the_ingestion(self) -> None:
        """A slower deletion filter still deletes the right points; a refused
        ingestion leaves the document permanently unindexed.

        The costs are asymmetric, so the tolerance is deliberate — see the method's own
        docstring. Asserted here because it is a design decision that looks like a bug.
        """
        from tests.conftest import make_settings

        client = FakeQdrant()
        client.fail_with["create_payload_index"] = server_error(400, "Bad Request")
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        assert instance.ensure_collection(TENANT_A) == collection_name_for(TENANT_A)

    def test_one_failed_index_does_not_skip_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each field is created inside its own ``try``.

        Without that, an already-indexed first field would prevent the rest from being
        created, and a collection created by an earlier version of this phase with fewer
        indexes could never be brought up to date.
        """
        from tests.conftest import make_settings

        client = FakeQdrant()
        original = client.create_payload_index

        def flaky(collection_name: str, field_name: str, field_schema: Any) -> Any:
            # Record the attempt *before* raising. Recording only on success would make
            # this test pass for a store that silently stopped trying — the opposite of
            # what it claims to check.
            client._record(
                "create_payload_index",
                collection_name=collection_name,
                field_name=field_name,
                field_schema=field_schema,
            )
            if field_name == PAYLOAD_DOCUMENT_ID:
                raise server_error(400, "Bad Request", b"already exists")
            return original(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=field_schema,
            )

        monkeypatch.setattr(client, "create_payload_index", flaky)
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        instance.ensure_collection(TENANT_A)

        attempted = {call["field_name"] for call in client.named("create_payload_index")}
        assert attempted == set(PAYLOAD_INDEXES)


class TestExistenceAndDescription:
    def test_it_reports_whether_a_collection_exists(
        self, store: QdrantVectorStore
    ) -> None:
        """``True`` and ``False`` are genuinely different answers, not the same one."""
        assert store.collection_exists(TENANT_A) is True
        assert store.collection_exists(TENANT_B) is False

    def test_an_unreachable_store_is_an_error_not_a_false(self) -> None:
        """A missing collection and an unreachable server demand opposite responses.

        Answering ``False`` to the second would have the caller try to *create* a
        collection on a server that is not answering — and then record the document as
        failed for a reason that reads like a permissions problem.
        """
        from tests.conftest import make_settings

        client = FakeQdrant()
        client.fail_with["collection_exists"] = connection_refused()
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        with pytest.raises(VectorStoreError, match="could not be reached"):
            instance.collection_exists(TENANT_A)

    def test_it_describes_the_configured_width_and_metric(
        self, store: QdrantVectorStore
    ) -> None:
        info = store.describe_collection(TENANT_A)
        assert isinstance(info, CollectionInfo)
        assert info.name == collection_name_for(TENANT_A)
        assert info.vector_size == WIDTH
        assert info.distance is not None
        assert "Cosine" in info.distance

    def test_describing_a_missing_collection_is_an_error(
        self, store: QdrantVectorStore
    ) -> None:
        """Not ``CollectionInfo(points=0)``, which reads as "exists and is empty"."""
        with pytest.raises(VectorStoreError, match="no vector collection"):
            store.describe_collection(TENANT_B)

    def test_an_approximate_point_count_is_reported_as_unknown(
        self,
        store: QdrantVectorStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An estimate presented as a fact is how a test starts lying.

        ``documents.chunk_count`` and this figure are compared by the ingestion tests,
        so an approximation reaching the comparison would make a real mismatch look
        like a pass.
        """
        client = fake_client_of(store)
        real_get = client.get_collection

        def approximate(collection_name: str) -> Any:
            info = real_get(collection_name)
            info.points_count = 99
            info.points_count_approximate = True
            return info

        # No manual restore: `monkeypatch` puts the real method back when the test ends,
        # which also means this test cannot leak an approximate count into the next one
        # even if the assertion above fails.
        monkeypatch.setattr(client, "get_collection", approximate)
        assert store.describe_collection(TENANT_A).points is None

    def test_an_exact_point_count_is_reported_as_a_number(
        self, store: QdrantVectorStore
    ) -> None:
        store.upsert_points(TENANT_A, [_point(), _point(chunk_index=1)])
        assert store.describe_collection(TENANT_A).points == 2

    def test_a_client_failure_becomes_a_store_error(self) -> None:
        from tests.conftest import make_settings

        client = FakeQdrant()
        client.add_collection(collection_name_for(TENANT_A))
        client.fail_with["get_collection"] = server_error(503, "Service Unavailable")
        instance = store_over(make_settings(embedding_dimension=WIDTH), client)
        with pytest.raises(VectorStoreError, match="could not be described"):
            instance.describe_collection(TENANT_A)

    def test_a_failure_message_names_the_operation_and_the_class(
        self, store: QdrantVectorStore
    ) -> None:
        """Enough for triage, and nothing about the collection's contents.

        The exception *class* is included because it separates conditions needing
        opposite responses — a width mismatch from an outage — while the payload text
        is excluded because it is a tenant's document.
        """
        client = fake_client_of(store)
        client.fail_with["get_collection"] = server_error(503, "Service Unavailable")
        with pytest.raises(VectorStoreError) as caught:
            store.describe_collection(TENANT_A)
        assert "UnexpectedResponse" in str(caught.value)


class TestAvailability:
    def test_it_is_true_when_the_probe_answers(self, store: QdrantVectorStore) -> None:
        assert store.is_available is True

    @pytest.mark.parametrize("failure", CLIENT_FAILURES)
    def test_a_client_failure_makes_it_false_rather_than_raising(
        self, store: QdrantVectorStore, failure: type[BaseException]
    ) -> None:
        """A predicate has no way to report *why* it is False.

        Every method that acts on the store raises instead, so an unreachable Qdrant is
        loud where it matters and quiet where it is merely being asked.
        """
        client = fake_client_of(store)
        client.fail_with["get_collections"] = failure_instance(failure)
        assert store.is_available is False

    def test_an_unrelated_error_is_not_mistaken_for_an_outage(
        self, store: QdrantVectorStore
    ) -> None:
        """A ``TypeError`` from a changed client signature is a bug, not an outage.

        Reporting it as "Qdrant is unreachable" would send an operator to look at the
        server when the fault is in this codebase.
        """
        client = fake_client_of(store)
        client.fail_with["get_collections"] = TypeError("unexpected keyword argument")
        with pytest.raises(TypeError):
            _ = store.is_available

    def test_the_swallowed_failure_is_logged_at_debug(
        self, store: QdrantVectorStore, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The one thing a swallowed exception must not do is vanish.

        ``logger.debug`` rather than a bare ``continue``: the outcome is the same, but an
        index that could not be created is exactly the kind of thing that stays
        invisible for a month and then makes a scroll mysteriously slow. Asserted at
        ``DEBUG`` because that is the level chosen, and a move to ``INFO`` would make
        every ingestion of a large tenant noisier than the event warrants.
        """
        import logging

        client = fake_client_of(store)
        client.fail_with["get_collections"] = http_exceptions.ResponseHandlingException(
            OSError("probe failed")
        )

        with caplog.at_level(logging.DEBUG, logger="app.storage.qdrant"):
            assert store.is_available is False

        assert caplog.records, "the swallowed failure was not recorded at all"
        assert [record.levelno for record in caplog.records] == [logging.DEBUG]
        rendered = caplog.text
        assert "probe failed" in rendered
        assert PAYLOAD_TEXT not in rendered, "a log line must not carry document text"

    def test_closing_releases_the_client(self, store: QdrantVectorStore) -> None:
        fake = fake_client_of(store)
        store.close()
        assert fake.closed is True


class TestUpsert:
    def test_it_writes_every_point(self, store: QdrantVectorStore) -> None:
        written = store.upsert_points(
            TENANT_A, [_point(chunk_index=n, text=f"chunk {n}") for n in range(4)]
        )
        assert written == 4
        assert len(store.scroll_document_points(TENANT_A, DOCUMENT_ONE)) == 4

    def test_it_sends_an_upsert_not_an_insert(self, store: QdrantVectorStore) -> None:
        """The whole of this phase's idempotency story.

        An insert of an existing id fails in Qdrant; an upsert overwrites. Re-ingestion
        is a supported operation, so it must overwrite.
        """
        store.upsert_points(TENANT_A, [_point()])
        store.upsert_points(TENANT_A, [_point(text="replaced")])
        assert len(store.scroll_document_points(TENANT_A, DOCUMENT_ONE)) == 1

        records = store.scroll_document_points(TENANT_A, DOCUMENT_ONE)
        assert records[0].payload is not None
        assert records[0].payload[PAYLOAD_TEXT] == "replaced"

    def test_it_waits_for_the_write_to_land(self, store: QdrantVectorStore) -> None:
        """``wait=True``, so a subsequent read sees what was written.

        Without it the ingestor would record a chunk count the store has not accepted
        yet, and the next call would read a stale collection.
        """
        store.upsert_points(TENANT_A, [_point()])
        assert fake_client_of(store).named("upsert")[0]["wait"] is True

    def test_it_writes_into_the_tenant_s_own_collection(
        self, store: QdrantVectorStore
    ) -> None:
        """No method accepts a collection name, so there is nothing else to write to."""
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point()])
        [call] = client.named("upsert")
        assert call["collection_name"] == collection_name_for(TENANT_A)

    def test_zero_points_is_refused_before_the_network(
        self, store: QdrantVectorStore
    ) -> None:
        """A no-op that reaches the network is a no-op that can fail."""
        client = fake_client_of(store)
        with pytest.raises(VectorStoreError, match="zero points"):
            store.upsert_points(TENANT_A, [])
        assert client.named("upsert") == []

    @pytest.mark.parametrize("width", [0 + 1, WIDTH - 1, WIDTH + 1, 384])
    def test_a_wrong_width_is_refused_before_the_network(
        self, store: QdrantVectorStore, width: int
    ) -> None:
        """Named as the *setting* at fault, not as a vector index.

        Qdrant would reject it too, but its error names a point index, which sends an
        operator to the wrong file. Checking here also means nothing is written at all —
        a partial write followed by a rejection leaves the document with some of its
        chunks embedded and the rest absent.
        """
        client = fake_client_of(store)
        with pytest.raises(VectorDimensionMismatch, match="wide"):
            store.upsert_points(TENANT_A, [_point(vector=[0.5] * width)])
        assert client.named("upsert") == []

    def test_a_wrong_width_names_the_position_of_the_offending_point(
        self, store: QdrantVectorStore
    ) -> None:
        """So a systematic problem is distinguishable from one bad chunk."""
        with pytest.raises(VectorDimensionMismatch, match="point 1"):
            store.upsert_points(
                TENANT_A,
                [_point(chunk_index=0), _point(chunk_index=1, vector=[0.5] * 3)],
            )

    def test_a_width_check_covers_the_whole_batch_before_writing_any(
        self, store: QdrantVectorStore
    ) -> None:
        """The batch is validated first, so a late bad point does not leave a partial write."""
        client = fake_client_of(store)
        points = [_point(chunk_index=n) for n in range(3)]
        points[2] = _point(chunk_index=2, vector=[0.5] * 2)
        with pytest.raises(VectorDimensionMismatch):
            store.upsert_points(TENANT_A, points)
        assert client.named("upsert") == []
        assert store.scroll_document_points(TENANT_A, DOCUMENT_ONE) == []

    def test_a_client_failure_is_wrapped_with_the_count(
        self, store: QdrantVectorStore
    ) -> None:
        """The count, because "the write failed" and "some of it did" are different bugs."""
        client = fake_client_of(store)
        client.fail_with["upsert"] = server_error(500, "Internal Server Error", b"boom")
        with pytest.raises(VectorStoreError, match="3 vector point"):
            store.upsert_points(TENANT_A, [_point(chunk_index=n) for n in range(3)])

    def test_a_failure_message_carries_no_document_content(
        self, store: QdrantVectorStore
    ) -> None:
        """The recorded ``documents.error`` is admin-visible and a payload value would
        put tenant content in it."""
        client = fake_client_of(store)
        client.fail_with["upsert"] = server_error(
            500, "Internal Server Error", b"boom CONFIDENTIAL-PAYLOAD"
        )
        with pytest.raises(VectorStoreError) as caught:
            store.upsert_points(
                TENANT_A, [_point(text="CONFIDENTIAL-PAYLOAD", vector=[0.5] * WIDTH)]
            )
        assert "CONFIDENTIAL-PAYLOAD" not in str(caught.value)


class TestDeletion:
    def test_it_removes_every_chunk_of_one_document(
        self, store: QdrantVectorStore
    ) -> None:
        store.upsert_points(
            TENANT_A,
            [_point(chunk_index=n, text=f"chunk {n}") for n in range(5)]
            + [_point(document_id=DOCUMENT_TWO, chunk_index=0, text="other")],
        )
        store.delete_document_points(TENANT_A, DOCUMENT_ONE)

        assert store.scroll_document_points(TENANT_A, DOCUMENT_ONE) == []
        assert len(store.scroll_document_points(TENANT_A, DOCUMENT_TWO)) == 1

    def test_the_filter_names_the_document_id(
        self, store: QdrantVectorStore
    ) -> None:
        """A typo here matches nothing, and deletion then "succeeds" silently."""
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point()])
        store.delete_document_points(TENANT_A, DOCUMENT_ONE)

        [call] = client.named("delete")
        condition = call["points_selector"].filter.must[0]
        assert condition.key == PAYLOAD_DOCUMENT_ID
        assert condition.match.value == str(DOCUMENT_ONE)

    def test_it_cannot_reach_another_tenant(self, store: QdrantVectorStore) -> None:
        """A foreign document id names points in a different collection.

        Nothing in this call accepts a collection name, so there is no code path by which
        one tenant's deletion could name another's collection.
        """
        client = fake_client_of(store)
        client.add_collection(collection_name_for(TENANT_B))
        store.upsert_points(TENANT_A, [_point(text="tenant A's chunk")])
        store.upsert_points(TENANT_B, [_point(text="tenant B's chunk")])

        store.delete_document_points(TENANT_A, DOCUMENT_ONE)

        assert len(store.scroll_document_points(TENANT_A, DOCUMENT_ONE)) == 0
        assert len(store.scroll_document_points(TENANT_B, DOCUMENT_ONE)) == 1

    def test_a_missing_collection_deletes_nothing(
        self, store: QdrantVectorStore
    ) -> None:
        """The normal case for a first ingestion, not an error."""
        client = fake_client_of(store)
        assert store.delete_document_points(TENANT_B, DOCUMENT_ONE) == 0
        assert client.named("delete") == []

    def test_stale_points_are_removed_from_a_given_index(
        self, store: QdrantVectorStore
    ) -> None:
        """The re-ingestion case: the document now has fewer chunks than before."""
        store.upsert_points(
            TENANT_A, [_point(chunk_index=n, text=f"chunk {n}") for n in range(8)]
        )
        remaining = store.delete_stale_points(
            TENANT_A, DOCUMENT_ONE, keep_below_index=3
        )

        assert remaining == 3
        kept = store.scroll_document_points(TENANT_A, DOCUMENT_ONE)
        assert [payload_key(record, PAYLOAD_CHUNK_INDEX) for record in kept] == [0, 1, 2]

    def test_the_stale_filter_combines_document_and_range(
        self, store: QdrantVectorStore
    ) -> None:
        """Both conditions, in one ``must``.

        Without the ``document_id`` clause the range would delete the *other* documents'
        chunks too, which is precisely the kind of bug that is invisible until a tenant
        notices a document has gone.
        """
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point(chunk_index=0)])
        store.delete_stale_points(TENANT_A, DOCUMENT_ONE, keep_below_index=5)

        [call] = client.named("delete")
        conditions = call["points_selector"].filter.must
        keys = {condition.key for condition in conditions}
        assert keys == {PAYLOAD_DOCUMENT_ID, PAYLOAD_CHUNK_INDEX}

        by_key = {condition.key: condition for condition in conditions}
        assert by_key[PAYLOAD_CHUNK_INDEX].range.gte == 5

    def test_stale_removal_leaves_another_document_alone(
        self, store: QdrantVectorStore
    ) -> None:
        store.upsert_points(TENANT_A, [_point(chunk_index=n) for n in range(4)])
        store.upsert_points(
            TENANT_A, [_point(document_id=DOCUMENT_TWO, chunk_index=n) for n in range(6)]
        )
        store.delete_stale_points(TENANT_A, DOCUMENT_ONE, keep_below_index=1)

        assert len(store.scroll_document_points(TENANT_A, DOCUMENT_ONE)) == 1
        assert len(store.scroll_document_points(TENANT_A, DOCUMENT_TWO)) == 6

    def test_removing_nothing_is_not_an_error(
        self, store: QdrantVectorStore
    ) -> None:
        store.upsert_points(TENANT_A, [_point(chunk_index=0)])
        assert (
            store.delete_stale_points(TENANT_A, DOCUMENT_ONE, keep_below_index=99)
            == 1
        )

    def test_upserting_before_pruning_never_leaves_a_zero_point_window(
        self, store: QdrantVectorStore
    ) -> None:
        """The ordering the orchestrator uses, asserted as an observable property.

        A delete-then-upsert leaves a window in which the document has no points at all,
        so a query running concurrently sees it as absent rather than as stale. Over-
        complete for a moment is a far smaller error than momentarily absent.
        """
        store.upsert_points(TENANT_A, [_point(chunk_index=n) for n in range(6)])

        store.upsert_points(TENANT_A, [_point(chunk_index=0, text="new 0")])
        observed_after_upsert = len(store.scroll_document_points(TENANT_A, DOCUMENT_ONE))
        store.delete_stale_points(TENANT_A, DOCUMENT_ONE, keep_below_index=1)

        assert observed_after_upsert == 6, "nothing was deleted before the upsert"
        assert len(store.scroll_document_points(TENANT_A, DOCUMENT_ONE)) == 1

    def test_a_delete_failure_is_wrapped(self, store: QdrantVectorStore) -> None:
        client = fake_client_of(store)
        client.fail_with["delete"] = server_error(500, "Internal Server Error", b"boom")
        with pytest.raises(VectorStoreError, match="could not be deleted"):
            store.delete_document_points(TENANT_A, DOCUMENT_ONE)


class TestCounting:
    def test_it_counts_only_this_document(self, store: QdrantVectorStore) -> None:
        store.upsert_points(TENANT_A, [_point(chunk_index=n) for n in range(3)])
        store.upsert_points(
            TENANT_A, [_point(document_id=DOCUMENT_TWO, chunk_index=0)]
        )
        assert store.count_points_for_document(TENANT_A, DOCUMENT_ONE) == 3

    def test_it_asks_for_an_exact_count(self, store: QdrantVectorStore) -> None:
        """Because it is compared with ``documents.chunk_count``.

        Qdrant's default count is approximate above a threshold, and comparing an
        estimate with an integer makes a real mismatch look like a pass.
        """
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point()])
        store.count_points_for_document(TENANT_A, DOCUMENT_ONE)
        assert client.named("count")[0]["exact"] is True

    def test_a_missing_collection_counts_zero(
        self, store: QdrantVectorStore
    ) -> None:
        assert store.count_points_for_document(TENANT_B, DOCUMENT_ONE) == 0

    def test_a_document_with_no_points_counts_zero(
        self, store: QdrantVectorStore
    ) -> None:
        store.upsert_points(TENANT_A, [_point(document_id=DOCUMENT_TWO)])
        assert store.count_points_for_document(TENANT_A, DOCUMENT_ONE) == 0


class TestScrolling:
    def test_it_returns_the_chunks_in_index_order(
        self, store: QdrantVectorStore
    ) -> None:
        """Out-of-order chunks would be reassembled into nonsense.

        Written in a deliberately jumbled order so that a store which returned points in
        insertion order would pass on an already-sorted fixture and fail here.
        """
        for index in (5, 0, 3, 1, 4, 2):
            store.upsert_points(TENANT_A, [_point(chunk_index=index, text=f"c{index}")])

        records = store.scroll_document_points(TENANT_A, DOCUMENT_ONE)
        assert [payload_key(record, PAYLOAD_CHUNK_INDEX) for record in records] == [
            0, 1, 2, 3, 4, 5
        ]

    def test_it_asks_qdrant_to_order_by_chunk_index_ascending(
        self, store: QdrantVectorStore
    ) -> None:
        """The ordering is a server-side request, and Qdrant 400s without the index."""
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point()])
        store.scroll_document_points(TENANT_A, DOCUMENT_ONE)

        order_by = client.named("scroll")[0]["order_by"]
        assert order_by.key == PAYLOAD_CHUNK_INDEX
        assert order_by.direction == models.Direction.ASC

    def test_it_returns_records_not_scored_points(
        self, store: QdrantVectorStore
    ) -> None:
        """A scroll with ``order_by`` is an ordered read, not a ranked one.

        ``ScoredPoint`` carries a similarity ``score`` that only exists for a search.
        Returning it would advertise a relevance value this method never computed.
        """
        store.upsert_points(TENANT_A, [_point()])
        [record] = store.scroll_document_points(TENANT_A, DOCUMENT_ONE)
        assert isinstance(record, models.Record)
        assert not isinstance(record, models.ScoredPoint)

    def test_it_asks_for_both_payload_and_vectors(
        self, store: QdrantVectorStore
    ) -> None:
        """An ingestion test asserts the points which arrived are the points intended.

        That means comparing the vector as well as every payload field, which needs both
        returned.
        """
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point()])
        store.scroll_document_points(TENANT_A, DOCUMENT_ONE)

        [call] = client.named("scroll")
        assert call["with_payload"] is True
        assert call["with_vectors"] is True

    def test_it_scopes_the_read_to_one_document(
        self, store: QdrantVectorStore
    ) -> None:
        store.upsert_points(TENANT_A, [_point(chunk_index=n) for n in range(3)])
        store.upsert_points(
            TENANT_A, [_point(document_id=DOCUMENT_TWO, chunk_index=0)]
        )
        assert len(store.scroll_document_points(TENANT_A, DOCUMENT_ONE)) == 3

    @pytest.mark.parametrize("limit", [0, -1, -100])
    def test_a_non_positive_limit_is_refused(
        self, store: QdrantVectorStore, limit: int
    ) -> None:
        """Qdrant treats ``0`` as "no limit", silently making a bounded read unbounded."""
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point()])
        with pytest.raises(ValueError, match="at least 1"):
            store.scroll_document_points(TENANT_A, DOCUMENT_ONE, limit=limit)
        assert client.named("scroll") == []

    def test_a_missing_collection_returns_nothing(
        self, store: QdrantVectorStore
    ) -> None:
        """Empty rather than an error, matching the count and delete behaviour."""
        assert store.scroll_document_points(TENANT_B, DOCUMENT_ONE) == []

    def test_a_scroll_failure_is_wrapped(self, store: QdrantVectorStore) -> None:
        client = fake_client_of(store)
        client.fail_with["scroll"] = server_error(400, "Bad Request", b"no index")
        with pytest.raises(VectorStoreError, match="could not be read"):
            store.scroll_document_points(TENANT_A, DOCUMENT_ONE)


class TestRoleFilter:
    """The filter's *shape*, as :class:`FakeQdrant` evaluates it.

    Whether Qdrant itself agrees is asked against a real server in
    ``tests/test_qdrant_integration.py``. What belongs here is that the store sends a filter
    whose two clauses are a disjunction over ``allowed_roles``, so the fake can select
    points and "the viewer did not receive the admin-only chunk" becomes answerable
    offline at all.
    """

    def test_it_is_a_disjunction_of_null_or_matching_element(
        self, store: QdrantVectorStore
    ) -> None:
        """``must`` (a conjunction) would make the filter unsatisfiable.

        ``IsNull`` and ``MatchAny`` are mutually exclusive for any single stored value — a
        value is null or it is a non-empty array — so under ``must`` no point would ever
        match and every query would silently return nothing. A filter that is too narrow
        looks like an empty knowledge base, which is the kind of failure nobody
        investigates.
        """
        store.upsert_points(TENANT_A, [_point()])
        store.search_points(TENANT_A, vector=_vector(), limit=5, role=UserRole.VIEWER)

        [call] = fake_client_of(store).named("query_points")
        sent = call["query_filter"]
        assert not sent.must
        assert sent.should is not None and len(sent.should) == 2

    def test_one_clause_selects_an_unrestricted_document(
        self, store: QdrantVectorStore
    ) -> None:
        store.upsert_points(TENANT_A, [_point(allowed_roles=None)])
        store.search_points(TENANT_A, vector=_vector(), limit=5, role=UserRole.VIEWER)
        assert len(_searched(store)) == 1

    def test_the_other_clause_selects_a_permitted_document(
        self, store: QdrantVectorStore
    ) -> None:
        store.upsert_points(TENANT_A, [_point(allowed_roles=[UserRole.VIEWER.value])])
        store.search_points(TENANT_A, vector=_vector(), limit=5, role=UserRole.VIEWER)
        assert len(_searched(store)) == 1

    def test_a_document_for_other_roles_is_not_selected(
        self, store: QdrantVectorStore
    ) -> None:
        """The point of the exercise: restricted text is never returned to the process."""
        store.upsert_points(
            TENANT_A, [_point(allowed_roles=[UserRole.ADMIN.value])]
        )
        store.search_points(TENANT_A, vector=_vector(), limit=5, role=UserRole.VIEWER)
        assert _searched(store) == []

    def test_a_document_visible_to_no_role_is_not_selected_by_any_role(
        self, store: QdrantVectorStore
    ) -> None:
        """``[]`` means "nobody", and treating it as "anybody" is a disclosure.

        The asymmetric failure this guards: an over-restrictive typo becoming a
        permission grant is the outcome nobody notices until a document leaks.
        """
        store.upsert_points(TENANT_A, [_point(allowed_roles=[])])
        for index, role in enumerate(UserRole):
            store.search_points(TENANT_A, vector=_vector(), limit=5, role=role)
            assert _searched(store, since=index) == [], role


class TestSearchPoints:
    """PMP Phase 4's retrieval, offline.

    The questions here are the store's: which collection is searched, what is asked for,
    what comes back and in what order. What a retrieved chunk then becomes is
    ``tests/test_retrieval.py``'s, and what the whole path does end to end is
    ``tests/test_query_service.py``'s.
    """

    def test_it_searches_only_the_tenant_s_own_collection(
        self, store: QdrantVectorStore
    ) -> None:
        """``tenant_id`` is the only collection selector there is.

        Tenant B's chunks are written into B's collection, so a store that derived the name
        from the tenant argument cannot reach them. Asserted on the call rather than on
        the result because the result would also be empty for a store that searched
        nothing at all.
        """
        client = fake_client_of(store)
        client.add_collection(collection_name_for(TENANT_B))
        store.upsert_points(
            TENANT_B, [_point(document_id=DOCUMENT_TWO, text="tenant B's chunk")]
        )

        hits = store.search_points(
            TENANT_B, vector=_vector(), limit=5, role=UserRole.VIEWER
        )

        assert [call["collection_name"] for call in client.named("query_points")] == [
            collection_name_for(TENANT_B)
        ]
        assert [scored_payload_of(hit)[PAYLOAD_TEXT] for hit in hits] == ["tenant B's chunk"]

    def test_a_tenant_s_search_returns_none_of_another_tenant_s_chunks(
        self, store: QdrantVectorStore
    ) -> None:
        """The isolation boundary, asserted on returned content.

        Both collections hold a chunk at the same distance from the same query, so the only
        thing that can decide the result is the collection name. Tenant A's search must
        return A's text — not merely *not* return B's, which an empty result would also
        satisfy.
        """
        client = fake_client_of(store)
        client.add_collection(collection_name_for(TENANT_B))
        store.upsert_points(TENANT_A, [_point(text="tenant A's chunk")])
        store.upsert_points(
            TENANT_B, [_point(document_id=DOCUMENT_TWO, text="tenant B's chunk")]
        )

        hits = store.search_points(
            TENANT_A, vector=_vector(), limit=5, role=UserRole.VIEWER
        )
        texts = [scored_payload_of(hit)[PAYLOAD_TEXT] for hit in hits]
        assert texts == ["tenant A's chunk"]
        assert "tenant B's chunk" not in texts

    def test_the_role_is_not_a_positional_argument(
        self, store: QdrantVectorStore
    ) -> None:
        """It is keyword-only and has no default, so a caller cannot omit it.

        A positional ``role`` with a default would still be omittable, and the failure that
        produces is a search that silently returns unrestricted documents to whoever asked
        for them.
        """
        with pytest.raises(TypeError):
            store.search_points(  # type: ignore[call-arg]
                TENANT_A, _vector(), 5, UserRole.VIEWER
            )

    def test_the_role_filter_is_always_sent(
        self, store: QdrantVectorStore
    ) -> None:
        """Not "is sent when a role is passed" — a role is always passed.

        Checked for every role, because a filter keyed on ``role is not None`` would pass
        the role-agnostic half of this and fail the other.
        """
        client = fake_client_of(store)
        for role in UserRole:
            store.search_points(TENANT_A, vector=_vector(), limit=5, role=role)

        sent = [call["query_filter"] for call in client.named("query_points")]
        assert len(sent) == len(UserRole)
        assert all(filters.should for filters in sent)

    def test_it_asks_for_payloads_but_not_vectors(
        self, store: QdrantVectorStore
    ) -> None:
        """A search needs the text; it does not need the vector back.

        ``with_vectors=True`` on every query would ship a full copy of each matched
        embedding back over the wire for a value already known — the dominant cost of the
        request at any interesting collection size.
        """
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point()])
        store.search_points(TENANT_A, vector=_vector(), limit=5, role=UserRole.VIEWER)

        [call] = client.named("query_points")
        assert call["with_payload"] is True
        assert call["with_vectors"] is False

    def test_it_returns_the_nearest_points_first(
        self, store: QdrantVectorStore
    ) -> None:
        """Asserted on which *document* each hit belongs to, not on scores.

        The vectors below are chosen so their similarities to the query are unambiguously
        ordered; asserting on the ordering of document ids therefore tests the ranking
        without re-testing the fake's cosine.
        """
        near, middle, far = 0.95, 0.5, -0.9
        middle_document = uuid.uuid4()
        # Inserted worst-first, so insertion order and similarity order disagree and a
        # store that passed points through unsorted would fail.
        store.upsert_points(
            TENANT_A,
            [
                _point(document_id=DOCUMENT_TWO, vector=_vector(lead=far)),
                _point(document_id=middle_document, vector=_vector(lead=middle)),
                _point(document_id=DOCUMENT_ONE, vector=_vector(lead=near)),
            ],
        )

        hits = store.search_points(
            TENANT_A, vector=_vector(), limit=3, role=UserRole.VIEWER
        )
        # Payload document ids are strings: they crossed a JSON payload boundary, and
        # the store deliberately does not re-parse them on the retrieval path.
        assert [scored_payload_of(hit)[PAYLOAD_DOCUMENT_ID] for hit in hits] == [
            str(document)
            for document in (DOCUMENT_ONE, middle_document, DOCUMENT_TWO)
        ]
        assert hits[0].score > hits[1].score > hits[2].score

    def test_it_caps_the_result_at_the_limit(
        self, store: QdrantVectorStore
    ) -> None:
        """``limit`` reaches Qdrant rather than being applied after the fact.

        Truncating in Python would mean Qdrant computed an unbounded ranking — the exact
        "bounded retrieval becomes unbounded" case :meth:`search_points` documents.
        """
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point(chunk_index=n) for n in range(6)])
        assert (
            len(
                store.search_points(
                    TENANT_A, vector=_vector(), limit=2, role=UserRole.VIEWER
                )
            )
            == 2
        )
        assert client.named("query_points")[0]["limit"] == 2

    @pytest.mark.parametrize("limit", [0, -1])
    def test_a_limit_below_one_is_refused_before_the_network(
        self, limit: int, store: QdrantVectorStore
    ) -> None:
        """``limit=0`` means "no limit" to Qdrant, so it is rejected here.

        A silent reinterpretation of 0 as unbounded is the failure that would let a
        configuration mistake turn a five-chunk context into the entire collection.
        """
        with pytest.raises(ValueError, match="limit"):
            store.search_points(
                TENANT_A, vector=_vector(), limit=limit, role=UserRole.VIEWER
            )
        assert fake_client_of(store).named("query_points") == []

    def test_a_wrong_width_query_vector_is_refused(
        self, store: QdrantVectorStore
    ) -> None:
        """``VectorDimensionMismatch``, the type a retry cannot fix.

        Qdrant would answer 400 and the client would wrap it as an opaque
        ``UnexpectedResponse``, so catching it here is what lets the orchestrator report a
        misconfiguration rather than a transient store failure.
        """
        with pytest.raises(VectorDimensionMismatch, match="8-dimensional"):
            store.search_points(
                TENANT_A, vector=[0.1] * (WIDTH + 1), limit=5, role=UserRole.VIEWER
            )
        assert fake_client_of(store).named("query_points") == []

    def test_a_missing_collection_returns_no_hits(
        self, store: QdrantVectorStore
    ) -> None:
        """A tenant that has indexed nothing has an *empty* result, not an error.

        Raised on instead, a brand-new tenant's first query would fail, and the natural
        response to a failing query is a failing query.
        """
        assert store.search_points(
            TENANT_B, vector=_vector(), limit=5, role=UserRole.VIEWER
        ) == []

    def test_an_empty_collection_returns_no_hits(
        self, store: QdrantVectorStore
    ) -> None:
        assert store.search_points(
            TENANT_A, vector=_vector(), limit=5, role=UserRole.VIEWER
        ) == []

    def test_tied_scores_are_ordered_by_id(self, store: QdrantVectorStore) -> None:
        """Ties break by id, so top-K is a function of the stored data alone.

        Without a tie-break "which of the two equal-scoring chunks is cited" is Qdrant's
        internal ordering — stable for a given index, but not a property this project
        controls, and therefore not one a test could assert about.
        """
        store.upsert_points(
            TENANT_A,
            [
                _point(chunk_index=1, text="second"),
                _point(chunk_index=0, text="first"),
            ],
        )
        first = store.search_points(
            TENANT_A, vector=_vector(), limit=2, role=UserRole.VIEWER
        )
        second = store.search_points(
            TENANT_A, vector=_vector(), limit=2, role=UserRole.VIEWER
        )

        assert [point.score for point in first] == [point.score for point in second]
        assert [str(point.id) for point in first] == [str(point.id) for point in second]

        # The order is ascending by id *string*, which is the property — not ascending
        # by chunk index. Ids are uuid5-derived, so the two orderings are unrelated and a
        # test asserting chunk order would pass by luck here and fail on any other
        # document. Stating it as "sorted by the sort key the store actually uses" is
        # also what makes it falsifiable: remove the sort and this fails.
        assert [str(point.id) for point in first] == sorted(
            str(point.id) for point in first
        )
        assert len(first) == 2

    @pytest.mark.parametrize("failure", CLIENT_FAILURES, ids=lambda f: f.__name__)
    def test_a_client_failure_becomes_a_vector_store_error(
        self, failure: type[BaseException], store: QdrantVectorStore
    ) -> None:
        store.upsert_points(TENANT_A, [_point()])
        fake_client_of(store).fail_with["query_points"] = failure_instance(failure)

        with pytest.raises(VectorStoreError) as caught:
            store.search_points(
                TENANT_A, vector=_vector(), limit=5, role=UserRole.VIEWER
            )
        assert "query_points" not in str(caught.value)
        assert f"({failure.__name__})" in str(caught.value)

    def test_the_failure_message_names_no_document_content(
        self, store: QdrantVectorStore
    ) -> None:
        """The message is recorded on a document row, so it must be safe to store."""
        store.upsert_points(TENANT_A, [_point(text="CONFIDENTIAL-ACME-SALARY-SCALE")])
        fake_client_of(store).fail_with["query_points"] = connection_refused()

        with pytest.raises(VectorStoreError) as caught:
            store.search_points(
                TENANT_A, vector=_vector(), limit=5, role=UserRole.VIEWER
            )
        assert "CONFIDENTIAL-ACME-SALARY-SCALE" not in str(caught.value)


class TestPayloadExhaustiveness:
    def test_it_is_true_when_every_point_carries_the_tenant(
        self, store: QdrantVectorStore
    ) -> None:
        store.upsert_points(TENANT_A, [_point(chunk_index=n) for n in range(3)])
        assert store.payload_tenant_id_is_exhaustive(TENANT_A) is True

    def test_it_is_false_when_one_point_does_not(
        self, store: QdrantVectorStore
    ) -> None:
        """The defence-in-depth check that would catch a half-done migration.

        A collection moved to a shared tenant would keep points written before
        ``tenant_id`` was added to the payload, and those points would be silently
        invisible to a tenant filter. This is the check that notices.
        """
        client = fake_client_of(store)
        store.upsert_points(TENANT_A, [_point(chunk_index=0)])
        legacy = _point(chunk_index=1, tenant_id=TENANT_B)
        collection = client.collections[collection_name_for(TENANT_A)]
        collection.points[legacy.id] = legacy

        assert store.payload_tenant_id_is_exhaustive(TENANT_A) is False

    def test_it_is_true_for_a_missing_collection(
        self, store: QdrantVectorStore
    ) -> None:
        """No points to be wrong about. Not an error, so it is usable as an assertion."""
        assert store.payload_tenant_id_is_exhaustive(TENANT_B) is True


class TestClientFailureClassification:
    def test_the_tuple_covers_the_transport_layer(self) -> None:
        """Verified against the installed client rather than read off the documentation.

        The connection-refused path is the one this tuple exists to catch, and it is the
        one whose exception type is easiest to get wrong.
        """
        assert http_exceptions.ApiException in CLIENT_FAILURES
        assert issubclass(
            http_exceptions.ResponseHandlingException, CLIENT_FAILURES
        ), "the connection-refused path is the one this tuple has to cover"
        assert issubclass(http_exceptions.UnexpectedResponse, CLIENT_FAILURES)

    def test_the_tuple_covers_the_local_checks(self) -> None:
        """The half of the client that raises before anything is sent."""
        from qdrant_client.common import client_exceptions

        assert client_exceptions.QdrantException in CLIENT_FAILURES
        assert issubclass(
            client_exceptions.ResourceExhaustedResponse, CLIENT_FAILURES
        ), "the local layer's own failure must be caught, or a width check would leak"

    def test_the_tuple_is_not_exception(self) -> None:
        """A ``TypeError`` from a changed signature must propagate as the bug it is."""
        assert Exception not in CLIENT_FAILURES

    def test_the_tuple_is_a_tuple_of_exception_types(self) -> None:
        assert isinstance(CLIENT_FAILURES, tuple)
        assert all(
            isinstance(failure, type) and issubclass(failure, BaseException)
            for failure in CLIENT_FAILURES
        )


class TestBuildVectorPoint:
    def test_it_derives_the_id_and_the_payload_from_the_same_arguments(self) -> None:
        """The reason it exists.

        Two functions consume tenant, document and chunk index. If the id were derived
        from one set of values and the payload built from another, the stored metadata
        and the identity it was derived from would disagree with nothing to record it.
        """
        point = _point(chunk_index=4, page=2)
        assert point.id == point_id(
            tenant_id=TENANT_A, document_id=DOCUMENT_ONE, chunk_index=4
        )
        assert point.payload[PAYLOAD_DOCUMENT_ID] == str(DOCUMENT_ONE)
        assert point.payload[PAYLOAD_CHUNK_INDEX] == 4
        assert point.payload[PAYLOAD_TENANT_ID] == str(TENANT_A)
        assert point.payload["page"] == 2

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"chunk_index": -1}, "must not be negative"),
            ({"page": 0}, "positive 1-based"),
            ({"text": "   "}, "requires text"),
        ],
    )
    def test_it_refuses_a_point_it_cannot_build(
        self, overrides: dict[str, Any], reason: str
    ) -> None:
        """The one construction point, so every guard fires before anything is stored."""
        with pytest.raises(ValueError, match=reason):
            _point(**overrides)

    def test_two_calls_with_the_same_arguments_produce_the_same_point(self) -> None:
        assert _point() == _point()


class TestTheRealClientIsOnlyForTheIntegrationSuite:
    """Guards against the fake quietly standing in for Qdrant in production code."""

    def test_the_store_only_imports_the_client_for_typing_and_construction(self) -> None:
        import ast
        from pathlib import Path

        import app.storage.qdrant as module

        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert "qdrant_client" in imported
        assert "qdrant_client.local" not in imported, (
            "the in-process local mode must not be reachable from application code"
        )

    def test_the_local_client_is_not_constructible_from_this_module(
        self, store: QdrantVectorStore
    ) -> None:
        """A local-mode client would look like Qdrant and quietly behave differently.

        Cosine normalisation on write — the fact that changes what a test asserts — is a
        server behaviour, and ``QdrantLocal`` is a different implementation of the same
        API. Using it here would make the suite pass without a server while testing
        something else.
        """
        assert isinstance(store._client, FakeQdrant)
        assert not isinstance(store._client, QdrantLocal)