"""Grounded answer generation — PMP §18's "Chat / Generation Model".

The one place tenant text leaves the process
-------------------------------------------
Every other stage of this pipeline is deterministic and local: parsing, cleaning,
chunking and embedding are pure functions of what was uploaded, and their inputs and
outputs are this project's own objects. This module is the first and only place a
tenant's words are handed to something outside the deployment. That one fact decides
the shape of everything below — the transport is one hand-written POST rather than an
SDK with its own retry, logging and telemetry defaults; every failure message is
composed here rather than reused from whatever the HTTP client raised; and nothing
here logs at all.

Three inputs, and why they are three
-----------------------------------
:meth:`GenerationProvider.generate` takes ``instructions``, ``question`` and
``evidence`` separately rather than one assembled ``prompt: str``. The reason is that
the three have different provenance and therefore different rules:

* ``instructions`` is authored by this project. It is the only part of the request
  this project fully controls, and it is what tells the model to answer from the
  evidence and to say so when the evidence does not contain the answer.
* ``question`` is the asker's own text. It is still not logged.
* ``evidence`` is untrusted, retrieved tenant content (PMP §17). It is the injection
  surface, and it is the reason ``instructions`` is a separate parameter at all: a
  caller cannot accidentally teach the model something by concatenating a document
  into its own instructions, because this module decides which field the document
  goes into.

Accepting a pre-rendered prompt would make all three rules unenforceable here. A
caller could put a document anywhere in it and this module would have no way to tell
which part was which, so the "the instructions are never derived from document
content" invariant would be a convention rather than a type. The request format is
therefore decided here, where it can be asserted, and a caller that wants a different
one adds a method here rather than reaching around it.

Nothing here is logged
----------------------
There is no ``_logger`` in this module, and that is a decision rather than an
omission. Every string that would be worth logging at this boundary is the API key,
the retrieved document, the question, or the model's answer — and the answer is
untrusted content too, because it is the model's rendering of untrusted content. The
only genuinely safe line is "generation failed, here is the status and a bounded
provider reason", and that belongs to the caller, in the caller's own vocabulary.

So the two places a provider's own words could escape are handled explicitly.
:func:`_provider_error_reason` reads ``error.message`` because an operator told only
"429" cannot triage anything, and bounds it, because a provider error page may quote
what was sent and what was sent is a tenant's document. :func:`_decode_json` never
puts a body in a message at all. ``tests/test_generation.py`` asserts the absence of
every sentinel.

Why no SDK, and why the standard library
----------------------------------------
No generation SDK is installed, deliberately. One JSON POST does not need one, and an
SDK would bring retry policies, log records and usage metadata that all have to be
audited for leakage before they can be trusted with tenant content — machinery that
the ~40 lines below express with every byte visible. ``google-genai`` is recorded in
the requirements file for the *embedding* row and is used there;
:mod:`app.rag.embeddings` does not use it either. Nothing in this package imports a
provider SDK.

The transport is a one-call callable — :data:`GenerationTransport` — for the same
reason :data:`app.rag.embeddings.EmbeddingTransport` is one: a test can then drive
the real client through the real request and response shapes with no socket, so what
is tested is the code that ships rather than a reimplementation of it.

Every request carries a timeout
-------------------------------
:data:`GENERATION_TIMEOUT_SECONDS`, never ``None``. Generation is slower than
embedding — a model reads the whole context and then writes prose — but it is still a
bounded call made from a request-handling thread. An unbounded one holds a pooled
database connection while a provider that was never going to answer decides that,
which is worse than a slow answer: it is an unavailable application.

Why the temperature is zero
---------------------------
A grounded answer should be the one the evidence supports, not a sample from a
distribution over phrasings. Non-zero temperature on *this* call buys nothing and
costs reproducibility: the same question over the same evidence must not become a
differently-worded non-answer on a retry, because a user cannot tell the difference
between a model that hedged and a model that was asked twice.

Two exception types, one failure
-------------------------------
A caller that only wants to answer a question needs one ``except GenerationError``.
:class:`GenerationProviderUnavailable` exists *inside* that so a caller that can act
differently — retry, raise a service error, degrade — can, without the common case
needing two handlers.

A malformed 2xx body is a :class:`GenerationError` and never a bare ``KeyError``,
``TypeError`` or ``IndexError``. It is the provider's fault rather than the caller's,
and letting an ``IndexError`` escape from three frames below would report a vendor
surprise as a programming bug.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from app.core.config import Settings

#: The `generateContent` endpoint, relative to :attr:`Settings.gemini_api_base_url`.
#:
#: The ``v1beta`` path segment comes from the configured base URL rather than being
#: hard-coded here, so moving the client to another API version is a configuration
#: change. `:generateContent` is used rather than `:streamGenerateContent` because a
#: streamed answer cannot be returned as a single string, and this boundary's whole
#: contract is "one call, one answer".
GEMINI_GENERATE_PATH: Final[str] = "/models/{model}:generateContent"

#: The header Gemini authenticates with.
#:
#: Duplicated from :data:`app.rag.embeddings.GEMINI_AUTH_HEADER` rather than imported,
#: so the two modules have no import edge and this one stays readable on its own. It
#: is named rather than inlined so a test can assert the header is present *and* that
#: the key appears nowhere else — a URL is the one artefact here that routinely
#: reaches a proxy access log or a traceback.
GEMINI_AUTH_HEADER: Final[str] = "x-goog-api-key"

#: How long to wait on a single generation request, in seconds.
#:
#: Not read from configuration: it is a property of this provider's service level
#: rather than of a deployment, and a tenant cannot influence it. Larger than
#: :data:`app.rag.embeddings.EMBEDDING_TIMEOUT_SECONDS` because generation reads the
#: retrieved context and writes prose, and bounded because the caller is a request
#: handler. 60s is beyond any honest grounded answer and comfortably inside the
#: window in which a client will still be waiting.
GENERATION_TIMEOUT_SECONDS: Final[float] = 60.0

#: How much of a provider-supplied message is retained.
#:
#: Bounded because this text reaches an exception message that a caller may persist
#: or return, and a provider is free to send as much as it likes. The value matches
#: :data:`app.rag.embeddings.MAX_PROVIDER_MESSAGE_CHARS` so the two providers cannot
#: drift apart in how much third-party text they are willing to pass along.
MAX_PROVIDER_MESSAGE_CHARS: Final[int] = 500

#: What replaces a credential a provider echoed back to us.
#:
#: Not an empty string, because "the provider said ``... your key AIza... was
#: rejected``" and "the provider said ``... your key  was rejected``" are different
#: operator experiences, and the shape of the redaction tells whoever reads it that
#: something was removed.
REDACTED: Final[str] = "<redacted>"

#: The shortest run of key characters that counts as a leak.
#:
#: Used by :func:`_scrub` to decide whether a truncation cut landed inside the key. Eight
#: characters is chosen for two reasons that pull in opposite directions: long enough that
#: it cannot plausibly appear by chance in a provider's English error prose, and short
#: enough that no plausible API key is missed. A key shorter than this cannot be
#: distinguished from ordinary text at all, which is why the constant is a floor on the
#: *fragment* rather than on the key.
LEAK_FRAGMENT_CHARS: Final[int] = 8

#: The one HTTP call this module makes, as an injectable callable.
#:
#: A :data:`~collections.abc.Callable` alias rather than a :class:`~typing.Protocol`
#: because the production implementation is :func:`urllib.request.urlopen` — a stdlib
#: function, which cannot be subclassed usefully — and because the tests supply a
#: plain function or a callable object rather than a mock. Keeping the seam this
#: narrow is the point: a test can drive the real client through a real request shape
#: and a real response shape without a socket.
GenerationTransport = Callable[[urllib.request.Request, float], tuple[int, bytes]]


class GenerationError(RuntimeError):
    """No answer could be produced.

    A :class:`RuntimeError` because nothing about the *input* is necessarily wrong:
    a provider being unreachable, rate-limiting, or returning a shape we do not
    understand are all operational conditions rather than client errors. The message
    is the only thing a caller keeps, so it must never contain the API key, the
    question, the evidence or the answer.
    """


class GenerationProviderUnavailable(GenerationError):
    """The provider could not be reached, or answered with a failure.

    Separate from :class:`GenerationError` so a caller can tell "the provider is
    down" from "the provider answered, and what it said was not an answer" — the
    first is worth retrying and the second is not. Callers that do not care need
    only catch the parent.
    """


@dataclass(frozen=True, slots=True)
class _GenerateResponse:
    """One decoded ``generateContent`` response, validated.

    Parsing is separated from the request so the shape rules live in one place and
    can be tested against payloads rather than against HTTP. Everything is validated
    on construction, so an unexpected shape raises :class:`GenerationError` here
    rather than surfacing as a ``TypeError`` or an ``IndexError`` three frames
    deeper, where it would read as a bug in this project rather than as a surprise
    from a vendor.
    """

    text: str

    @classmethod
    def from_payload(cls, payload: Any) -> _GenerateResponse:
        """Validate a decoded Gemini response body.

        Args:
            payload: the decoded JSON body.

        Returns:
            The validated response, carrying the concatenated answer text.

        Raises:
            GenerationError: the body is not shaped like a ``generateContent``
                response, or is shaped like one that carries no text.
        """
        if not isinstance(payload, dict):
            raise GenerationError("the generation response was not a JSON object")
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            raise GenerationError(
                "the generation response carried no 'candidates' list"
            )
        if not candidates:
            # Gemini returns an empty list when a prompt is refused or filtered,
            # often with a `promptFeedback` block explaining why. Saying so is more
            # use to an operator than "the response was empty", and the feedback
            # block itself is not surfaced because it can quote the request.
            raise GenerationError(
                "the generation response carried no candidates; the request may "
                "have been refused or filtered before any answer was produced"
            )

        candidate = candidates[0]
        if not isinstance(candidate, dict):
            raise GenerationError("candidate 0 was not a JSON object")
        content = candidate.get("content")
        if not isinstance(content, dict):
            raise GenerationError("candidate 0 carried no 'content' object")
        parts = content.get("parts")
        if not isinstance(parts, list):
            raise GenerationError("candidate 0 carried no 'parts' list")

        # A part is a `{"text": ...}` object in the ordinary case, but Gemini also
        # returns non-text parts — inline data, and reasoning traces on models that
        # expose them — and a model under development has been seen to return a bare
        # string. Skipping anything that is not a text part keeps a valid answer
        # readable when it arrives next to a part we do not consume, and the
        # emptiness check below is what stops that leniency from turning a
        # text-free response into an empty answer.
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)

        answer = "".join(texts)
        if not answer.strip():
            raise GenerationError(
                "candidate 0 held no answer text; a candidate with nothing in it is "
                "not an answer a caller can use, and returning an empty string "
                "would look like a successful, unhelpful answer"
            )
        return cls(text=answer)


class GenerationProvider(ABC):
    """Turns a question and retrieved evidence into an answer.

    An abstract class rather than a :class:`~typing.Protocol` because the factory
    constructs providers from a table and — unlike :data:`GenerationTransport` — a
    provider is a real object with configuration and a lifecycle. Subclasses must be
    constructible from a :class:`~app.core.config.Settings` and nothing else.
    """

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The concrete model this provider calls.

        Recorded so an operator reading an answer, a log line or a request id can
        tell which model produced it — the question "why did it say that" usually
        turns out to be "which model said that".
        """

    @abstractmethod
    def generate(self, *, instructions: str, question: str, evidence: str) -> str:
        """Return the model's answer text.

        Args:
            instructions: application-authored system instructions. NEVER derived
                from document content.
            question: the user's question.
            evidence: the retrieved, untrusted chunk text.

        Returns:
            The answer. Never empty and never ``None``: a response that carried no
            text is a :class:`GenerationError` rather than an empty string, because
            an empty string is indistinguishable at the call site from a model that
            legitimately declined to answer.

        Raises:
            GenerationError: the provider answered 2xx with a body this module
                cannot turn into answer text.
            GenerationProviderUnavailable: the provider could not be reached, timed
                out, or answered with a non-2xx status.
        """


