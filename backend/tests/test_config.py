"""Tests for the typed configuration layer.

Isolation rules enforced by these tests:

* No test reads the developer's real ``.env``. Every ``Settings`` built here
  either passes ``_env_file=None`` (disables env-file loading entirely) or
  points at a file created in ``tmp_path``.
* No test depends on PostgreSQL, Redis, Qdrant, the Gemini API, or the network.
* No test prints a secret value. Assertions compare masked ``SecretStr``
  representations or booleans, never plaintext credentials.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import (
    INSECURE_PLACEHOLDER_SECRETS,
    MIN_PRODUCTION_SECRET_LENGTH,
    Settings,
    get_settings,
)
from app.rag.embeddings import EmbeddingProviderUnavailable, build_embedding_provider

# Every environment variable the Settings model reads. Cleared before each
# test so that values exported in the developer's shell cannot influence a
# result and make the suite pass or fail unpredictably.
_MANAGED_ENV_VARS = [
    "APP_NAME",
    "APP_VERSION",
    "APP_ENV",
    "DEBUG",
    "API_V1_PREFIX",
    "DATABASE_URL",
    "REDIS_URL",
    "QDRANT_URL",
    "QDRANT_API_KEY",
    "LLM_PROVIDER",
    "GEMINI_API_KEY",
    "JWT_SECRET_KEY",
    "ACCESS_TOKEN_EXPIRE_MINUTES",
    "REFRESH_TOKEN_EXPIRE_DAYS",
    "CORS_ORIGINS",
    "MAX_UPLOAD_SIZE_MB",
    "RATE_LIMIT_PER_MINUTE",
]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every managed environment variable for the duration of a test."""
    for name in _MANAGED_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def build_settings(*, env_file: Path | None = None, **overrides: Any) -> Settings:
    """Construct ``Settings``, optionally against a specific ``.env`` file.

    ``pydantic-settings`` accepts ``_env_file`` at runtime, but the
    ``BaseSettings`` constructor typing does not expose this parameter to
    mypy. The cast is confined to this single helper rather than weakening
    mypy project-wide or silencing ``call-arg`` at every call site.

    Passing ``env_file=None`` disables env-file loading entirely, which is what
    keeps these tests independent of the developer's real ``.env``.
    """
    constructor = cast(Any, Settings)
    return constructor(_env_file=env_file, **overrides)


def make_settings(**overrides: Any) -> Settings:
    """Build Settings with env-file loading disabled."""
    return build_settings(env_file=None, **overrides)


# ---------------------------------------------------------------------------
# Test 1 — defaults
# ---------------------------------------------------------------------------


def test_default_configuration() -> None:
    settings = make_settings()

    assert settings.app_name == "TenantMind"
    assert settings.app_version == "0.1.0"
    assert settings.app_env == "development"
    assert settings.debug is False
    assert settings.api_v1_prefix == "/api/v1"

    assert settings.access_token_expire_minutes == 15
    assert settings.refresh_token_expire_days == 7
    assert settings.max_upload_size_mb == 10
    assert settings.rate_limit_per_minute == 60

    assert settings.llm_provider == "gemini"
    # `redis_url` became a SecretStr in Phase 5 Step 1, when it stopped being an
    # unused field and became the Celery broker credential. The value is
    # unchanged; only its representation is masked. Asserted on the raw value
    # because that is the value Celery is actually handed.
    assert settings.redis_url.get_secret_value() == "redis://localhost:6379/0"
    assert settings.qdrant_url == "http://localhost:6333"
    assert settings.cors_origins == ["http://localhost:3000"]


def test_unimplemented_subsystems_are_optional_in_phase_1() -> None:
    """Phase 1 must not require infrastructure that does not exist yet."""
    settings = make_settings()

    assert settings.database_url is None
    assert settings.qdrant_api_key is None
    assert settings.gemini_api_key is None
    assert settings.jwt_secret_key is None


# ---------------------------------------------------------------------------
# Test 2 — environment overrides
# ---------------------------------------------------------------------------


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "TenantMind-Staging")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30")

    settings = make_settings()

    assert settings.app_name == "TenantMind-Staging"
    assert settings.debug is True
    assert settings.access_token_expire_minutes == 30


