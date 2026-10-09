"""Tests for :mod:`app.rag.generation` — the generation boundary, with no network.

What is actually under test
---------------------------
This is the one module that hands untrusted tenant text to a third party, so the
assertions here are mostly about what must *not* escape: the question, the retrieved
evidence, the model's answer, and the API key. Those are four separate obligations
with four separate failure modes, and each has a test class below.

Every test drives the real :class:`~app.rag.generation.GeminiGenerationProvider` —
the real URL construction, the real JSON body, the real header set, the real response
validation — through a stubbed :data:`~app.rag.generation.GenerationTransport`, which
is a single callable returning ``(status, bytes)``. Asserting against a mocked client
instead would only prove the mock was called; asserting against a stubbed transport
proves the code that ships builds the request it thinks it is building. See the module
docstring in :mod:`tests.generation`.

What a live API could tell us, and what it could not
---------------------------------------------------
A real key would tell us Google's response shape has not changed. It would tell us
nothing these tests cannot: that a malformed body becomes a ``GenerationError`` rather
than an ``IndexError`` three frames down, that a provider echoing the key back does
not get it into a traceback, that a successful call logs nothing at all. Those are the
failures that matter, and every one of them is reachable from here without a socket.

The four things this file insists on
------------------------------------
1. **The request is the request we think it is.** The instructions are in
   ``systemInstruction`` and nowhere else; the question precedes the evidence; the
   temperature is zero.
2. **Nothing tenant-derived is logged** — the question, the evidence, the answer, or a
   provider's echo of the request. Asserted over ``caplog`` with distinctive
   sentinels, and guarded by a test proving the capture was live.
3. **The API key is in the header and nowhere else** — not the URL, not the body, not
   an exception message, not a ``__cause__`` chain, and not in a provider's error text
   even when the provider quotes it straight back.
4. **Every failure mode is one of two named exceptions.** A malformed 2xx body is a
   :class:`~app.rag.generation.GenerationError`; an unreachable or refusing provider is
   a :class:`~app.rag.generation.GenerationProviderUnavailable`. No bare builtin ever
   escapes.
"""

from __future__ import annotations

import io
import json
import logging
import socket
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

import app.rag.generation as generation_module
from app.core.config import Settings
from app.rag.generation import (
    GEMINI_AUTH_HEADER,
    GENERATION_TIMEOUT_SECONDS,
    MAX_PROVIDER_MESSAGE_CHARS,
    REDACTED,
    GeminiGenerationProvider,
    GenerationError,
    GenerationProvider,
    GenerationProviderUnavailable,
    build_generation_provider,
)
from tests.conftest import make_settings
from tests.generation import (
    DEFAULT_ANSWER,
    FAKE_MODEL_NAME,
    FakeGenerationProvider,
    Raw,
    RecordingTransport,
    fake_transport,
    gemini_answer,
)

#: Obviously not a credential, so a leak of it is unmistakable and nothing that reads
#: this file can mistake it for something to use.
FAKE_KEY = "not-a-real-generation-key-for-tests-only-00000000"

#: Distinctive sentinels. Every leak assertion below checks for these by name, so a
#: leak is visible in the failure message rather than being inferred from a diff.
INSTRUCTIONS = "SENTINEL-INSTRUCTIONS-4a1c-do-not-log"
QUESTION = "SENTINEL-QUESTION-7b2d-do-not-log"
EVIDENCE = "SENTINEL-EVIDENCE-9e3f-do-not-log"
ANSWER = "SENTINEL-ANSWER-1c5a-do-not-log"

#: The model this suite claims to be configured with. Asserted by name so that a test
#: can prove the configured model reached the URL.
MODEL = "gemini-2.5-flash"

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

#: An ``HTTPError``'s header mapping, for the cases that never read one.
NO_HEADERS: Message[str, str] = Message()

#: Merged with per-test overrides by :func:`generation_settings` rather than passed as
#: ``**overrides`` to :func:`make_settings`, for the reason given at the same line in
#: ``tests/test_embeddings.py``: a defaults dict spliced with a caller's keywords
#: collides the moment a test overrides one of the defaults.
_GENERATION_DEFAULTS: dict[str, object] = {
    "llm_provider": "gemini",
    "llm_model": MODEL,
    "gemini_api_key": FAKE_KEY,
    "gemini_api_base_url": BASE_URL,
}

#: One well-formed 200 body, reused wherever the response is not what is under test.
GOOD_ANSWER: bytes = gemini_answer(ANSWER)


def generation_settings(**overrides: object) -> Settings:
    return make_settings(**{**_GENERATION_DEFAULTS, **overrides})


def body_of(request: urllib.request.Request) -> bytes:
    """The request's body, asserted to be raw bytes.

    ``urllib`` types a request body as anything from a raw buffer to an iterable of
    byte chunks, and this provider only ever supplies ``bytes``. Narrowing once here
    rather than eight times — and *asserting* rather than defaulting, because a body
    that was not bytes would be a change in how the request is built.
    """
    data = request.data
    assert isinstance(data, bytes), (
        f"the request body is {type(data).__name__}, not bytes; the provider builds "
        "every body with json.dumps(...).encode()"
    )
    return data


def asking(provider: GenerationProvider) -> str:
    """Ask ``provider`` the standard question, and return its answer.

    Typed against the abstraction rather than against
    :class:`~app.rag.generation.GeminiGenerationProvider`, because ``generate`` is the
    only thing this helper uses and it is declared on the base class. Narrowing the
    annotation would make this a test of *which* class was passed — asserting a
    requirement the interface does not state, and rejecting a second implementation that
    satisfies it perfectly well.
    """
    return provider.generate(
        instructions=INSTRUCTIONS, question=QUESTION, evidence=EVIDENCE
    )


