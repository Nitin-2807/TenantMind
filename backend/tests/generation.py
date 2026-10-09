"""Shared test doubles for the generation boundary.

Two objects, and why both exist
-------------------------------
:func:`app.rag.generation` is an HTTP client that reads untrusted text. Testing it
has the same trap as testing any HTTP client: a mocked client proves the mock was
called, while a stubbed *transport* proves the real client builds the right URL, sends
the right headers, builds the right body, and handles what comes back. So the
generation tests stub :data:`app.rag.generation.GenerationTransport` — one callable —
and drive the real :class:`~app.rag.generation.GeminiGenerationProvider` through it.
:class:`RecordingTransport` is that seam.

:class:`FakeGenerationProvider` is the other half, and it is deliberately not a
``Mock``. It implements the real
:class:`~app.rag.generation.GenerationProvider` ABC, so a test that substitutes it is
exercising the abstraction the application depends on rather than a stand-in that
satisfies nothing — which is what makes it useful to the query layer as well as to
these tests.

Both are collected here rather than defined per test file because the layer above this
one needs them too: a query service should be testable against a provider that returns
a known answer and records what it was asked, with no network and no key.
"""

from __future__ import annotations

import json
import urllib.request

from app.rag.generation import GenerationProvider, GenerationTransport

#: What :class:`FakeGenerationProvider` answers when a test does not set one.
#:
#: A fixed string rather than an empty one, because an empty answer is a value the
#: real provider refuses to return — so a double that produced one would let a test
#: pass against a shape production can never produce.
DEFAULT_ANSWER = "A grounded answer."

#: The model name :class:`FakeGenerationProvider` reports.
#:
#: Distinctive rather than plausible, so an assertion that a real provider was *not*
#: used cannot pass by accident on a familiar model name.
FAKE_MODEL_NAME = "fake-generation-model"


class FakeGenerationProvider(GenerationProvider):
    """Deterministic, offline :class:`~app.rag.generation.GenerationProvider`.

    Exists only for tests and makes no network call. A genuine implementation of the
    ABC rather than a ``Mock``, so code under test depends on the interface it
    depends on in production.

    Every call is recorded in :attr:`calls` as a dict with the keys ``instructions``,
    ``question`` and ``evidence``, and :meth:`generate` returns :attr:`answer`. That
    combination is what lets a caller assert *what was grounded on* — the one question
    this layer exists to answer — without a provider in the room.

    Attributes:
        answer: what :meth:`generate` returns. Set it per test.
        fail_with: an exception instance for :meth:`generate` to raise instead of
            returning, or ``None``. Set when the behaviour under test is how the
            caller handles a provider failure.
    """

    def __init__(
        self, *, answer: str = DEFAULT_ANSWER, fail_with: BaseException | None = None
    ) -> None:
        self.answer = answer
        self.fail_with = fail_with
        self.calls: list[dict[str, str]] = []

    @property
    def model_name(self) -> str:
        return FAKE_MODEL_NAME

    def generate(self, *, instructions: str, question: str, evidence: str) -> str:
        """Record the call, then return :attr:`answer` or raise :attr:`fail_with`.

        Raises:
            BaseException: whatever :attr:`fail_with` holds, re-raised so the test
                sees the failure it staged rather than an ``AssertionError`` from the
                double.
        """
        self.calls.append(
            {"instructions": instructions, "question": question, "evidence": evidence}
        )
        if self.fail_with is not None:
            raise self.fail_with
        return self.answer


class RecordingTransport:
    """A transport that records requests and replays a canned response.

    A class rather than a closure so the recorded requests survive as attributes the
    caller can inspect afterwards. That is the whole point of the double: the
    assertions that matter — the key is a header and not a query parameter, the
    instructions are in ``systemInstruction``, the evidence reached the request at
    all — are all assertions about a request that must be inspected afterwards.

    Constructed with no responses, it raises ``AssertionError`` on any call, which is
    how a test asserts that a provider refused locally and made no request.
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
            raise AssertionError(
                "the provider made a request the test did not allow; either the "
                "provider should have refused locally, or the test needs a response"
            )
        return self._responses.pop(0)

    @property
    def request_count(self) -> int:
        return len(self.requests)

    def header(self, index: int, name: str) -> str | None:
        """The value of header ``name`` on request ``index``, or ``None``.

        Looked up case-insensitively because :meth:`urllib.request.Request.
        header_items` capitalises header names, so a test that hard-coded
        ``"x-goog-api-key"`` would pass or fail depending on Python's capitalisation
        rules rather than on whether the header was sent at all.
        """
        target = name.lower()
        for key, value in self.requests[index].header_items():
            if key.lower() == target:
                return value
        return None


#: How many responses :func:`fake_transport` will answer. Large rather than one, so
#: that a loop in a test cannot exhaust it and turn a passing assertion into an
#: ``AssertionError`` about the double.
_UNBOUNDED_CALLS = 64


def fake_transport(payload: bytes, status: int = 200) -> GenerationTransport:
    """A transport that answers every call with ``(status, payload)``.

    Answers every call rather than one, because a caller that has to keep count of how
    many times it was invoked has to be written around this double — and a test whose
    assertion is "the provider asked twice" should read as two requests, not as a
    failed expectation. Construct :class:`RecordingTransport` directly when the number
    of requests is itself the thing under test.

    Returns the instance rather than a bare closure so the caller can still reach
    :attr:`RecordingTransport.requests`; a closure would have to smuggle the list out
    through a default argument, which hides it from the type checker and from the
    reader.

    Args:
        payload: the response body to return.
        status: the status code to return. Defaults to 200.
    """
    return RecordingTransport(*((status, payload) for _ in range(_UNBOUNDED_CALLS)))


class Raw:
    """Marks a value that :func:`gemini_answer` must place **verbatim**.

    A test needs this when it wants a malformed part in a response body — a bare string
    where Gemini sends ``{"text": ...}`` — and :func:`gemini_answer` would otherwise wrap
    every ``str`` into the well-formed shape and quietly turn the malformed case into a
    valid one. The test then passes without ever exercising the skip path it is named for.
    """

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value


def gemini_answer(*parts: object) -> bytes:
    """A well-formed ``generateContent`` 200 body carrying ``parts`` as text.

    ``parts`` is typed :class:`object` on purpose: a test that needs to assert the
    provider *skips* a non-text part has to be able to build one, and a helper typed
    ``*parts: str`` would put a cast in the test that is trying to prove no cast is
    needed.

    Args:
        parts: the values to place in ``candidates[0].content.parts[*].text``. A
            :class:`str` becomes ``{"text": ...}``; anything else is placed verbatim.

    The ``str`` convenience is why :class:`Raw` exists. Wrapping strings is what lets the
    ordinary tests read ``gemini_answer(ANSWER)`` instead of ``gemini_answer({"text":
    ANSWER})``, but it also means this helper **cannot** produce a part that is a bare
    string in the array — which is exactly the shape a test asserting "a part that is not a
    ``{"text": str}`` object is skipped" needs to build. A ``str`` wrapped in :class:`Raw`
    is placed verbatim, so both are expressible and neither is silently rewritten into the
    other. Without it that test would appear to pass while asserting nothing: the helper
    would have turned the malformed part into a well-formed one.
    """
    encoded = [
        part.value
        if isinstance(part, Raw)
        else ({"text": part} if isinstance(part, str) else part)
        for part in parts
    ]
    body: dict[str, object] = {
        "candidates": [{"content": {"role": "model", "parts": encoded}}]
    }
    return json.dumps(body).encode("utf-8")
