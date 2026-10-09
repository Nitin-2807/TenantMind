"""Tests for :mod:`app.rag.embeddings` — the provider abstraction, with no network.

No test in this file reaches an embedding API, and that is a rule rather than an
accident
------------------------------------------------------------------------------
Two ways to test an HTTP client, and one of them is a trap. Asserting on a *mocked
client* proves the mock was called; asserting on a *stubbed transport* proves the real
client builds the right request, sends it to the right URL with the right headers, and
handles what comes back. Only the second says anything about the code that ships.

So :class:`~app.rag.embeddings.EmbeddingTransport` is the only seam, and it is one
callable. Every test below drives the real :class:`GeminiEmbeddingProvider` — the real
URL construction, the real JSON body, the real header set, the real response
validation — through a function that returns a canned ``(status, bytes)``.

What a live API could still tell us, and what it could not
----------------------------------------------------------
A real key would tell us that Google's response shape has not changed. It would not
tell us anything these tests cannot: that a 429 becomes a recorded ingestion failure
rather than a traceback, that a 200 with three vectors for four inputs is refused
rather than half-applied, that the key never appears in a message. Those are the
failures that matter, and all of them are reachable from here.

Four things this file insists on, each of which has a real failure behind it
--------------------------------------------------------------------------
1. **No secret in any artefact.** The API key must not appear in the URL, in a log
   record, in an exception message, or in a provider's echoed error text. A URL is the
   one thing here that routinely reaches a proxy log or a traceback.
2. **A wrong-width vector fails loudly.** Never truncated, never padded. A silently
   truncated vector embeds a *different* point than the one indexed, and retrieval
   returns confident nonsense.
3. **A short batch fails.** A provider that returned three vectors for four inputs, in
   order, would misalign every vector after the third with the chunk it belongs to.
4. **Nothing about a document is logged.** Document text is untrusted data (PMP §17).
"""

from __future__ import annotations

import io
import json
import logging
import math
import socket
import urllib.error
import urllib.request
from email.message import Message

import pytest

from app.core.config import Settings
from app.rag.embeddings import (
    GEMINI_AUTH_HEADER,
    MAX_BATCH_SIZE,
    EmbeddingDimensionMismatch,
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingProviderUnavailable,
    EmbeddingTransport,
    GeminiEmbeddingProvider,
    StaticEmbeddingProvider,
    build_embedding_provider,
)
from tests.conftest import make_settings


#: ``urllib`` types a request body as anything from a raw buffer to an iterable of
#: byte chunks to a file object, because that is what ``Request`` accepts. This provider
#: only ever supplies ``bytes``, so the assertions below narrow through this helper once
#: rather than eight times — and each call *asserts* rather than defaults, because a
#: body that was not bytes would be a change in how the request is built.
def body_of(request: urllib.request.Request) -> bytes:
    """The request's body, asserted to be raw bytes."""
    data = request.data
    assert isinstance(data, bytes), (
        f"the request body is {type(data).__name__}, not bytes; the provider builds "
        "every body with json.dumps(...).encode()"
    )
    return data


#: An ``HTTPError``'s header mapping, for the cases that never read one.
NO_HEADERS: Message[str, str] = Message()

#: A key that is obviously not a credential, so a leak of it is unmistakable and
#: nothing that reads this file can mistake it for something to use.
FAKE_KEY = "not-a-real-key-for-tests-only-000000000000"

#: Seeded so a failure is reproducible.
SEED = 20251004


#: The configuration every Gemini test starts from. Merged with per-test overrides by
#: :func:`gemini_settings` rather than passed as ``**overrides`` to ``make_settings``,
#: because a defaults dict spliced with a caller's keyword arguments collides the moment
#: a test overrides one of the defaults -- ``TypeError: got multiple values for
#: keyword argument`` -- which is a confusing way to learn that a test wants a different
#: embedding width.
_GEMINI_DEFAULTS: dict[str, object] = {
    "embedding_provider": "gemini",
    "embedding_model": "text-embedding-004",
    "embedding_dimension": 768,
    "gemini_api_key": FAKE_KEY,
    "gemini_api_base_url": "https://generativelanguage.googleapis.com/v1beta",
}


def gemini_settings(**overrides: object) -> Settings:
    return make_settings(**{**_GEMINI_DEFAULTS, **overrides})


def gemini_response(*vectors: list[float]) -> tuple[int, bytes]:
    """A well-formed ``batchEmbedContents`` 200 response."""
    return 200, json.dumps(
        {"embeddings": [{"values": vector} for vector in vectors]}
    ).encode()


def unit(width: int) -> list[float]:
    """A vector of the right width, with a recognisable non-zero component."""
    return [0.5] + [0.25] * (width - 1)


