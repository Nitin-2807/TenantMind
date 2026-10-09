/**
 * Dashboard integration smoke tests (real API client + mocked fetch).
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "@/lib/auth/auth-provider";
import { jsonResponse, mockFetchByUrl, seedSession } from "@/test/utils";

import DashboardPage from "./page";

const DOCUMENTS = {
  items: [
    {
      id: "d1",
      filename: "lease.pdf",
      status: "ready",
      allowed_roles: null,
      content_type: "application/pdf",
      size_bytes: 2048,
      chunk_count: 12,
      error: null,
      created_at: "2026-01-01T10:00:00Z",
      updated_at: "2026-01-01T10:00:00Z",
    },
    {
      id: "d2",
      filename: "policy.md",
      status: "processing",
      allowed_roles: null,
      content_type: "text/markdown",
      size_bytes: 1024,
      chunk_count: 0,
      error: null,
      created_at: "2026-01-02T09:00:00Z",
      updated_at: "2026-01-02T09:00:00Z",
    },
  ],
};

const HISTORY = {
  items: [
    {
      id: "q1",
      question: "When is rent due?",
      answer: "The 1st.",
      grounded: true,
      context_truncated: false,
      cache_hit: false,
      status: "success",
      error_kind: null,
      citations: [{ document_id: "d1", filename: "lease.pdf", chunk_index: 0, page: 1 }],
      latency_ms: 142,
      feedback: null,
      feedback_at: null,
      created_at: "2026-01-03T10:00:00Z",
    },
  ],
  limit: 5,
  offset: 0,
};

const USAGE = {
  total_queries: 14,
  total_answered: 13,
  total_failed: 1,
  avg_latency_ms: 305,
  queries_last_24h: 4,
  queries_by_day: [],
  queries_by_user: [],
  cache_hit_rate: 0.214,
  feedback_up: 6,
  feedback_down: 2,
};

function stubBackend() {
  return mockFetchByUrl((url) => {
    if (url.startsWith("/api/v1/documents")) return jsonResponse({ body: DOCUMENTS });
    if (url.startsWith("/api/v1/query/history")) return jsonResponse({ body: HISTORY });
    if (url.startsWith("/api/v1/admin/usage")) return jsonResponse({ body: USAGE });
    return jsonResponse({ status: 404 });
  });
}

function renderDashboard() {
  return render(
    <AuthProvider>
      <DashboardPage />
    </AuthProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("dashboard", () => {
  it("loads documents, history, and (for admins) usage", async () => {
    seedSession({ id: "u1", email: "admin@example.com", role: "admin" });
    stubBackend();
    renderDashboard();

    await waitFor(() => expect(screen.getByText("Total documents")).toBeInTheDocument());
    expect(screen.getByText("Ready to search")).toBeInTheDocument();
    // Recent activity + a usage stat (both values come from the real payload).
    expect(screen.getByText("When is rent due?")).toBeInTheDocument();
    expect(screen.getByText("Total questions")).toBeInTheDocument();
    expect(screen.getByText("14", { selector: "p" })).toBeInTheDocument();
  });

  it("does not fetch usage for non-admins", async () => {
    seedSession({ id: "u2", email: "editor@example.com", role: "editor" });
    const fetchMock = stubBackend();
    renderDashboard();

    await waitFor(() => expect(screen.getByText("When is rent due?")).toBeInTheDocument());
    const usageCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes("/api/v1/admin/usage"),
    );
    expect(usageCalls).toHaveLength(0);
    expect(screen.queryByText("Total questions")).not.toBeInTheDocument();
  });

  it("shows the empty document state without charts or fake data", async () => {
    seedSession();
    mockFetchByUrl((url) => {
      if (url.startsWith("/api/v1/documents")) return jsonResponse({ body: { items: [] } });
      if (url.startsWith("/api/v1/query/history")) return jsonResponse({ body: { items: [], limit: 5, offset: 0 } });
      if (url.startsWith("/api/v1/admin/usage")) return jsonResponse({ body: USAGE });
      return jsonResponse({ status: 404 });
    });
    renderDashboard();

    await waitFor(() => expect(screen.getByText("No documents yet")).toBeInTheDocument());
    expect(screen.getByText("No questions yet")).toBeInTheDocument();
  });

  it("shows an error state and reloads on retry", async () => {
    seedSession();
    const fetchMock = vi
      .fn()
      // First load fails for documents.
      .mockResolvedValueOnce(jsonResponse({ status: 500, body: { detail: "nope" } }))
      // Retry succeeds.
      .mockImplementation((url: string) => {
        if (url.startsWith("/api/v1/documents")) {
          return Promise.resolve(jsonResponse({ body: { items: [] } }));
        }
        if (url.startsWith("/api/v1/query/history")) {
          return Promise.resolve(jsonResponse({ body: { items: [], limit: 5, offset: 0 } }));
        }
        // Admins fetch usage; a 403 is tolerated (dashboard skips the widget).
        if (url.startsWith("/api/v1/admin/usage")) {
          return Promise.resolve(jsonResponse({ status: 403, body: { detail: "forbidden" } }));
        }
        return Promise.resolve(jsonResponse({ status: 404 }));
      });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    renderDashboard();
    await waitFor(() => expect(screen.getByText("Could not load the dashboard")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByText("No documents yet")).toBeInTheDocument());
  });
});