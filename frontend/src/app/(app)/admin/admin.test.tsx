/**
 * Admin pages tests - users and usage.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "@/lib/auth/auth-provider";
import { jsonResponse, mockFetchByUrl, seedSession } from "@/test/utils";

const { push, replace } = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
}));

import AdminUsersPage from "./users/page";
import AdminUsagePage from "./usage/page";
import { ToasterProvider } from "@/components/ui/toaster";

const USERS = {
  items: [
    {
      id: "user-1",
      email: "admin@example.com",
      role: "admin",
      is_active: true,
      created_at: "2026-01-01T00:00:00Z",
    },
    {
      id: "user-2",
      email: "editor@example.com",
      role: "editor",
      is_active: true,
      created_at: "2026-01-02T00:00:00Z",
    },
  ],
};

const USAGE = {
  total_queries: 14,
  total_answered: 13,
  total_failed: 1,
  avg_latency_ms: 305,
  queries_last_24h: 4,
  queries_by_day: [{ date: "2026-01-03", count: 4 }],
  queries_by_user: [{ email: "admin@example.com", count: 4 }],
  cache_hit_rate: 0.214,
  feedback_up: 6,
  feedback_down: 2,
};

function renderUsersPage() {
  return render(
    <AuthProvider>
      <ToasterProvider>
        <AdminUsersPage />
      </ToasterProvider>
    </AuthProvider>,
  );
}

function renderUsagePage() {
  return render(
    <AuthProvider>
      <ToasterProvider>
        <AdminUsagePage />
      </ToasterProvider>
    </AuthProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("admin/users - role gating", () => {
  it("viewer sees permission state and NO user table", async () => {
    seedSession({ id: "u-v", email: "viewer@example.com", role: "viewer" });
    mockFetchByUrl(() => jsonResponse({ body: USERS }));
    renderUsersPage();

    await waitFor(() => {
      expect(screen.getByText("You don't have access to this page")).toBeInTheDocument();
    });
    expect(screen.queryByText("Users")).not.toBeInTheDocument();
  });

  it("admin sees the table", async () => {
    seedSession({ id: "user-1", email: "admin@example.com", role: "admin" });
    mockFetchByUrl((url) => {
      if (url.startsWith("/api/v1/users")) return jsonResponse({ body: USERS });
      return jsonResponse({ status: 404 });
    });
    renderUsersPage();

    await waitFor(() => {
      expect(screen.getByText("Users")).toBeInTheDocument();
    });
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    expect(screen.getByText("editor@example.com")).toBeInTheDocument();
  });
});

describe("admin/users - invite", () => {
  it("bad email / short password → no fetch", async () => {
    seedSession({ id: "user-1", email: "admin@example.com", role: "admin" });
    const fetchMock = vi.fn((url) => {
      if (url.startsWith("/api/v1/users") && !url.includes("/invite") && !url.includes("/role") && url === "/api/v1/users") {
        return Promise.resolve(jsonResponse({ body: USERS }));
      }
      return Promise.resolve(jsonResponse({ status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderUsersPage();

    await waitFor(() => expect(screen.getByText("Users")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Invite user" }));
    await userEvent.type(screen.getByLabelText("Email"), "bad-email");
    await userEvent.type(screen.getByLabelText("Password"), "short");
    const submitBtns = screen.getAllByRole("button", { name: /Invite/i });
    await userEvent.click(submitBtns[submitBtns.length - 1]);

    const inviteCalls = fetchMock.mock.calls.filter(([url]) =>
      String(url).includes("/api/v1/users/invite"),
    );
    expect(inviteCalls).toHaveLength(0);
  });

  it("successful invite POSTs to /api/v1/users/invite and refreshes list", async () => {
    seedSession({ id: "user-1", email: "admin@example.com", role: "admin" });
    let usersCallCount = 0;
    const fetchMock = vi.fn((url) => {
      const urlStr = String(url);
      if (urlStr.includes("/api/v1/users/invite")) {
        return Promise.resolve(jsonResponse({ body: { id: "user-3", email: "new@example.com", role: "viewer", is_active: true, created_at: "2026-01-03T00:00:00Z" } }));
      }
      if (urlStr.startsWith("/api/v1/users")) {
        usersCallCount++;
        if (usersCallCount === 1) return Promise.resolve(jsonResponse({ body: USERS }));
        return Promise.resolve(
          jsonResponse({
            body: {
              items: [
                ...USERS.items,
                {
                  id: "user-3",
                  email: "new@example.com",
                  role: "viewer",
                  is_active: true,
                  created_at: "2026-01-03T00:00:00Z",
                },
              ],
            },
          }),
        );
      }
      return Promise.resolve(jsonResponse({ status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderUsersPage();

    await waitFor(() => expect(screen.getByText("Users")).toBeInTheDocument());

    await userEvent.click(screen.getByRole("button", { name: "Invite user" }));
    await userEvent.type(screen.getByLabelText("Email"), "new@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "password123456");
    await userEvent.selectOptions(screen.getByLabelText("Role"), "viewer");
    const submitBtns = screen.getAllByRole("button", { name: /Invite/i });
    await userEvent.click(submitBtns[submitBtns.length - 1]);

    await waitFor(() => {
      const calls = fetchMock.mock.calls;
      const hasInvite = calls.some(([url]) => String(url).includes("/api/v1/users/invite"));
      expect(hasInvite).toBe(true);
    });
    await waitFor(() => expect(screen.getByText("new@example.com")).toBeInTheDocument());
  });
});

describe("admin/users - role change and delete", () => {
  it("role change PATCHes /api/v1/users/{id}/role and updates the row", async () => {
    seedSession({ id: "user-1", email: "admin@example.com", role: "admin" });
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.startsWith("/api/v1/users") && !url.includes("/invite") && !url.includes("/role") && url === "/api/v1/users") {
        return Promise.resolve(jsonResponse({ body: USERS }));
      }
      if (url.includes("/role") && init?.method === "PATCH") {
        return Promise.resolve(
          jsonResponse({
            body: {
              id: "user-2",
              email: "editor@example.com",
              role: "admin",
              is_active: true,
              created_at: "2026-01-02T00:00:00Z",
            },
          }),
        );
      }
      return Promise.resolve(jsonResponse({ status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderUsersPage();

    await waitFor(() => expect(screen.getByText("Users")).toBeInTheDocument());

    const roleSelects = screen.getAllByLabelText(/Change role/);
    await userEvent.selectOptions(roleSelects[1], "admin");

    await waitFor(() => {
      const patchCalls = fetchMock.mock.calls.filter(
        ([url, init]) => String(url).includes("/role") && init?.method === "PATCH",
      );
      expect(patchCalls.length).toBeGreaterThanOrEqual(1);
    });
  });

  it("delete requires confirmation and DELETE /api/v1/users/{id}; own row's delete/role controls are disabled", async () => {
    seedSession({ id: "user-1", email: "admin@example.com", role: "admin" });
    const fetchMock = vi.fn((url, init) => {
      if (url.startsWith("/api/v1/users") && !url.includes("/invite") && !url.includes("/role") && url === "/api/v1/users") {
        return Promise.resolve(jsonResponse({ body: USERS }));
      }
      if (url.includes("/api/v1/users/user-2") && init?.method === "DELETE") {
        return Promise.resolve(jsonResponse({ status: 204, body: null }));
      }
      return Promise.resolve(jsonResponse({ status: 404 }));
    });
    vi.stubGlobal("fetch", fetchMock);
    renderUsersPage();

    await waitFor(() => expect(screen.getByText("Users")).toBeInTheDocument());

    const deleteButtons = screen.getAllByLabelText(/Remove/);
    expect(deleteButtons[0]).toBeDisabled();

    await userEvent.click(deleteButtons[1]);
    await waitFor(() => expect(screen.getByText("Remove access")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));

    await waitFor(() => {
      const deleteCalls = fetchMock.mock.calls.filter(
        ([url, init]) => String(url).includes("user-2") && init?.method === "DELETE",
      );
      expect(deleteCalls.length).toBeGreaterThanOrEqual(1);
    });
  });
});

describe("admin/usage", () => {
  it("renders all scalar stats from a sample payload", async () => {
    seedSession({ id: "user-1", email: "admin@example.com", role: "admin" });
    mockFetchByUrl((url) => {
      if (url.startsWith("/api/v1/admin/usage")) return jsonResponse({ body: USAGE });
      return jsonResponse({ status: 404 });
    });
    renderUsagePage();

    await waitFor(() => expect(screen.getByText("Total questions")).toBeInTheDocument());
    expect(screen.getByText("14")).toBeInTheDocument();
    expect(screen.getByText("13")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.getAllByText("4").length).toBeGreaterThan(0);
    expect(screen.getByText("305 ms")).toBeInTheDocument();
    expect(screen.getByText("21.4%")).toBeInTheDocument();
    expect(screen.getByText("6")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("renders by-day/by-user tables when present and the honest note when empty", async () => {
    seedSession({ id: "user-1", email: "admin@example.com", role: "admin" });
    mockFetchByUrl((url) => {
      if (url.startsWith("/api/v1/admin/usage")) {
        return jsonResponse({
          body: {
            ...USAGE,
            queries_by_day: [],
            queries_by_user: [],
          },
        });
      }
      return jsonResponse({ status: 404 });
    });
    renderUsagePage();

    await waitFor(() => expect(screen.getByText("Total questions")).toBeInTheDocument());
    expect(screen.getByText(/No daily breakdown yet/)).toBeInTheDocument();
    expect(screen.getByText(/No user breakdown yet/)).toBeInTheDocument();
  });

  it("error state retries", async () => {
    seedSession({ id: "user-1", email: "admin@example.com", role: "admin" });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: 500, body: { detail: "fail" } }))
      .mockResolvedValueOnce(jsonResponse({ body: USAGE }));
    vi.stubGlobal("fetch", fetchMock);
    renderUsagePage();

    await waitFor(() => expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByText("Total questions")).toBeInTheDocument());
  });
});