class RecordingTransport:
    """A transport that records requests and replays canned responses.

    A class rather than a closure so the recorded requests survive as an attribute the
    tests can inspect afterwards, and so several tests can share one instance.
    """

    def __init__(self, *responses: tuple[int, bytes]) -> None:
        self._responses = list(responses)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def __call__(
        self, request: urllib.request.Request, timeout: float
    ) -> tuple[int, bytes]:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self._responses:
            raise AssertionError("the provider made more requests than the test allowed")
        return self._responses.pop(0)

    @property
    def request_count(self) -> int:
        return len(self.requests)

    def header(self, index: int, name: str) -> str | None:
        """The value of header ``name`` on request ``index``.

        Looked up case-insensitively because :meth:`urllib.request.Request.header_items`
        capitalises header names, and a test that hard-coded ``"x-goog-api-key"`` would
        pass or fail depending on Python's capitalisation rules rather than on whether
        the header was sent.
        """
        target = name.lower()
        for key, value in self.requests[index].header_items():
            if key.lower() == target:
                return value
        return None


class TestTheAbstraction:
    def test_the_base_class_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingProvider()  # type: ignore[abstract]

    def test_a_provider_without_dimensions_cannot_be_instantiated(self) -> None:
        class NoDimensions(EmbeddingProvider):
            @property
            def model_name(self) -> str:
                return "x"

            def embed_texts(self, texts: list[str]) -> list[list[float]]:
                return []

        with pytest.raises(TypeError):
            NoDimensions()  # type: ignore[abstract]

    def test_a_new_provider_needs_no_change_to_the_factory_caller(self) -> None:
        """Extensible without rewriting the pipeline.

        The factory is the only place a concrete class is named, and it takes a
        ``Settings``. A provider that satisfies the interface drops in.
        """

        class ConstantProvider(EmbeddingProvider):
            def __init__(self, *, dimensions: int) -> None:
                self._dimensions = dimensions

            @property
            def dimensions(self) -> int:
                return self._dimensions

            @property
            def model_name(self) -> str:
                return "constant"

            def embed_texts(self, texts: list[str]) -> list[list[float]]:
                return [[1.0] + [0.0] * (self._dimensions - 1) for _ in texts]

        provider = ConstantProvider(dimensions=4)
        assert isinstance(provider, EmbeddingProvider)
        assert provider.embed_texts(["a", "b"]) == [[1.0, 0.0, 0.0, 0.0]] * 2

    @pytest.mark.parametrize("name", ["gemini", "static"])
    def test_the_factory_returns_something_satisfying_the_interface(
        self, name: str
    ) -> None:
        settings = make_settings(
            embedding_provider=name,
            gemini_api_key=FAKE_KEY if name == "gemini" else None,
        )
        provider = build_embedding_provider(settings)
        assert isinstance(provider, EmbeddingProvider)
        assert provider.dimensions == settings.embedding_dimension
        assert provider.model_name

    def test_the_factory_takes_the_transport_for_tests_only(self) -> None:
        transport = RecordingTransport(gemini_response(unit(768)))
        provider = build_embedding_provider(gemini_settings(), transport=transport)
        provider.embed_texts(["hello"])
        assert transport.request_count == 1

    def test_the_gemini_provider_never_reads_the_environment(self) -> None:
        """Configuration comes from ``Settings`` and nowhere else (PMP §8).

        Asserted over the module source, because a stray ``os.environ`` read would be
        invisible in the output: the provider would work, and the one thing the test
        suite relies on — that no test can be influenced by the developer's own ``.env``
        — would quietly stop being true.
        """
        from pathlib import Path

        import app.rag.embeddings as module

        source = Path(module.__file__).read_text(encoding="utf-8")
        for forbidden in ("os.environ", "os.getenv", "getenv("):
            assert forbidden not in source, (
                f"app/rag/embeddings.py uses {forbidden!r}; configuration must come "
                "only from Settings"
            )


class TestStaticEmbeddingProvider:
    def test_it_returns_one_vector_per_input_in_order(self) -> None:
        provider = StaticEmbeddingProvider(dimensions=4)
        vectors = provider.embed_texts(["a", "b", "c"])
        assert len(vectors) == 3
        assert vectors[0] == vectors[1] == vectors[2]

    def test_the_vector_has_the_declared_width(self) -> None:
        for width in (1, 3, 768):
            provider = StaticEmbeddingProvider(dimensions=width)
            assert all(len(v) == width for v in provider.embed_texts(["a"]))

    def test_the_vector_is_a_unit_vector(self) -> None:
        """Normalisable, so Qdrant's cosine normalisation has something to work with."""
        provider = StaticEmbeddingProvider(dimensions=768)
        vector = provider.embed_texts(["a"])[0]
        assert math.isclose(sum(c * c for c in vector), 1.0)

    def test_a_zero_or_negative_width_is_refused(self) -> None:
        for width in (0, -1):
            with pytest.raises(ValueError):
                StaticEmbeddingProvider(dimensions=width)

    def test_an_empty_batch_is_refused(self) -> None:
        """Refused rather than returning an empty list.

        An empty list is a legitimate-looking answer that would make a caller believe it
        had embedded everything it asked for.
        """
        with pytest.raises(ValueError):
            StaticEmbeddingProvider(dimensions=4).embed_texts([])

    def test_it_records_what_it_was_asked_for(self) -> None:
        """So a test can prove batching happened without counting network calls."""
        provider = StaticEmbeddingProvider(dimensions=4)
        provider.embed_texts(["a", "b"])
        provider.embed_texts(["c"])
        assert provider.calls == [["a", "b"], ["c"]]

    def test_each_call_returns_a_fresh_list(self) -> None:
        """A shared inner list would let one chunk's mutation reach every other."""
        provider = StaticEmbeddingProvider(dimensions=4)
        first = provider.embed_texts(["a"])[0]
        first[0] = 999.0
        assert provider.embed_texts(["b"])[0][0] == 1.0