class TestTheAbstraction:
    """The interface, before any HTTP is involved."""

    def test_the_base_class_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            GenerationProvider()  # type: ignore[abstract]

    def test_both_members_are_abstract(self) -> None:
        """Named rather than inferred from a ``TypeError``, so a change to which
        members a new provider must implement is reported as *this* failure."""
        assert GenerationProvider.__abstractmethods__ == frozenset(
            {"model_name", "generate"}
        )

    def test_a_provider_without_generate_cannot_be_instantiated(self) -> None:
        class NoGenerate(GenerationProvider):
            @property
            def model_name(self) -> str:
                return "x"

        with pytest.raises(TypeError):
            NoGenerate()  # type: ignore[abstract]

    def test_a_new_provider_needs_no_change_to_the_factory_caller(self) -> None:
        """Extensible without rewriting anything above this layer.

        The factory is the only place a concrete class is named, and it takes a
        ``Settings``. A provider that satisfies the interface drops in, and the
        factory's caller never learns its name.
        """

        class ShoutingProvider(GenerationProvider):
            @property
            def model_name(self) -> str:
                return "shouting"

            def generate(self, *, instructions: str, question: str, evidence: str) -> str:
                return question.upper()

        assert ShoutingProvider().generate(
            instructions="i", question="q", evidence="e"
        ) == "Q"

    def test_the_fake_double_satisfies_the_real_interface(self) -> None:
        """Not a ``Mock``, so the abstraction above is genuinely exercised.

        A mock satisfies nothing: it would let a test pass against a signature the
        application does not actually call.
        """
        assert isinstance(FakeGenerationProvider(), GenerationProvider)

    def test_the_fake_records_what_it_was_grounded_on(self) -> None:
        """So a caller can assert which text a model was given, with no provider."""
        provider = FakeGenerationProvider(answer="grounded")
        provider.generate(instructions="i", question="q", evidence="e")
        provider.generate(instructions="i2", question="q2", evidence="e2")

        assert provider.calls == [
            {"instructions": "i", "question": "q", "evidence": "e"},
            {"instructions": "i2", "question": "q2", "evidence": "e2"},
        ]
        assert provider.model_name == FAKE_MODEL_NAME

    def test_the_fake_can_be_told_to_fail(self) -> None:
        """Because "the provider refused" is a state callers must handle, not a
        scenario only this file should be able to reach."""
        provider = FakeGenerationProvider(fail_with=GenerationProviderUnavailable("no"))
        with pytest.raises(GenerationProviderUnavailable, match="no"):
            provider.generate(instructions="i", question="q", evidence="e")

    def test_the_fake_makes_no_network_call(self, no_network: None) -> None:
        provider = FakeGenerationProvider()
        assert (
            provider.generate(instructions="i", question="q", evidence="e")
            == DEFAULT_ANSWER
        )

    def test_the_factory_takes_the_transport_for_tests_only(self) -> None:
        transport = RecordingTransport((200, GOOD_ANSWER))
        provider = build_generation_provider(
            generation_settings(), transport=transport
        )
        asking(provider)
        assert transport.request_count == 1

    def test_the_provider_never_reads_the_environment(self) -> None:
        """Configuration comes from ``Settings`` and nowhere else (PMP §8).

        Asserted over the module source, because a stray ``os.environ`` read would be
        invisible in the output: the provider would work, and the one thing the test
        suite relies on — that no test can be influenced by the developer's own
        ``.env`` — would quietly stop being true.
        """
        source = Path(generation_module.__file__).read_text(encoding="utf-8")
        for forbidden in ("os.environ", "os.getenv", "getenv("):
            assert forbidden not in source, (
                f"app/rag/generation.py uses {forbidden!r}; configuration must come "
                "only from Settings"
            )


