"""Request-scoped FastAPI dependencies.

Several kinds live here, and keeping them in one module is deliberate:
:func:`get_current_identity` establishes *who* the caller is, :data:`AppSettings`
hands a route its configuration, and :func:`get_file_store` builds the document
store that route will write into. The retrieval side adds three more of the same
shape — :func:`get_embedding_provider`, :func:`get_generation_provider` and
:data:`VectorStore` — so that a handler takes what it needs as parameters rather
than reaching for module-level state.

:func:`get_file_store` was the first dependency here that builds a resource, and is
here for the same reason the other two authentication/config dependencies are:
``storage_root`` is a live setting, and a test that uploads a document must be able
to redirect it. It is also the only one whose construction is side-effect free, which
its docstring records. The three retrieval dependencies build heavier objects — a
cached Qdrant store, two providers — and each exists for the same overriding reason:
every one of them is chosen by configuration or by an interface with several
implementations, so a test has to be able to substitute its own through
``app.dependency_overrides`` rather than mutating process state.

``get_current_identity`` is the only door into tenant-scoped application code.
It answers one question — *who is this request, and which tenant do they belong
to?* — and it answers it from a verified bearer token and nothing else.

Authentication versus authorization
----------------------------------
These are deliberately different functions and this module does not contain the
second one:

* **Authentication** (:func:`get_current_identity`) proves *who*. It verifies a
  signature, an expiry, a token type and a claim shape, then returns an
  :class:`~app.core.identity.AuthenticatedIdentity`.
* **Authorization** — *what they may do* — is route-level RBAC over
  ``identity.role``, and belongs to Phase 2 Step 3. Nothing here checks a role,
  and no function in this module should grow one: a dependency that both
  authenticates and authorizes is impossible to reuse in a route that needs
  authentication but a different role rule.

Why ``auto_error=False``
------------------------
FastAPI's :class:`~fastapi.HTTPBearer` can raise its own ``403`` for a missing
or malformed header. This dependency sets ``auto_error=False`` and raises a
single ``401`` itself, for two reasons: ``401`` is the correct status for a
missing credential (``403`` means "authenticated, but not allowed", which is
false here), and one uniform failure is what keeps the reason — absent header,
bad scheme, bad signature, expired, wrong type — from becoming an oracle.

What is not in the error
-----------------------
The detail string is a single fixed sentence. It never contains a PyJWT
exception type, a claim name, a token fragment, a user id, or a database error.
:func:`app.core.security.decode_token` raises with the precise reason in its
message precisely so it can be *logged*; this module logs it and returns
nothing. Operators need it; an attacker does not.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings
from app.core.identity import AuthenticatedIdentity
from app.core.security import TokenValidationError
from app.rag.embeddings import EmbeddingProvider, build_embedding_provider
from app.rag.generation import GenerationProvider, build_generation_provider
from app.storage.local import LocalFileStore
from app.storage.qdrant import QdrantVectorStore
from app.storage.qdrant import get_vector_store as build_cached_vector_store

#: The single message returned for every authentication failure.
UNAUTHORIZED_DETAIL = "Could not validate credentials"

#: Attached to the 401 so a client knows which scheme to retry with, per
#: RFC 6750 §3.
WWW_AUTHENTICATE_VALUE = "Bearer"

_logger = logging.getLogger(__name__)

#: Scheme extraction only. ``auto_error=False`` is required, not stylistic:
#: with the default ``True`` FastAPI raises its own ``403`` before this
#: function runs, which would both use the wrong status code and let the
#: "missing vs malformed header" distinction leak.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAccessToken",
    description=(
        "A TenantMind access token. Obtain it from the login exchange; do not "
        "construct one."
    ),
)


def get_current_identity(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
    app_settings: Annotated[Settings, Depends(get_settings)],
) -> AuthenticatedIdentity:
    """Resolve the caller's identity from a bearer access token.

    Args:
        credentials: injected by :class:`~fastapi.HTTPBearer`. ``None`` when no
            ``Authorization`` header was sent, or when the scheme was not
            ``Bearer``.
        app_settings: injected by :func:`app.core.config.get_settings`. Taken
            by dependency injection rather than read from a module global, so a
            test substitutes a known configuration through
            ``app.dependency_overrides`` instead of mutating process state.

    Returns:
        The verified :class:`~app.core.identity.AuthenticatedIdentity`. Its
        ``tenant_id`` is the tenant context for the rest of the request, and it
        came from a signed claim — never from the request body, a header, or a
        query parameter.

    Raises:
        fastapi.HTTPException: ``401`` for every authentication failure. The
            ``WWW-Authenticate`` header is always set.
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=UNAUTHORIZED_DETAIL,
        headers={"WWW-Authenticate": WWW_AUTHENTICATE_VALUE},
    )

    if credentials is None or not credentials.credentials:
        raise unauthorized
    if credentials.scheme.lower() != "bearer":
        raise unauthorized

    try:
        return AuthenticatedIdentity.from_access_token(
            credentials.credentials, app_settings=app_settings
        )
    except TokenValidationError as exc:
        # Log the reason, return nothing. `exc` carries the PyJWT exception
        # type and which claim failed, which is exactly what is wanted in a log
        # and exactly what must not reach the client.
        _logger.warning("access token rejected: %s", exc)
        raise unauthorized from exc


#: The annotated form to use as a route parameter type. Declared here rather
#: than repeated per route so that every protected endpoint depends on the same
#: function object, which is what makes
#: ``app.dependency_overrides[get_current_identity]`` work in tests.
CurrentIdentity = Annotated[AuthenticatedIdentity, Depends(get_current_identity)]

