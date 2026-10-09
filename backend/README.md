# TenantMind — Backend

## Purpose

This directory contains the FastAPI backend for **TenantMind**, a multi-tenant
enterprise AI knowledge platform.

Organizations upload internal documents (employee handbooks, HR policies,
technical documentation, SOPs, compliance documents). TenantMind indexes those
documents asynchronously into a vector store and answers user questions using
Retrieval-Augmented Generation, with citations back to the source chunks.

The engineering problem is deliberately **not** "build a document chatbot". It
is:

> How can a shared AI platform securely serve multiple organizations while
> ensuring that retrieval, authorization, storage, caching, and generated
> responses remain isolated and grounded?

That means two concerns run through every layer of this codebase:

1. **SaaS backend engineering** — multi-tenancy, authentication, RBAC,
   asynchronous processing, caching, rate limiting.
2. **AI/RAG engineering** — parsing, chunking, embeddings, hybrid retrieval,
   reranking, grounded generation, citations.

## Architecture

```text
                          ┌──────────────────────┐
                          │    API Client        │
                          │  (Swagger / Postman) │
                          └──────────┬───────────┘
                                     │
                               HTTPS / SSE
                                     │
                                     ▼
                          ┌──────────────────────┐
                          │      FastAPI API     │
                          │                      │
                          │ JWT │ RBAC │ Rate    │
                          │ Limit │ Request ID   │
                          └──────────┬───────────┘
                                     │
               ┌─────────────────────┼─────────────────────┐
               │                     │                     │
               ▼                     ▼                     ▼
         PostgreSQL                Redis                Qdrant
               │                     │                     │
               │                     ▼                     │
               │               Task Queue                  │
               │                     │                     │
               │                     ▼                     │
               │              ┌──────────────┐             │
               │              │ Celery Worker│             │
               │              └──────┬───────┘             │
               │                     │                     │
               │              Parse → Chunk → Embed       │
               │                     │                     │
               │                     └────────────────────►│
               │                                           │
               └───────────────────────────────────────────┘
```

### Upload / write path

```text
User → POST /documents → JWT auth → RBAC check → file validation
    → object storage → PostgreSQL document row → queue ingestion job → 202 Accepted
```

Background: `Worker → read → parse → clean → chunk → embed → Qdrant upsert → update status`

Document states: `processing` → `ready` | `failed`

### Query / read path

```text
Question → JWT auth → tenant context → rate limit → cache lookup
        → query embedding → hybrid retrieval (dense ‖ BM25) → RRF fusion
        → tenant + role filters → rerank → top 5 context chunks
        → prompt construction → LLM → SSE stream → answer + citations → query log
```

### Package layout

```text
backend/app/
├── api/             # FastAPI routers and dependencies (auth, RBAC, tenant context)
├── core/            # Configuration, security primitives (JWT, hashing), logging, errors
├── middleware/      # Request ID, rate limiting, CORS, security headers
├── models/          # ORM models (tenants, users, documents, query logs, ...)
├── schemas/         # Pydantic request/response schemas
├── repositories/    # Tenant-scoped data access — the only layer that writes SQL
├── services/        # Business logic (auth, documents, RAG, caching, audit)
└── workers/         # Celery application and background tasks
```

Layering rules, in priority order:

1. Routers (`api/`) never touch a database session directly.
2. Services never build SQL; they call `repositories/`.
3. `repositories/` never derive a tenant id from request data — the tenant comes
   from the authenticated identity only.
4. Every repository method requires `tenant_id` as an explicit argument. A
   missing tenant id is an error, never a silent data leak.

## Current implementation phase

**Phase 1 complete. Phase 2 (Database and multi-tenancy): Steps 1 and 2
complete — core models and the multi-tenancy foundation, then the
authentication identity foundation. Both PostgreSQL integration gates passed
against a live server. Step 3 (the `/auth/*` endpoints) not started.**

What exists right now:

- Directory structure and Python package markers (`__init__.py`)
- `requirements.txt` and `requirements-dev.txt`, fully pinned, with all
  packages installed
- Python 3.12.14 virtual environment at the repository root (`.venv/`)
- `.env.example` with configuration placeholders, and `.gitignore`
- Typed configuration (`app/core/config.py`) with a cached `settings` object,
  `SecretStr` credentials, and production-secret validation
- FastAPI application foundation (`app/main.py`, `app/api/health.py`,
  `app/schemas/health.py`) with `/health`, `/health/ready`, and OpenAPI
- Database foundation (`app/db/`): engine construction, session lifecycle,
  readiness probe, and Alembic configured to resolve its DSN from `Settings`
- Models (`app/models/`): `Tenant`, `User`, and `RefreshToken`, with
  `tenant_id` a required, indexed foreign key supplied by `TenantScopedMixin`
- Migrations `0001` (tenants, users) and `0002` (refresh_tokens)
- Security primitives (`app/core/security.py`): Argon2id password hashing, and
  HS256 access/refresh token creation and verification
- Auth service (`app/services/auth.py`): login identity resolution, token-pair
  issuance, rotate-on-use refresh exchange, logout, bulk revocation
- `AuthenticatedIdentity` (`app/core/identity.py`) and the
  `get_current_identity` dependency (`app/api/deps.py`) that establishes tenant
  context from a verified access token
- Ruff and mypy clean from `backend/`

What does **not** exist yet, by design:

