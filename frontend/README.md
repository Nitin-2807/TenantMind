# TenantMind — Frontend

The TenantMind web client: a multi-tenant enterprise SaaS frontend for asking
questions against tenant-scoped document corpora, built with Next.js 16.4
(App Router), React 19, TypeScript, and Tailwind CSS v4. It integrates the
FastAPI backend in `../backend` through the backend's **real API contracts** —
no invented fields, no client-supplied `tenant_id`, and a role-aware UI whose
enforcement boundary remains the backend's RBAC.

## Requirements

- Node.js with npm (Next.js 16 / React 19 / TypeScript 5)
- The backend service on `http://localhost:8000` (see `../backend/README.md`)

## Getting started

```bash
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). Health probes and API
calls are proxied to the backend:

- `/api/v1/*` and `/health*` are rewritten to `BACKEND_URL`
  (default `http://localhost:8000`), so the browser only ever talks to the
  same origin.
- Uploads are allowed up to `15mb` through the proxy
  (`proxyClientMaxBodySize`), aligned with the backend's own size policy.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `BACKEND_URL` | `http://localhost:8000` | Server-side target for the `/api/v1/*` and `/health*` rewrites. |
| `NEXT_PUBLIC_API_BASE_URL` | unset | Optional override for the API base used by the typed client in the browser. |

**No secrets are read from the environment.** Auth tokens are returned by the
backend and kept in browser `localStorage` under `tm.*` keys; they are never
logged and never rendered.

## Scripts

```bash
npm run dev        # development server
npm run build      # production build
npm start          # run the production build
npm run lint       # ESLint (eslint-config-next)
npm run typecheck  # tsc --noEmit
npm test           # unit/component tests (Vitest 5 + Testing Library)
npm run test:watch # tests in watch mode
npm run test:e2e   # live-backend E2E suite (skips cleanly if backend is down)
npm run check      # the full frontend gate: lint + typecheck + test + build
```

## Architecture

```
src/
  app/
    (app)/            # authenticated, role-aware applications
      dashboard/      # entry point after login
      documents/      # list, upload, detail + ingestion status
      assistant/      # SSE streaming chat
      history/        # past queries + feedback
      admin/          # users (invite / role / delete) + usage — admin-only
      settings/       # account info + sign out
    (auth)/           # login / register (tenant_slug + email + password)
    layout.tsx        # root layout, fonts, providers
  components/
    ui/               # primitives (button, dialog, field, select, card, …)
    layout/           # shell, nav (role-aware), page header
    assistant/        # chat UI + streaming message bubbles
    documents/        # upload dropzone, table, status
    users/            # invite / delete dialogs, users table (admin)
    admin/            # usage breakdown
  hooks/
    use-query-stream.ts  # SSE lifecycle: start → answer → citations → done/error
  lib/
    api/              # typed, centralized API client (http, auth, documents,
                      # queries, users, admin) — 401 → refresh → retry once
    auth/             # token store (external store), provider, route guards
    errors.ts         # ApiError / NetworkError / RateLimitError mapping
    types.ts          # wire types mirroring backend schemas
    validation.ts     # client-side mirrors of backend field rules (UX only)
    format.ts         # date / byte / latency / percent formatters
  test/               # shared test helpers (fetch mock, seeded sessions)
e2e/                  # live-backend E2E suite (Vitest config: vitest.e2e.config.ts)
```

## Auth & role model

- Login is `tenant_slug` + `email` + `password` — the tenant is resolved
  server-side from the authenticated identity; the client never sends or
  stores a `tenant_id`.
- Roles: **admin**, **editor**, **viewer**, with role-aware navigation and
  UI, mirroring the backend's real RBAC:
  - All roles: view documents, ask questions, history, feedback.
  - Editor (+ admin): upload documents, delete own-role documents.
  - Admin only: delete documents, manage users (invite, role change,
    delete), usage statistics.
- The backend remains the final authorization boundary. The UI hides what a
  role may not do, but every request is still authorized server-side.

## Streaming answers

`POST /query/stream` is consumed by `useQueryStream` through an SSE parser
that follows the backend lifecycle: `start` → `answer` → `citations` →
`done` (or `error`). `grounded: false` responses render an honest
"no context found" state instead of inventing an answer. Malformed frames
are skipped, and the stream surfaces friendly, obligation-free errors.

## Error handling

Every API call flows through the typed client, which maps failures to
`ApiError` (with schema details), `RateLimitError` (rendered as
"Too many requests. Please try again shortly."), `NetworkError`, and 401
handling with a **single** silent refresh-then-retry. A failed refresh clears
the session and redirects to login with a "session expired" notice. Raw
traces, JWT material, and database error strings are never rendered.

## Testing

- **Unit / component** (`npm test`): 113 tests across 19 suites covering
  auth, route protection, role navigation, document upload/status,
  query + SSE lifecycle, citations, feedback, history, admin, error states
  (401/403/404/422/429/500), viewer-can't-see-admin, and
  expired-token → refresh → retry.
- **E2E** (`npm run test:e2e`): 10 scenarios against a live backend
  (`e2e/live-backend.test.ts`), including a full end-to-end SSE query. The
  suite probes `/health` first and **skips cleanly when the backend is
  unreachable**, so it can run in CI that has the services and cannot fail
  one that does not. With the backend running (see `../backend` + compose),
  `npm run test:e2e` runs it for real.

## Phase 9 status

Full frontend gate green at close of PMP Phase 9: lint clean, typecheck
clean, 113/113 tests, production build succeeding. Backend gate re-verified:
ruff clean, mypy clean (70 files), `pip check` clean, 2454 passed / 203
skipped / 1 xfailed, alembic head `0007` single chain. See
`../PROJECT_EXECUTION_LOG.md` (PMP Phase 9) for the detailed record.