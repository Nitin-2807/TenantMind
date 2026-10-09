"""ASGI entry point for the TenantMind API.

This module is the first executable application layer. It builds the FastAPI
instance, attaches the health router, and exposes ``app`` for the ASGI server::

    ../.venv/bin/uvicorn app.main:app

Design notes
------------

**Factory.** :func:`create_app` builds the application explicitly instead of
mutating a module global, so tests can construct isolated instances against
supplied settings rather than monkeypatching module state.

**Configuration over constants.** Everything that can differ per deployment —
name, version, API prefix, documentation exposure — comes from
:class:`~app.core.config.Settings`. Only the static product description and
the OpenAPI document paths live here, because they are fixed for the product.

**No lifespan handler.** The application owns no resource that needs acquiring
or releasing at startup: no connection pool is opened here, no Redis or Qdrant
client is constructed, no Celery worker is started, and no model is loaded. A
lifespan hook that opens nothing is dead weight and would only be written to be
deleted later. It is added in the phase that first has something to manage.

**Mounting.** The health probes sit at the application root, outside the
versioned namespace, so they survive an API version change. Business routers
are mounted under :attr:`~app.core.config.Settings.api_v1_prefix`; PMP §15 lists
its paths unversioned and those are the paths *within* that namespace.

**Docs exposure.** The interactive Swagger/ReDoc UIs describe the entire
attack surface, so they are enabled in every environment except production.
The raw ``/openapi.json`` document stays available in all environments,
including production, because machine-readable schema is a normal deployment
requirement.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.documents import admin_router as documents_admin_router
from app.api.documents import router as documents_router
from app.api.health import router as health_router
from app.api.history import router as history_router
from app.api.query import router as query_router
from app.api.query_stream import router as query_stream_router
from app.api.users import router as users_router
from app.core.config import Settings, settings

#: Static, environment-independent product description. Anything that varies
#: per deployment comes from settings instead.
DESCRIPTION = "Multi-Tenant Enterprise AI Knowledge Platform"

#: OpenAPI document paths, named so the documentation surface is declared in
#: one place rather than inherited implicitly from library defaults.
OPENAPI_URL = "/openapi.json"
DOCS_URL = "/docs"
REDOC_URL = "/redoc"


def create_app(app_settings: Settings | None = None) -> FastAPI:
    """Build and return a configured TenantMind FastAPI application.

    ``app_settings`` defaults to the canonical
    :data:`app.core.config.settings` singleton. Tests pass an isolated instance
    to prove that metadata and routing genuinely follow configuration instead
    of matching by coincidence.

    No lifespan handler is registered. Phases 0 through 2 own no resource that
    needs acquiring or releasing at startup: no connection pool is opened here
    (the engine is created lazily on first use), no client is constructed, and
    no model is loaded. A lifespan hook that opens nothing is dead weight and
    would only be written to be deleted later. It is added in the phase that
    first has something to manage.

    Two mounting rules, and the difference is deliberate
    ---------------------------------------------------
    The health probes are mounted at the application root so that they keep
    working across API version changes — an infrastructure check that 404s after
    a version bump is a failed deployment, not a safe one.

    Business routers are mounted under
    :attr:`~app.core.config.Settings.api_v1_prefix`, which is ``/api/v1``. PMP
    §15 lists its paths unversioned (``POST /auth/login``); those are the paths
    *within* the versioned API, and the served path is the prefix plus them. The
    prefix is read from configuration rather than written as a literal so a
    deployment can move the whole versioned surface in one setting.
    """
    config = settings if app_settings is None else app_settings

    # Interactive docs are a development and operations convenience. In
    # production they hand an attacker the full route inventory, so they are
    # switched off. The raw schema document remains served.
    expose_docs = config.app_env != "production"

    application = FastAPI(
        title=config.app_name,
        description=DESCRIPTION,
        version=config.app_version,
        openapi_url=OPENAPI_URL,
        docs_url=DOCS_URL if expose_docs else None,
        redoc_url=REDOC_URL if expose_docs else None,
    )

    # Recorded on `state` as well as used below, so the mounted layout is
    # discoverable from the application object without reading this function.
    application.state.api_v1_prefix = config.api_v1_prefix

    # Infrastructure probes, outside the versioned namespace on purpose.
    application.include_router(health_router)

    # Business surface (PMP §15). Authentication, user management, document management
    # and querying exist; the ingestion router belongs to a later phase and is not
    # stubbed here.
    #
    # `documents.admin_router` is mounted separately from `documents.router` even
    # though both live in one module, so the administrator-only status route is a
    # distinct group in the API document — which is how PMP §15 presents it. `query`'s
    # router is mounted the same way for the same reason: it is its own group in §15.
    application.include_router(auth_router, prefix=config.api_v1_prefix)
    application.include_router(users_router, prefix=config.api_v1_prefix)
    application.include_router(documents_router, prefix=config.api_v1_prefix)
    application.include_router(
        documents_admin_router, prefix=config.api_v1_prefix
    )
    application.include_router(query_router, prefix=config.api_v1_prefix)
    application.include_router(query_stream_router, prefix=config.api_v1_prefix)
    application.include_router(history_router, prefix=config.api_v1_prefix)
    application.include_router(admin_router, prefix=config.api_v1_prefix)

    return application


#: The ASGI application object served by uvicorn and imported by tests.
app: FastAPI = create_app()