class TestTheAnswer:
    """What comes back out of a well-formed response."""

    def test_a_well_formed_response_returns_its_text(self) -> None:
        transport = RecordingTransport((200, gemini_answer(ANSWER)))
        assert asking(GeminiGenerationProvider(generation_settings(), transport=transport)) == ANSWER

    def test_several_parts_are_concatenated_in_order(self) -> None:
        """Order is load-bearing: the parts are one answer, not several.

        Concatenated with no separator, because Gemini emits text parts as fragments
        of a single message and inserting a newline would corrupt a word split across
        two parts.
        """
        transport = RecordingTransport((200, gemini_answer("Rent is ", "due on ", "the 1st.")))
        assert asking(GeminiGenerationProvider(generation_settings(), transport=transport)) == (
            "Rent is due on the 1st."
        )

    def test_the_model_role_is_ignored(self) -> None:
        """The role travels in the fixture already; the answer is the text and only
        the text, so a caller never has to know the field exists."""
        body = json.dumps(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": ANSWER}, {"text": "!"}],
                        },
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ]
            }
        ).encode()
        transport = RecordingTransport((200, body))
        assert asking(GeminiGenerationProvider(generation_settings(), transport=transport)) == (
            f"{ANSWER}!"
        )

    @pytest.mark.parametrize(
        "part",
        [
            {"text": 42},
            {"text": None},
            {"text": ["a", "list"]},
            {"text": {"nested": "object"}},
            {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
            {},
            Raw("a bare string where an object was expected"),
            7,
            None,
            ["unexpected", "list"],
        ],
    )
    def test_a_part_that_is_not_text_is_skipped(self, part: object) -> None:
        """A valid answer next to a part this module does not consume stays readable.

        Gemini returns inline data and, on models that expose them, reasoning parts.
        Refusing the whole response over one would make the provider unusable the
        first time a model started emitting them; the emptiness check is what stops
        that leniency from turning a text-free response into an empty answer.
        """
        transport = RecordingTransport((200, gemini_answer(ANSWER, part, " tail")))
        assert asking(GeminiGenerationProvider(generation_settings(), transport=transport)) == (
            f"{ANSWER} tail"
        )

    def test_the_answer_is_returned_verbatim(self) -> None:
        """Not stripped, not normalised.

        The caller decides whether an answer needs trimming, and a module that
        silently trimmed would make two different provider answers indistinguishable
        to whatever compares them.
        """
        transport = RecordingTransport((200, gemini_answer(f"  {ANSWER}\n\n")))
        assert asking(GeminiGenerationProvider(generation_settings(), transport=transport)) == (
            f"  {ANSWER}\n\n"
        )

    def test_unicode_survives_both_directions(self) -> None:
        """The evidence goes out as UTF-8 and the answer comes back as UTF-8.

        The unicode text is used as the **evidence**, not as the provider's answer. Both
        directions have to be exercised and they are different directions: a request is
        serialised by this module, while a response is parsed by it, so a bug in one says
        nothing about the other. The first version of this test put the text only in the
        response and then asserted it appeared in the *request* body, which cannot hold for
        any implementation — the sentinels are what goes out.
        """
        text = "Frist — naïve café"
        transport = RecordingTransport((200, gemini_answer(text)))
        provider = GeminiGenerationProvider(generation_settings(), transport=transport)
        assert (
            provider.generate(
                instructions=INSTRUCTIONS, question=QUESTION, evidence=text
            )
            == text
        )
        # Decoded, not compared as raw bytes. `json.dumps` escapes non-ASCII to
        # ``\uXXXX`` by default, which is valid JSON that any decoder renders back to the
        # original characters — so a substring check against the encoded body would fail
        # for an implementation that transmits the text perfectly. What has to hold is
        # that the value survives serialisation and arrives as itself, and only parsing
        # the body can show that.
        sent = json.loads(body_of(transport.requests[0]).decode("utf-8"))
        assert text in sent["contents"][0]["parts"][0]["text"]

    def test_a_second_candidate_is_not_read(self) -> None:
        """Only the first is the answer.

        A provider that emits several candidates is offering alternatives, and
        silently concatenating them would produce a plausible sentence that no model
        actually said. Which candidate wins is a policy question for a future phase
        that asks for one; until then the first is the documented answer.
        """
        body = json.dumps(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "first"}]}},
                    {"content": {"parts": [{"text": "second"}]}},
                ]
            }
        ).encode()
        transport = RecordingTransport((200, body))
        assert asking(GeminiGenerationProvider(generation_settings(), transport=transport)) == (
            "first"
        )


class TestTheRequest:
    """What goes out. Everything here is asserted on the recorded request."""

    def test_it_posts_json_to_the_model_endpoint(self) -> None:
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))

        request = transport.requests[0]
        assert request.get_method() == "POST"
        assert request.full_url == f"{BASE_URL}/models/{MODEL}:generateContent"
        assert json.loads(body_of(request))["contents"]

    def test_a_trailing_slash_in_the_base_url_does_not_double_up(self) -> None:
        transport = RecordingTransport((200, GOOD_ANSWER))
        settings = generation_settings(gemini_api_base_url="https://example.test/v1beta/")
        asking(GeminiGenerationProvider(settings, transport=transport))
        assert transport.requests[0].full_url == (
            f"https://example.test/v1beta/models/{MODEL}:generateContent"
        )

    def test_the_content_type_is_declared(self) -> None:
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))
        assert transport.header(0, "Content-Type") == "application/json"

    def test_the_system_instruction_carries_the_instructions(self) -> None:
        """Under the camelCase REST field name, which the API silently ignores when
        spelled any other way — so an error here would leave the model answering an
        ungrounded question that still looked grounded, because the evidence was sent.
        """
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))

        body = json.loads(body_of(transport.requests[0]))
        assert body["systemInstruction"]["parts"][0]["text"] == INSTRUCTIONS

    def test_the_instructions_do_not_appear_in_the_user_turn(self) -> None:
        """So the two can never be confused downstream.

        The invariant that makes grounded answering auditable is that the
        instructions are application-authored and the user turn is not. If the same
        string were in both, nothing would distinguish "the application said this"
        from "a document said this".
        """
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))

        body = json.loads(body_of(transport.requests[0]))
        user_text = body["contents"][0]["parts"][0]["text"]
        assert INSTRUCTIONS not in user_text

    def test_one_user_turn_carries_the_question_then_the_evidence(self) -> None:
        """Ordering, not just membership.

        The question first and the evidence after it, so "answer only from the text
        below" is a statement about position that a model can act on. One part
        rather than two: two parts invite the model to read the second as a separate
        instruction turn.
        """
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))

        body = json.loads(body_of(transport.requests[0]))
        assert len(body["contents"]) == 1
        turn = body["contents"][0]
        assert turn["role"] == "user"

        text = turn["parts"][0]["text"]
        assert QUESTION in text
        assert EVIDENCE in text
        assert text.index(QUESTION) < text.index(EVIDENCE)

    def test_the_temperature_is_zero(self) -> None:
        """Grounded answering is not a creative task.

        A retry of the same question over the same evidence must not produce a
        differently-worded non-answer, because a user cannot tell a model that hedged
        from a model that was asked twice.
        """
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))

        body = json.loads(body_of(transport.requests[0]))
        assert body["generationConfig"]["temperature"] == 0.0

    def test_the_api_key_is_a_header_not_a_query_parameter(self) -> None:
        """The load-bearing assertion of this class.

        A URL reaches proxy access logs and tracebacks. A header does not. Most
        Google's own API examples put the key in the query string, so this fails
        loudly the moment someone follows one.
        """
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))

        request = transport.requests[0]
        assert transport.header(0, GEMINI_AUTH_HEADER) == FAKE_KEY
        assert FAKE_KEY not in request.full_url
        assert "key=" not in request.full_url

    def test_the_key_is_in_no_other_request_artefact(self) -> None:
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))
        request = transport.requests[0]

        assert FAKE_KEY not in body_of(request).decode()
        assert FAKE_KEY not in repr(request.full_url)

    def test_the_timeout_is_the_declared_one(self) -> None:
        """Bounded, because the caller is a request handler holding a connection.

        An unbounded generation call does not merely answer slowly; it keeps the
        whole application unavailable until the provider decides to answer.
        """
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))
        assert transport.timeouts == [GENERATION_TIMEOUT_SECONDS]

    def test_the_same_question_produces_the_same_request(self) -> None:
        """Determinism, so two identical answers from two callers are comparable and a
        change to the request shape shows up in a diff rather than in a report."""
        transport = RecordingTransport((200, GOOD_ANSWER), (200, GOOD_ANSWER))
        first = GeminiGenerationProvider(generation_settings(), transport=transport)
        second = GeminiGenerationProvider(generation_settings(), transport=transport)

        asking(first)
        asking(second)

        assert body_of(transport.requests[0]) == body_of(transport.requests[1])
        assert transport.requests[0].full_url == transport.requests[1].full_url

    def test_two_questions_do_not_influence_each_other(self) -> None:
        """The provider holds no per-request state, so the second answer cannot depend
        on the first having been asked."""
        transport = RecordingTransport((200, GOOD_ANSWER), (200, GOOD_ANSWER))
        provider = GeminiGenerationProvider(generation_settings(), transport=transport)

        provider.generate(instructions=INSTRUCTIONS, question="first", evidence="e1")
        provider.generate(instructions=INSTRUCTIONS, question="second", evidence="e2")

        texts = [
            json.loads(body_of(request))["contents"][0]["parts"][0]["text"]
            for request in transport.requests
        ]
        assert [text.split("\n\n")[0] for text in texts] == ["first", "second"]