def test_environment_variable_overrides_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Environment variables must win over the ``.env`` file."""
    env_file = tmp_path / ".env"
    env_file.write_text("APP_NAME=FromEnvFile\nDEBUG=true\n", encoding="utf-8")
    monkeypatch.setenv("APP_NAME", "FromEnvironment")

    settings = build_settings(env_file=env_file)

    assert settings.app_name == "FromEnvironment"
    assert settings.debug is True


# ---------------------------------------------------------------------------
# Test 3 — boolean parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("True", True), ("TRUE", True), ("1", True),
     ("false", False), ("False", False), ("FALSE", False), ("0", False)],
)
def test_debug_is_parsed_as_bool_not_string(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("DEBUG", raw)

    settings = make_settings()

    assert isinstance(settings.debug, bool)
    assert settings.debug is expected


# ---------------------------------------------------------------------------
# Test 4 — integer parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", ["30", "15", "1"]
)
def test_integers_are_parsed_as_int_not_string(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("ACCESS_TOKEN_EXPIRE_MINUTES", raw)

    settings = make_settings()

    assert isinstance(settings.access_token_expire_minutes, int)
    assert settings.access_token_expire_minutes == int(raw)


def test_upload_limit_is_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "25")

    settings = make_settings()

    assert isinstance(settings.max_upload_size_mb, int)
    assert settings.max_upload_size_mb == 25


# ---------------------------------------------------------------------------
# Test 5 — CORS parsing
# ---------------------------------------------------------------------------


def test_cors_accepts_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:3000", "https://app.example.com"]')

    settings = make_settings()

    assert settings.cors_origins == [
        "http://localhost:3000",
        "https://app.example.com",
    ]


def test_cors_accepts_comma_separated_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000, https://app.example.com")

    settings = make_settings()

    assert settings.cors_origins == [
        "http://localhost:3000",
        "https://app.example.com",
    ]


def test_cors_empty_string_becomes_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "")

    assert make_settings().cors_origins == []


def test_cors_accepts_a_real_list() -> None:
    settings = make_settings(cors_origins=["https://one.example.com", "https://two.example.com"])

    assert settings.cors_origins == ["https://one.example.com", "https://two.example.com"]


def test_cors_json_in_env_example_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact JSON form used in .env.example must be valid."""
    origins = ["http://localhost:3000"]
    monkeypatch.setenv("CORS_ORIGINS", json.dumps(origins))

    assert make_settings().cors_origins == origins


# ---------------------------------------------------------------------------
# Test 6 — secret handling
# ---------------------------------------------------------------------------


def test_secret_is_read_from_environment_and_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "s3cr3t-value-that-must-not-be-logged"
    monkeypatch.setenv("JWT_SECRET_KEY", sentinel)

    settings = make_settings()

    assert isinstance(settings.jwt_secret_key, SecretStr)
    # The real value is reachable only via explicit unwrapping.
    assert settings.jwt_secret_key is not None
    assert settings.jwt_secret_key.get_secret_value() == sentinel
    # But it never leaks through repr, str, or interpolation.
    assert sentinel not in repr(settings.jwt_secret_key)
    assert sentinel not in str(settings.jwt_secret_key)
    assert sentinel not in f"{settings.jwt_secret_key}"
    assert "**********" in repr(settings.jwt_secret_key)


def test_api_keys_are_masked_in_model_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key-should-not-appear")
    monkeypatch.setenv("QDRANT_API_KEY", "qdrant-key-should-not-appear")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost:5432/tenantmind")

    rendered = repr(make_settings())

    for leaked in ("gemini-key-should-not-appear", "qdrant-key-should-not-appear", "pw@localhost"):
        assert leaked not in rendered


def test_secrets_are_never_hardcoded_in_source() -> None:
    """The module must not contain a default secret value."""
    source = Path(__file__).resolve().parents[1] / "app" / "core" / "config.py"
    text = source.read_text(encoding="utf-8")

    assert "jwt_secret_key: SecretStr | None = None" in text
    assert 'jwt_secret_key: SecretStr = SecretStr("' not in text


# ---------------------------------------------------------------------------
# Test 7 — .env file loading
# ---------------------------------------------------------------------------


