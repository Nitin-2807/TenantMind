/**
 * Test helpers shared across suites. All fetch calls are mocked at the
 * `window.fetch` level so suites exercise the real API client and UI logic
 * without a backend.
 */

import { vi } from "vitest";

import { clearStoredSession, setSession, setTenantSlug } from "@/lib/auth/tokens";
import type { User, UserRole } from "@/lib/types";

export interface JsonResponse {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}

export function jsonResponse({
  status = 200,
  body,
  headers = {},
}: JsonResponse): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/** Mock `fetch` to return a sequence of responses per call. */
export function mockFetchSequence(...responses: Response[]): ReturnType<typeof vi.fn> {
  const mock = vi.fn();
  responses.forEach((response) => mock.mockResolvedValueOnce(response));
  vi.stubGlobal("fetch", mock);
  return mock;
}

/** Mock `fetch` with a per-call responder (e.g. route by URL). */
export function mockFetchByUrl(
  responder: (url: string, init?: RequestInit) => Response | Promise<Response>,
): ReturnType<typeof vi.fn> {
  const mock = vi.fn((url: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve(responder(String(url), init)),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

/** Example authenticated session tokens (fixed shape, safe to reuse). */
export const TEST_TOKENS = {
  access_token:
    "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhYmMiLCJyb2xlIjoiYWRtaW4iLCJ0ZW5hbnRfaWQiOiJ0ZW5hbnQtMSIsImV4cCI6OTk5OTk5OTk5OX0.c2ln",
  refresh_token: "refresh-token-abc",
  token_type: "Bearer",
  expires_in_seconds: 900,
} as const;

export const TEST_USER = {
  id: "user-1",
  email: "admin@example.com",
  role: "admin",
  is_active: true,
  created_at: "2026-01-01T00:00:00Z",
} as const;

/**
 * Seed an authenticated session through the real write path (tokens.ts) so the
 * component-facing external store reacts exactly as it does in production.
 */
export function seedSession(
  user: Partial<User> & { id: string; email: string; role: UserRole } = TEST_USER as Partial<User> & { id: string; email: string; role: UserRole },
): void {
  const fullUser: User = {
    id: user.id,
    email: user.email,
    role: user.role,
    is_active: user.is_active ?? true,
    created_at: user.created_at ?? "2026-01-01T00:00:00Z",
  };
  setSession(fullUser, TEST_TOKENS);
  setTenantSlug("acme");
}

export function clearSession(): void {
  clearStoredSession();
  window.localStorage.clear();
}