class TestMalformedResponses:
    """A 2xx body this module cannot read is a ``GenerationError``, never a builtin.

    Each case below is a shape a vendor can produce and this project did not
    anticipate. Letting the corresponding ``KeyError``/``IndexError`` escape would
    report a vendor surprise as a programming bug, three frames away from the cause
    and with the vendor's bytes nowhere in the message.
    """

    #: Every body in this list must fail, with the reason each one gives. The reason
    #: is asserted so a failure cannot be reported as "some ``GenerationError``".
    CASES: tuple[tuple[bytes, str], ...] = (
        (b"not json at all", "not JSON"),
        (b"", "not JSON"),
        (b"\xff\xfe not utf-8", "not JSON"),
        (b"[]", "not a JSON object"),
        (b'"a bare JSON string"', "not a JSON object"),
        (b"42", "not a JSON object"),
        (b"null", "not a JSON object"),
        (b"{}", "no 'candidates' list"),
        (b'{"candidates": "nope"}', "no 'candidates' list"),
        (b'{"candidates": {}}', "no 'candidates' list"),
        (b'{"candidates": []}', "no candidates"),
        (b'{"candidates": [1]}', "not a JSON object"),
        (b'{"candidates": ["text?"]}', "not a JSON object"),
        (b'{"candidates": [{}]}', "no 'content' object"),
        (b'{"candidates": [{"content": 1}]}', "no 'content' object"),
        (b'{"candidates": [{"content": []}]}', "no 'content' object"),
        (b'{"candidates": [{"content": {}}]}', "no 'parts' list"),
        (b'{"candidates": [{"content": {"parts": "nope"}}]}', "no 'parts' list"),
        (b'{"candidates": [{"content": {"parts": []}}]}', "held no answer text"),
        (b'{"candidates": [{"content": {"parts": [{}]}}]}', "held no answer text"),
        (b'{"candidates": [{"content": {"parts": [{"text": 42}]}}]}', "held no answer text"),
        (b'{"candidates": [{"content": {"parts": [{"text": null}]}}]}', "held no answer text"),
        (b'{"candidates": [{"content": {"parts": [{"text": "   "}]}}]}', "held no answer text"),
        (b'{"candidates": [{"content": {"parts": [null, 7]}}]}', "held no answer text"),
    )

    @pytest.mark.parametrize(("body", "reason"), CASES)
    def test_a_malformed_body_is_refused(self, body: bytes, reason: str) -> None:
        transport = RecordingTransport((200, body))
        with pytest.raises(GenerationError, match=reason):
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))

    @pytest.mark.parametrize(("body", "reason"), CASES)
    def test_a_malformed_body_is_never_a_bare_builtin(
        self, body: bytes, reason: str
    ) -> None:
        """The point of the class, asserted separately from the reason.

        ``GenerationError`` is a ``RuntimeError``, so this cannot pass by accident:
        a ``KeyError``/``TypeError``/``IndexError``/``AttributeError`` is not one.
        """
        transport = RecordingTransport((200, body))
        with pytest.raises(GenerationError) as caught:
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))

        assert not isinstance(
            caught.value, (KeyError, TypeError, IndexError, AttributeError)
        ), f"{type(caught.value).__name__} escaped for body {body!r}"
        assert reason in str(caught.value)

    def test_a_malformed_body_message_never_quotes_the_body(self) -> None:
        """A provider error page may quote the request, and the request is a tenant's
        document — so the raw bytes are never quoted back."""
        transport = RecordingTransport((200, f"<html>{EVIDENCE}</html>".encode()))
        with pytest.raises(GenerationError) as caught:
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))
        assert EVIDENCE not in str(caught.value)

    def test_an_unexpected_error_class_still_reaches_the_caller(self) -> None:
        """One ``except`` for the common case, while the specific type survives."""
        assert issubclass(GenerationProviderUnavailable, GenerationError)
        assert issubclass(GenerationError, RuntimeError)


