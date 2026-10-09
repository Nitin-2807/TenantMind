"""Narrowing helpers for reading what the vector store returns.

``qdrant_client`` types a record's ``payload`` as ``dict[str, Any] | None`` and its
``vector`` as ``float | list[float] | str``, because over REST a point may carry no
payload at all, and — for named vectors — may store its components as a map or a
multi-vector list. Both are real possibilities that TenantMind's calls do not produce,
and both would make every assertion in the test suite read
``record.payload["text"] if record.payload else None``.

That is noise repeated on dozens of lines, and it also hides the interesting part: a
record that genuinely arrived with no payload is a condition worth failing on, not
something to work around inline. So the narrowing lives here once, and each helper
*asserts* rather than defaults — an empty payload is a bug, not a value to tolerate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from qdrant_client import QdrantClient, models

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance for the type checker
    from app.core.config import Settings
    from app.storage.qdrant import QdrantVectorStore


def store_over(settings: Settings, client: Any) -> QdrantVectorStore:
    """A :class:`QdrantVectorStore` wired to a stand-in client.

    The store's constructor declares ``client: QdrantClient | None``, because in
    production the client is always a real :class:`~qdrant_client.QdrantClient`. A test
    double that is not one therefore cannot be passed without a type-level
    reconciliation, and this function is where exactly one such reconciliation lives.

    Why here and not scattered across the suite
    ------------------------------------------
    Every unit test in this phase that touches the vector store needs a fake client, and
    there are several hundred of them. Doing the cast at each call site would mean either
    hundreds of suppressions — which is a way of telling the type checker to stop
    reading the files — or hundreds of chances to write it slightly differently. One
    function, imported everywhere, means the claim "this stand-in is substitutable for
    the real client everywhere it is used" is made in a single place and can be checked.

    Why not make the double a subclass instead
    -------------------------------------------
    It would also work, and would need no cast at all. The nine methods the store calls
    exist on the real client, so overriding them is possible — but the real signatures
    carry roughly fifty optional parameters between them, and restating all of them here
    would make this file a second copy of the client's API that has to be updated on
    every upgrade. The store passes eleven arguments across those nine calls; matching
    the other fifty proves nothing about the store.
    """
    from app.storage.qdrant import QdrantVectorStore as Store

    return store_of(settings, client, Store)


def store_of(settings: Settings, client: Any, store_class: type[Any]) -> Any:
    """Build ``store_class`` over a stand-in client.

    :func:`store_over` is this function with :class:`~app.storage.qdrant.QdrantVectorStore`
    as the class, and the split exists for one caller: ``tests/test_query_service.py``
    subclasses the store to record one method, and that subclass needs the same cast at its
    own ``__init__``. Having the cast in two files would mean the claim "a stand-in client
    is substitutable everywhere it is used" is made twice and checked twice, and the second
    copy is the one a future test would copy from.

    ``Any`` rather than a bound of ``type[T]`` because the return type depends on
    ``store_class`` and Python's typing cannot express "some subclass of this" without a
    generic parameter that would only be exercised here.
    """
    return store_class(settings, client=cast(QdrantClient, client))


def fake_client_of(store: Any) -> Any:
    """The stand-in client a store built by :func:`store_over` is holding.

    The mirror of the cast above, and asserted rather than cast: a store built by
    :func:`store_over` really is holding the object it was given, so this cannot fail —
    and if it ever could, that would be the single most important thing to learn about
    this suite.
    """
    client = store._client
    assert type(client).__name__ == "FakeQdrant", (
        f"the store is holding a {type(client).__name__}, not a FakeQdrant"
    )
    return client


def payload_of(record: models.Record) -> dict[str, Any]:
    """The record's payload, asserted to be present.

    Raises:
        AssertionError: the record carried no payload. Every point this project writes
            has one, so a record without one is a defect worth naming.
    """
    assert record.payload is not None, (
        f"point {record.id} came back with no payload; every point TenantMind writes "
        "carries document_id, chunk_index, text, tenant_id and filename"
    )
    return record.payload


def payload_key(record: models.Record, key: str) -> Any:
    """One payload value by name, asserted present.

    Separate from :func:`payload_of` for the fields whose *absence* would be the
    finding — ``assert "tenant_id" not in payload_of(record)`` reads as an absence,
    whereas ``assert payload_key(record, "tenant_id") is None`` reads as a value
    comparison, and the two are easy to confuse in a test that is specifically about
    one key not being there.
    """
    payload = payload_of(record)
    assert key in payload, (
        f"point {record.id} has no {key!r} in its payload; it has "
        f"{sorted(payload)}"
    )
    return payload[key]


def scored_payload_of(point: models.ScoredPoint) -> dict[str, Any]:
    """A search hit's payload, asserted to be present.

    The :class:`~qdrant_client.http.models.ScoredPoint` counterpart to
    :func:`payload_of`. ``search_points`` asks for ``with_payload=True`` and asserts that
    itself in ``tests/test_qdrant_store.py``, so a hit without one is the same class of
    defect as a record without one: the payload is what the retrieval layer turns into a
    citation, and a hit missing it is a bug rather than a value to tolerate.
    """
    assert point.payload is not None, (
        f"search hit {point.id} came back with no payload; every point TenantMind writes "
        "carries document_id, chunk_index, text, tenant_id, filename and allowed_roles"
    )
    return point.payload


def vector_of(record: models.Record) -> list[float]:
    """The record's vector as plain floats, asserted to be a flat list.

    A Cosine collection stores a flat list, so anything else — a map of named vectors, a
    list of lists, a string — is a shape this project never writes and is worth failing
    on rather than coercing.
    """
    stored = record.vector
    assert isinstance(stored, list), (
        f"point {record.id} stored its vector as {type(record.vector).__name__}, "
        "not a flat list of floats"
    )
    # A multi-vector point stores a list of lists. Checked element by element rather than
    # through `all()`, because that narrows the type for the checker as well as for the
    # reader, and a nested list here would be a real shape change rather than noise.
    components: list[float] = []
    for component in stored:
        assert isinstance(component, (int, float)), (
            f"point {record.id} stored a non-numeric vector component, "
            f"{type(component).__name__}"
        )
        components.append(float(component))
    return components
