# TenantMind Frontend — Agent Conventions (Phase 9)

Work reference for feature agents. The coordinator owns the foundation; you own
only the files listed in your brief. **Do not edit files outside your ownership
list** — several agents work in parallel on disjoint slices.

## Stack & tooling

- Next.js 16.4 (App Router, TypeScript strict), React 19, Tailwind CSS v4.
- Tests: Vitest + jsdom + @testing-library/react + user-event. Run:
  `npm run lint`, `npm run typecheck`, `npm run test`, `npm run build`.
- **Next 16 changed APIs.** Read `node_modules/next/dist/docs/` before using an
  API you are unsure about. Known gotchas already in use:
  - Route-aware type helpers are global: `LayoutProps<"/route">`, `PageProps<"/route">`.
  - Error boundaries receive `retry` (not `reset`).
  - `middleware` was renamed `proxy`; we do not use server middleware.
- API calls go through the centralized client (`src/lib/api/`). No component
  or hook may call `fetch()` directly.

## Ownership map (do not cross)

| Owner     | Files                                                          |
|-----------|----------------------------------------------------------------|
| Coordinator | config, `src/lib/**`, `src/components/ui/**`, `src/components/layout/**`, `src/components/icons.tsx`, `src/components/dashboard/**`, `src/app/layout.tsx`, `src/app/providers.tsx`, `src/app/page.tsx`, `src/app/error.tsx`, `src/app/not-found.tsx`, `src/app/(app)/layout.tsx`, `src/app/(app)/loading.tsx`, `src/app/(app)/dashboard/**`, `src/test/**` |
| Agent B   | `src/app/(auth)/**` (login, register, layout), `src/components/auth/**` |
| Agent C   | `src/app/(app)/documents/**`, `src/components/documents/**`, `src/hooks/use-document-status.ts` |
| Agent D   | `src/app/(app)/assistant/**`, `src/app/(app)/history/**`, `src/components/assistant/**`, `src/components/history/**`, `src/hooks/use-query-stream.ts` |
| Agent E   | `src/app/(app)/admin/**`, `src/app/(app)/settings/**`, `src/components/users/**`, `src/components/admin/**` |

Literal imports from shared files stay fine, just never *write* to another
owner's files.

## Design system (available)

Import from `@/components/ui/*` (or `@/components/ui` barrel roles where named).
Semantic Tailwind tokens (light/dark auto): `bg-background`, `bg-surface`,
`text-foreground`, `text-muted`, `border-border`, `bg-primary`,
`bg-primary-hover`, `text-primary`, `text-danger`, `text-success`,
`text-warning`, `bg-danger`, `ring-ring`.

- `Button` — `variant: primary|secondary|outline|ghost|danger`, `size: sm|md|lg`,
  `loading?: boolean`, default `type="button"`.
- `Input`, `Textarea`, `Select`, `Label`, `Field` (`label`,`htmlFor`,`error`,`hint`).
- `Card`, `CardHeader`, `CardTitle`, `CardDescription`, `CardContent`.
- `Badge` — `tone: neutral|success|warning|danger|brand`.
- `StatusBadge` — renders `DocumentStatus` with icon + text (never color-only).
- `Spinner` — `size: sm|md|lg`.
- `EmptyState` — `icon`,`title`,`description`,`action` + honest next action.
- `ErrorState` — `title`,`message`,`retry` → friendly message + retry button.
- `Dialog` — `open`,`onClose`,`title`,`description?`,`children?`,`footer?` (a11y handled).
- `useToast` / `ToasterProvider` — `toast({variant: success|error|info, title, description})`.
- `PageHeader` — `title`,`description?`,`actions?`.
- Icons: `@/components/icons` — `DocumentsIcon, ChatIcon, HistoryIcon, UsersIcon,
  ChartIcon, SettingsIcon, UploadIcon, TrashIcon, MenuIcon, LogoutIcon, DashboardIcon`.

## API client (never call fetch directly)

Namespaced barrels from `@/lib/api`: `documentsApi`, `queryApi`, `usersApi`,
`adminApi`, `authApi`; named exports also exist per module.

- `documentsApi.listDocuments()` → `{ items: Document[] }`
- `documentsApi.uploadDocument(file: File, allowedRoles?: UserRole[])` → `Document`
  (multipart; do NOT send a tenant id — never send `tenant_id` from the client)