class GeminiGenerationProvider(GenerationProvider):
    """PMP §23's API option: Google's ``generateContent`` endpoint.

    Configuration comes from :class:`~app.core.config.Settings` and nowhere else,
    which is the same rule :mod:`app.core.config` exists to enforce (PMP §8,
    "Environment-based configuration"). This module reads no environment variable.

    The API key is passed as a header and only to the transport. It is never placed
    in a URL, a log record, a return value, or an exception message — and, unlike
    :mod:`app.rag.embeddings`, it is also *stripped out of* a provider's error text
    before that text enters a message. See :func:`_provider_error_reason`: the API
    key is the one secret this module holds in full, so it is the one secret it can
    guarantee never to repeat.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: GenerationTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport: GenerationTransport = transport or _urllib_transport

    @property
    def model_name(self) -> str:
        return self._settings.llm_model

    def generate(self, *, instructions: str, question: str, evidence: str) -> str:
        """Answer ``question`` from ``evidence``.

        ``evidence`` is not checked for relevance, and an empty one is not refused
        here. Deciding whether the retrieved text can answer the question belongs to
        the layer that retrieved it — it knows the similarity scores, and this module
        does not. Refusing locally would mean a second, worse policy living at the
        wrong boundary, and would turn a legitimate "I found nothing relevant" into a
        provider failure.

        Raises:
            GenerationError: the provider answered 2xx with a body this module
                cannot turn into answer text.
            GenerationProviderUnavailable: the provider could not be reached, timed
                out, or answered with a non-2xx status. The message carries the
                status and a bounded provider reason — never the request, which is
                the tenant's document.
        """
        status, body = self._request(
            self._payload(instructions=instructions, question=question, evidence=evidence)
        )
        if not 200 <= status < 300:
            # The status range rather than `!= 200`: a 2xx that is not 200 is still a
            # success, and turning Google's next informational status into a
            # `GenerationError` would be this module guessing at a contract it does
            # not have.
            raise GenerationProviderUnavailable(
                f"the generation provider answered {status}: {self._reason(body)}"
            )
        return _GenerateResponse.from_payload(_decode_json(body)).text

    def _payload(
        self, *, instructions: str, question: str, evidence: str
    ) -> dict[str, Any]:
        """The request body.

        ``systemInstruction`` is camelCase because that is the REST field name; the
        snake_case spelling is silently ignored by the API, which would turn the
        application's own instructions into nothing and leave the model answering an
        ungrounded question that still *looks* grounded because the evidence was
        sent.

        The question and the evidence share one user part, question first and
        evidence after a blank line, rather than being two parts. Two parts would
        invite a model to treat the second as a separate instruction turn, and the
        ordering is what makes "answer only from the text below this line" mean
        something to the model. It is deliberately *not* fenced or XML-tagged here:
        the fencing instruction belongs to the caller's ``instructions`` — this
        project decides its prompt policy, this module only transmits it.

        ``generationConfig.temperature`` is ``0.0`` for grounded answering: the answer
        should be the one the evidence supports, not a sample over phrasings, and a
        retry of the same question must not produce a differently-worded non-answer.
        See the module docstring for the longer argument.
        """
        return {
            "systemInstruction": {"parts": [{"text": instructions}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": f"{question}\n\n{evidence}"}],
                }
            ],
            "generationConfig": {"temperature": 0.0},
        }

    def _secret(self) -> str:
        """The configured API key, or an empty string when there is none.

        Separated from :meth:`_request` because two callers need it — the one that
        authenticates, and the one that redacts — and a second read of the
        ``SecretStr`` should not be a second place where the emptiness check could
        be forgotten.
        """
        key = self._settings.gemini_api_key
        return "" if key is None else key.get_secret_value()

    def _reason(self, body: bytes) -> str:
        """The provider's own error reason, bounded and with the key removed."""
        return _provider_error_reason(body, secret=self._secret())

    def _request(self, payload: dict[str, Any]) -> tuple[int, bytes]:
        """POST ``payload`` to the configured model endpoint.

        Raises:
            GenerationProviderUnavailable: the request could not be made, or the
                provider answered with a non-2xx status. The message names the
                variable to set when the key is missing, and otherwise carries the
                status and the provider's bounded reason — never the URL with a key
                in it, and never the request body.
        """
        api_key = self._secret()
        if not api_key.strip():
            raise GenerationProviderUnavailable(
                "gemini_api_key is not configured; the Gemini generation provider "
                "cannot authenticate"
            )

        url = (
            f"{self._settings.gemini_api_base_url.rstrip('/')}"
            f"{GEMINI_GENERATE_PATH.format(model=self.model_name)}"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                GEMINI_AUTH_HEADER: api_key,
            },
        )
        try:
            return self._transport(request, GENERATION_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            # `HTTPError` is a response rather than a transport failure, so it
            # carries a body worth reading. The reason is extracted and redacted
            # below; the URL is not, because the URL is what a traceback prints and
            # what a proxy logs, and the key is the one thing that must never be in
            # either.
            raise GenerationProviderUnavailable(
                f"the generation provider answered {exc.code}: "
                f"{self._reason(exc.read())}"
            ) from exc
        except urllib.error.URLError as exc:
            # A timeout reaches us two different ways depending on where it happens:
            # raised directly as ``TimeoutError`` (which ``socket.timeout`` aliases)
            # when the socket read expires, and wrapped as
            # ``URLError(reason=TimeoutError)`` when it expires earlier in the exchange.
            # ``URLError`` is matched first because it is the outer type, so without the
            # check below a genuine timeout would be reported as "could not be reached",
            # the specific timeout message would be unreachable for real network
            # timeouts, and an operator would be sent to look at DNS and certificates for
            # a request that simply ran out of time.
            if isinstance(exc.reason, TimeoutError):
                raise GenerationProviderUnavailable(
                    "the generation provider did not answer within "
                    f"{GENERATION_TIMEOUT_SECONDS:g}s"
                ) from exc
            # The class name of the reason, never its text: a transport is free to
            # put anything in a reason — including a chunk of the request — and
            # `URLError` itself is identical for DNS failure, refused connection and
            # bad certificate alike, so naming the reason's class is what keeps
            # those three distinguishable.
            raise GenerationProviderUnavailable(
                f"the generation provider could not be reached "
                f"({type(exc.reason).__name__})"
            ) from exc
        except TimeoutError as exc:
            raise GenerationProviderUnavailable(
                "the generation provider did not answer within "
                f"{GENERATION_TIMEOUT_SECONDS:g}s"
            ) from exc


def build_generation_provider(
    settings: Settings, *, transport: GenerationTransport | None = None
) -> GenerationProvider:
    """Return the provider :attr:`Settings.llm_provider` names.

    The only place a concrete provider class is named. A new provider is one branch
    here and one subclass of :class:`GenerationProvider`; no caller changes, and no
    caller names a class — the query path that will consume this factory does not know
    or care that the first implementation happens to be Gemini.

    Args:
        settings: the application configuration. Nothing else is read.
        transport: overrides the HTTP transport, for tests. Never passed by
            application code.

    Raises:
        GenerationError: the configured provider name has no implementation. Should
            be unreachable — :attr:`Settings.llm_provider` is a ``Literal``, so
            pydantic rejects an unknown name at construction. The branch is total
            anyway, so that widening the ``Literal`` fails loudly here rather than
            falling through to a ``None`` a caller would treat as a provider.

        GenerationProviderUnavailable: the chosen provider has no credential to
            authenticate with. Checked here rather than in
            :meth:`Settings._validate_production_settings` — see the note in that
            validator — and checked here rather than left to the first HTTP call so
            that a misconfigured deployment is told once, naming the variable to set,
            rather than once per question, forever.
    """
    match settings.llm_provider:
        case "gemini":
            key = settings.gemini_api_key
            if key is None or not key.get_secret_value().strip():
                raise GenerationProviderUnavailable(
                    "gemini_api_key is not set, so the Gemini generation provider "
                    "cannot authenticate. Set it from the environment; it is read "
                    "here and nowhere else."
                )
            return GeminiGenerationProvider(settings, transport=transport)
        case _:
            raise GenerationError(
                f"no generation provider is implemented for {settings.llm_provider!r}"
            )


def _urllib_transport(request: urllib.request.Request, timeout: float) -> tuple[int, bytes]:
    """The production transport: one POST, returning ``(status, body)``.

    Wrapped in a function rather than used as :func:`urllib.request.urlopen` directly
    so the *signature* the provider depends on is explicit, and so a test can
    substitute it without caring that one of them returns a response object and the
    other returns a tuple.
    """
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.status
        body = response.read()
    return int(status), body


def _decode_json(body: bytes) -> Any:
    """Decode a JSON body, or raise :class:`GenerationError`.

    A decoding failure is a provider fault rather than a bug in this module's
    request, so it is reported as one. The body is deliberately *not* included: a
    provider's error page may quote the request, and the request is a tenant's
    document.
    """
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise GenerationError(
            f"the generation provider returned a body that is not JSON "
            f"({type(exc).__name__})"
        ) from exc


def _scrub(reason: str, secret: str) -> str:
    """Bound ``reason`` and guarantee no fragment of ``secret`` survives in it.

    Neither obvious order of the two operations is correct, and getting this wrong leaks a
    credential in whichever direction one did not check:

    * **Truncate, then redact.** The replacement marker stays intact, because the cut
      happened before anything was substituted. But if the cut lands *inside* an
      occurrence of the key, what is left is a fragment the ``replace`` can no longer
      match — and a prefix of a live credential in a traceback is worse than none,
      because it reads as a usable one.
    * **Redact, then truncate.** Nothing survives the cut, because the intact key was
      removed first. But the cut can then land inside the replacement marker, so a
      message ending ``<redac`` looks like corrupted content rather than a deliberate
      omission, and a reader cannot tell that anything was withheld.

    So this takes a third order, and each step exists because of a specific failure of the
    simpler ones:

    1. Reserve room for the marker by cutting at
       ``MAX_PROVIDER_MESSAGE_CHARS + len(REDACTED)``. In the ordinary case the key is not
       at the boundary, so this step changes nothing a reader would notice.
    2. Replace the intact key. This is what actually removes it.
    3. **Verify.** If any run of at least :data:`LEAK_FRAGMENT_CHARS` of the key's own
       characters is still present — which is what step 1 leaves behind when it *did* cut
       into the key — discard the reason and return :data:`REDACTED` alone. Losing a
       provider's explanation is a real cost, and it is paid only when the alternative is
       emitting part of a live credential.
    4. Cut to the bound, now that any marker substitution is safely inside it.

    The verification is what makes this safe rather than merely tidy. Steps 1 and 2 alone
    would still emit up to ``len(REDACTED) - 1`` characters of a key that sat exactly on
    the boundary, and a test asserting only that the key is absent would pass while that
    happened.
    """
    if not secret:
        return reason[:MAX_PROVIDER_MESSAGE_CHARS]
    scrubbed = reason[: MAX_PROVIDER_MESSAGE_CHARS + len(REDACTED)].replace(
        secret, REDACTED
    )
    if any(
        secret[start : start + LEAK_FRAGMENT_CHARS] in scrubbed
        for start in range(len(secret) - LEAK_FRAGMENT_CHARS + 1)
    ):
        return REDACTED
    return scrubbed[:MAX_PROVIDER_MESSAGE_CHARS]


def _provider_error_reason(body: bytes, *, secret: str) -> str:
    """Extract a provider's error reason: bounded, and with the key removed.

    Google's error envelope puts a human-readable ``error.message`` inside ``error``,
    and an operator who is told "429" and nothing else cannot triage anything — so the
    reason is surfaced, which means a third party now chooses part of an exception
    message. Two rules bound that:

    * **Bound it.** Truncated to :data:`MAX_PROVIDER_MESSAGE_CHARS`, because a
      provider may send megabytes and because a provider error page may quote the
      request, and the request is a tenant's document.
    * **Strip the credential.** A provider that echoes the key back — in a "key
      ``AIza…`` is not valid for this API" message, which is exactly what it does —
      would otherwise put a live credential into a traceback. Tenant content cannot
      be stripped this way because we do not hold a copy of it, but the API key we
      do hold in full, so it is the one secret this module can guarantee never to
      repeat. Redaction happens before truncation so a key that straddles the cut is
      removed rather than half-removed.

    When the body carries nothing usable, the status is all that is reported.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return "no reason given"
    reason = "no reason given"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                reason = message
            else:
                status = error.get("status")
                if isinstance(status, str):
                    reason = status
    return _scrub(reason, secret)
