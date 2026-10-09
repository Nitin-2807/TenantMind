"""Tests for the FastAPI application foundation.

Isolation rules, mirroring :mod:`tests.test_config`:

* No test reads the developer's real ``.env``. Assertions that need a known
  configuration build an isolated ``Settings`` with ``_env_file=None`` and
  pass it to :func:`app.main.create_app`.
* No test depends on PostgreSQL, Redis, Qdrant, the Gemini API, or the
  network. The health probes do no I/O, and ``TestClient`` talks to the ASGI
  app in-process.
* No test prints a secret value. Sentinel credentials are asserted *absent*
  from responses and schema, never echoed.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, settings
from app.main import (
    DESCRIPTION,
    DOCS_URL,
    OPENAPI_URL,
    REDOC_URL,
    app,
    create_app,
)


def make_settings(**overrides: Any) -> Settings:
    """Build isolated ``Settings`` with env-file loading disabled.

    ``pydantic-settings`` accepts ``_env_file`` at runtime, but the
    ``BaseSettings`` constructor typing does not expose this parameter to
    mypy. The cast is confined to this helper, matching the same pattern used
    in :mod:`tests.test_config`, rather than weakening mypy project-wide.
    """
    constructor = cast(Any, Settings)
    return constructor(_env_file=None, **overrides)


@pytest.fixture
def client() -> TestClient:
    """A test client for the canonical application, served in-process."""
    return TestClient(app)


@pytest.fixture
def isolated_client() -> TestClient:
    """A client for an app built from a known, fully explicit configuration."""
    return TestClient(create_app(make_settings(app_name="TenantMind-Isolated")))


# ---------------------------------------------------------------------------
# Test 1 — application creation
# ---------------------------------------------------------------------------


def test_app_is_a_fastapi_instance() -> None:
    assert isinstance(app, FastAPI)


def test_create_app_returns_a_fastapi_instance() -> None:
    assert isinstance(create_app(), FastAPI)


def test_create_app_builds_independent_applications() -> None:
    """A factory must not hand every caller the same mutable object."""
    first = create_app()
    second = create_app()

    assert first is not second
    assert first is not app
    assert second is not app


def test_application_registers_no_startup_or_shutdown_handlers() -> None:
    """Phase 1 owns no resources, so there is nothing to start or stop.

    FastAPI always installs a default no-op lifespan, so ``lifespan_context``
    is never ``None`` and asserting on it proves nothing. The meaningful
    property is that no *handler* was registered: empty handler lists mean no
    connection pool, cache client, worker, or model is warmed up at startup.
    """
    fresh = create_app()

    assert fresh.router.on_startup == []
    assert fresh.router.on_shutdown == []


def test_lifespan_starts_and_serves_without_any_infrastructure() -> None:
    """Drive the real startup path and prove it needs nothing external.

    PostgreSQL, Redis, Qdrant, Celery and the LLM provider are all absent in
    Phase 1. Entering and leaving the lifespan must still succeed and serve
    traffic, which is only true if startup does no I/O.
    """
    fresh = create_app()

    with TestClient(fresh) as started:
        assert started.get("/health").json() == {"status": "ok"}
        assert started.get("/health/ready").json() == {"status": "ready"}


# ---------------------------------------------------------------------------
# Test 2 — metadata
# ---------------------------------------------------------------------------


def test_metadata_comes_from_configuration() -> None:
    assert app.title == settings.app_name
    assert app.version == settings.app_version
    assert app.description == DESCRIPTION


def test_metadata_follows_supplied_settings() -> None:
    """Proves the metadata is wired to configuration, not coincidentally equal."""
    custom = make_settings(app_name="TenantMind-Renamed", app_version="9.9.9")
    application = create_app(custom)

    assert application.title == "TenantMind-Renamed"
    assert application.version == "9.9.9"
    # Not overridden by the caller's settings: the static description stands.
    assert application.description == DESCRIPTION


def test_tenantmind_metadata_is_sensible() -> None:
    assert app.title == "TenantMind"
    assert app.description == "Multi-Tenant Enterprise AI Knowledge Platform"
    assert app.version == "0.1.0"


def test_documentation_urls_are_declared() -> None:
    assert app.openapi_url == OPENAPI_URL == "/openapi.json"
    assert app.docs_url == DOCS_URL == "/docs"
    assert app.redoc_url == REDOC_URL == "/redoc"


def test_docs_are_exposed_outside_production(isolated_client: TestClient) -> None:
    assert isolated_client.get("/docs").status_code == 200
    assert isolated_client.get("/redoc").status_code == 200


def test_interactive_docs_are_disabled_in_production() -> None:
    """Production must not publish an interactive attack-surface inventory."""
    production = create_app(
        make_settings(app_env="production", jwt_secret_key="a" * 32)
    )

    assert production.docs_url is None
    assert production.redoc_url is None

    production_client = TestClient(production)
    assert production_client.get("/docs").status_code == 404
    # The machine-readable schema stays available in every environment.
    assert production_client.get("/openapi.json").status_code == 200


# ---------------------------------------------------------------------------
# Test 3 — OpenAPI
# ---------------------------------------------------------------------------


def test_openapi_document_is_served(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200

    document = response.json()
    # The version is whatever the installed FastAPI generates; it is read from
    # the application rather than hard-coded in the test.
    assert document["openapi"] == app.openapi_version
    assert document["openapi"].startswith("3.")
    assert document["info"]["title"] == "TenantMind"
    assert document["info"]["version"] == settings.app_version


def test_openapi_document_includes_both_health_paths(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    assert "/health" in paths
    assert "/health/ready" in paths
    assert paths["/health"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("HealthResponse")
    assert paths["/health/ready"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"].endswith("ReadinessResponse")


def test_openapi_documents_exactly_the_implemented_routes(client: TestClient) -> None:
    """The API document must describe the real surface, and no more.

    The assertion is an exact set equality, in both directions: a route that
    exists but is undocumented fails it, and — the reason it is written this way
    — a route that is documented but does not exist also fails it. An
    "unimplemented route" is a documentation promise the server does not keep,
    and it is how a client ends up integrating against an endpoint that 404s.

    Renamed and rewritten at Phase 2.3. The original asserted
    ``paths == {"/health", "/health/ready"}``, which was correct for Phase 1 and
    became wrong the moment a real router was mounted. The *shape* of the
    assertion — a closed set, not a subset check — is unchanged, and both Phase 2.4
    and PMP Phase 4 extended the set rather than relaxing the check, which is the
    point of writing it this way.

    Note the two namespaces. The health probes are at the root because
    infrastructure checks must survive an API version change; the business
    surface is under the configured ``/api/v1`` prefix. ``/auth/*``,
    ``/users/*``, ``/documents/*`` and ``/query`` are PMP §15's paths as
    *unversioned* names, and the served paths carry the prefix.

    The five document paths are PMP §15's five, and their count is asserted
    separately by :func:`test_the_document_surface_is_exactly_the_five_the_pmp_names`,
    so that a future phase adding a sixth route fails a test whose *name* says what
    changed rather than this one, whose name says "the OpenAPI document". ``/query`` is
    PMP Phase 4's addition: one route, added because §15 lists it, not because a
    retrieval implementation happened to be finished. Phase 7 adds the rest of the
    ``Query`` group — ``/query/history``, ``/query/stream`` and
    ``/query/{{query_id}}/feedback`` (PMP §22) — and ``/admin/usage`` under
    Administration.
    """
    paths = set(client.get("/openapi.json").json()["paths"])

    assert paths == {
        "/health",
        "/health/ready",
        f"{settings.api_v1_prefix}/auth/register-tenant",
        f"{settings.api_v1_prefix}/auth/login",
        f"{settings.api_v1_prefix}/auth/refresh",
        f"{settings.api_v1_prefix}/auth/logout",
        f"{settings.api_v1_prefix}/users",
        f"{settings.api_v1_prefix}/users/invite",
        f"{settings.api_v1_prefix}/users/{{user_id}}/role",
        f"{settings.api_v1_prefix}/users/{{user_id}}",
        f"{settings.api_v1_prefix}/documents",
        f"{settings.api_v1_prefix}/documents/{{document_id}}",
        f"{settings.api_v1_prefix}/admin/documents/{{document_id}}/status",
        f"{settings.api_v1_prefix}/admin/usage",
        f"{settings.api_v1_prefix}/query",
        f"{settings.api_v1_prefix}/query/history",
        f"{settings.api_v1_prefix}/query/stream",
        f"{settings.api_v1_prefix}/query/{{query_id}}/feedback",
    }


def test_the_document_surface_is_exactly_the_five_the_pmp_names(
    client: TestClient,
) -> None:
    """PMP §15 defines five document endpoints, and there are five.

    Asserted as ``(path, method)`` pairs rather than paths, because PMP §15's list
    is a list of operations — ``POST /documents`` and ``GET /documents`` share a
    path and are two of the five. Counting paths instead would let a sixth
    *operation* hide behind an existing path, which is the most likely way this
    surface could grow unnoticed.
    """
    document_operations = {
        (path, method)
        for path, operations in client.get("/openapi.json").json()["paths"].items()
        if "/document" in path
        for method in operations
    }

    prefix = settings.api_v1_prefix
    assert document_operations == {
        (f"{prefix}/documents", "get"),
        (f"{prefix}/documents", "post"),
        (f"{prefix}/documents/{{document_id}}", "get"),
        (f"{prefix}/documents/{{document_id}}", "delete"),
        (f"{prefix}/admin/documents/{{document_id}}/status", "get"),
    }


def test_there_is_no_document_update_endpoint(client: TestClient) -> None:
    """No ``PATCH`` or ``PUT`` may exist on a document.

    PMP §15 defines none, and this is the gap a later phase is most likely to fill
    without noticing: an update route would allow ``allowed_roles`` to be rewritten
    after the fact, handing a restricted document to whoever can reach the endpoint.
    Phase 2.4's own service layer exposes no such writer either, so the router
    check and the service check agree.
    """
    document_paths = {
        path: operations
        for path, operations in client.get("/openapi.json").json()["paths"].items()
        if "/document" in path
    }

    for path, operations in document_paths.items():
        assert not {"patch", "put"} & set(operations), (path, sorted(operations))


# ---------------------------------------------------------------------------
# Test 4 — API prefix architecture
# ---------------------------------------------------------------------------


def test_api_v1_prefix_is_available_to_the_application() -> None:
    assert app.state.api_v1_prefix == settings.api_v1_prefix
    assert app.state.api_v1_prefix == "/api/v1"


def test_api_v1_prefix_follows_supplied_settings() -> None:
    application = create_app(make_settings(api_v1_prefix="/api/v2"))

    assert application.state.api_v1_prefix == "/api/v2"


def test_health_routes_are_not_nested_under_the_api_prefix(
    client: TestClient,
) -> None:
    """Infrastructure probes must not move when the API version changes."""
    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").status_code == 200

    prefix = app.state.api_v1_prefix
    assert client.get(f"{prefix}/health").status_code == 404
    assert client.get(f"{prefix}/health/ready").status_code == 404


# ---------------------------------------------------------------------------
# Test 5 — routing behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/api",
        "/api/v1",
        # Phase 2.4 replaced `/api/v1/documents` in this list with paths that are
        # still unrouted. That entry was asserting something real when it was
        # written — the versioned namespace was genuinely empty — and it became
        # false the moment `documents.router` was mounted, so keeping it would have
        # meant deleting a failing assertion instead of re-aiming it.
        "/api/v1/queries",
        "/api/v1/admin/ingestion/jobs",
        "/healthz",
        "/health/live",
        "/does-not-exist",
    ],
)
def test_unknown_routes_return_404(client: TestClient, path: str) -> None:
    """Confirms normal routing still works and no route is accidentally shadowing.

    ``/api/v1`` and the two unimplemented business paths are included
    specifically to prove the versioned namespace holds the routers that exist and
    nothing else — a stubbed or auto-created route would return ``405`` or ``401``
    here instead of ``404``.
    """
    response = client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_an_unrouted_document_subpath_returns_404(client: TestClient) -> None:
    """``/documents/{id}`` exists; a path *below* it does not.

    Guards against a future router declaration accidentally swallowing the whole
    ``/documents`` subtree with a catch-all path parameter, which would turn every
    typo into a ``401`` from the auth dependency rather than a ``404``.
    """
    response = client.get("/api/v1/documents/anything/anything")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_health_endpoints_reject_wrong_methods(client: TestClient) -> None:
    """Probes are read-only; a write attempt must not be handled."""
    assert client.post("/health").status_code == 405
    assert client.post("/health/ready").status_code == 405


# ---------------------------------------------------------------------------
# Test 6 — the application is not a catch-all
# ---------------------------------------------------------------------------


def test_application_adds_no_middleware_or_global_dependencies(
    client: TestClient,
) -> None:
    """No middleware, and no dependency applied to the whole application.

    CORS, rate limiting and request-ID middleware remain later-phase concerns
    and are still absent; asserting that keeps the boundary honest and makes an
    accidental early addition visible in review.

    Rewritten at Phase 2.3. The original also asserted that the OpenAPI
    document declared **no** security scheme, which was true while the
    application had no authenticated route. It is no longer true, and it should
    not be: ``/users/*`` is authenticated by
    :class:`~fastapi.HTTPBearer`, so the document now declares exactly one
    scheme. The replacement asserts the scheme that is actually declared — an
    HTTP bearer token, named — rather than asserting its absence, which is the
    stronger statement: an unrecognised or extra scheme fails the test.
    """
    assert app.user_middleware == []
    assert app.dependency_overrides == {}

    security_schemes = app.openapi().get("components", {}).get("securitySchemes")
    assert security_schemes == {
        "BearerAccessToken": {
            "type": "http",
            "description": (
                "A TenantMind access token. Obtain it from the login exchange; "
                "do not construct one."
            ),
            "scheme": "bearer",
        }
    }