class TestTheRequest:
    def test_it_posts_json_to_the_model_endpoint(self) -> None:
        transport = RecordingTransport(gemini_response(unit(768)))
        GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(["x"])

        request = transport.requests[0]
        assert request.get_method() == "POST"
        assert request.full_url == (
            "https://generativelanguage.googleapis.com/v1beta"
            "/models/text-embedding-004:batchEmbedContents"
        )
        assert (
            json.loads(body_of(request))["requests"][0]["content"]["parts"][0]["text"]
            == "x"
        )

    def test_the_api_key_is_a_header_not_a_query_parameter(self) -> None:
        """The load-bearing assertion of this class.

        A URL reaches proxy logs, access logs and tracebacks. A header does not. This
        fails loudly if the key is ever moved into the query string, which is how most
        Google API examples are written.
        """
        transport = RecordingTransport(gemini_response(unit(768)))
        GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(["x"])

        request = transport.requests[0]
        assert transport.header(0, GEMINI_AUTH_HEADER) == FAKE_KEY
        assert FAKE_KEY not in request.full_url
        assert "key=" not in request.full_url

    def test_the_key_never_appears_in_the_request_body(self) -> None:
        transport = RecordingTransport(gemini_response(unit(768)))
        GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(["x"])
        assert FAKE_KEY.encode() not in body_of(transport.requests[0])

    def test_a_trailing_slash_in_the_base_url_does_not_double_up(self) -> None:
        transport = RecordingTransport(gemini_response(unit(768)))
        settings = gemini_settings(gemini_api_base_url="https://example.test/v1beta/")
        GeminiEmbeddingProvider(settings, transport=transport).embed_texts(["x"])
        assert transport.requests[0].full_url == (
            "https://example.test/v1beta/models/text-embedding-004:batchEmbedContents"
        )

    def test_the_content_type_is_declared(self) -> None:
        transport = RecordingTransport(gemini_response(unit(768)))
        GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(["x"])
        assert transport.header(0, "Content-Type") == "application/json"

    def test_one_request_carries_every_text_in_order(self) -> None:
        texts = ["alpha", "beta", "gamma"]
        transport = RecordingTransport(
            gemini_response(unit(768), unit(768), unit(768))
        )
        GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(texts)

        body = json.loads(body_of(transport.requests[0]))
        assert [r["content"]["parts"][0]["text"] for r in body["requests"]] == texts

    def test_the_model_is_named_in_every_request(self) -> None:
        transport = RecordingTransport(gemini_response(unit(768), unit(768)))
        GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
            ["a", "b"]
        )
        body = json.loads(body_of(transport.requests[0]))
        assert all(r["model"] == "models/text-embedding-004" for r in body["requests"])

    def test_a_timeout_is_passed_to_the_transport(self) -> None:
        """Bounded, because ingestion is synchronous in this phase.

        An unbounded call holds a pooled database connection while it waits, so the
        request is not merely slow — it is holding a resource the whole application
        needs.
        """
        transport = RecordingTransport(gemini_response(unit(768)))
        GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(["x"])
        assert transport.timeouts[0] > 0
        assert math.isfinite(transport.timeouts[0])

    def test_unicode_text_is_sent_as_utf8(self) -> None:
        transport = RecordingTransport(gemini_response(unit(768)))
        GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
            ["café — naïve"]
        )
        assert "café — naïve" in json.loads(
            body_of(transport.requests[0]).decode()
        )["requests"][0]["content"]["parts"][0]["text"]