class TestFailures:
    """What happens when the provider is down, refusing, or slow."""

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
    def test_a_non_2xx_is_unavailable_not_a_crash(self, status: int) -> None:
        transport = RecordingTransport(
            (status, json.dumps({"error": {"message": "quota exceeded"}}).encode())
        )
        with pytest.raises(GenerationProviderUnavailable, match=str(status)):
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))

    @pytest.mark.parametrize("status", [200, 201, 202, 299])
    def test_any_2xx_is_an_answer(self, status: int) -> None:
        """The whole 2xx range, not just 200.

        Turning a future informational status into a ``GenerationError`` would be this
        module guessing at a contract it does not have, and would turn a *success*
        into a failure for users.
        """
        transport = RecordingTransport((status, GOOD_ANSWER))
        assert asking(
            GeminiGenerationProvider(generation_settings(), transport=transport)
        ) == ANSWER

    def test_a_provider_error_message_is_surfaced(self) -> None:
        """The operator needs to know *why* — bounded, and without the request."""
        transport = RecordingTransport(
            (429, json.dumps({"error": {"message": "Resource has been exhausted"}}).encode())
        )
        with pytest.raises(GenerationProviderUnavailable, match="exhausted"):
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))

    def test_the_provider_status_is_used_when_there_is_no_message(self) -> None:
        transport = RecordingTransport(
            (403, json.dumps({"error": {"status": "PERMISSION_DENIED"}}).encode())
        )
        with pytest.raises(GenerationProviderUnavailable, match="PERMISSION_DENIED"):
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))

    def test_an_unparsable_error_body_reports_only_that(self) -> None:
        transport = RecordingTransport((500, f"<html>{EVIDENCE}</html>".encode()))
        with pytest.raises(GenerationProviderUnavailable) as caught:
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))
        assert "no reason given" in str(caught.value)
        assert EVIDENCE not in str(caught.value)

    def test_a_provider_error_message_is_bounded(self) -> None:
        """A provider returning a megabyte of text must not put it in an exception
        message that a caller may store or return."""
        transport = RecordingTransport(
            (400, json.dumps({"error": {"message": "x" * 100_000}}).encode())
        )
        with pytest.raises(GenerationProviderUnavailable) as caught:
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))
        assert len(str(caught.value)) < MAX_PROVIDER_MESSAGE_CHARS + 200

    def test_an_http_error_is_unavailable(self) -> None:
        """``HTTPError`` is a *response*, so it carries a body worth reading."""
        error = urllib.error.HTTPError(
            "https://example.test/x",
            429,
            "Too Many Requests",
            NO_HEADERS,
            io.BytesIO(json.dumps({"error": {"message": "slow down"}}).encode()),
        )
        with pytest.raises(GenerationProviderUnavailable, match="slow down"):
            asking(
                GeminiGenerationProvider(generation_settings(), transport=_raise(error))
            )

    def test_an_http_error_body_is_bounded_too(self) -> None:
        error = urllib.error.HTTPError(
            "https://example.test/x",
            500,
            "Server Error",
            NO_HEADERS,
            io.BytesIO(json.dumps({"error": {"message": "y" * 50_000}}).encode()),
        )
        with pytest.raises(GenerationProviderUnavailable) as caught:
            asking(
                GeminiGenerationProvider(generation_settings(), transport=_raise(error))
            )
        assert len(str(caught.value)) < MAX_PROVIDER_MESSAGE_CHARS + 200

    @pytest.mark.parametrize(
        ("reason", "expected"),
        [
            (ConnectionRefusedError("nope"), "ConnectionRefusedError"),
            (OSError("nope"), "OSError"),
            (socket.gaierror("nope"), "gaierror"),
            (TimeoutError("nope"), "did not answer within"),
        ],
    )
    def test_a_url_error_reports_the_reason_class_not_its_text(
        self, reason: BaseException, expected: str
    ) -> None:
        """``URLError`` wraps a reason; the reason is what an operator needs.

        ``URLError`` is identical whether the host does not resolve, the connection is
        refused, or the certificate is bad, so naming it would make every network
        failure look the same. Naming ``exc.reason``'s class separates them; naming
        the reason's *text* would risk echoing tenant content, because a transport may
        put anything in there.
        """
        with pytest.raises(GenerationProviderUnavailable, match=expected):
            asking(
                GeminiGenerationProvider(
                    generation_settings(),
                    transport=_raise(urllib.error.URLError(reason)),
                )
            )

    def test_a_url_error_message_never_carries_the_evidence(self) -> None:
        with pytest.raises(GenerationProviderUnavailable) as caught:
            asking(
                GeminiGenerationProvider(
                    generation_settings(),
                    transport=_raise(urllib.error.URLError(OSError(EVIDENCE))),
                )
            )
        assert EVIDENCE not in str(caught.value)

    def test_a_timeout_is_unavailable_and_says_how_long_it_waited(self) -> None:
        """The number, so the operator can decide whether to raise the ceiling."""
        with pytest.raises(GenerationProviderUnavailable, match="did not answer within"):
            asking(
                GeminiGenerationProvider(
                    generation_settings(), transport=_raise(TimeoutError())
                )
            )
        with pytest.raises(
            GenerationProviderUnavailable, match=f"{GENERATION_TIMEOUT_SECONDS:g}s"
        ):
            asking(
                GeminiGenerationProvider(
                    generation_settings(), transport=_raise(TimeoutError())
                )
            )

    def test_a_timeout_never_carries_its_own_text(self) -> None:
        """``TimeoutError("...")`` can be constructed with anything, so the timeout
        message is written from the constant rather than from the exception."""
        with pytest.raises(GenerationProviderUnavailable) as caught:
            asking(
                GeminiGenerationProvider(
                    generation_settings(),
                    transport=_raise(TimeoutError(EVIDENCE)),
                )
            )
        assert EVIDENCE not in str(caught.value)

    def test_a_missing_api_key_is_refused_before_any_request(self) -> None:
        """Failing locally is cheaper than a 401 and names the cause."""
        transport = RecordingTransport()
        settings = generation_settings(gemini_api_key=None)
        with pytest.raises(GenerationProviderUnavailable, match="gemini_api_key"):
            asking(GeminiGenerationProvider(settings, transport=transport))
        assert transport.request_count == 0

    def test_a_blank_api_key_is_refused_too(self) -> None:
        """Whitespace is not a key, and a blank one produces a confusing 401."""
        transport = RecordingTransport()
        settings = generation_settings(gemini_api_key="   ")
        with pytest.raises(GenerationProviderUnavailable, match="gemini_api_key"):
            asking(GeminiGenerationProvider(settings, transport=transport))
        assert transport.request_count == 0

    def test_a_missing_api_key_is_refused_without_a_socket(self, no_network: None) -> None:
        """With no transport injected at all, so the refusal provably happens before
        any socket is opened rather than after a failed connection attempt."""
        provider = GeminiGenerationProvider(generation_settings(gemini_api_key=None))
        with pytest.raises(GenerationProviderUnavailable, match="gemini_api_key"):
            asking(provider)


