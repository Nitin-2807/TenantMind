/**
 * Documents list page tests: loading → ready/empty/error state machine,
 * role-aware controls, upload/delete flows, and status polling (fake timers).
 */

import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import DocumentsPage from "@/app/(app)/documents/page";
import { AuthProvider } from "@/lib/auth/auth-provider";
import { clearStoredSession } from "@/lib/auth/tokens";
import { formatBytes, formatDateTime } from "@/lib/format";
import type { Document, UserRole } from "@/lib/types";
import { ToasterProvider } from "@/components/ui/toaster";
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

/**
 * Seed a session for `role`. Clearing through the token store (not just
 * localStorage) resets its module-level cache, so one test's user can never
 * shadow the next test's role.
 */
function signIn(role: UserRole = "admin"): void {
  clearStoredSession();
  seedSession({ ...TEST_USER, role });
}

function renderPage() {
  return render(
    <AuthProvider>
      <ToasterProvider>
        <DocumentsPage />
      </ToasterProvider>
    </AuthProvider>,
  );
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

function isListCall(url: string, init?: RequestInit): boolean {
  return url.endsWith("/api/v1/documents") && (init?.method ?? "GET") === "GET";
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  clearSession();
});

// ---------------------------------------------------------------------------
// Render state machine
// ---------------------------------------------------------------------------

it("shows a loading state until the documents arrive", async () => {
  signIn("admin");
  let resolveList: ((response: Response) => void) | undefined;
  const pending = new Promise<Response>((resolve) => {
    resolveList = resolve;
  });
  vi.stubGlobal("fetch", vi.fn(() => pending));

  renderPage();
  expect(screen.getByRole("status", { name: "Loading documents" })).toBeInTheDocument();
  expect(screen.queryByRole("table")).toBeNull();

  await act(async () => {
    resolveList?.(jsonResponse({ body: { items: [makeDoc()] } }));
    await pending;
  });

  expect(await screen.findByRole("table", { name: "Documents" })).toBeInTheDocument();
  expect(screen.queryByRole("status", { name: "Loading documents" })).toBeNull();
});

it("renders documents with status, size, created date, and the Restricted badge", async () => {
  signIn("admin");
  const readyDoc = makeDoc({ id: "doc-1", filename: "handbook.md", status: "ready" });
  const processingDoc = makeDoc({
    id: "doc-2",
    filename: "policy.pdf",
    status: "processing",
    allowed_roles: ["admin", "editor"],
    size_bytes: 5120,
    created_at: "2026-01-07T08:15:00Z",
    updated_at: "2026-01-07T08:16:00Z",
  });
  const failedDoc = makeDoc({
    id: "doc-3",
    filename: "scan.docx",
    status: "failed",
    error: "Unsupported file type",
    size_bytes: 51200,
    created_at: "2026-01-08T12:00:00Z",
    updated_at: "2026-01-08T12:01:00Z",
  });
  mockFetchByUrl(() => jsonResponse({ body: { items: [readyDoc, processingDoc, failedDoc] } }));

  renderPage();
  const table = await screen.findByRole("table", { name: "Documents" });

  expect(within(table).getByText("handbook.md")).toBeInTheDocument();
  expect(within(table).getByText("policy.pdf")).toBeInTheDocument();
  expect(within(table).getByText("scan.docx")).toBeInTheDocument();

  expect(within(table).getByText("Ready")).toBeInTheDocument();
  expect(within(table).getByText("Processing")).toBeInTheDocument();
  expect(within(table).getByText("Failed")).toBeInTheDocument();

  expect(within(table).getByText(formatBytes(readyDoc.size_bytes))).toBeInTheDocument();
  expect(within(table).getByText(formatDateTime(readyDoc.created_at))).toBeInTheDocument();

  // Restricted only appears for the role-limited document.
  expect(within(table).getByText("Restricted")).toBeInTheDocument();
  expect(within(table).getAllByText("Restricted")).toHaveLength(1);

  expect(
    within(table).getByRole("link", { name: "View handbook.md" }),
  ).toHaveAttribute("href", "/documents/doc-1");
});

it("shows an empty state with an upload action", async () => {
  signIn("admin");
  mockFetchByUrl(() => jsonResponse({ body: { items: [] } }));

  renderPage();
  expect(await screen.findByText("Upload your first document")).toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "Upload document" }).length).toBeGreaterThan(0);
});