class TestTheResponse:
    def test_a_well_formed_response_is_returned_in_order(self) -> None:
        first, second = [1.0] + [0.0] * 767, [0.0, 1.0] + [0.0] * 766
        transport = RecordingTransport(gemini_response(first, second))
        vectors = GeminiEmbeddingProvider(
            gemini_settings(), transport=transport
        ).embed_texts(["a", "b"])
        assert vectors == [first, second]

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            (b"not json at all", "not JSON"),
            (b"[]", "not a JSON object"),
            (b"{}", "no 'embeddings' list"),
            (b'{"embeddings": "nope"}', "no 'embeddings' list"),
            (b'{"embeddings": [1]}', "not a JSON object"),
            (b'{"embeddings": [{}]}', "no 'values' list"),
            (b'{"embeddings": [{"values": "nope"}]}', "no 'values' list"),
            (b'{"embeddings": [{"values": ["a"]}]}', "non-numeric"),
            (b'{"embeddings": [{"values": [true, false]}]}', "boolean"),
        ],
    )
    def test_a_malformed_response_is_refused(self, body: bytes, reason: str) -> None:
        transport = RecordingTransport((200, body))
        with pytest.raises(EmbeddingError, match=reason):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a"]
            )

    def test_a_short_batch_is_refused_rather_than_partially_applied(self) -> None:
        """The misaligning failure, and the most important one in this class.

        Three vectors for four inputs, in order, is a perfectly well-formed response
        that a careless implementation would accept and then zip against the chunks —
        putting vector 3 on chunk 4, which belongs to a different sentence.
        """
        transport = RecordingTransport(gemini_response(unit(768), unit(768), unit(768)))
        with pytest.raises(EmbeddingError, match="3 vectors for 4 inputs"):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a", "b", "c", "d"]
            )

    def test_a_long_batch_is_refused_too(self) -> None:
        """Three vectors for two inputs. A short batch is the dangerous one, but the
        length is checked in both directions so a miscount cannot slip through
        whichever way it is wrong."""
        transport = RecordingTransport(
            gemini_response(unit(768), unit(768), unit(768))
        )
        with pytest.raises(EmbeddingError, match="3 vectors for 2 inputs"):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a", "b"]
            )

    def test_a_vector_of_the_wrong_width_is_refused(self) -> None:
        """Never truncated, never padded. See the module docstring.

        A truncated vector embeds a point in a different space from the one the
        collection was created for, and retrieval returns confident nonsense rather
        than an error.
        """
        transport = RecordingTransport(gemini_response([0.1] * 512))
        with pytest.raises(EmbeddingDimensionMismatch, match="512 wide"):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a"]
            )

    def test_the_mismatch_names_the_position_of_the_bad_vector(self) -> None:
        """So an operator can tell a systematic problem from one bad chunk."""
        transport = RecordingTransport(
            gemini_response(unit(768), [0.1] * 3, unit(768))
        )
        with pytest.raises(EmbeddingDimensionMismatch, match="vector 1"):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a", "b", "c"]
            )

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
    def test_a_non_finite_component_is_refused(self, value: str) -> None:
        """A NaN cannot be normalised and poisons every distance it takes part in.

        Worse, Qdrant indexes it without error — so without this check a NaN vector
        would be stored, and the damage would appear as permanently absent results
        rather than as a failed ingestion.
        """
        body = json.dumps(
            {"embeddings": [{"values": [float(value)] + [0.0] * 767}]}
        ).encode()
        transport = RecordingTransport((200, body))
        with pytest.raises(EmbeddingDimensionMismatch, match="non-finite"):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a"]
            )

    def test_a_dimension_mismatch_is_an_embedding_error(self) -> None:
        """One ``except`` clause for callers, while keeping the specific type.

        A caller that wants to distinguish a width problem from an outage can; a caller
        that does not need not write two handlers for one failure.
        """
        assert issubclass(EmbeddingDimensionMismatch, EmbeddingError)
        assert issubclass(EmbeddingProviderUnavailable, EmbeddingError)


class TestBatchLimits:
    def test_an_empty_batch_is_refused(self) -> None:
        transport = RecordingTransport()
        with pytest.raises(ValueError, match="at least one"):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts([])
        assert transport.request_count == 0, "no request should be made for no work"

    def test_the_batch_ceiling_is_refused_not_silently_split(self) -> None:
        """Refused, because the *caller* owns the split.

        Splitting here would hide a mis-configured batch size: the ingestion
        orchestrator batches so it can log and bound each request, and a provider that
        split behind its back would make its own accounting a lie.
        """
        transport = RecordingTransport()
        with pytest.raises(ValueError, match=f"at most {MAX_BATCH_SIZE}"):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["x"] * (MAX_BATCH_SIZE + 1)
            )
        assert transport.request_count == 0

    def test_the_batch_ceiling_itself_is_accepted(self) -> None:
        transport = RecordingTransport(
            gemini_response(*([unit(768)] * MAX_BATCH_SIZE))
        )
        vectors = GeminiEmbeddingProvider(
            gemini_settings(), transport=transport
        ).embed_texts(["x"] * MAX_BATCH_SIZE)
        assert len(vectors) == MAX_BATCH_SIZE