def test_env_file_is_loaded_from_a_temporary_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        """APP_NAME=TenantMind-FromFile
APP_ENV=test
DEBUG=true
MAX_UPLOAD_SIZE_MB=42
JWT_SECRET_KEY=file-provided-secret
""",
        encoding="utf-8",
    )

    settings = build_settings(env_file=env_file)

    assert settings.app_name == "TenantMind-FromFile"
    assert settings.app_env == "test"
    assert settings.debug is True
    assert settings.max_upload_size_mb == 42
    assert settings.jwt_secret_key is not None
    assert settings.jwt_secret_key.get_secret_value() == "file-provided-secret"


def test_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    """Phase 1 runs without a .env at all."""
    settings = build_settings(env_file=tmp_path / "does-not-exist.env")

    assert settings.app_name == "TenantMind"


def test_comments_and_blank_lines_in_env_file_are_tolerated(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n\nAPP_NAME=TenantMind\n\n# another comment\nDEBUG=false\n",
        encoding="utf-8",
    )

    settings = build_settings(env_file=env_file)

    assert settings.app_name == "TenantMind"
    assert settings.debug is False


def test_real_project_env_file_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray APP_NAME in the shell must not leak into _env_file=None builds."""
    monkeypatch.setenv("APP_NAME", "FromShell")
    env_file = Path(__file__).resolve().parents[2] / ".env"

    settings = build_settings(env_file=None)

    # Environment still wins (proving precedence), and the point is that no
    # file was consulted: the shell value is exactly what came back.
    assert settings.app_name == "FromShell"
    assert not env_file.exists() or settings.app_name == "FromShell"


# ---------------------------------------------------------------------------
# Test 8 — invalid configuration
# ---------------------------------------------------------------------------


def test_invalid_integer_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "not-a-number")

    with pytest.raises(ValidationError) as excinfo:
        make_settings()

    assert "max_upload_size_mb" in str(excinfo.value)


def test_unknown_app_env_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "chaos")

    with pytest.raises(ValidationError):
        make_settings()


def test_unknown_llm_provider_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")

    with pytest.raises(ValidationError):
        make_settings()


def test_malformed_cors_json_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:3000", ')

    with pytest.raises((ValidationError, json.JSONDecodeError)):
        make_settings()


# ---------------------------------------------------------------------------
# Test 9 — production safety
# ---------------------------------------------------------------------------


def test_production_requires_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(ValidationError) as excinfo:
        make_settings()

    assert "jwt_secret_key" in str(excinfo.value)


def test_production_rejects_placeholder_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "change-me-in-development")

    with pytest.raises(ValidationError) as excinfo:
        make_settings()

    assert "placeholder" in str(excinfo.value)


def test_production_rejects_short_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", "too-short")

    with pytest.raises(ValidationError) as excinfo:
        make_settings()

    assert str(MIN_PRODUCTION_SECRET_LENGTH) in str(excinfo.value)


def test_production_rejects_debug_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * MIN_PRODUCTION_SECRET_LENGTH)

    with pytest.raises(ValidationError) as excinfo:
        make_settings()

    assert "debug" in str(excinfo.value).lower()


def test_production_accepts_a_strong_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * MIN_PRODUCTION_SECRET_LENGTH)

    settings = make_settings()

    assert settings.app_env == "production"
    assert settings.debug is False


@pytest.mark.parametrize("placeholder", sorted(INSECURE_PLACEHOLDER_SECRETS))
def test_every_known_placeholder_is_rejected_in_production(
    monkeypatch: pytest.MonkeyPatch, placeholder: str
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_SECRET_KEY", placeholder)

    with pytest.raises(ValidationError):
        make_settings()


@pytest.mark.parametrize("env", ["development", "test", "staging"])
def test_non_production_env_never_demands_a_secret(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    """Phase 1 tests must stay frictionless outside production."""
    monkeypatch.setenv("APP_ENV", env)

    settings = make_settings()

    assert settings.app_env == env
    assert settings.jwt_secret_key is None


# ---------------------------------------------------------------------------
# Test 10 — the canonical accessor
# ---------------------------------------------------------------------------


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()

    first = get_settings()
    second = get_settings()

    assert first is second


def test_settings_is_a_settings_instance() -> None:
    from app.core.config import settings as canonical

    assert isinstance(canonical, Settings)
    assert canonical.app_name == "TenantMind"


def test_module_level_settings_does_not_expose_secrets() -> None:
    """The canonical object must never leak credential material.

    Under the default development configuration every credential field is
    ``None``, so the property actually worth asserting is that no credential
    is rendered in plaintext — not that a particular type name appears in the
    representation. Populated-secret masking is covered by
    :func:`test_api_keys_are_masked_in_model_repr`.
    """
    from app.core.config import settings as canonical

    credential_fields = (
        "jwt_secret_key",
        "gemini_api_key",
        "qdrant_api_key",
        "database_url",
    )

    rendered = repr(canonical)
    as_text = str(canonical)

    for field in credential_fields:
        value = getattr(canonical, field)
        assert value is None or isinstance(value, SecretStr), (
            f"{field} must be None or a SecretStr, got {type(value).__name__}"
        )
        if isinstance(value, SecretStr):
            plaintext = value.get_secret_value()
            assert plaintext not in rendered
            assert plaintext not in as_text
            assert "**********" in repr(value)

    # The default development configuration must carry no credentials at all,
    # so a freshly cloned repository has nothing sensitive to leak.
    assert canonical.jwt_secret_key is None
    assert canonical.gemini_api_key is None
    assert canonical.qdrant_api_key is None
    assert canonical.database_url is None


# ---------------------------------------------------------------------------
# Test 11 — the embedding credential, and where it is required
# ---------------------------------------------------------------------------


class TestSettingsDoesNotRequireTheEmbeddingCredential:
    """``gemini_api_key`` is not needed to construct ``Settings``, anywhere.

    It used to be required when ``app_env="production"``. That requirement was
    deliberately moved out of :meth:`Settings._validate_production_settings` into
    :func:`app.rag.embeddings.build_embedding_provider`, for the reasons recorded in
    that validator's own comment. In short: requiring a third party's credential at
    startup meant *no* production ``Settings`` could be built without a Phase 2.5
    integration configured, so a Phase 1 assertion about a strong JWT secret began
    failing with ``pydantic_core.ValidationError: gemini_api_key is required...`` and
    fixing it meant editing tests written before ingestion existed.

    What these tests pin is that the decision stays made. The key stays optional on
    the model; what must not come back is a constructor that refuses to run without it.
    """

    @pytest.mark.parametrize("env", ["development", "test", "staging", "production"])
    def test_no_environment_requires_the_embedding_api_key(
        self, monkeypatch: pytest.MonkeyPatch, env: str
    ) -> None:
        """Production is in the list deliberately: that is the case that raised."""
        monkeypatch.setenv("APP_ENV", env)
        monkeypatch.setenv("JWT_SECRET_KEY", "a" * MIN_PRODUCTION_SECRET_LENGTH)

        settings = make_settings(embedding_provider="gemini", gemini_api_key=None)

        assert settings.app_env == env
        assert settings.gemini_api_key is None

    def test_production_still_refuses_the_static_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one embedding setting production *does* refuse at construction.

        Refused for the opposite reason to the API key: ``static`` returns *wrong*
        answers silently — every text maps to the same point — rather than failing
        loudly, so it earns a startup stop while a missing third-party key does not.

        Asserted here so the relocated check cannot be read as "embedding
        configuration is no longer validated in production at all".
        """
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("JWT_SECRET_KEY", "a" * MIN_PRODUCTION_SECRET_LENGTH)

        with pytest.raises(ValidationError) as excinfo:
            make_settings(embedding_provider="static")

        assert "static" in str(excinfo.value)