class TestNothingIsLogged:
    """The prompt, the question, the evidence and the answer reach no log handler.

    Four separate obligations. The failure paths could leak them through an exception
    message that something upstream logs, and the success path could leak them
    through nothing at all — and "nothing at all" is the claim worth checking.
    """

    def test_a_successful_call_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            provider = GeminiGenerationProvider(
                generation_settings(), transport=fake_transport(GOOD_ANSWER)
            )
            asking(provider)

        text = caplog.text
        for sentinel in (INSTRUCTIONS, QUESTION, EVIDENCE, ANSWER, FAKE_KEY):
            assert sentinel not in text, f"{sentinel} reached a log handler"

    @pytest.mark.parametrize(
        ("status", "body"),
        [
            (429, json.dumps({"error": {"message": "quota exceeded"}}).encode()),
            (500, b"<html>an error page</html>"),
            (200, b"not json at all"),
            (200, b"{}"),
        ],
    )
    def test_a_failing_call_logs_nothing(
        self,
        status: int,
        body: bytes,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            provider = GeminiGenerationProvider(
                generation_settings(), transport=fake_transport(body, status)
            )
            with pytest.raises(GenerationError):
                asking(provider)

        text = caplog.text
        for sentinel in (INSTRUCTIONS, QUESTION, EVIDENCE, ANSWER, FAKE_KEY):
            assert sentinel not in text, f"{sentinel} reached a log handler on failure"

    def test_a_transport_failure_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            provider = GeminiGenerationProvider(
                generation_settings(),
                transport=_raise(urllib.error.URLError(OSError(EVIDENCE))),
            )
            with pytest.raises(GenerationProviderUnavailable):
                asking(provider)

        assert EVIDENCE not in caplog.text

    def test_the_endpoint_is_not_logged_either(self, caplog: pytest.LogCaptureFixture) -> None:
        """The URL is the artefact a proxy and a traceback both print, so a module that
        logged it would be logging its own shape."""
        with caplog.at_level(logging.DEBUG):
            provider = GeminiGenerationProvider(
                generation_settings(), transport=fake_transport(GOOD_ANSWER)
            )
            asking(provider)

        assert "generativelanguage" not in caplog.text
        assert MODEL not in caplog.text

    def test_the_capture_actually_captured_something(self, caplog: pytest.LogCaptureFixture) -> None:
        """A guard on the guard.

        If the handler were attached wrongly, every "nothing leaked" assertion above
        would pass while capturing nothing at all — the most likely way for this class
        of test to become a false negative. So something is logged deliberately,
        through the same mechanism, and asserted to arrive.
        """
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("tests.test_generation.sentinel").debug("MARKER-VISIBLE")
        assert "MARKER-VISIBLE" in caplog.text

    def test_the_module_declares_no_logger_at_all(self) -> None:
        """The structural claim behind every test above.

        Asserted over the source so the assertion cannot rot into "we checked once".
        A future change that adds logging here has to delete this test and justify
        each call site, which is the point: at this boundary, logging is a decision.
        """
        source = Path(generation_module.__file__).read_text(encoding="utf-8")
        # Structural tokens only. The obvious third candidate, a bare `_logger`, is
        # deliberately absent from this list: the module docstring explains *why* there is
        # no logger and names it in prose, so asserting the substring would fail on the
        # documentation of the property rather than on a violation of it — a test that can
        # only be satisfied by deleting the explanation. These three are what an actual
        # logging call site must contain, and a module that logs cannot contain all three
        # nowhere: it would have to import `logging`, obtain a logger from it, and hold
        # it in a module-level name.
        for forbidden in ("import logging", "getLogger", "logger ="):
            assert forbidden not in source, (
                f"app/rag/generation.py now contains {forbidden!r}. Nothing that "
                "reaches this boundary is safe to log: the key, the question, the "
                "evidence and the answer are all untrusted or secret. Log the "
                "status and a bounded reason from the caller instead, and update "
                "this test deliberately."
            )


class TestTheKeyNeverAppearsAnywhere:
    """The API key is *ours*, which makes it the one secret this module can police.

    Tenant content cannot be scrubbed this way — we do not hold a copy of it — so the
    guarantee here is narrower and stronger: nothing *this module* puts anywhere may
    carry the key, and a provider that quotes the key back to us does not get to put
    it in our message either.
    """

    def _every_failure(self) -> list[GenerationError]:
        """One exception per way this client can fail.

        A matrix rather than a list of individual assertions, because the claim is
        about *all* of them and a per-case test would let a new failure path be added
        without being checked.
        """
        echo = json.dumps({"error": {"message": f"invalid key {FAKE_KEY}"}}).encode()
        transports = [
            fake_transport(echo, 400),
            fake_transport(echo, 401),
            fake_transport(b"<html>" + FAKE_KEY.encode() + b"</html>", 500),
            fake_transport(b"not json", 200),
            fake_transport(b"{}", 200),
            _raise(urllib.error.URLError(OSError("nope"))),
            _raise(urllib.error.URLError(ConnectionRefusedError("nope"))),
            _raise(TimeoutError()),
            _raise(TimeoutError("nope")),
            _raise(
                urllib.error.HTTPError(
                    "https://example.test/x",
                    401,
                    "Unauthorized",
                    NO_HEADERS,
                    io.BytesIO(echo),
                )
            ),
        ]
        failures: list[GenerationError] = []
        for transport in transports:
            provider = GeminiGenerationProvider(
                generation_settings(), transport=transport
            )
            with pytest.raises(GenerationError) as caught:
                provider.generate(
                    instructions=INSTRUCTIONS,
                    question=QUESTION,
                    evidence=EVIDENCE,
                )
            failures.append(caught.value)
        return failures

    def test_the_matrix_reaches_more_than_one_failure_type(self) -> None:
        """A guard on the guard.

        Without this, a change to the transport list that made every case reach the
        same exception would turn the leak tests below into something narrower than
        they read.
        """
        failures = self._every_failure()
        assert len(failures) == 10
        assert {type(failure) for failure in failures} == {
            GenerationError,
            GenerationProviderUnavailable,
        }

    def test_the_key_is_in_no_failure_message(self) -> None:
        for failure in self._every_failure():
            assert FAKE_KEY not in str(failure), (
                f"{type(failure).__name__} echoed the API key back"
            )

    def test_the_key_is_in_no_chained_cause(self) -> None:
        """The cause chain too, because that is what a traceback prints.

        ``str(exception)`` walks no cause chain, so a message-only assertion would
        pass while the traceback a developer actually reads showed the secret.
        """
        for failure in self._every_failure():
            assert FAKE_KEY not in _rendered_chain(failure), (
                f"{type(failure).__name__} carries the API key in its __cause__ chain"
            )

    def test_the_key_is_in_no_request_artefact(self) -> None:
        """The root cause: nothing this module *builds* may carry it.

        Asserted over the URL and the body separately from the header, because the
        header is where it belongs and the other two are where it must not be.
        """
        transport = RecordingTransport((200, GOOD_ANSWER))
        asking(GeminiGenerationProvider(generation_settings(), transport=transport))
        request = transport.requests[0]

        assert FAKE_KEY not in request.full_url
        assert FAKE_KEY not in body_of(request).decode()
        assert transport.header(0, GEMINI_AUTH_HEADER) == FAKE_KEY

    def test_a_provider_echo_of_the_key_is_redacted(self) -> None:
        """The interesting leak, because it is the one a provider controls.

        Google answers an invalid key by quoting it. Surfacing that verbatim would
        put a live credential into a traceback that gets pasted into a ticket, so the
        key is stripped from the provider's text before it becomes part of our
        message — and the surrounding words survive, so the operator still learns why
        the call failed.
        """
        transport = RecordingTransport(
            (400, json.dumps({"error": {"message": f"API key not valid: {FAKE_KEY}"}}).encode())
        )
        with pytest.raises(GenerationProviderUnavailable) as caught:
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))

        message = str(caught.value)
        assert FAKE_KEY not in message
        assert REDACTED in message
        assert "API key not valid" in message

    def test_a_key_straddling_the_truncation_cut_is_still_removed(self) -> None:
        """Redaction happens *before* truncation.

        Truncating first and redacting after would leave a prefix of the key in the
        message — most of a credential, which is worse than none, because it looks
        like a full one to a reader skimming a traceback.
        """
        padding = "y" * (MAX_PROVIDER_MESSAGE_CHARS - 5)
        transport = RecordingTransport(
            (400, json.dumps({"error": {"message": padding + FAKE_KEY}}).encode())
        )
        with pytest.raises(GenerationProviderUnavailable) as caught:
            asking(GeminiGenerationProvider(generation_settings(), transport=transport))

        message = str(caught.value)
        assert FAKE_KEY[:20] not in message
        assert REDACTED in message

    def test_the_key_is_not_surfaced_by_the_factory_either(self) -> None:
        with pytest.raises(GenerationProviderUnavailable) as caught:
            build_generation_provider(generation_settings(gemini_api_key="   "))
        assert FAKE_KEY not in str(caught.value)