class TestFailures:
    @pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
    def test_a_non_200_is_unavailable_not_a_crash(self, status: int) -> None:
        """Ingestion records a failure and moves on; it must not raise past itself."""
        transport = RecordingTransport(
            (status, json.dumps({"error": {"message": "quota exceeded"}}).encode())
        )
        with pytest.raises(EmbeddingProviderUnavailable, match=str(status)):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a"]
            )

    def test_a_provider_error_message_is_surfaced(self) -> None:
        """The operator needs to know *why*. Bounded, and without the request."""
        transport = RecordingTransport(
            (429, json.dumps({"error": {"message": "Resource has been exhausted"}}).encode())
        )
        with pytest.raises(EmbeddingProviderUnavailable, match="exhausted"):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a"]
            )

    def test_an_unparsable_error_body_reports_the_status_only(self) -> None:
        """The raw body may quote the request, and the request is tenant content."""
        transport = RecordingTransport((500, b"<html>SECRET-CHUNK-TEXT</html>"))
        with pytest.raises(EmbeddingProviderUnavailable) as caught:
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a"]
            )
        assert "no reason given" in str(caught.value)
        assert "SECRET-CHUNK-TEXT" not in str(caught.value)

    def test_a_provider_error_message_is_bounded(self) -> None:
        """A provider returning a megabyte of text must not put it in a log record."""
        from app.rag.embeddings import MAX_PROVIDER_MESSAGE_CHARS

        transport = RecordingTransport(
            (400, json.dumps({"error": {"message": "x" * 100_000}}).encode())
        )
        with pytest.raises(EmbeddingProviderUnavailable) as caught:
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a"]
            )
        assert len(str(caught.value)) < MAX_PROVIDER_MESSAGE_CHARS + 200

    def test_an_http_error_is_unavailable(self) -> None:
        """``HTTPError`` is a *response*, so it carries a body worth reading."""
        error = urllib.error.HTTPError(
            "https://example.test/x", 429, "Too Many Requests", NO_HEADERS, io.BytesIO(
                json.dumps({"error": {"message": "slow down"}}).encode()
            )
        )

        def transport(
            request: urllib.request.Request, timeout: float
        ) -> tuple[int, bytes]:
            raise error

        with pytest.raises(EmbeddingProviderUnavailable, match="slow down"):
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a"]
            )

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (ConnectionRefusedError("nope"), "ConnectionRefusedError"),
            (OSError("nope"), "OSError"),
            (TimeoutError(), "TimeoutError"),
            (socket.gaierror("nope"), "gaierror"),
        ],
    )
    def test_a_url_error_reports_the_reason_class_not_its_text(
        self, reason: BaseException, expected: str
    ) -> None:
        """``URLError`` wraps a reason; the *reason* is what an operator needs.

        ``URLError`` is always the same regardless of whether the host does not
        resolve, the connection is refused, or the certificate is bad, so naming it
        would make every network failure look identical. Naming ``exc.reason``'s class
        separates them. Naming nothing and reporting only "could not be reached" would
        lose the distinction; printing the reason's *text* would risk echoing tenant
        content, because a transport can put anything in there.
        """
        provider = GeminiEmbeddingProvider(
            gemini_settings(),
            transport=_raise(urllib.error.URLError(reason)),
        )
        with pytest.raises(EmbeddingProviderUnavailable, match=expected):
            provider.embed_texts(["a"])

    def test_a_url_error_message_never_carries_the_document(self) -> None:
        provider = GeminiEmbeddingProvider(
            gemini_settings(),
            transport=_raise(urllib.error.URLError(OSError("CONFIDENTIAL-PAYLOAD"))),
        )
        with pytest.raises(EmbeddingProviderUnavailable) as caught:
            provider.embed_texts(["CONFIDENTIAL-PAYLOAD"])
        assert "CONFIDENTIAL" not in str(caught.value)

    def test_a_timeout_is_unavailable_and_says_how_long_it_waited(self) -> None:
        provider = GeminiEmbeddingProvider(
            gemini_settings(), transport=_raise(TimeoutError())
        )
        with pytest.raises(EmbeddingProviderUnavailable, match="did not answer"):
            provider.embed_texts(["a"])

    def test_a_missing_api_key_is_refused_before_any_request(self) -> None:
        """Failing locally is better than a 401, and cheaper than a round trip."""
        transport = RecordingTransport()
        settings = gemini_settings(gemini_api_key=None)
        with pytest.raises(EmbeddingProviderUnavailable, match="gemini_api_key"):
            GeminiEmbeddingProvider(settings, transport=transport).embed_texts(["a"])
        assert transport.request_count == 0

    def test_a_blank_api_key_is_refused_too(self) -> None:
        """Whitespace is not a key, and a blank one would produce a confusing 401."""
        transport = RecordingTransport()
        settings = gemini_settings(gemini_api_key="   ")
        with pytest.raises(EmbeddingProviderUnavailable, match="gemini_api_key"):
            GeminiEmbeddingProvider(settings, transport=transport).embed_texts(["a"])
        assert transport.request_count == 0