#: The annotated form for a route that needs the application settings but does
#: not need an identity — the unauthenticated endpoints.
#:
#: The same optional-settings-by-dependency-injection argument that applies to
#: :func:`get_current_identity` applies here: a handler that read
#: :data:`app.core.config.settings` directly would be untestable without mutating
#: process state, because ``app.dependency_overrides[get_settings]`` could not
#: reach it. Declared with ``Annotated`` rather than as a ``= Depends(...)``
#: default so the call is not made in a default argument value, which is a
#: FastAPI pattern the linter rightly flags (B008).
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_file_store(app_settings: AppSettings) -> LocalFileStore:
    """Build the document store for this request.

    Present as a dependency rather than constructed inside the handler for one
    reason that is about testability: ``storage_root`` is now a *live* setting, so
    a test that exercises upload or delete has to redirect it. A handler calling
    ``LocalFileStore(app_settings.storage_root)`` internally could only be
    redirected by mutating settings inside the handler's own process, whereas
    ``app.dependency_overrides[get_file_store]`` lets a test supply a store over
    ``tmp_path`` outright — which is also how the rest of this project avoids
    writing to the developer's real ``storage/`` directory during a test run.

    A store is cheap: it resolves one path in ``__init__`` and creates no directory
    until something is written, so building one per request costs a path resolution
    and no filesystem mutation.

    Args:
        app_settings: injected by :func:`app.core.config.get_settings`, so the
            resolved :attr:`~app.core.config.Settings.storage_root` follows the
            same override path as every other setting.
    """
    return LocalFileStore(app_settings.storage_root)


#: The annotated form for a route that needs the document store.
FileStore = Annotated[LocalFileStore, Depends(get_file_store)]


def get_embedding_provider(app_settings: AppSettings) -> EmbeddingProvider:
    """Build the embedding provider this request's retrieval will use.

    The read-side counterpart to :func:`app.rag.embeddings.build_embedding_provider`'s
    ingestion caller, and a dependency for the reason :func:`get_file_store` is one: the
    provider is chosen by configuration (``embedding_provider``), so a test that needs
    retrieval to work offline must be able to redirect it without mutating process state.
    ``app.dependency_overrides[get_embedding_provider]`` does that; constructing the
    provider inside the handler or the service could not.

    Raises:
        EmbeddingProviderUnavailable: the configured provider has no credential. Surfaced
            as a ``503`` by the route rather than as a ``500``, because it is a
            deployment misconfiguration rather than a fault in the request.
    """
    return build_embedding_provider(app_settings)


def get_generation_provider(app_settings: AppSettings) -> GenerationProvider:
    """Build the generation provider this request's answer will come from.

    Present so a test can supply a deterministic provider — see
    ``tests/generation.py`` — and so ``POST /query`` never constructs one inline. The
    Gemini provider makes no network call until :meth:`GenerationProvider.generate` is
    invoked, so this is a cheap object to build per request; it is not cached the way
    :func:`app.storage.qdrant.get_vector_store` is, and the difference is deliberate.

    Raises:
        GenerationProviderUnavailable: no API key is configured.
    """
    return build_generation_provider(app_settings)


def get_vector_store(app_settings: AppSettings) -> QdrantVectorStore:
    """Hand the request the process-wide store, resolving settings by injection.

    This is a one-line wrapper, and it exists because of a defect it fixes rather than
    for any symmetry with the three dependencies above.

    :func:`app.storage.qdrant.get_vector_store` is the cached *factory*: it takes the
    settings object as an ordinary argument, because
    :mod:`app.services.ingestion` calls it directly and that call must keep working.
    Used directly as a FastAPI dependency it is a fault, and a silent one. A dependency
    parameter that is a plain pydantic model is not an error — FastAPI treats it as
    *another request body field*. So ``Annotated[QdrantVectorStore,
    Depends(get_vector_store)]`` produced an implicit body model containing the handler's
    own body plus a required ``settings`` field, and ``POST /query`` answered ``422`` for
    every request a client could correctly send. Nothing raised; the OpenAPI document
    simply described a two-field body that no caller could satisfy, and only a request
    through the real application would ever notice.

    The wrapper's parameter is :data:`AppSettings`, so ``Depends(get_settings)`` is what
    fills it, exactly as for :func:`get_file_store`. This module is the layer that knows
    about dependency injection, and the fix is placed here rather than by importing
    ``fastapi`` into :mod:`app.storage.qdrant`, which has to stay usable from ingestion
    and from a script.

    Args:
        app_settings: injected by :func:`app.core.config.get_settings`, so the store is
            built from the same settings object every other dependency sees — including
            a test's ``dependency_overrides[get_settings]``.
    """
    return build_cached_vector_store(app_settings)


#: The annotated provider forms for a route that retrieves or generates.
#:
#: Declared with :data:`Annotated` for the reason :data:`AppSettings` documents: a
#: dependency called in a default argument value is a FastAPI pattern the linter rightly
#: flags (B008), and building these in a handler body would put them beyond the reach of
#: ``dependency_overrides``.
EmbeddingProviderDep = Annotated[EmbeddingProvider, Depends(get_embedding_provider)]
GenerationProviderDep = Annotated[GenerationProvider, Depends(get_generation_provider)]
VectorStore = Annotated[QdrantVectorStore, Depends(get_vector_store)]