class TestBuildGenerationProvider:
    """The factory, and the one place a concrete class is named."""

    def test_gemini_is_the_configured_provider(self) -> None:
        provider = build_generation_provider(
            generation_settings(), transport=RecordingTransport()
        )
        assert isinstance(provider, GeminiGenerationProvider)
        assert isinstance(provider, GenerationProvider)

    def test_the_model_comes_from_settings(self) -> None:
        """So "which model answered this" is a configuration question, and so a
        model upgrade is not a code change."""
        for model in ("gemini-2.5-flash", "gemini-2.5-pro", "a-model-that-does-not-exist"):
            provider = build_generation_provider(
                generation_settings(llm_model=model),
                transport=RecordingTransport(),
            )
            assert provider.model_name == model

    def test_the_model_is_readable_without_a_request(self) -> None:
        transport = RecordingTransport()
        provider = GeminiGenerationProvider(generation_settings(), transport=transport)
        assert provider.model_name == MODEL
        assert transport.request_count == 0

    @pytest.mark.parametrize("key", [None, "", "   ", "\t\n"])
    def test_a_missing_or_blank_key_names_the_variable_to_set(
        self, key: str | None
    ) -> None:
        """Told once, at construction, naming the variable — not once per question.

        The message names ``gemini_api_key`` and says nothing about its value,
        because a configuration error page is exactly the sort of artefact that ends
        up in a screenshot.
        """
        with pytest.raises(GenerationProviderUnavailable) as caught:
            build_generation_provider(generation_settings(gemini_api_key=key))

        message = str(caught.value)
        assert "gemini_api_key" in message
        assert FAKE_KEY not in message
        # Only meaningful for a key that has content. `"" in anything` is always True and
        # `"   " in message` is a question about three spaces appearing in an English
        # sentence, so asserting them would fail for a reason that has nothing to do with
        # the credential — and asserting nothing at all for the blank cases would leave
        # the case that matters untested. The empty key cannot leak because there is
        # nothing to leak; `build_generation_provider` refuses it before any message is
        # built.
        if key is not None and key.strip():
            assert key not in message

    def test_a_misconfiguration_is_refused_without_a_socket(self, no_network: None) -> None:
        with pytest.raises(GenerationProviderUnavailable, match="gemini_api_key"):
            build_generation_provider(generation_settings(gemini_api_key=None))

    def test_an_unimplemented_provider_is_a_generation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The total ``case _``: a widened ``Literal`` fails here, loudly.

        Unreachable through configuration — pydantic rejects an unknown name — so the
        value is set on the instance to reach the branch. Without it, the fallback
        would be a statement in a file that nothing has ever executed.
        """
        settings = generation_settings()
        monkeypatch.setattr(settings, "llm_provider", "not-a-provider")

        with pytest.raises(GenerationError, match="not-a-provider"):
            build_generation_provider(settings, transport=RecordingTransport())


class TestWhatTheLayerAboveNeeds:
    """The contract a query service depends on, stated as assertions.

    Not a duplicate of the classes above: these are the things a caller cannot work
    around, so that a change to this provider which breaks one of them is reported as
    *this* failure rather than as a mystery in a service that has no other way to know
    which assumption broke.
    """

    def test_one_call_per_generate(self) -> None:
        """So a caller can assert "no second model call happened" by counting."""
        transport = RecordingTransport((200, GOOD_ANSWER), (200, GOOD_ANSWER))
        provider = GeminiGenerationProvider(generation_settings(), transport=transport)
        asking(provider)
        asking(provider)
        assert transport.request_count == 2

    def test_the_returned_value_is_a_plain_string(self) -> None:
        """Not a dict, not a candidate, not a model object. One string, so a caller
        cannot accidentally persist the provider's whole envelope."""
        answer = asking(
            GeminiGenerationProvider(
                generation_settings(), transport=fake_transport(GOOD_ANSWER)
            )
        )
        assert type(answer) is str

    def test_the_provider_name_is_stable_across_calls(self) -> None:
        provider = GeminiGenerationProvider(
            generation_settings(), transport=fake_transport(GOOD_ANSWER)
        )
        assert provider.model_name == provider.model_name

    def test_a_caller_can_use_a_different_provider_entirely(self) -> None:
        """The seam, exercised: the query layer above needs no knowledge of Gemini."""
        provider = FakeGenerationProvider(answer="grounded in the fake")
        answer = provider.generate(
            instructions=INSTRUCTIONS, question=QUESTION, evidence=EVIDENCE
        )

        assert answer == "grounded in the fake"
        assert provider.calls[0]["evidence"] == EVIDENCE
        assert provider.calls[0]["question"] == QUESTION
        assert provider.calls[0]["instructions"] == INSTRUCTIONS


def _rendered_chain(failure: BaseException, *, limit: int = 10) -> str:
    """Everything about ``failure`` a traceback would show.

    The message, the ``repr``, and every argument, walked down the ``__cause__``
    chain. ``str(exception)`` alone walks none of that, so a leak assertion built on
    it would pass while the traceback a developer actually reads printed the secret.

    Bounded by ``limit`` so a self-referential cause chain cannot hang the suite.
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

    A factory rather than a lambda so each call site gets a *fresh* exception
    instance, which matters: a shared instance accumulates a ``__traceback__`` and
    re-raising it twice produces a traceback chain that makes the failure unreadable.
    """

    def transport(
        request: urllib.request.Request, timeout: float
    ) -> tuple[int, bytes]:
        raise exception

    return transport