class TestSecretsAndDocumentsNeverLeak:
    """The API key is never leaked. Document text is leaked only where a third party
    put it there, and the mitigation is downstream — stated here rather than asserted
    away.

    The two prohibitions are not symmetric, and pretending they are would make this file
    assert something untrue:

    * **The API key is entirely this module's responsibility.** It is a secret *we*
      hold and *we* place in the request. Nothing in the request the module builds — URL,
      headers, body — may contain it, so it can reach no failure message and no cause
      chain. That is asserted absolutely, below.

    * **A provider's own error text is not.** Google is free to echo the request in
      ``error.message``, and :func:`app.rag.embeddings._provider_error_reason` surfaces
      that text (bounded to 500 characters) because an operator who is told "429" and
      nothing else cannot triage anything. A transport is equally free to put whatever
      it likes in a reason, and ``raise ... from exc`` keeps that reason in the
      ``__cause__`` chain, which is what a developer reads when the exception escapes.

      Rather than assert that this does not happen, this file asserts **where it is
      contained**, which is the real guarantee:

      * this module does not log, so nothing reaches a log handler — asserted below;
      * :func:`app.services.ingestion.ingest_document` catches
        :class:`~app.rag.embeddings.EmbeddingError`, so it never escapes a request; and
      * :func:`app.services.ingestion._failure_message` records the exception *class
        name alone* for ``EmbeddingError`` and its subclasses, so a provider's wording
        never reaches ``documents.error``. Asserted end-to-end in
        ``tests/test_ingestion.py::TestFailureRecording``.

      Scrubbing the cause chain here instead would mean discarding the one piece of
      diagnostic information that distinguishes a DNS failure from a refused connection
      — trading a real operational cost for a guarantee that is already made one layer
      up.
    """

    DOCUMENT = "CONFIDENTIAL-PAYLOAD-DO-NOT-LOG-9f3a"

    def _transports(self) -> list[EmbeddingTransport]:
        """One transport per way this client can fail.

        A ``RecordingTransport`` gets exactly one response, because each case here is a
        single ``embed_texts`` call and a second call would be an ``AssertionError``
        from the transport rather than the failure under test.
        """
        document = self.DOCUMENT
        return [
            _raise(urllib.error.URLError(OSError(document))),
            _raise(urllib.error.URLError(ConnectionRefusedError(document))),
            _raise(TimeoutError()),
            _raise(TimeoutError(document)),
            _raise(
                urllib.error.HTTPError(
                    "https://example.test/x",
                    500,
                    "Server Error",
                    NO_HEADERS,
                    io.BytesIO(document.encode()),
                )
            ),
            RecordingTransport((500, document.encode())),
            RecordingTransport((429, document.encode())),
            RecordingTransport((200, document.encode())),
            RecordingTransport((200, b"{}")),
            RecordingTransport(
                (400, json.dumps({"error": {"message": document}}).encode())
            ),
            RecordingTransport(
                (403, json.dumps({"error": {"message": document}}).encode())
            ),
        ]

    def _every_failure(self) -> list[EmbeddingError]:
        failures: list[EmbeddingError] = []
        for transport in self._transports():
            provider = GeminiEmbeddingProvider(gemini_settings(), transport=transport)
            with pytest.raises(EmbeddingError) as caught:
                provider.embed_texts([self.DOCUMENT])
            failures.append(caught.value)
        return failures

    def test_the_matrix_actually_exercises_the_failure_paths(self) -> None:
        """A guard on the guard.

        Without this, a change to the transport list that made every case *succeed*
        would turn the leak tests below into vacuous passes — and those are exactly the
        tests that must never silently stop testing anything.
        """
        failures = self._every_failure()
        assert len(failures) == len(self._transports())
        assert len({type(failure) for failure in failures}) >= 2, (
            "the matrix should reach more than one exception type to be worth having"
        )

    def test_the_api_key_is_in_no_failure_message(self) -> None:
        for failure in self._every_failure():
            assert FAKE_KEY not in str(failure), (
                f"{type(failure).__name__} echoed the API key back"
            )

    def test_the_api_key_is_in_no_chained_cause(self) -> None:
        """The cause chain too, because that is what a traceback prints.

        ``str(exception)`` walks no cause chain, so a message-only assertion would pass
        while the traceback a developer actually reads showed the secret.
        """
        for failure in self._every_failure():
            assert FAKE_KEY not in _rendered_chain(failure), (
                f"{type(failure).__name__} carries the API key in its __cause__ chain"
            )

    def test_the_api_key_is_in_no_request_artefact(self) -> None:
        """The root cause: nothing *this module builds* may carry it.

        Asserted over the URL and the body separately from the header, because the
        header is where it is supposed to be and the other two are where it must not
        be. A test that only checked ``full_url`` would miss a key in a query parameter
        added later, and one that only checked the absence from ``str(exc)`` would miss
        a key in a proxy log.
        """
        transport = RecordingTransport(gemini_response(unit(768)))
        GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(["x"])
        request = transport.requests[0]

        assert FAKE_KEY not in request.full_url
        assert FAKE_KEY not in body_of(request).decode()
        assert FAKE_KEY not in repr(request.full_url)
        assert FAKE_KEY in (transport.header(0, GEMINI_AUTH_HEADER) or "")

    def test_a_provider_error_message_is_bounded(self) -> None:
        """Surfaced, but never unbounded — it goes in a human-readable message."""
        from app.rag.embeddings import MAX_PROVIDER_MESSAGE_CHARS

        transport = RecordingTransport(
            (400, json.dumps({"error": {"message": "y" * 50_000}}).encode())
        )
        with pytest.raises(EmbeddingProviderUnavailable) as caught:
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                ["a"]
            )
        assert len(str(caught.value)) < MAX_PROVIDER_MESSAGE_CHARS + 200

    def test_an_error_body_that_quotes_the_request_reaches_only_the_exception(
        self,
    ) -> None:
        """The boundary, pinned deliberately so it cannot move unnoticed.

        A provider that echoes the request in ``error.message`` does put document text
        into the raised exception. That is accepted here for the reason in the class
        docstring, and the containment is asserted downstream rather than hoped for. If
        this test ever starts failing because the exception *stopped* containing the
        text, that is an improvement — update the docstring rather than the assertion.
        """
        transport = RecordingTransport(
            (400, json.dumps({"error": {"message": self.DOCUMENT}}).encode())
        )
        with pytest.raises(EmbeddingProviderUnavailable) as caught:
            GeminiEmbeddingProvider(gemini_settings(), transport=transport).embed_texts(
                [self.DOCUMENT]
            )
        assert self.DOCUMENT in str(caught.value)
        assert FAKE_KEY not in str(caught.value)
        assert FAKE_KEY not in _rendered_chain(caught.value)

    def test_the_ingestion_layer_records_the_class_name_alone(self) -> None:
        """The containment, checked here so it is not only checked elsewhere.

        ``tests/test_ingestion.py`` asserts this end-to-end through a real document. This
        is the narrow version, at the seam, because a leak here would be invisible in an
        end-to-end test that used a well-behaved provider.
        """
        from app.services.ingestion import IngestionStage, _failure_message

        leaky = EmbeddingProviderUnavailable(
            f"the embedding provider answered 400: {self.DOCUMENT}"
        )
        recorded = _failure_message(IngestionStage.EMBED, leaky)

        assert self.DOCUMENT not in recorded
        assert "EmbeddingProviderUnavailable" in recorded

    def test_nothing_is_logged_at_all(self) -> None:
        """Neither the document, the key, nor the endpoint reaches a log handler.

        Captured at ``DEBUG`` on the *root* logger, so a record emitted by this module,
        by :mod:`urllib`, or by any library in between is caught. Asserting over the
        module's own source for ``logger.`` instead would miss a record emitted
        indirectly, and would break on a harmless refactor that only renames a variable.

        Both a failing and a succeeding call are made, because the two could leak
        differently: the failure path through an exception message, the success path
        through nothing at all — and "nothing at all" is the claim worth checking.
        """
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        root = logging.getLogger()
        previous_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            for transport in (
                RecordingTransport((500, b"boom")),
                RecordingTransport(gemini_response(unit(768))),
                _raise(urllib.error.URLError(OSError(self.DOCUMENT))),
            ):
                provider = GeminiEmbeddingProvider(
                    gemini_settings(), transport=transport
                )
                try:
                    provider.embed_texts([self.DOCUMENT])
                except EmbeddingError:
                    pass
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        output = stream.getvalue()
        assert self.DOCUMENT not in output
        assert FAKE_KEY not in output
        assert "generativelanguage" not in output
        assert "text-embedding-004" not in output

    def test_the_capture_actually_captured_something(self) -> None:
        """A guard on the guard, again.

        If the handler were attached wrongly the test above would pass while capturing
        nothing at all — which is the most likely way for a "nothing leaked" test to
        become a false negative. So something is logged deliberately, through the same
        mechanism, and asserted to arrive.
        """
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        sentinel = logging.getLogger("tests.test_embeddings.sentinel")
        previous_level = sentinel.level
        sentinel.addHandler(handler)
        sentinel.setLevel(logging.DEBUG)
        try:
            sentinel.debug("MARKER-VISIBLE")
        finally:
            sentinel.removeHandler(handler)
            sentinel.setLevel(previous_level)

        assert "MARKER-VISIBLE" in stream.getvalue()


