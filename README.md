# TenantMind

**Multi-Tenant Enterprise AI Knowledge Platform**

TenantMind is a secure, multi-tenant SaaS platform that lets organizations upload internal documents and query them through a Retrieval-Augmented Generation (RAG) based AI assistant.

It is not a document chatbot. It is a production-oriented backend exercise in serving multiple organizations from one deployment while keeping their users, documents, vector data, caches, and generated answers isolated from each other and grounded in verifiable sources.

## Features

### Multi-Tenancy & Security
- **Strict tenant isolation** — users, documents, vector data, and query logs are isolated per organization
- **JWT authentication** — access tokens (15 min) + refresh tokens (7 days, rotatable, revocable)
- **Role-based access control** — admin, editor, viewer roles with document-level permissions
- **Admin user management** — invite users, assign roles, deactivate accounts
- **Password security** — Argon2id hashing, minimum 12 characters
- **Refresh token revocation** — immediate logout, prevents session renewal
- **Rate limiting** — declared framework (Redis-backed, not yet enforced)
- **Audit logging** — immutable trail of admin actions

### Document Ingestion
- **Async pipeline** — upload → Celery queue → parse → chunk → embed → Qdrant + PostgreSQL BM25
- **Supported formats** — PDF, DOCX, XLSX, PPTX, TXT, Markdown, HTML
- **Chunking** — 1200 chars with 200 char overlap, page-aware
- **Embeddings** — Google `text-embedding-004` (768 dim) or static test provider
- **Idempotent retries** — transient failures re-queued with exponential backoff
- **Stale reconciliation** — documents stuck in `processing` re-queued after 5 min

### Retrieval (Hybrid)
- **Dense vectors** — Qdrant cosine similarity search (top-k=5)
- **BM25 full-text** — PostgreSQL `tsvector` + GIN indexes on documents + chunks
- **Rank fusion** — Reciprocal Rank Fusion (RRF) combining dense + BM25
- **Optional reranking** — cross-encoder (sentence-transformers, optional dependency)
- **Authorization re-verification** — RBAC re-checked at retrieval time

### Generation
- **Grounded answers** — Gemini 3.5 Flash via Google Generative Language API
- **Citations** — chunk-level citations with `[1]`, `[2]` markers in answer text
- **SSE streaming** — `start` → `answer` → `citations` → `done` events
- **Forced refusal** — "An answer could not be generated right now" when no evidence
- **Cache** — Redis-backed query/answer cache (TTL 300s, disabled in dev)

### Platform & Observability
- **Query history** — paginated, filterable, with latency + status
- **Feedback** — up/down per query, included in admin analytics
- **Admin usage dashboard** — total queries, answered/failed, cache hit rate, feedback breakdown
- **Admin document status** — per-document status management
- **Docker deployment** — PostgreSQL, Redis, Qdrant via compose

## Architecture

```
Frontend (Next.js 16, React 18, TypeScript, Tailwind)
        ↓ Same-origin proxy (Next.js rewrites)
Backend (FastAPI, Python 3.12)
        ├── PostgreSQL 16 (primary DB, BM25, migrations via Alembic)
        ├── Redis 7 (Celery broker, cache, rate limit, session)
        ├── Qdrant 1.19 (per-tenant vector collections, cosine)
        └── Celery (ingestion workers, retry + reconciliation)
                ↓
        RAG Pipeline
                ├── Parse (PDF/DOCX/XLSX/PPTX/TXT/MD/HTML)
                ├── Clean (normalize, dedupe)
                ├── Chunk (1200 chars, 200 overlap)
                ├── Embed (Google text-embedding-004, 768d)
                ├── Index (Qdrant collection + PostgreSQL BM25)
                ├── Retrieve (hybrid dense + BM25, RRF, optional rerank)
                ├── Authorize (re-verify RBAC per chunk)
                ├── Construct context (12000 char budget)
                ├── Generate (Gemini 3.5 Flash, grounded)
                └── Citations (chunk index, doc_id, filename)
        ↓ SSE / REST
Frontend
```

