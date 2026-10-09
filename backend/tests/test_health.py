"""Tests for the health and readiness endpoints.

These probes are the only publicly reachable code in Phase 1, so the tests
here are as much about what the endpoints *must not* do as about the two lines
they do execute.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.main import create_app


def make_settings(**overrides: Any) -> Settings:
    """Build isolated ``Settings`` with env-file loading disabled.

    ``pydantic-settings`` accepts ``_env_file`` at runtime, but the
    ``BaseSettings`` constructor typing does not expose this parameter to
    mypy. The cast is confined to this helper, matching the same pattern used
    in :mod:`tests.test_config`.
    """
    constructor = cast(Any, Settings)
    return constructor(_env_file=None, **overrides)


@pytest.fixture
def client() -> TestClient:
    """A test client for the canonical application, served in-process."""
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Test 1 — liveness
# ---------------------------------------------------------------------------


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_response_is_json(client: TestClient) -> None:
    response = client.get("/health")

    assert response.headers["content-type"].startswith("application/json")


def test_health_body_contains_only_the_status_field(client: TestClient) -> None:
    """A liveness probe must answer with a fixed, minimal shape.

    Any additional field is a potential disclosure channel, so the body is
    asserted to be exactly ``{"status": "ok"}`` and nothing more.
    """
    body = client.get("/health").json()

    assert list(body) == ["status"]


# ---------------------------------------------------------------------------
# Test 2 — readiness
# ---------------------------------------------------------------------------


def test_readiness_returns_ready(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readiness_body_contains_only_the_status_field(client: TestClient) -> None:
    body = client.get("/health/ready").json()

    assert list(body) == ["status"]


def test_readiness_does_not_fabricate_dependency_health(client: TestClient) -> None:
    """Phase 1 has no database, Redis, Qdrant, Celery or LLM to probe.

    Reporting them as healthy would be a lie to whoever monitors this service.
    The endpoints must therefore report readiness of the application alone,
    and say nothing at all about infrastructure.
    """
    for path in ("/health", "/health/ready"):
        body = client.get(path).json()
        keys = {key.lower() for key in body}

        assert keys == {"status"}
        for fabricated in (
            "database",
            "postgres",
            "redis",
            "qdrant",
            "celery",
            "llm",
            "gemini",
            "dependencies",
        ):
            assert fabricated not in keys


def test_health_endpoints_do_not_require_any_credential() -> None:
    """The probes must work on a machine with no configuration at all."""
    bare = create_app(
        make_settings(
            app_name="Bare",
            database_url=None,
            qdrant_api_key=None,
            gemini_api_key=None,
            jwt_secret_key=None,
        )
    )
    bare_client = TestClient(bare)

    assert bare_client.get("/health").json() == {"status": "ok"}
    assert bare_client.get("/health/ready").json() == {"status": "ready"}


# ---------------------------------------------------------------------------
# Test 3 — disclosure
# ---------------------------------------------------------------------------


def test_health_responses_leak_no_configuration() -> None:
    """A fully populated configuration must not surface through the probes."""
    sentinel = "sentinel-that-must-never-be-returned"
    configured = create_app(
        make_settings(
            app_name="TenantMind-Disclosure",
            database_url=SecretStr(f"postgresql://user:{sentinel}@db:5432/tenantmind"),
            qdrant_api_key=SecretStr(sentinel),
            gemini_api_key=SecretStr(sentinel),
            jwt_secret_key=SecretStr(sentinel),
            redis_url=f"redis://:{sentinel}@cache:6379/0",
            qdrant_url=f"http://internal-service:{sentinel}@qdrant:6333",
        )
    )
    client = TestClient(configured)

    for path in ("/health", "/health/ready", "/openapi.json"):
        rendered = client.get(path).text

        assert sentinel not in rendered

    # The application title is public metadata, so it is expected to appear;
    # the credentials that share the same object must not.
    assert configured.title == "TenantMind-Disclosure"
    assert client.get("/health").json() == {"status": "ok"}


def test_health_responses_reveal_no_filesystem_internals(client: TestClient) -> None:
    """Responses must not leak paths, hostnames, or configuration values."""
    for path in ("/health", "/health/ready"):
        rendered = client.get(path).text.lower()

        for internal in ("/home/", "c:\\", ".env", "postgresql://", "redis://"):
            assert internal not in rendered


def test_health_endpoints_are_reachable_without_trailing_slash(
    client: TestClient,
) -> None:
    """Probe URLs configured by orchestrators rarely carry a trailing slash."""
    assert client.get("/health/").status_code == 200
    assert client.get("/health/ready/").status_code == 200


def test_health_endpoints_have_no_query_parameters(client: TestClient) -> None:
    """Unexpected input must not be able to change what a probe reports."""
    assert client.get("/health?verbose=true").json() == {"status": "ok"}
    assert client.get("/health/ready?debug=true").json() == {"status": "ready"}


def test_probes_are_stable_across_repeated_calls(client: TestClient) -> None:
    """A liveness probe must not become flaky under polling."""
    for _ in range(5):
        assert client.get("/health").status_code == 200
        assert client.get("/health/ready").status_code == 200