- `documentsApi.deleteDocument(id)`; `documentsApi.getDocument(id)`
- `queryApi.getHistory(limit=50, offset=0)` → `{ items: HistoryItem[], limit, offset }`
- `queryApi.askQuestion(question)` → `QueryAnswer` (synchronous; use for tests/E2E)
- `queryApi.streamQuery(question, { onEvent, signal })` → Promise<void>.
  SSE lifecycle events via `onEvent`: `start` → `answer` → `citations` → `done`.
  `answer` carries `{ answer, grounded, context_truncated }`; `grounded: false`
  must render as an honest "no documents matched" state, never a fabricated
  answer. `citations: Citation[]` arrives separately. Resolves on `done`;
  rejects with a typed error on an `error` event or transport failure.
- `queryApi.submitFeedback(queryId, "up"|"down")` → `{ query_id, feedback, feedback_at }`
- `usersApi.listUsers()` → `{ items: User[] }`
- `usersApi.inviteUser({ email, password, role? })` → `User`
- `usersApi.changeUserRole(userId, role)` → `User`
- `usersApi.deleteUser(userId)` → void
- `adminApi.getUsage()` → `UsageSummary`
- `authApi.login({ tenant_slug, email, password })`; `authApi.registerTenant({ name, slug, admin_email, admin_password })`; `authApi.logout({ refresh_token })`

Types: `@/lib/types` — `UserRole` (`admin|editor|viewer`), `DocumentStatus`
(`processing|ready|failed`), `Document`, `HistoryItem`, `Citation`, `UsageSummary`,
`SSEStreamEvent`, `FeedbackValue` (`up|down`), … Field names are the backend's —
do not rename or invent.

## Auth & guards

- `useAuth()` from `@/lib/auth/auth-provider` → `{ status, user, tenant, login, register, logout }`.
  `status`: `loading | authenticated | anonymous`.
- `RequireAuth`, `RequireRole` from `@/lib/auth/route-guard`. Admin screens wrap
  content in `<RequireRole role="admin">`. These are UX-only; the backend
  authorizes every request.
- Tokens must never be displayed, logged, or sent to the UI. Tenant comes from
  the token — the client never sends `tenant_id`.

## Error handling (all pages)

- Every data screen renders distinct loading / success / empty / error states.
- Friendly messages only: no stack traces, no JWT payloads, no raw DB errors.
- `ApiError` has `.status` and `.detail`. 429 ⇒ “Too many requests. Please try
  again shortly.” (the client already maps it to `RateLimitError`).
- 401/403 handled by the client + guards; pages don’t need special-casing beyond
  optionally surfacing `PermissionError`/`AuthenticationError` messages.

## Quality bar (hard requirements)

- **XSS safety:** never inject raw HTML from the LLM or documents. Render with
  React text nodes. Answers/citations are plain text (line breaks as `<br/>` or
  CSS `whitespace-pre-wrap` — do not use `dangerouslySetInnerHTML`).
- **Accessibility:** every input has a label (use `Field`/`Label`), interactive
  elements are keyboard-reachable, dialogs focus-manage (built-in), status never
  communicated by color alone.
- **Responsive:** layouts work on desktop, tablet, and mobile.
- **Validation:** use `@/lib/validation` (mirrors backend rules: slug pattern,
  email shape, new passwords ≥ 12 chars, question ≤ 500 chars, file ≤ 10 MB).
  Friendlier errors; the backend remains authoritative.
- **Honesty:** no fabricated metrics or features. Empty states explain the next
  action.

## Tests

- Colocate `*.test.tsx` / `*.test.ts` next to the code (`src/**/*.test.*` is the
  include pattern). Tests run in jsdom with globals.
- Mock network at `window.fetch` using helpers in `@/src/test/utils`:
  `jsonResponse({status, body, headers})`, `mockFetchSequence(...)`,
  `mockFetchByUrl(responder)`, `seedSession(user?)`, `TEST_USER`, `TEST_TOKENS`,
  `clearSession()`.
- Cover your flows: render state machine (loading → data/empty/error), role
  gating, forms validation + submit (user-event), and the backend contract
  claims that matter for your slice.