**Component Responsibilities**
- **Frontend** — SSR/CSR pages, auth state (access/refresh in cookies), API client with token refresh, SSE hook for streaming, React 18 + Next.js 16 app router
- **FastAPI** — REST + SSE endpoints, dependency injection (auth, DB, cache), Pydantic validation, OpenAPI docs
- **Celery** — ingestion tasks (parse → chunk → embed → index), retry policy, stale reconciliation beat task
- **PostgreSQL** — tenants, users, documents, chunks, query_logs, refresh_tokens, audit_logs, BM25 indexes
- **Qdrant** — one collection per tenant (`tenant_<uuid>`), cosine distance, 768-dim vectors
- **Redis** — Celery broker/results, query cache, rate limit counters, JWT refresh token revocation list
- **Gemini** — generation only (not embeddings), stateless HTTPS POST, no SDK

## RAG Pipeline

1. **Parse** — extract text from PDF/DOCX/XLSX/PPTX/TXT/MD/HTML via `pypdf`, `python-docx`, `openpyxl`, `markdown-it-py`
2. **Clean** — normalize whitespace, remove control chars, dedupe consecutive lines
3. **Chunk** — 1200 chars, 200 overlap, page-aware (one page ≤ 1200 chars → one chunk)
4. **Embed** — Google `text-embedding-004` (768-dim), batch size 100; fallback static provider for dev
5. **Index** — upsert to Qdrant (`tenant_<uuid>` collection, cosine), upsert to PostgreSQL `document_chunks` with `tsvector`
6. **Retrieve** —
   - Dense: Qdrant top-k=5 cosine
   - BM25: PostgreSQL `ts_rank_cd` on `documents.search_vector` + `document_chunks.search_vector`
   - Fuse: RRF (k=60), then optional cross-encoder rerank
   - Authorize: filter chunks by `document.allowed_roles` vs user role
7. **Context** — pack chunks until 12000 char budget, cite by `[chunk_index]`
8. **Generate** — `gemini-3.5-flash` via REST, prompt enforces "answer from evidence, refuse if missing"
9. **Cite** — replace `[n]` markers with structured citations `{doc_id, filename, chunk_index, page}`

## Multi-Tenancy & Security

- **Tenant identity** — derived from authenticated JWT (`tenant_id` claim), never from request body
- **Tenant-scoped repositories** — every DB query includes `WHERE tenant_id = :tenant_id`
- **Tenant-scoped vector collections** — Qdrant collection per tenant (`tenant_<uuid>`)
- **Document authorization** — `allowed_roles` (nullable, null = all roles), enforced at retrieval
- **Role hierarchy** — admin > editor > viewer, enforced at API + retrieval layers
- **Refresh token revocation** — SHA-256 stored, rotation on use, immediate revocation on logout
- **Password hashing** — Argon2id (m=65536, t=3, p=4), verified via `passlib`
- **Rate limiting** — declared (60/min, 60s window), Redis-backed, not yet enforced
- **Upload limits** — 10 MB max, enforced at streaming write (413 before full receive)
- **Prompt injection defense** — evidence never concatenated into instructions, strict field separation in generation
- **Secrets** — environment variables only, `SecretStr` in Settings, masked in logs/repr

## Tech Stack

### Backend
- Python 3.12
- FastAPI 0.115+
- SQLAlchemy 2.0 (async + sync)
- Alembic 1.14 (migrations)
- Pydantic 2 + pydantic-settings
- Celery 5.4 (Redis broker)
- Qdrant Client 1.13
- Redis-py 5.0
- Google Generative AI (embeddings + generation, no SDK)
- passlib (Argon2id)
- python-jose (JWT)
- pypdf, python-docx, openpyxl, markdown-it-py
- Ruff (lint), mypy (type check), pytest (test)

### RAG / ML
- Qdrant 1.19 (vector store)
- PostgreSQL 16 (BM25 via `tsvector` + GIN)
- Google Generative Language API (`text-embedding-004`, `gemini-3.5-flash`)
- sentence-transformers (optional, cross-encoder reranker)

### Infrastructure
- PostgreSQL 16 (Docker)
- Redis 7 (Docker)
- Qdrant 1.19 (Docker)
- Docker Compose (local dev)

### Frontend
- Next.js 16.4 (app router, Turbopack)
- React 18
- TypeScript 5
- Tailwind CSS 3
- Vitest (unit + E2E)
- ESLint 9 (flat config)

## Project Structure