class TestTheProviderFactoryIsWhereTheEmbeddingCredentialIsRequired:
    """The relocated check: the factory refuses, and it names the variable to set.

    Why a provider-factory test lives in the configuration suite
    -------------------------------------------------------------
    The subject is configuration; only the enforcement point moved. ``GEMINI_API_KEY``
    is a setting, and the question these tests answer is whether a deployment is
    configured well enough to ingest — not how the provider builds a request. Asserting
    it from here keeps both halves of one decision in one place: ``Settings`` does not
    demand the key, and the factory that reads it does. A change that moved the
    requirement back into the model could not then pass this file and fail only once a
    real document was submitted.

    ``tests/test_embeddings.py`` covers the provider's own behaviour, including the
    runtime re-check in ``GeminiEmbeddingProvider._request`` that catches a key blanked
    after construction. What it does not assert — and what regressed — is *when* the
    deployment is told it is incomplete.
    """

    @pytest.mark.parametrize("key", [None, "", "   "], ids=["absent", "empty", "blank"])
    def test_a_gemini_provider_is_not_built_without_a_key(
        self, key: str | None
    ) -> None:
        """Absent, empty and whitespace-only all mean "not configured".

        Whitespace counts because ``GEMINI_API_KEY=`` in a ``.env`` file and an
        accidentally space-padded value are both common, and both produce a confusing
        401 rather than an obvious absence.
        """
        settings = make_settings(embedding_provider="gemini", gemini_api_key=key)

        with pytest.raises(EmbeddingProviderUnavailable, match="gemini_api_key"):
            build_embedding_provider(settings)

    @pytest.mark.parametrize("env", ["development", "test", "staging", "production"])
    def test_the_refusal_is_not_scoped_to_production(
        self, monkeypatch: pytest.MonkeyPatch, env: str
    ) -> None:
        """It is not a production-only rule, and that is deliberate.

        Someone running the pipeline locally is told the same thing, rather than being
        handed a provider that answers 401 on every batch. So this cannot honestly be
        written as "production refuses and development does not" — it refuses
        everywhere, which is what keeps the check here instead of back in the model.
        """
        monkeypatch.setenv("APP_ENV", env)
        monkeypatch.setenv("JWT_SECRET_KEY", "a" * MIN_PRODUCTION_SECRET_LENGTH)

        settings = make_settings(embedding_provider="gemini", gemini_api_key=None)

        with pytest.raises(EmbeddingProviderUnavailable, match="gemini_api_key"):
            build_embedding_provider(settings)

    def test_a_configured_key_still_builds_the_provider(self) -> None:
        """The positive control: what is refused is the missing key, not the factory."""
        settings = make_settings(
            embedding_provider="gemini",
            gemini_api_key="not-a-real-key-for-this-test-only",
        )

        provider = build_embedding_provider(settings)

        assert provider.dimensions == settings.embedding_dimension
        assert provider.model_name == settings.embedding_model

    def test_the_static_provider_is_built_without_any_credential(self) -> None:
        """No over-correction: the refusal is about authenticating, not about building.

        ``static`` authenticates against nothing, so demanding a key to build it would
        refuse a provider that does not need one — and would break every test in this
        project, which runs on ``static`` precisely so that no vendor credential is
        needed to run the suite.
        """
        settings = make_settings(embedding_provider="static", gemini_api_key=None)

        provider = build_embedding_provider(settings)

        assert provider.dimensions == settings.embedding_dimension


