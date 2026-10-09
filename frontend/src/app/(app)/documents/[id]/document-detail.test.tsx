/**
 * Document detail tests: metadata rendering (including the honest failure
 * reason), friendly missing/forbidden states, and role-aware status polling.
 */

import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import DocumentDetailPage from "@/app/(app)/documents/[id]/page";
import { AuthProvider } from "@/lib/auth/auth-provider";
import { clearStoredSession } from "@/lib/auth/tokens";
import { formatBytes, formatDateTime } from "@/lib/format";
import type { Document, UserRole } from "@/lib/types";
import {
  clearSession,
  jsonResponse,
  mockFetchByUrl,
  seedSession,
  TEST_USER,
} from "@/test/utils";

function makeDoc(overrides: Partial<Document> = {}): Document {
  return {
    id: "doc-1",
    filename: "report.pdf",
    status: "ready",
    allowed_roles: null,
    content_type: "application/pdf",
    size_bytes: 2048,
    chunk_count: 3,
    error: null,
    created_at: "2026-01-05T10:30:00Z",
    updated_at: "2026-01-06T11:00:00Z",
    ...overrides,
  };
}

function signIn(role: UserRole = "admin"): void {
  clearStoredSession();
  seedSession({ ...TEST_USER, role });
}

/** Render inside an awaited `act` so the `use(params)` suspension resolves. */
async function renderDetail(): Promise<void> {
  await act(async () => {
    render(
      <AuthProvider>
        <DocumentDetailPage
        params={Promise.resolve({ id: "doc-1" })}
        searchParams={Promise.resolve({})}
      />
      </AuthProvider>,
    );
  });
}

/** Drain pending promises and React work while fake timers are installed. */
async function settle(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
    for (let i = 0; i < 100; i += 1) await Promise.resolve();
    await vi.advanceTimersByTimeAsync(0);
    for (let i = 0; i < 100; i += 1) await Promise.resolve();
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  clearSession();
});

it("renders the document metadata, including the failure reason when failed", async () => {
  signIn("admin");
  const failed = makeDoc({
    status: "failed",
    error: "Unsupported file type: .exe",
    allowed_roles: ["admin"],
    chunk_count: 0,
  });
  mockFetchByUrl((url) => {
    if (String(url).endsWith("/api/v1/documents/doc-1")) {
      return jsonResponse({ body: failed });
    }
    throw new Error(`Unexpected request: ${String(url)}`);
  });

  await renderDetail();

  expect(
    await screen.findByRole("heading", { name: "report.pdf" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Failed")).toBeInTheDocument();
  // The backend's own error, shown honestly — no invented explanation.
  expect(screen.getByText("Unsupported file type: .exe")).toBeInTheDocument();
  expect(screen.getByText("Processing failed")).toBeInTheDocument();

  expect(screen.getByText("application/pdf")).toBeInTheDocument();
  expect(screen.getByText(formatBytes(failed.size_bytes))).toBeInTheDocument();
  expect(screen.getByText("0")).toBeInTheDocument(); // chunk count
  expect(screen.getByText("admin")).toBeInTheDocument(); // visible to
  expect(screen.getByText("Restricted")).toBeInTheDocument();
  expect(screen.getByText(formatDateTime(failed.created_at))).toBeInTheDocument();
  expect(screen.getByText(formatDateTime(failed.updated_at))).toBeInTheDocument();

  expect(
    screen.getByRole("link", { name: "Back to documents" }),
  ).toHaveAttribute("href", "/documents");
});

it("shows a friendly state with a back link when the document is missing", async () => {
  signIn("admin");
  mockFetchByUrl(() => jsonResponse({ status: 404, body: { detail: "Not found" } }));

  await renderDetail();
  const alert = await screen.findByRole("alert");

  expect(alert).toHaveTextContent("Could not load this document");
  expect(alert).toHaveTextContent("This document no longer exists, or the link is out of date.");
  expect(alert).toHaveTextContent("Back to documents");
  expect(screen.queryByRole("heading", { name: "report.pdf" })).toBeNull();
});

it("shows a permission state when the backend refuses access", async () => {
  signIn("editor");
  mockFetchByUrl(() =>
    jsonResponse({ status: 403, body: { detail: "Not visible to your role." } }),
  );

  await renderDetail();
  const alert = await screen.findByRole("alert");

  expect(alert).toHaveTextContent("You don't have permission to view this document.");
  expect(alert).toHaveTextContent("Back to documents");
});

it("editors poll getDocument, never the admin-only status endpoint", async () => {
  signIn("editor");
  vi.useFakeTimers();

  let docCalls = 0;
  let adminCalls = 0;
  mockFetchByUrl((url) => {
    const path = String(url);
    if (path.includes("/admin/documents/")) {
      adminCalls += 1;
      return jsonResponse({
        body: { id: "doc-1", status: "ready", chunk_count: 9, error: null, updated_at: "2026-01-06T11:00:00Z" },
      });
    }
    docCalls += 1;
    const items = docCalls === 1 ? makeDoc({ status: "processing" }) : makeDoc({ status: "ready" });
    return jsonResponse({ body: items });
  });

  await renderDetail();
  await settle();
  expect(screen.getByText("Processing")).toBeInTheDocument();
  expect(docCalls).toBe(1);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(docCalls).toBe(2);
  expect(adminCalls).toBe(0);
  expect(screen.getByText("Ready")).toBeInTheDocument();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(15000);
  });
  expect(docCalls).toBe(2);
  expect(adminCalls).toBe(0);
});

it("administrators poll the admin status endpoint while processing", async () => {
  signIn("admin");
  vi.useFakeTimers();

  let docCalls = 0;
  let adminCalls = 0;
  mockFetchByUrl((url) => {
    const path = String(url);
    if (path.includes("/admin/documents/")) {
      adminCalls += 1;
      return jsonResponse({
        body: {
          id: "doc-1",
          status: "ready",
          chunk_count: 9,
          error: null,
          updated_at: "2026-01-07T09:00:00Z",
        },
      });
    }
    docCalls += 1;
    return jsonResponse({ body: makeDoc({ status: "processing" }) });
  });

  await renderDetail();
  await settle();
  expect(screen.getByText("Processing")).toBeInTheDocument();
  expect(docCalls).toBe(1);
  expect(adminCalls).toBe(0);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(adminCalls).toBe(1);
  expect(docCalls).toBe(1); // the admin endpoint replaced a full refetch
  expect(screen.getByText("Ready")).toBeInTheDocument();
  expect(screen.getByText("9")).toBeInTheDocument(); // merged chunk count

  await act(async () => {
    await vi.advanceTimersByTimeAsync(15000);
  });
  expect(adminCalls).toBe(1);
  expect(docCalls).toBe(1);
});