it("shows a viewer-friendly empty state without an upload action", async () => {
  signIn("viewer");
  mockFetchByUrl(() => jsonResponse({ body: { items: [] } }));

  renderPage();
  expect(await screen.findByText("No documents yet")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Upload document" })).toBeNull();
});

it("shows an error state whose retry refetches the list", async () => {
  signIn("admin");
  let calls = 0;
  mockFetchByUrl(() => {
    calls += 1;
    if (calls === 1) {
      return jsonResponse({ status: 500, body: { detail: "Database unavailable" } });
    }
    return jsonResponse({ body: { items: [makeDoc()] } });
  });

  renderPage();
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Could not load documents");
  expect(alert).toHaveTextContent("Database unavailable");

  await userEvent.setup().click(within(alert).getByRole("button", { name: "Try again" }));
  expect(await screen.findByRole("table", { name: "Documents" })).toBeInTheDocument();
  expect(calls).toBeGreaterThanOrEqual(2);
});

// ---------------------------------------------------------------------------
// Role gating (mirrors the backend's rules; backend stays authoritative)
// ---------------------------------------------------------------------------

it("viewer sees no upload or delete controls but can still open documents", async () => {
  signIn("viewer");
  mockFetchByUrl(() => jsonResponse({ body: { items: [makeDoc()] } }));

  renderPage();
  const table = await screen.findByRole("table", { name: "Documents" });

  expect(screen.queryByRole("button", { name: "Upload document" })).toBeNull();
  expect(screen.queryAllByRole("button", { name: /^Delete/ })).toHaveLength(0);
  expect(within(table).getByRole("link", { name: "View report.pdf" })).toBeInTheDocument();
});

it("editor sees upload but not delete", async () => {
  signIn("editor");
  mockFetchByUrl(() => jsonResponse({ body: { items: [makeDoc()] } }));

  renderPage();
  await screen.findByRole("table", { name: "Documents" });

  expect(screen.getByRole("button", { name: "Upload document" })).toBeInTheDocument();
  expect(screen.queryAllByRole("button", { name: /^Delete/ })).toHaveLength(0);
});

it("admin sees both upload and delete", async () => {
  signIn("admin");
  mockFetchByUrl(() => jsonResponse({ body: { items: [makeDoc()] } }));

  renderPage();
  await screen.findByRole("table", { name: "Documents" });

  expect(screen.getByRole("button", { name: "Upload document" })).toBeInTheDocument();
  expect(screen.queryAllByRole("button", { name: /^Delete/ }).length).toBeGreaterThan(0);
});

// ---------------------------------------------------------------------------
// Upload flow
// ---------------------------------------------------------------------------

it("uploads a file, shows processing, and re-fetches until it is ready", async () => {
  signIn("admin");
  vi.useFakeTimers();

  let listCalls = 0;
  let uploadBody: BodyInit | null | undefined;
  mockFetchByUrl((url, init) => {
    const path = String(url);
    if (init?.method === "POST" && path.endsWith("/api/v1/documents")) {
      uploadBody = init.body;
      return jsonResponse({ status: 201, body: makeDoc({ status: "processing" }) });
    }
    if (isListCall(path, init)) {
      listCalls += 1;
      if (listCalls === 1) return jsonResponse({ body: { items: [] } });
      if (listCalls === 2) {
        return jsonResponse({ body: { items: [makeDoc({ status: "processing" })] } });
      }
      return jsonResponse({ body: { items: [makeDoc({ status: "ready" })] } });
    }
    throw new Error(`Unexpected request: ${path}`);
  });

  renderPage();
  await settle();
  expect(screen.getByText("Upload your first document")).toBeInTheDocument();

  // fireEvent (not user-event): user-event's internal waits hang when fake
  // timers are installed, and the polling assertions below need them.
  fireEvent.click(screen.getAllByRole("button", { name: "Upload document" })[0]);
  const dialog = screen.getByRole("dialog", { name: "Upload document" });
  fireEvent.change(within(dialog).getByLabelText("File"), {
    target: { files: [new File(["hello"], "notes.txt", { type: "text/plain" })] },
  });
  fireEvent.change(within(dialog).getByLabelText("Visible to"), {
    target: { value: "editor" },
  });
  fireEvent.click(within(dialog).getByRole("button", { name: "Upload" }));
  await settle();

  // Success: dialog closed, honest toast, list re-fetched and now processing.
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(screen.getByText("Uploaded — processing…")).toBeInTheDocument();
  expect(listCalls).toBe(2);
  const table = screen.getByRole("table", { name: "Documents" });
  expect(within(table).getByText("Processing")).toBeInTheDocument();

  // Multipart contract: file part + allowed_roles as a JSON array.
  const form = uploadBody as FormData;
  expect(form.get("allowed_roles")).toBe(JSON.stringify(["editor"]));
  expect((form.get("file") as File).name).toBe("notes.txt");

  // Status polling picks the list up again once processing finishes.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(listCalls).toBe(3);
  expect(within(screen.getByRole("table", { name: "Documents" })).getByText("Ready")).toBeInTheDocument();

  // Nothing is processing any more, so polling stops.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15000);
  });
  expect(listCalls).toBe(3);
});