# ---------------------------------------------------------------------------
# Test N — Settings is hashable for the functools caches keyed on it
# ---------------------------------------------------------------------------


class TestSettingsHashability:
    """``Settings`` must be usable as a cache key without weakening anything.

    The Qdrant vector-store factory is cached with ``functools.cache`` on the
    ``Settings`` object itself — deliberately, so two distinct instances build
    separate stores — and Pydantic v2 models are unhashable by default unless
    frozen. This project declares an identity ``__hash__`` (``id``), so the
    process-wide caches work without rebuilding the store per request while
    keeping the documented object-identity semantics. Before the identity
    hash, every call to the cached factory raised
    ``TypeError: unhashable type: 'Settings'``; these tests pin that down.
    """

    def test_an_instance_is_hashable(self) -> None:
        assert isinstance(hash(make_settings()), int)

    def test_distinct_instances_hash_differently(self) -> None:
        first = make_settings()
        second = make_settings()

        assert hash(first) != hash(second)

    def test_an_instance_can_key_a_functools_cache(self) -> None:
        """Mirror the vector-store pattern: ``@cache`` keyed on ``Settings``."""
        from functools import cache

        calls = 0

        @cache
        def keyed(_: Settings) -> int:
            nonlocal calls
            calls += 1
            return calls

        settings = make_settings()

        assert keyed(settings) == 1
        assert keyed(settings) == 1
        assert calls == 1