```
.
├── backend/
│   ├── app/
│   │   ├── api/           # FastAPI routes (auth, docs, query, admin, etc.)
│   │   ├── core/          # Config, security, identity, Redis client
│   │   ├── db/            # SQLAlchemy engine, session, base, health
│   │   ├── middleware/    # CORS, error handling
│   │   ├── models/        # SQLAlchemy ORM models
│   │   ├── rag/           # RAG pipeline (parse, chunk, embed, retrieve, generate)
│   │   ├── repositories/  # Data access layer
│   │   ├── schemas/       # Pydantic request/response models
│   │   ├── services/      # Business logic (auth, ingestion, query, history, etc.)
│   │   ├── storage/       # Local file storage + Qdrant vector store
│   │   ├── workers/       # Celery app + ingestion tasks
│   │   └── main.py        # FastAPI app factory
│   ├── alembic/           # Migrations (7 revisions)
│   ├── evaluation/        # Phase 8 evaluation package (stand-ins + real retrieval)
│   ├── requirements.txt   # Production deps
│   ├── requirements-dev.txt
│   └── tests/             # Unit + integration (postgres, qdrant, worker)
├── frontend/
│   ├── src/
│   │   ├── app/           # Next.js app router pages
│   │   ├── components/    # React components (UI, domain)
│   │   ├── hooks/         # Custom hooks (SSE, document status)
│   │   ├── lib/           # API client, auth, validation, utilities
│   │   └── test/          # Test utilities
│   ├── e2e/               # Vitest E2E tests (live backend)
│   ├── public/            # Static assets
│   ├── package.json
│   ├── next.config.ts
│   └── vitest.config.ts
├── docker-compose.yml     # PostgreSQL, Redis, Qdrant
├── .env.example           # Configuration template
├── .env                   # Local env (gitignored)
├── .gitignore
└── README.md
```

## Prerequisites

