"""Central, typed configuration for TenantMind.

This module is the single source of environment configuration for the whole
application. Application code reads :data:`settings`; it must not call
``os.getenv`` directly (PMP §8 Maintainability, "Environment-based
configuration").

Value precedence is the standard pydantic-settings order, highest first:

    1. environment variables
    2. the ``.env`` file in the project root
    3. the defaults declared on :class:`Settings`

Scope note: this module declares configuration only. Several settings describe
subsystems that are not implemented yet (PostgreSQL, Redis, Qdrant, the LLM,
rate limiting). Those fields exist so that later phases read configuration
rather than inventing it, and none of them are used here.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
# backend/app/core/config.py -> parents[1] is backend/, whose parent is the
# project root. The project root is where `.env` lives, alongside the committed
# `.env.example` template.
_BACKEND_ROOT: Path = Path(__file__).resolve().parents[2]
PROJECT_ROOT: Path = _BACKEND_ROOT.parent
ENV_FILE: Path = PROJECT_ROOT / ".env"

AppEnv = Literal["development", "test", "staging", "production"]

#: Length floor for the JWT signing secret once the app runs in production.
MIN_PRODUCTION_SECRET_LENGTH = 32

#: Minimum signing-key length whenever a key is actually *used*, in every
#: environment. RFC 7518 §3.2 requires a key at least as long as the hash
#: output for HMAC, and PyJWT emits an ``InsecureKeyLengthWarning`` below 32
#: bytes for HS256. Enforcing it at the point of use — rather than only in
#: production, as :data:`MIN_PRODUCTION_SECRET_LENGTH` does — turns a warning
#: that would otherwise be printed on every single token operation into a
#: startup-shaped failure with an actionable message.
MIN_JWT_SECRET_LENGTH = 32

#: Well-known placeholder that must never reach production. Checked explicitly
#: so that copying `.env.example` verbatim cannot silently produce a weak key.
INSECURE_PLACEHOLDER_SECRETS = frozenset(
    {
        "change-me-in-development",
        "changeme",
        "secret",
        "insecure",
        "your-secret-key-here",
    }
)

#: URL schemes Celery's Redis transport understands, and the only ones this
#: project will configure. ``rediss`` is TLS to Redis and is included because a
#: managed Redis broker in a real deployment will be remote; ``unix`` is
#: excluded on purpose, because a local-socket broker is not the deployment
#: shape in PMP §23 and accepting it here would advertise a topology this
#: project's compose file and configuration both contradict.
_REDIS_URL_SCHEMES = ("redis://", "rediss://")


class JwtSecretNotConfiguredError(RuntimeError):
    """Raised when a token operation is attempted without a usable secret.

    Named after :class:`app.db.engine.DatabaseNotConfiguredError` and for the
    same reason: a missing configuration value is an operational fault, not a
    bad request, so it must not be reported to a client as a 4xx. The
    authentication code catches this nowhere; it propagates, because the correct
    response is to fix the deployment.
    """


class Settings(BaseSettings):
    """Typed application settings loaded from the environment.

    Credential-bearing values are :class:`~pydantic.SecretStr` so that they are
    masked in ``repr()``, in log output, and in validation errors. Consumers
    that genuinely need the raw value call ``.get_secret_value()`` at the point
    of use.
    """

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application ------------------------------------------------------
    app_name: str = "TenantMind"
    # Deployment identity, surfaced as the OpenAPI ``info.version``. Declared
    # here rather than in ``main.py`` so the version is changeable per
    # deployment without editing code, and so it is documented in
    # ``.env.example`` alongside every other setting. No version-management
    # package is used; the value is simply declared.
    app_version: str = "0.1.0"
    app_env: AppEnv = "development"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    # -- Database (Phase 2) ----------------------------------------------
    # PostgreSQL is not implemented yet. ``database_url`` commonly embeds a
    # password, so it is masked. It stays optional so that Phase 1 tests run
    # without a database; Phase 2 is where it becomes required.
    database_url: SecretStr | None = None

    # -- Redis / Celery broker (Phase 5 Step 1) ----------------------------
    # Connected as of Phase 5 Step 1: this is the Celery broker and result
    # backend, read in exactly one place, `app.workers.celery_app`. See that
    # module for why one URL serves both roles rather than two settings.
    #
    # Now a :class:`~pydantic.SecretStr`, which it was not for the four phases
    # it sat here unused. The reason is Phase 5 Step 1 itself: `redis://` URLs
    # routinely carry the password inline, exactly like `database_url` above,
    # so once this field started being *read* by a broker it became a
    # credential-bearing field and belongs in the same category. Before this
    # step it held nothing sensitive — the local default has no password — so
    # leaving it a plain `str` would have been defensible; leaving it one now
    # would put a broker password into any `repr(settings)`, into a pydantic
    # validation error, and into anything that logs the configuration object.
    #
    # The default is unchanged and still has no credential, so a bare
    # development install and every existing test keep working without one.
    #
    # A consequence worth knowing before generating a password: kombu parses
    # this with `urllib.parse`, so the credentials inside it must be
    # percent-encoded, and a password containing a raw `/`, `@`, `:` or `#`
    # produces a `ValueError` from the parser rather than a connection error.
    # Generate one with `secrets.token_urlsafe(32)`, which is already safe.
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")

    # -- Qdrant (Phase 2.5) ----------------------------------------------
    # PMP §13 "Vector Database Strategy": Qdrant, one collection per tenant.
    # The client is built in `app/storage/qdrant.py` from these three values and
    # nowhere else, so no module reads an environment variable directly.
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: SecretStr | None = None
    #: Per-request timeout for Qdrant calls, in whole seconds.
    #:
    #: Bounded rather than left to the client's own default because ingestion is
    #: synchronous in this phase: an unbounded call blocks the thread that owns the
    #: database session, and the request is holding a pooled connection while it
    #: does. Five seconds is generous for a local Qdrant and short enough that a
    #: hung server produces a recorded ingestion failure rather than a stalled
    #: worker.
    #:
    #: An :class:`int`, not a :class:`float`, because
    #: :class:`~qdrant_client.QdrantClient` takes an integer number of seconds and a
    #: fractional value is silently truncated by the client rather than honoured — so
    #: declaring it ``float`` here would advertise a precision that does not exist.
    qdrant_timeout_seconds: int = 5

    # -- Embeddings (Phase 2.5) ------------------------------------------
    # PMP §23 lists "Sentence Transformers / API model" for the Embeddings row —
    # two options, and this is the API-model one. The sentence-transformers route
    # requires `torch`, which a plain requirements file cannot pin to CPU-only
    # wheels, so it would resolve to a ~2.5 GB CUDA download. See the rationale in
    # `requirements.txt`; switching later is one provider subclass and one branch
    # in `app.rag.embeddings.build_embedding_provider`, and nothing in the
    # ingestion pipeline changes.
    #
    # `Literal`, not a bare `str`, for the same reason `llm_provider` is: a typo in
    # the environment becomes a startup validation error rather than a provider
    # that is silently unavailable at the first ingestion.
    #
    # `static` is the third option and is the odd one out: it returns the same unit
    # vector for every input, so it has no semantic content at all and retrieval
    # against it returns the first chunk of the first document for any query. It
    # exists so the ingestion pipeline can be exercised end-to-end with no external
    # dependency — and the production validator below refuses it precisely because it
    # would silently destroy the product if it ever reached a real deployment.
    embedding_provider: Literal["gemini", "static"] = "gemini"
    #: The Gemini embedding model. Configurable because model choice is a
    #: deployment decision (PMP §23 lists several) and because switching models
    #: without switching dimensions is the mistake this pairing below prevents.
    embedding_model: str = "text-embedding-004"
    #: Vector width the collection is created with, and the width every returned
    #: vector must have.
    #:
    #: `text-embedding-004` natively produces 768 dimensions, which is why that is
    #: the default rather than a round number. Qdrant collections cannot be
    #: resized, so a wrong value here is not recoverable by configuration alone —
    #: see `app.storage.qdrant.QdrantVectorStore`, which checks an existing
    #: collection's configured width against this one and refuses rather than
    #: writing vectors the store cannot index.
    embedding_dimension: int = 768
    #: Gemini's REST base URL. Declared so a test can point the client at a local
    #: stub and so a self-hosted or proxied endpoint is a configuration change
    #: rather than a code edit. Not a credential, hence not a `SecretStr`.
    gemini_api_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    #: Upper bound on how many chunks go into one embedding request.
    #:
    #: Exists because a 200-page PDF produces thousands of chunks, and sending
    #: them in a single request would build one enormous JSON body in memory and
    #: rely on the provider accepting it. Batching bounds both. 100 is Google's
    #: documented per-request limit for `batchEmbedContents`, so this is the
    #: provider's ceiling rather than an arbitrary one.
    embedding_batch_size: int = 100

    # -- Chunking (Phase 2.5) --------------------------------------------
    #: Target chunk size in characters.
    #:
    #: The PMP specifies neither a size nor an overlap anywhere, so this is a
    #: project decision rather than a requirement, and it is stated as one. 1200
    #: characters is roughly 300 English tokens, which sits under the ~512-token
    #: window of every model the PMP's technology stack names while holding enough
    #: context for a paragraph plus its neighbours. Characters rather than tokens
    #: so that chunking is pure Python with no tokenizer dependency and no
    #: tokenizer version skew.
    chunk_size_chars: int = 1200
    #: How much of the previous chunk each chunk repeats, in characters.
    #:
    #: 200 is ~17% of a chunk. Enough that a sentence spanning a boundary appears
    #: whole in at least one chunk — the failure mode that loses meaning at a
    #: boundary — and small enough that the index does not spend a sixth of its
    #: storage on duplicated text. Must be less than `chunk_size_chars`; the
    #: validator below refuses a configuration where it is not, because an overlap
    #: at or above the size produces an infinite loop rather than a wrong answer.
    chunk_overlap_chars: int = 200

    # -- LLM (Phase 4) ---------------------------------------------------
    # The provider integration is a stdlib HTTP call against the Gemini REST API —
    # see `app.rag.generation`. No SDK is installed, deliberately: the only thing
    # this project needs from a provider is one authenticated POST and one JSON
    # response, and an SDK would add a dependency whose upgrade cadence is not this
    # project's to control for no capability it uses.
    # ``gemini`` is the only supported value today; alternatives are added
    # when an integration actually exists rather than pre-declared here.
    llm_provider: Literal["gemini"] = "gemini"
    gemini_api_key: SecretStr | None = None
    #: The generation model answers are produced by.
    #:
    #: A setting rather than a constant because the model is the one part of this
    #: pipeline an operator has a reason to change without a code change, and
    #: because the value appears in log lines describing which model answered a
    #: query. `app_env` is not validated against it: a nonexistent model name is a
    #: provider error surfaced as a failed request, not a configuration this project
    #: should refuse to start over — the model catalogue moves faster than this
    #: file.
    llm_model: str = "gemini-2.0-flash"

    # -- Retrieval (Phase 4) ---------------------------------------------
    #: How many chunks a query retrieves.
    #:
    #: Five is a project decision, not a PMP requirement: PMP §22 Phase 4 asks for
    #: "basic semantic search" and names no k. Five sits under the ~8-10 chunks
    #: usually needed for good recall on a small corpus while keeping the generated
    #: answer focused, and `query_max_context_chars` below is the real ceiling — a
    #: larger k cannot produce more evidence than that budget allows.
    query_top_k: int = 5
    #: The character ceiling on the evidence block handed to the model.
    #:
    #: The bound that actually protects the request path, and bounded in *characters*
    #: rather than tokens for the same reason `chunk_size_chars` is: no tokenizer
    #: dependency and no tokenizer version skew, so the number below is exactly the
    #: number of characters the provider receives. 12000 is roughly 3000 English
    #: tokens, comfortably inside the context window of every model the PMP's
    #: technology stack names, with room left for the instructions and the question.
    #: Retrieval drops whole chunks to stay inside it rather than slicing one, because
    #: half a chunk is a worse citation target than no chunk.
    query_max_context_chars: int = 12_000
    #: The longest question accepted, in characters.
    #:
    #: A request-length bound, not a semantic one. A question is text a human typed
    #: into one box; 500 characters is several sentences, and anything beyond it is
    #: either a pasted document or an attempt to spend someone else's tokens. Refused
    #: at validation with a 422 rather than truncated, because silently shortening a
    #: question answers a question the user did not ask.
    query_max_question_chars: int = 500

    # -- Advanced Retrieval (Phase 6) --------------------------------------
    #: Enable hybrid retrieval (dense + BM25 fused with RRF).
    #: When False, only dense vector search is used (Phase 4 behavior).
    hybrid_retrieval_enabled: bool = True
    #: Enable cross-encoder reranking of hybrid candidates.
    #: When False, RRF-fused candidates are used directly without reranking.
    rerank_enabled: bool = True
    #: Cross-encoder model name for reranking.
    #: Defaults to a small, fast model. Override for different quality/latency tradeoffs.
    rerank_model: str | None = None
    #: Maximum candidates to fetch from each retriever before fusion.
    #: Larger values give more signal to fusion/reranker but increase latency.
    hybrid_candidate_limit: int = 50

    # -- Security / authentication (Phase 2 Step 2) ----------------------
    # Phase 2 Step 2 added the JWT and Argon2 implementation, so these three
    # fields are no longer declared "for later" — they are read on every token
    # operation. The secret stays optional at construction time so that
    # development and test environments can build a ``Settings`` without one;
    # :meth:`_validate_production_settings` makes it mandatory in production,
    # and :meth:`jwt_signing_key` refuses to sign or verify without one. There
    # is deliberately NO development fallback secret in this module: a default
    # secret that works in development is a secret that silently ships.
    jwt_secret_key: SecretStr | None = None
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # The signing algorithm, and it is configuration rather than a constant so
    # that the *server* decides which algorithm is acceptable. PyJWT takes the
    # accepted-algorithm list as a decode-time argument; feeding it this single
    # value is what makes `alg: none` and `alg: RS256` tokens from a client
    # impossible rather than merely discouraged.
    #
    # Declared as a single-valued Literal on purpose. The value is still worth
    # declaring: it makes a typo in the environment a startup-time validation
    # error instead of a silently ignored variable, and it records the decision
    # in one place. Asymmetric algorithms are deliberately NOT offered —
    # accepting RS256/ES256 would require distributing a public key to
    # whichever services verify these tokens, and this architecture has exactly
    # one issuer and one verifier (the same deployment). If that ever changes,
    # widen this Literal and add the key material in the same change; do not
    # accept an algorithm "whatever the token says".
    #
    # `iss`/`aud` are intentionally absent. PMP §15 declares a single set of
    # `/auth/*` endpoints on one service, and no requirement in the PMP states
    # that a token must be rejected because it was minted for another audience.
    # Adding unverified issuer/audience claims would be security theatre.
    jwt_algorithm: Literal["HS256"] = "HS256"

    # -- CORS -------------------------------------------------------------
    # Used by the Next.js frontend (PMP §9 architecture). Typed as a list so
    # application code never re-parses a comma-separated string. ``NoDecode``
    # disables pydantic-settings' automatic JSON decoding so that the
    # validator below can accept JSON arrays, comma-separated strings, or an
    # already-parsed list.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # -- Uploads (Phase 3) -----------------------------------------------
    max_upload_size_mb: int = 10

    #: Where uploaded documents are written, for Phase 2.4.
    #:
    #: Resolved against the project root by the validator below rather than by
    #: whichever process happens to be running, so ``STORAGE_ROOT=./storage`` means
    #: the same directory whether the API is started from ``backend/`` or from the
    #: repository root. Without that, a relative default would silently split
    #: uploads across two directories depending on how the server was launched —
    #: and the failure would look like documents vanishing.
    #:
    #: Absolute paths are honoured unchanged, which is what lets a test point the
    #: store at ``tmp_path``.
    storage_root: Path = Path("storage")

    # -- Rate limiting (Phase 5) -----------------------------------------
    # Declared only; Redis-backed limiting is not implemented.
    rate_limit_per_minute: int = 60
    #: Rate limit window in seconds. Default 60 matches ``rate_limit_per_minute``.
    rate_limit_window_seconds: int = 60

    # -- Query cache (Phase 7) -------------------------------------------
    #: Time-to-live for cached query responses, in seconds.
    #: Set to 0 to disable caching.
    query_cache_ttl_seconds: int = 300

    # -- Ingestion reconciliation (Phase 5 Step 4B) ------------------------
    #: How long a document may stay ``processing`` before it is considered
    #: stale and eligible for re-enqueueing. A document that has been
    #: ``processing`` this long has either crashed before the worker could pick it
    #: up (the commit-before-publish window from Step 3), or its worker died
    #: mid-ingestion. The threshold must be longer than any normal ingestion run
    #: but short enough that an operator does not wait hours to learn a job is
    #: stuck. 300 seconds (5 minutes) is a development default; production
    #: deployments should tune it to the observed p99 ingestion latency.
    ingestion_stale_threshold_seconds: int = 300

    # -- Validators -------------------------------------------------------

    @field_validator("storage_root")
    @classmethod
    def _resolve_storage_root(cls, value: Path) -> Path:
        """Make ``storage_root`` absolute against the project root.

        Uses :meth:`~pathlib.Path.absolute` rather than
        :meth:`~pathlib.Path.resolve` on purpose: ``resolve`` also follows
        symlinks, which would make the configured value depend on the shape of
        whatever filesystem the project is checked out on. The containment check in
        :mod:`app.storage.local` resolves separately, at the point where canonical
        paths are what is being compared.
        """
        if value.is_absolute():
            return value
        return (PROJECT_ROOT / value).absolute()

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: Any) -> Any:
        """Accept a JSON array, a comma-separated string, or a real list.

        Malformed JSON is deliberately left to Pydantic, which converts it
        into a validation error rather than a bare ``JSONDecodeError``.
        """
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if not raw:
            return []
        if raw.startswith("["):
            return json.loads(raw)
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @model_validator(mode="after")
    def _validate_ingestion_settings(self) -> Settings:
        """Refuse an ingestion configuration that cannot produce correct vectors.

        Two relationships, both of which fail *silently* if left unchecked:

        **Overlap must be smaller than the chunk size.** A chunker that advances its
        window by ``size - overlap`` makes no progress at all when the two are
        equal, and moves backwards when the overlap is larger. That is an infinite
        loop inside a request, not a wrong answer — the worst failure mode
        available — so it is refused at construction where an operator will see it
        as a configuration error rather than as a hung worker.

        **A positive dimension.** Zero or negative is not a smaller vector, it is a
        collection Qdrant cannot create and an embedding call that cannot succeed.

        **The broker URL must actually name Redis.** Celery's own default is
        ``amqp://localhost``, so an empty or misspelled ``redis_url`` does not
        fail loudly — it resolves to a broker transport nobody in this project
        runs, and the symptom is a worker that starts cleanly, accepts every
        task, and delivers none of them. That is the silent-failure shape this
        method exists to prevent, so a blank or non-Redis URL is refused here
        rather than discovered as a queue that never drains.
        """
        if self.chunk_overlap_chars >= self.chunk_size_chars:
            raise ValueError(
                f"chunk_overlap_chars ({self.chunk_overlap_chars}) must be less "
                f"than chunk_size_chars ({self.chunk_size_chars}); an overlap at "
                "or above the size prevents the chunker from advancing"
            )
        if self.chunk_size_chars < 1:
            raise ValueError("chunk_size_chars must be at least 1")
        if self.chunk_overlap_chars < 0:
            raise ValueError("chunk_overlap_chars must not be negative")
        if self.embedding_dimension < 1:
            raise ValueError("embedding_dimension must be at least 1")
        if self.embedding_batch_size < 1:
            raise ValueError("embedding_batch_size must be at least 1")
        if self.qdrant_timeout_seconds <= 0:
            raise ValueError("qdrant_timeout_seconds must be greater than 0")
        if self.query_top_k < 1:
            raise ValueError("query_top_k must be at least 1")
        if self.query_max_context_chars < 1:
            raise ValueError("query_max_context_chars must be at least 1")
        if self.query_max_question_chars < 1:
            raise ValueError("query_max_question_chars must be at least 1")
        if not self.llm_model.strip():
            raise ValueError("llm_model must not be blank")

        # `redis_url` is the Celery broker and result backend as of Phase 5 Step 1.
        # The check is on the *scheme*, not on reachability: refusing at
        # construction because a server is down would make `Settings` unusable
        # for a worker that is deliberately started before its broker finishes
        # booting. What must be caught here is only the case where Celery would
        # silently substitute a different transport.
        broker_url = self.redis_url.get_secret_value()
        if not broker_url.strip():
            raise ValueError(
                "redis_url must not be blank; it is the Celery broker and "
                "result backend. Set REDIS_URL in the environment or .env."
            )
        if not broker_url.startswith(_REDIS_URL_SCHEMES):
            raise ValueError(
                f"redis_url must begin with one of "
                f"{sorted(_REDIS_URL_SCHEMES)}, got {broker_url.split('://', 1)[0]!r}. "
                "Celery's own fallback is amqp://localhost, which this project "
                "does not run, so a mistyped scheme would produce a worker that "
                "starts and delivers nothing."
            )
        return self

    @model_validator(mode="after")
    def _validate_production_settings(self) -> Settings:
        """Refuse to start with insecure configuration in production.

        This is deliberately scoped to ``app_env == "production"`` so that
        development and test environments stay frictionless, as required by the
        Phase 1 brief.
        """
        if self.app_env != "production":
            return self

        secret = (
            self.jwt_secret_key.get_secret_value()
            if self.jwt_secret_key is not None
            else ""
        )

        if not secret:
            raise ValueError(
                "jwt_secret_key is required when app_env is 'production'. "
                "Set it from the environment; never rely on a default."
            )
        if secret.strip().lower() in INSECURE_PLACEHOLDER_SECRETS:
            raise ValueError(
                "jwt_secret_key is a well-known placeholder and cannot be "
                "used when app_env is 'production'."
            )
        if len(secret) < MIN_PRODUCTION_SECRET_LENGTH:
            raise ValueError(
                "jwt_secret_key must be at least "
                f"{MIN_PRODUCTION_SECRET_LENGTH} characters when app_env is "
                "'production'."
            )
        if self.debug:
            raise ValueError("debug must be False when app_env is 'production'.")

        # An API-model embedding provider needs its credential. Checked here for
        # the same reason the JWT secret is: a provider that fails on the first
        # ingestion request turns a deployment mistake into a per-document failure,
        # which is discovered one document at a time instead of at startup.
        #
        # The static provider is a different case and is refused for a stronger
        # reason: it is not less secure, it is *useless*. Every text maps to the
        # same unit vector, so retrieval would return the same chunk for every
        # query while every status endpoint reported success. That is a silent,
        # total failure of the product's only feature, and it is far cheaper to
        # refuse at startup than to discover in a demo.
        if self.embedding_provider == "static" and self.app_env == "production":
            raise ValueError(
                "embedding_provider must not be 'static' when app_env is "
                "'production'. It returns one fixed vector for every input, so "
                "every query would retrieve the same chunk. Configure a real "
                "provider from the environment."
            )

        # `gemini_api_key` is deliberately NOT required here, and its absence is a
        # real decision rather than an oversight. It is checked in
        # `app.rag.embeddings.build_embedding_provider`, the one place the key is
        # read, for three reasons.
        #
        # It is not this service's own security posture. Every other check above is
        # about how *TenantMind* behaves — its own secret, its own debug flag, its own
        # search results. A third party's credential is a different thing, and holding
        # it in `Settings` is holding a value the API server never sends.
        #
        # It would couple unrelated phases. Requiring it at startup means no
        # production `Settings` can be built without a Phase 2.5 integration
        # configured — so a Phase 1 assertion about a strong JWT secret in production
        # starts failing because of a field that assertion knows nothing about, and
        # fixing it means editing a test written before ingestion existed.
        #
        # And failing later is more useful. A missing key cannot break the API; it can
        # only break ingestion, so the error belongs where ingestion starts, naming the
        # variable to set. A startup failure would instead take the whole service down
        # for a fault that affects one background job.
        #
        # `embedding_provider = "static"` stays refused above for the opposite reason:
        # that one silently returns the *wrong* answers, so it is worth a hard stop.

        return self

    # -- Derived accessors ----------------------------------------------

    def jwt_signing_key(self) -> str:
        """Return the raw JWT signing secret, or refuse.

        This is the *only* sanctioned way for application code to obtain the
        secret. It exists so that "no secret configured" and "secret too weak"
        are detected in one place with one message, instead of each caller
        re-deriving the rule.

        The production-only validator above deliberately does not run here:
        development and test environments are allowed to construct ``Settings``
        without a secret, and it is only once something tries to actually sign
        or verify a token that its absence becomes a fault. Note that the
        placeholder check is applied in every environment, not just production
        — a ``changeme`` secret that works in development is a secret that
        eventually reaches production.

        Raises:
            JwtSecretNotConfiguredError: if the secret is missing, blank, a
                known placeholder, or shorter than
                :data:`MIN_JWT_SECRET_LENGTH`.
        """
        secret = (
            self.jwt_secret_key.get_secret_value()
            if self.jwt_secret_key is not None
            else ""
        )

        # A whitespace-only value is an unset value. Without this it falls
        # through to the length check and gets reported as "too short", which
        # sends an operator looking for a length problem they do not have.
        if not secret.strip():
            raise JwtSecretNotConfiguredError(
                "jwt_secret_key is not set, so tokens cannot be signed or "
                "verified. Set JWT_SECRET_KEY in the environment or .env; "
                "generate one with "
                "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`. "
                "There is intentionally no development default."
            )
        if secret.strip().lower() in INSECURE_PLACEHOLDER_SECRETS:
            raise JwtSecretNotConfiguredError(
                "jwt_secret_key is a well-known placeholder value and cannot "
                "sign or verify tokens. Generate a real random secret."
            )
        if len(secret) < MIN_JWT_SECRET_LENGTH:
            raise JwtSecretNotConfiguredError(
                f"jwt_secret_key must be at least {MIN_JWT_SECRET_LENGTH} "
                "characters to sign HS256 tokens (RFC 7518 section 3.2)."
            )
        return secret

    #: Hashable by identity, for the functools caches that key on ``Settings``.
    #:
    #: Pydantic v2 models are unhashable by default (``__hash__`` is set to
    #: ``None``) unless frozen. The Qdrant vector-store factory is cached with
    #: ``functools.cache`` on the ``Settings`` object itself — deliberately, so
    #: two distinct instances build separate stores — which requires the object
    #: to be hashable. ``id()`` preserves exactly that object-identity
    #: semantics: equality stays Pydantic's fieldwise comparison, and no
    #: ``Settings`` is ever looked up by value, so the identity hash is sound
    #: for every use in this project.
    def __hash__(self) -> int:
        return id(self)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    ``lru_cache`` (standard library, no extra dependency) guarantees a single
    validated ``Settings`` object per process. Settings are read from the
    environment, so re-reading them repeatedly would be both wasteful and
    capable of yielding inconsistent values mid-request. Tests that need a
    fresh, isolated instance call ``Settings(...)`` directly or clear this
    cache with ``get_settings.cache_clear()``.
    """
    return Settings()


#: Canonical settings object for the rest of the application to import.
settings: Settings = get_settings()
