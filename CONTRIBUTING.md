# Contributing to TenantMind

Thank you for considering a contribution. This document outlines the development setup, workflow, and quality expectations.

## Development Setup

```bash
# 1. Clone
git clone <repository-url>
cd TenantMind

# 2. Backend
python3.12 -m venv .venv
./.venv/bin/pip install -r backend/requirements.txt -r backend/requirements-dev.txt

# 3. Frontend
cd frontend && npm install && cd ..

# 4. Environment
cp .env.example .env
# Edit .env with JWT_SECRET_KEY and GEMINI_API_KEY

# 5. Infrastructure
export TENANTMIND_TEST_PG_PASSWORD='<pw>'
export TENANTMIND_TEST_REDIS_PASSWORD='<pw>'
docker compose up -d --wait

# 6. Migrations (from backend/)
cd backend && ../.venv/bin/alembic upgrade head

# 7. Run servers
# Terminal 1: API
../.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
# Terminal 2: Celery
../.venv/bin/celery -A app.workers.celery_app worker -I app.workers.ingestion --loglevel=INFO
# Terminal 3: Frontend
cd frontend && npm run dev
```

## Branch & Commit

- Branch from `main` with descriptive names: `feat/...`, `fix/...`, `docs/...`, `refactor/...`
- Commits should be atomic and self-contained
- Conventional commits encouraged: `feat: add SSE streaming`, `fix: handle 401 in SSE`

## Before Opening a PR

Run the full gate locally:

```bash
# Backend (from backend/)
../.venv/bin/ruff check .
../.venv/bin/mypy app tests
../.venv/bin/pip check
TENANTMIND_TEST_PG_DSN='postgresql+psycopg://tenantmind_test:<pw>@127.0.0.1:5432/tenantmind_test_e2e' \
TENANTMIND_TEST_REDIS_URL='redis://:<pw>@127.0.0.1:6379/4' \
TENANTMIND_TEST_QDRANT_URL='http://127.0.0.1:6333' \
../.venv/bin/pytest

# Frontend (from frontend/)
npm run check
```

All checks must pass. CI runs the same gate.

## Code Quality

- **Python**: Ruff (lint + format), mypy strict, type hints on all public functions
- **TypeScript**: ESLint (eslint-config-next), strict tsc --noEmit, no `any` in new code
- **Tests**: New features require tests. Bug fixes require regression tests.
- **Documentation**: Update relevant README/docs when behavior changes.

## Security & Secrets

- **Never commit secrets** — `.env` is gitignored, `.env.example` has placeholders only
- **No hardcoded credentials** — use environment variables via `Settings`
- **JWT_SECRET_KEY** — ≥32 chars, never use placeholder in production
- **GEMINI_API_KEY** — inject from secret manager in production
- **Database/Redis passwords** — never in code, only in compose env vars

## Architecture Principles

- **Backend is the authority** — tenant identity, RBAC, validation all server-side
- **Frontend mirrors backend contracts** — types mirror schemas, no client-side trust
- **Tenant isolation is non-negotiable** — every query includes `tenant_id` from auth
- **No SDKs for external APIs** — hand-written HTTP for Gemini (auditable, no telemetry)
- **Evaluation ≠ production** — Phase 8 uses stand-in providers; don't conflate results

## Reporting Issues

- Security issues: email directly (do not open public issue)
- Bugs: include reproduction steps, expected vs actual, logs
- Features: describe the problem, not just the solution

## License

By contributing, you agree your contributions will be licensed under the project's license (to be determined).