- **Python 3.12** (backend)
- **Node.js 20+** (frontend)
- **Docker + Docker Compose** (PostgreSQL, Redis, Qdrant)
- **Gemini API key** (from https://aistudio.google.com/app/apikey)

## Configuration

Create your local environment from the template:

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

| Variable | Required | Description |
|----------|----------|-------------|
| `JWT_SECRET_KEY` | Yes | ≥32 chars, generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `GEMINI_API_KEY` | Yes* | From Google AI Studio (ingestion + generation) |
| `DATABASE_URL` | No | Dev optional; e.g. `postgresql+psycopg://user:pass@localhost:5432/tenantmind` |
| `REDIS_URL` | No | Default `redis://localhost:6379/0` |
| `QDRANT_URL` | No | Default `http://localhost:6333` |
| `EMBEDDING_PROVIDER` | No | `gemini` (default) or `static` (dev only, refused in prod) |

*Ingestion requires either `GEMINI_API_KEY` or `EMBEDDING_PROVIDER=static`.

All other variables have safe defaults in `.env.example`.

## Local Setup

```bash
# 1. Clone
git clone <repository-url>
cd TenantMind

# 2. Backend virtualenv + dependencies
python3.12 -m venv .venv
./.venv/bin/pip install -r backend/requirements.txt

# 3. Frontend dependencies
cd frontend && npm install && cd ..

# 4. Local environment
cp .env.example .env
# Edit .env with your JWT_SECRET_KEY and GEMINI_API_KEY

# 5. Start infrastructure (PostgreSQL, Redis, Qdrant)
export TENANTMIND_TEST_PG_PASSWORD='<your-pg-password>'
export TENANTMIND_TEST_REDIS_PASSWORD='<your-redis-password>'
docker compose up -d --wait

# 6. Run migrations (from backend/)
cd backend
../.venv/bin/alembic upgrade head

# 7. Start API server (from backend/)
../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# 8. Start ingestion worker (from backend/, separate terminal)
../.venv/bin/celery -A app.workers.celery_app worker -I app.workers.ingestion --loglevel=INFO

# 9. Start web client (from frontend/)
cd ../frontend && npm run dev
```

Open http://localhost:3000

## Usage Workflow

1. **Register** — `POST /api/v1/auth/register-tenant` (name, slug, admin email/password)
2. **Login** — `POST /api/v1/auth/login` (tenant_slug, email, password) → access + refresh tokens
3. **Upload** — `POST /api/v1/documents` (multipart file) → `202 Accepted`, status `processing`
4. **Wait** — poll `GET /api/v1/documents/{id}` until `status: "ready"` (chunk_count > 0)
5. **Query** — `POST /api/v1/query` (question, max_chunks) → grounded answer + citations
6. **Stream** — `POST /api/v1/query/stream` → SSE `start` → `answer` → `citations` → `done`
7. **History** — `GET /api/v1/query/history` → paginated query logs
8. **Feedback** — `POST /api/v1/query/{id}/feedback` (`value: "up"|"down"`)
9. **Admin** — `GET /api/v1/users`, `GET /api/v1/admin/usage`, `GET /api/v1/admin/documents/{id}/status`
10. **Logout** — `POST /api/v1/auth/logout` (revokes refresh token)

## API Overview

### Auth
- `POST /api/v1/auth/register-tenant` — create tenant + admin
- `POST /api/v1/auth/login` — login (requires `tenant_slug`)
- `POST /api/v1/auth/refresh` — rotate access + refresh
- `POST /api/v1/auth/logout` — revoke refresh token

### Documents
- `POST /api/v1/documents` — upload (multipart)
- `GET /api/v1/documents` — list (paginated)
- `GET /api/v1/documents/{id}` — detail
- `DELETE /api/v1/documents/{id}` — delete
- `GET /api/v1/admin/documents/{id}/status` — admin status

### Query
- `POST /api/v1/query` — grounded answer + citations
- `POST /api/v1/query/stream` — SSE streaming
- `GET /api/v1/query/history` — paginated history
- `POST /api/v1/query/{id}/feedback` — up/down vote

### Admin
- `GET /api/v1/users` — list users
- `POST /api/v1/users/invite` — invite user
- `PATCH /api/v1/users/{id}/role` — change role
- `GET /api/v1/admin/usage` — analytics dashboard
- `GET /api/v1/admin/documents/{id}/status` — doc status

### Health
- `GET /health` — liveness
- `GET /health/ready` — readiness (config loaded)
- `GET /docs` — OpenAPI (dev only)

## Testing

```bash
# Backend (from backend/)
# Unit + integration (requires Docker infra, disposable DB)
TENANTMIND_TEST_PG_DSN='postgresql+psycopg://tenantmind_test:<pw>@127.0.0.1:5432/tenantmind_test_e2e' \
TENANTMIND_TEST_REDIS_URL='redis://:<pw>@127.0.0.1:6379/4' \
TENANTMIND_TEST_QDRANT_URL='http://127.0.0.1:6333' \
../.venv/bin/pytest

# Static checks
../.venv/bin/ruff check .
../.venv/bin/mypy app tests
../.venv/bin/pip check

# Frontend (from frontend/)
npm run check          # lint + typecheck + unit + build
npm run test           # unit tests (19 files, 113 tests)
BACKEND_URL=http://localhost:8000 npx vitest run --config vitest.e2e.config.ts  # E2E
```

**Integration suite notes** — DSN database name must match disposable-name guard (`test`/`tmp`/`temp`/`ci`/`throwaway`/`scratch`), Redis index must not be 0, suites self-migrate pristine DB.

## RAG Evaluation (Phase 8)

An evaluation package exists at `backend/evaluation/` with:
- **Dataset** — 12 documents, 36 questions (factual, multi-doc, detail, context-required, unanswerable)
- **Metrics** — Hit@K, MRR, Context Precision/Recall, Faithfulness, Answer Relevance/Correctness
- **Configurations** — Dense / BM25 / Hybrid / Hybrid+Rerank
- **Stand-ins** — lexical-hash embeddings, extractive answer provider, no-op reranker (not production)

**Important**: The evaluation uses stand-in providers. Results in `docs/evaluation/results/` reflect the stand-in behavior, not production Gemini quality. Run via `backend/evaluation/runner.py`.

## Known Limitations

- **Reranker optional** — requires `sentence-transformers` + `torch` (CUDA wheel, not in default deps)
- **PostgreSQL-specific BM25** — relies on `tsvector`/`GIN`, not portable to other SQL databases
- **Gemini API dependency** — generation requires external API key, subject to Google rate limits/quota
- **Rate limiting declared not enforced** — Redis-backed limiting not yet implemented
- **No SSO/SAML** — local auth only
- **Single-region** — no multi-region replication for Qdrant/Redis/PostgreSQL
- **Evaluation stand-ins** — Phase 8 results use lexical-hash/extractive providers, not Gemini

## Future Improvements

- Production deployment (Kubernetes, secrets management, TLS, observability)
- SSO/SAML/OIDC integration
- Multi-region Qdrant/Redis/PostgreSQL replication
- Billing/metering per tenant
- Stronger evaluation datasets (production traffic replay)
- Additional LLM providers (Anthropic, OpenAI, local)
- Document versioning + diff
- Advanced permissions (document-level ACL beyond roles)

## License

This project does not currently have a LICENSE file. A license decision is required before public release.