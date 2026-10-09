"""Embedding providers (PMP §18's "Embedding Model", PMP §23's Embeddings row).

The abstraction, and why there is one
-------------------------------------
PMP §23 names two options for this layer — "Sentence Transformers / API model" —
and this module is the seam between them. :class:`EmbeddingProvider` is that seam.
Adding a provider is one subclass plus one branch in :func:`build_embedding_provider`;
nothing in :mod:`app.services.ingestion` changes, and no caller names a concrete
class.

That matters most for the decision this phase actually made. The API-model option is
implemented here, because ``sentence-transformers`` requires ``torch``, which a
plain requirements file cannot pin to CPU-only wheels — so installing it resolves to
a ~2.5 GB CUDA download of PyTorch (see the note in ``requirements.txt``). A local
model is a deployment choice rather than a pipeline change, and that is exactly what
the abstraction is for.

Two properties every provider must have
---------------------------------------
**A declared width.** :attr:`EmbeddingProvider.dimensions` is the width the provider
will return. It is declared up front rather than discovered from the first response
because :class:`~app.storage.qdrant.QdrantVectorStore` creates a collection with a
fixed width, and a collection cannot be resized. Discovering the width after
creating the collection means a failed first ingestion and a manual repair.

**A width check on every vector.** :meth:`EmbeddingProvider.embed_texts` verifies
that what came back is the declared width, and refuses otherwise. This is not
defensive padding: an API provider that changes its default dimensionality in a
model update would otherwise return 1536-dim vectors into a 768-dim collection, and
Qdrant rejects those at the *upsert*, after the embedding bill has been paid. Catching
it at the provider boundary names the cause.

Nothing here is logged
----------------------
An API key is a credential and the text is untrusted tenant content (PMP §17). The
Gemini client therefore builds its ``Authorization`` header, passes it to
:func:`urllib.request.urlopen`, and does not retain, log or echo it; and the error
raised on a non-2xx response carries the provider's status code and the error
*reason*, never the request. ``tests/test_embeddings.py`` asserts both.

Why Gemini rather than OpenAI
-----------------------------
PMP §23 permits either ("Gemini / OpenAI") and the choice is a deployment decision,
so it lives in configuration (:attr:`Settings.embedding_provider`). Gemini is the
default because ``google-genai`` was the SDK this project had already recorded as
its Phase 4 dependency, which makes it the option with the least new machinery. The
transport is the **standard library** rather than that SDK: one HTTP POST with a JSON
body is not worth an SDK, and it keeps a second HTTP stack out of the requirements.
Revisit if a second provider is added and the hand-written transport stops being the
smaller option.

Why the REST API rather than ``google-genai``
---------------------------------------------
Two reasons, both about size rather than principle. The SDK is a much larger
dependency than the one endpoint used here, and it would pin a specific SDK release
to a specific embedding-model generation, so a model upgrade becomes a dependency
upgrade. The REST shape of ``batchEmbedContents`` has been stable for the model's
whole life. The cost of this decision is that a genuine API change surfaces as a
parse error at ingestion time rather than as a type error at compile time — which is
why the response handling below is strict about shape and says so.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from math import isfinite
from typing import Any, Final, Protocol

from app.core.config import Settings

#: How many chunks one provider call may be given.
#:
#: Passed explicitly rather than defaulted, so the caller — not this module — owns
#: the batching policy. :attr:`Settings.embedding_batch_size` is where that policy
#: is declared, and the pipeline reads it from there.
MAX_BATCH_SIZE: Final[int] = 100

#: The embeddings batch endpoint, relative to
#: :attr:`Settings.gemini_api_base_url`.
#:
#: The ``v1beta`` path segment is supplied by the configured base URL rather than
#: hard-coded here, so pointing the client at a different API version is a
#: configuration change.
GEMINI_EMBED_PATH: Final[str] = "/models/{model}:batchEmbedContents"

#: The header Gemini authenticates with.
#:
#: Named rather than inlined at the call site so a test can assert the header is
#: present and — more importantly — that the key never appears anywhere else.
GEMINI_AUTH_HEADER: Final[str] = "x-goog-api-key"

#: How long to wait on a single embedding request, in seconds.
#:
#: Not read from configuration: it is a property of the provider's own service level
#: rather than of this deployment, and a tenant cannot influence it. Bounded because
#: ingestion is synchronous in this phase and holds a database connection while it
#: waits (see :attr:`Settings.qdrant_timeout_seconds` for the same reasoning applied
#: to Qdrant).
EMBEDDING_TIMEOUT_SECONDS: Final[float] = 30.0

#: How much of a provider's error message is retained.
#:
#: Bounded because the message reaches ``documents.error``, which
#: ``GET /admin/documents/{id}/status`` returns. A provider that echoes a large
#: request would otherwise have an unbounded path into an administrator's browser.
#: The document column's own limit is 2000 characters; this is well inside it.
MAX_PROVIDER_MESSAGE_CHARS: Final[int] = 500


class EmbeddingError(RuntimeError):
    """An embedding could not be produced.

    A :class:`RuntimeError` because nothing about the *input* is necessarily wrong:
    a provider being unreachable, rate-limiting or returning a shape we do not
    understand are all operational conditions, not client errors. The orchestrator
    catches this and records the document as ``failed`` with the message, which is
    why :attr:`message` must never contain document text.
    """


class EmbeddingDimensionMismatch(EmbeddingError):
    """A provider returned a vector that is not the width it declared.

    Separate from :class:`EmbeddingError` so the orchestrator can tell "the provider
    is unreachable" from "the provider is answering, but at a different width than
    the collection was created with". The second is a configuration fault — a model
    or ``embedding_dimension`` change without recreating the collection — and it
    needs an operator rather than a retry.
    """


class EmbeddingProviderUnavailable(EmbeddingError):
    """The provider could not be reached, or refused the request."""


class EmbeddingTransport(Protocol):
    """The one HTTP call the Gemini client makes, as an injectable callable.

    A :class:`~typing.Protocol` rather than a base class because the production
    implementation is :func:`urllib.request.urlopen` — a stdlib function, which
    cannot be subclassed usefully — and because the tests supply a plain function
    rather than a mock object.

    Keeping the seam this narrow is the point: a test can drive the real client
    through a real request shape and a real response shape without a socket, so
    what is tested is the code that ships and not a reimplementation of it.
    """

    def __call__(
        self, request: urllib.request.Request, timeout: float
    ) -> tuple[int, bytes]: ...


@dataclass(frozen=True, slots=True)
class _BatchResponse:
    """One decoded ``batchEmbedContents`` response, validated.

    Parsing is separated from the request so the *shape* rules live in one place and
    can be tested against payloads rather than against HTTP. Every field is validated
    on construction; anything unexpected raises :class:`EmbeddingError` here rather
    than surfacing as a ``TypeError`` or an ``IndexError`` three frames deeper.
    """

    vectors: tuple[tuple[float, ...], ...]

    @classmethod
    def from_payload(cls, payload: Any, *, expected: int) -> _BatchResponse:
        """Validate a decoded Gemini response body.

        Args:
            payload: the decoded JSON body.
            expected: the number of vectors the request asked for.

        Returns:
            The validated response.

        Raises:
            EmbeddingError: the body is not shaped like a
                ``batchEmbedContents`` response.
        """
        if not isinstance(payload, dict):
            raise EmbeddingError("the embedding response was not a JSON object")
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list):
            raise EmbeddingError("the embedding response carried no 'embeddings' list")

        if len(embeddings) != expected:
            raise EmbeddingError(
                f"the provider returned {len(embeddings)} vectors for {expected} "
                "inputs; the response cannot be matched to the request"
            )

        vectors: list[tuple[float, ...]] = []
        for position, item in enumerate(embeddings):
            if not isinstance(item, dict):
                raise EmbeddingError(
                    f"embedding {position} was not a JSON object"
                )
            values = item.get("values")
            if not isinstance(values, list):
                raise EmbeddingError(
                    f"embedding {position} carried no 'values' list"
                )
            if not all(isinstance(number, (int, float)) for number in values):
                raise EmbeddingError(
                    f"embedding {position} held a non-numeric component"
                )
            # `bool` is a subclass of `int`, so `True` would otherwise pass the check
            # above and become the vector 1.0. Rejecting it here costs one line and
            # removes a class of silently-wrong vector.
            if any(isinstance(number, bool) for number in values):
                raise EmbeddingError(
                    f"embedding {position} held a boolean component"
                )
            vectors.append(tuple(float(number) for number in values))

        return cls(vectors=tuple(vectors))


class EmbeddingProvider(ABC):
    """Turns text into vectors of a declared width.

    An abstract class rather than a :class:`~typing.Protocol` because the factory
    constructs providers from a table, and — unlike :class:`EmbeddingTransport` — a
    provider is a real object with configuration and a lifecycle. Subclasses must
    be constructible from a :class:`~app.core.config.Settings` and nothing else.
    """

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """The width of every vector this provider returns.

        Declared before any request is made, so the vector store can create its
        collection with the right width the first time.
        """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The model identifier, recorded so an operator can tell which produced a vector."""

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: the chunks to embed. Untrusted tenant content — see the module
                docstring on what may not be logged.

        Returns:
            One vector per input, in the same order, each exactly
            :attr:`dimensions` wide. Never a shorter list than was asked for: a
            provider that cannot embed every input fails the whole batch rather
            than returning a partial result, because a partial result would silently
            misalign vectors with the chunks they belong to.

        Raises:
            EmbeddingError: the provider could not embed the batch, or answered with
                something that is not a list of vectors.
            EmbeddingDimensionMismatch: a vector came back at the wrong width.
        """


def _validate_vectors(
    vectors: list[list[float]], *, expected_count: int, expected_width: int
) -> list[list[float]]:
    """Check a provider's output shape, and return it unchanged.

    Shared by every provider so the rules are written once. Three checks, each
    catching a distinct way a batch can silently misalign with its inputs:

    * the wrong number of vectors — one per input, in order;
    * the wrong width — see :class:`EmbeddingDimensionMismatch`;
    * a non-finite component — a ``NaN`` or infinity cannot be normalised, poisons
      every distance computation it takes part in, and (worse for a vector store)
      indexes without error.

    :func:`math.isfinite` rather than ``number != number or number in (inf, -inf)``:
    the explicit NaN test is the idiom that reads as a comparison of a value with
    itself, which is what it is, and ``isfinite`` says what it means and also covers
    the infinities in the same pass.

    A :class:`ValueError` here is caught and re-raised as
    :class:`EmbeddingDimensionMismatch` by the caller, so callers only need to catch
    :class:`EmbeddingError`.
    """
    if len(vectors) != expected_count:
        raise ValueError(
            f"expected {expected_count} vectors, received {len(vectors)}"
        )
    for position, vector in enumerate(vectors):
        if len(vector) != expected_width:
            raise ValueError(
                f"vector {position} is {len(vector)} wide, expected {expected_width}"
            )
        if not all(isfinite(number) for number in vector):
            raise ValueError(f"vector {position} holds a non-finite component")
    return vectors


class GeminiEmbeddingProvider(EmbeddingProvider):
    """PMP §23's API option: Google's ``batchEmbedContents`` endpoint.

    Configuration comes from :class:`~app.core.config.Settings` and nowhere else —
    this module reads no environment variable, which is the same rule
    :mod:`app.core.config` exists to enforce (PMP §8, "Environment-based
    configuration").

    The API key is passed as a header rather than a query parameter, and only to
    :func:`urllib.request.urlopen`. It is never placed in a URL, a log record, an
    exception message or a return value, because a URL is the one thing here that
    routinely ends up in a proxy log or a traceback.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: EmbeddingTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport: EmbeddingTransport = transport or _urllib_transport

    @property
    def dimensions(self) -> int:
        return self._settings.embedding_dimension

    @property
    def model_name(self) -> str:
        return self._settings.embedding_model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts`` in one ``batchEmbedContents`` call.

        Raises:
            ValueError: ``texts`` is empty or larger than :data:`MAX_BATCH_SIZE`.
                Refused rather than split, so a caller cannot silently exceed the
                provider's per-request limit; :mod:`app.services.ingestion` does the
                splitting, using
                :attr:`Settings.embedding_batch_size`.
            EmbeddingError: the provider is unreachable, refused the request, or
                answered with an unrecognised shape.
            EmbeddingDimensionMismatch: the vectors were not
                :attr:`dimensions` wide.
        """
        if not texts:
            raise ValueError("embed_texts requires at least one text")
        if len(texts) > MAX_BATCH_SIZE:
            raise ValueError(
                f"a single embedding request may carry at most {MAX_BATCH_SIZE} "
                f"texts, received {len(texts)}"
            )

        status, body = self._request(self._payload(texts))
        if status != 200:
            raise EmbeddingProviderUnavailable(
                f"the embedding provider answered {status}: "
                f"{_provider_error_reason(body)}"
            )

        response = _BatchResponse.from_payload(_decode_json(body), expected=len(texts))
        try:
            return _validate_vectors(
                [list(vector) for vector in response.vectors],
                expected_count=len(texts),
                expected_width=self.dimensions,
            )
        except ValueError as exc:
            raise EmbeddingDimensionMismatch(str(exc)) from exc

    def _payload(self, texts: list[str]) -> dict[str, Any]:
        """The request body for ``texts``.

        ``taskType`` is left unset. Google supports ``RETRIEVAL_DOCUMENT`` and
        ``RETRIEVAL_QUERY`` with different training, which is the *correct*
        configuration for RAG — but retrieval is Phase 2.6, and a phase that has not
        built retrieval should not be emitting query-shaped vectors it cannot yet
        justify. Recorded here so the omission is a decision rather than an oversight.
        """
        return {
            "requests": [
                {
                    "model": f"models/{self.model_name}",
                    "content": {"parts": [{"text": text}]},
                }
                for text in texts
            ]
        }

    def _request(self, payload: dict[str, Any]) -> tuple[int, bytes]:
        """POST ``payload`` to the configured model endpoint.

        Raises:
            EmbeddingProviderUnavailable: the request could not be made, or the
                provider answered with a non-2xx status. The API key is not in the
                message — only the status and the provider's own reason text.
        """
        api_key = self._settings.gemini_api_key
        if api_key is None or not api_key.get_secret_value().strip():
            raise EmbeddingProviderUnavailable(
                "gemini_api_key is not configured; the Gemini embedding provider "
                "cannot authenticate"
            )

        url = (
            f"{self._settings.gemini_api_base_url.rstrip('/')}"
            f"{GEMINI_EMBED_PATH.format(model=self.model_name)}"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                GEMINI_AUTH_HEADER: api_key.get_secret_value(),
            },
        )
        try:
            return self._transport(request, EMBEDDING_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            # `HTTPError` is a response, not a transport failure, so it carries a
            # body worth reading. The reason is extracted below; the URL is not,
            # because the key is a header here but the URL is what a traceback would
            # print and what a proxy would log.
            raise EmbeddingProviderUnavailable(
                f"the embedding provider answered {exc.code}: "
                f"{_provider_error_reason(exc.read())}"
            ) from exc
        except urllib.error.URLError as exc:
            raise EmbeddingProviderUnavailable(
                f"the embedding provider could not be reached "
                f"({type(exc.reason).__name__})"
            ) from exc
        except TimeoutError as exc:
            raise EmbeddingProviderUnavailable(
                "the embedding provider did not answer within "
                f"{EMBEDDING_TIMEOUT_SECONDS:g}s"
            ) from exc


class StaticEmbeddingProvider(EmbeddingProvider):
    """A provider that returns a fixed vector for every input.

    Exists for two purposes, and it is honest about both because its name is not
    "mock":

    1. **Tests.** Every test in this project that needs a provider uses this one. It
       is deterministic, needs no API key and makes no network call, so the suite's
       embedding assertions are about *this project's* logic rather than about a
       vendor's availability. It is a genuine :class:`EmbeddingProvider`, so a test
       that swaps it in is exercising the real abstraction rather than a mock that
       satisfies nothing.
    2. **Nothing else.** It is reachable from production configuration only by
       setting ``EMBEDDING_PROVIDER=static``, and it produces vectors with no
       semantic content whatsoever — every text maps to the same point, so retrieval
       returns the first chunk of the first document for any query. It exists so the
       pipeline can be exercised end-to-end without an external dependency, and
       ``tests/test_config.py`` asserts that ``app_env=production`` refuses it for
       exactly that reason.

    The single vector is a unit vector along the first axis rather than an array of
    ones, so the value that reaches Qdrant is a real, normalisable point.
    """

    def __init__(self, *, dimensions: int) -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be at least 1")
        self._dimensions = dimensions
        self._vector = [1.0] + [0.0] * (dimensions - 1)
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return "static-test-vectors"

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Return one copy of the fixed vector per input.

        Records what it was asked for in :attr:`calls`, which is what lets a test
        assert that batching actually happened without a network call to count.
        """
        if not texts:
            raise ValueError("embed_texts requires at least one text")
        self.calls.append(list(texts))
        return [list(self._vector) for _ in texts]


def build_embedding_provider(
    settings: Settings, *, transport: EmbeddingTransport | None = None
) -> EmbeddingProvider:
    """Return the provider :attr:`Settings.embedding_provider` names.

    The only place a concrete provider class is named. A new provider is one branch
    here and one subclass of :class:`EmbeddingProvider`; no caller changes.

    Args:
        settings: the application configuration. Nothing else is read.
        transport: overrides the HTTP transport, for tests. Never passed by
            application code.

    Raises:
        EmbeddingError: the configured provider name has no implementation. Should
            be unreachable — :attr:`Settings.embedding_provider` is a ``Literal``,
            so pydantic rejects an unknown name at construction. The branch is
            total anyway so a widening of the ``Literal`` fails loudly here rather
            than falling through to a ``None`` that a caller would treat as a
            provider.

        EmbeddingProviderUnavailable: the chosen provider has no credential to
            authenticate with. Checked here rather than in
            :meth:`Settings._validate_production_settings` — see the note in that
            validator — and checked here rather than left to the first HTTP call so
            that a misconfigured deployment is told once, naming the variable to set,
            rather than once per chunk of every document it fails to ingest.
    """
    match settings.embedding_provider:
        case "gemini":
            key = settings.gemini_api_key
            if key is None or not key.get_secret_value().strip():
                raise EmbeddingProviderUnavailable(
                    "gemini_api_key is not set, so the Gemini embedding provider "
                    "cannot authenticate. Set it from the environment; it is read "
                    "here and nowhere else."
                )
            return GeminiEmbeddingProvider(settings, transport=transport)
        case "static":
            return StaticEmbeddingProvider(dimensions=settings.embedding_dimension)
        case _:
            raise EmbeddingError(
                f"no embedding provider is implemented for "
                f"{settings.embedding_provider!r}"
            )


def _urllib_transport(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """The production transport: one POST, returning ``(status, body)``.

    Wrapped in a function rather than used as :func:`urllib.request.urlopen`
    directly so that the *signature* the provider depends on is explicit and so a
    test can substitute it without caring that one of them returns a response object
    and the other returns a tuple.
    """
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.status
        body = response.read()
    return int(status), body


def _decode_json(body: bytes) -> Any:
    """Decode a JSON body, or raise :class:`EmbeddingError`.

    Decoding failures are a provider fault, not a bug in this module's request, so
    they are reported as such — and the body is deliberately *not* included, because
    a provider's error page can echo the request, and the request is tenant content.
    """
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise EmbeddingError(
            f"the embedding provider returned a body that is not JSON "
            f"({type(exc).__name__})"
        ) from exc


def _provider_error_reason(body: bytes) -> str:
    """Extract a provider's error reason, bounded and without the request.

    Google's error envelope puts a human-readable ``error.message`` inside
    ``error``. When it is absent or unparsable, the status is all that is reported —
    the raw body is not, because a provider's error page may quote what was sent,
    and what was sent is a tenant's document.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "no reason given"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:MAX_PROVIDER_MESSAGE_CHARS]
            status = error.get("status")
            if isinstance(status, str):
                return status[:MAX_PROVIDER_MESSAGE_CHARS]
    return "no reason given"