// ---------------------------------------------------------------------------
// Delete flow
// ---------------------------------------------------------------------------

it("requires confirmation before calling DELETE, then refreshes the list", async () => {
  signIn("admin");
  let deleteCalls = 0;
  let listCalls = 0;
  mockFetchByUrl((url, init) => {
    const path = String(url);
    if (init?.method === "DELETE" && path.endsWith("/api/v1/documents/doc-1")) {
      deleteCalls += 1;
      return jsonResponse({ status: 204 });
    }
    listCalls += 1;
    return jsonResponse({ body: { items: listCalls === 1 ? [makeDoc()] : [] } });
  });

  renderPage();
  const table = await screen.findByRole("table", { name: "Documents" });
  const user = userEvent.setup();

  await user.click(within(table).getByRole("button", { name: "Delete report.pdf" }));
  const dialog = await screen.findByRole("dialog", { name: "Delete document?" });
  expect(dialog).toHaveTextContent("report.pdf");
  expect(
    within(dialog).getByText(/This removes it from the knowledge base immediately/),
  ).toBeInTheDocument();

  // Nothing was deleted before the explicit confirmation.
  expect(deleteCalls).toBe(0);

  await user.click(within(dialog).getByRole("button", { name: "Delete document" }));

  expect(await screen.findByText("Document deleted")).toBeInTheDocument();
  expect(deleteCalls).toBe(1);
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(await screen.findByText("Upload your first document")).toBeInTheDocument();
});

it("surfaces the backend reason when a delete is refused", async () => {
  signIn("admin");
  mockFetchByUrl((url, init) => {
    if (init?.method === "DELETE") {
      return jsonResponse({
        status: 403,
        body: { detail: "Only administrators can delete documents." },
      });
    }
    return jsonResponse({ body: { items: [makeDoc()] } });
  });

  renderPage();
  const table = await screen.findByRole("table", { name: "Documents" });
  const user = userEvent.setup();

  await user.click(within(table).getByRole("button", { name: "Delete report.pdf" }));
  const dialog = await screen.findByRole("dialog", { name: "Delete document?" });
  await user.click(within(dialog).getByRole("button", { name: "Delete document" }));

  expect(
    await screen.findByText("Only administrators can delete documents."),
  ).toBeInTheDocument();
  // The dialog stays open so the user can read the reason or cancel.
  expect(screen.getByRole("dialog", { name: "Delete document?" })).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Status polling (fake timers)
// ---------------------------------------------------------------------------

it("re-fetches the list every 3s while a document is processing and stops when ready", async () => {
  signIn("admin");
  vi.useFakeTimers();

  let listCalls = 0;
  const mock = mockFetchByUrl(() => {
    listCalls += 1;
    const items = listCalls === 1 ? [makeDoc({ status: "processing" })] : [makeDoc({ status: "ready" })];
    return jsonResponse({ body: { items } });
  });

  renderPage();
  await settle();
  expect(within(screen.getByRole("table", { name: "Documents" })).getByText("Processing")).toBeInTheDocument();
  expect(listCalls).toBe(1);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(3000);
  });
  expect(listCalls).toBe(2);
  expect(within(screen.getByRole("table", { name: "Documents" })).getByText("Ready")).toBeInTheDocument();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(15000);
  });
  expect(listCalls).toBe(2);
  expect(mock).toHaveBeenCalledTimes(2);
});