- **No `/auth/*` endpoints.** PMP §15's login, refresh, logout and me routes are
  the next step. Everything they need exists; the router does not.
- No RBAC enforcement. `role` travels on the identity and is typed, but nothing
  checks it — that is route-level work in the next step.
- No repositories, `documents`, or `query_logs` tables
- No RAG pipeline, vector store, cache, worker, or business API routes

### Using the identity dependency

`get_current_identity` is the only door into tenant-scoped code. It takes no
tenant argument, because the only tenant it can produce is the one inside a
verified token:

```python
from app.api.deps import CurrentIdentity

@router.get("/documents")
def list_documents(identity: CurrentIdentity) -> list[Document]:
    # `identity.tenant_id` came from the signed token, never from the request.
    return repository.find_all(tenant_id=identity.tenant_id)
```

Every authentication failure — absent header, wrong scheme, bad signature,
expired, wrong token type, unknown role — produces one identical `401`. The
reason is logged, never returned.

### PostgreSQL integration environment

The model and migration tests run against SQLite by default, which verifies the
tenant-isolation rules but not PostgreSQL's own behaviour (native `UUID`,
`TIMESTAMPTZ` round-tripping, `psycopg` binding, constraint enforcement). A real
PostgreSQL server for that is provided by `docker-compose.yml` at the repository
root.

The compose service is a **disposable** database: the integration suite issues
`DROP TABLE`, and refuses any DSN whose database name lacks a disposable-name
marker. Point it only at `tenantmind_test` or similar.

```text
database : tenantmind_test
role     : tenantmind_test
port     : 127.0.0.1:5432   (loopback only — never exposed)
image    : postgres:16.15-bookworm
```

#### Start PostgreSQL

The password is required and has no default, so nothing starts with a
well-known credential checked into git. Supply it from your shell:

```bash
cd ..
export TENANTMIND_TEST_PG_PASSWORD='<any local password you choose>'
docker compose up -d --wait
```

#### Check PostgreSQL status

```bash
docker compose ps          # STATUS must read "(healthy)"
docker compose logs -f postgres
```

#### Stop PostgreSQL

```bash
docker compose stop        # keep the data volume
docker compose down        # remove the container, keep the data
docker compose down -v     # remove the volume too — destroys the database
```

#### Run the integration tests

Export the DSN for the current shell, then run the suite from `backend/`. The
DSN is supplied per-run and is deliberately never written to `.env`,
`.env.example`, or any committed file.

```bash
export TENANTMIND_TEST_PG_DSN="postgresql+psycopg://tenantmind_test:${TENANTMIND_TEST_PG_PASSWORD}@127.0.0.1:5432/tenantmind_test"

cd backend
../.venv/bin/pytest tests/test_postgres_integration.py -v
```

Without that variable the PostgreSQL tests skip and the rest of the suite still
passes, so a machine with no Docker can run everything except this gate. The
skip is asserted to be caused *only* by the missing DSN, so a broken fixture
cannot masquerade as a correctly waiting suite.

#### Alembic against the test database

`alembic/env.py` resolves its DSN from `Settings`, so export `DATABASE_URL`
alongside the DSN above to drive the CLI directly:

```bash
export DATABASE_URL="$TENANTMIND_TEST_PG_DSN"

../.venv/bin/alembic upgrade head    # apply
../.venv/bin/alembic current         # inspect
../.venv/bin/alembic check            # assert models and migrations agree
../.venv/bin/alembic downgrade base   # remove
```

Subsequent phases, in order: the `/auth/*` and user-management endpoints →
document management → RAG foundation → background processing → advanced
retrieval → production features → testing and evaluation → deployment →
documentation.

## Canonical tooling root

**`backend/` is the canonical Python application and tooling root.** Run all
Python commands from this directory, not from the repository root.

```bash
cd backend

../.venv/bin/pytest                              # test suite
../.venv/bin/ruff check .                        # lint
../.venv/bin/mypy .                              # type check
../.venv/bin/pip check                           # dependency integrity
../.venv/bin/uvicorn app.main:app                # run the API
```

The virtual environment lives one level up, at the repository root
(`../.venv`), and is shared by every phase.

This is required, not stylistic. The importable package is `app`, which
resolves only when `backend/` is on `sys.path` — so from the repository root
`from app.core.config import settings` raises `ModuleNotFoundError`, and
Ruff classifies the `app` imports differently (first-party versus
third-party), which changes the expected import ordering in every test and
source module. Root-level invocations of those two commands are therefore
**not canonical** and their results are not a signal about the code. There is
no `pyproject.toml` or `ruff.toml`; tooling runs on library defaults from
this directory.

## Dependency installation

**All dependencies must be installed through the requirements files in this
directory, and only through them.**

```bash
# from the backend/ directory
pip install -r requirements.txt
pip install -r requirements-dev.txt     # development/test tooling
```

Rules:

- `requirements.txt` holds runtime dependencies. `requirements-dev.txt` holds
  development and testing tooling only, and is never required to run the
  application.
- Do **not** install packages ad hoc with `pip install <package>` while
  developing. Any new dependency belongs in a requirements file in the same
  commit that starts using it.
- Do not vendor dependencies into the repository and do not commit a
  virtual environment.
- Python 3.12 is the target interpreter.

Configuration is supplied through environment variables. Copy `.env.example`
from the repository root to `.env` in the same place, and fill in the
placeholders. `backend/app/core/config.py` resolves that file automatically.
Real secrets must never be committed.