class TestDeterminismOfTheRequest:
    def test_the_same_batch_produces_the_same_request(self) -> None:
        transport = RecordingTransport(
            gemini_response(unit(768), unit(768)),
            gemini_response(unit(768), unit(768)),
        )
        first = GeminiEmbeddingProvider(gemini_settings(), transport=transport)
        second = GeminiEmbeddingProvider(gemini_settings(), transport=transport)

        first.embed_texts(["a", "b"])
        second.embed_texts(["a", "b"])

        assert body_of(transport.requests[0]) == body_of(transport.requests[1])
        assert transport.requests[0].full_url == transport.requests[1].full_url

    def test_the_provider_holds_no_per_batch_state(self) -> None:
        """Two batches in a row must not influence each other.

        The registry of parsers is shared and stateless for the same reason; a provider
        that accumulated its inputs would make the second vector of a batch depend on
        the first.
        """
        transport = RecordingTransport(
            gemini_response(unit(768)), gemini_response(unit(768))
        )
        provider = GeminiEmbeddingProvider(gemini_settings(), transport=transport)
        provider.embed_texts(["alpha"])
        provider.embed_texts(["beta"])

        bodies = [json.loads(body_of(request)) for request in transport.requests]
        assert [b["requests"][0]["content"]["parts"][0]["text"] for b in bodies] == [
            "alpha",
            "beta",
        ]


class TestWhatThePipelineNeeds:
    """What :mod:`app.services.ingestion` relies on, asserted as a contract.

    Not a duplicate of the tests above: these are the four things the orchestrator
    cannot work around, stated so that a change to the provider that breaks one of them
    is reported as *this* failure rather than as an ingestion mystery.
    """

    def test_the_width_is_declared_before_any_request(self) -> None:
        """The vector store must be able to create its collection before embedding.

        Reading ``dimensions`` before a request is what lets ingestion create a
        correctly-sized collection on the first document rather than discovering the
        width from the first response and failing if it differs.
        """
        transport = RecordingTransport()
        provider = GeminiEmbeddingProvider(gemini_settings(), transport=transport)
        assert provider.dimensions == 768
        assert provider.model_name == "text-embedding-004"
        assert transport.request_count == 0

    def test_the_declared_width_matches_what_the_settings_ask_for(self) -> None:
        for width in (384, 768, 1536):
            provider = GeminiEmbeddingProvider(
                gemini_settings(embedding_dimension=width), transport=RecordingTransport()
            )
            assert provider.dimensions == width

    def test_one_call_per_batch(self) -> None:
        """So a test can assert that batching happened by counting requests."""
        batch = ["a", "b", "c"]
        transport = RecordingTransport(
            *(gemini_response(*(unit(768) for _ in batch)) for _ in range(3))
        )
        provider = GeminiEmbeddingProvider(gemini_settings(), transport=transport)
        for _ in range(3):
            provider.embed_texts(batch)
        assert transport.request_count == 3, "three batches must mean three requests"

    def test_the_result_is_plain_floats(self) -> None:
        """Qdrant's client is typed for ``list[float]``; a JSON integer would surprise it.

        JSON has one number type, so ``[1, 0, 0]`` arrives as integers. Converting to
        ``float`` is what stops an integer vector from tripping a type check somewhere
        inside the vector store, far from the cause.
        """
        transport = RecordingTransport(gemini_response([1, 0, 0]))
        vectors = GeminiEmbeddingProvider(
            gemini_settings(embedding_dimension=3), transport=transport
        ).embed_texts(["a"])
        assert vectors == [[1.0, 0.0, 0.0]]
        assert all(isinstance(component, float) for component in vectors[0])


def _rendered_chain(failure: BaseException, *, limit: int = 10) -> str:
    """Everything about ``failure`` a traceback would show.

    The message, the ``repr``, and every argument, walked down the ``__cause__``
    chain. ``str(exception)`` alone walks none of that, so an assertion about leaks
    built on it would pass while the traceback a developer actually reads printed the
    secret.

    Bounded by ``limit`` so a self-referential cause chain cannot hang the suite. A
    stdlib chain is at most two deep; the bound is a guard, not a real limit.
    """
    rendered: list[str] = []
    seen = 0
    current: BaseException | None = failure
    while current is not None and seen < limit:
        rendered.extend([str(current), repr(current)])
        rendered.extend(str(argument) for argument in getattr(current, "args", ()))
        current = current.__cause__
        seen += 1
    return " ".join(rendered)


def _raise(exception: BaseException):
    """A transport that raises ``exception``.

    A factory rather than a lambda so each call site gets a *fresh* exception instance,
    which matters: a shared instance accumulates a ``__traceback__`` and re-raising it
    twice produces a traceback chain that makes the failure unreadable.
    """

    def transport(
        request: urllib.request.Request, timeout: float
    ) -> tuple[int, bytes]:
        raise exception

    return transport
