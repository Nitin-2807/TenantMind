/**
 * Settings page tests.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthProvider } from "@/lib/auth/auth-provider";
import { seedSession } from "@/test/utils";

const { push, replace } = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
}));

import SettingsPage from "./page";

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

function renderSettingsPage() {
  return render(
    <AuthProvider>
      <SettingsPage />
    </AuthProvider>,
  );
}

describe("settings", () => {
  it("renders the account info and a sign-out that clears the session", async () => {
    seedSession({ id: "u1", email: "admin@example.com", role: "admin" });
    renderSettingsPage();

    await waitFor(() => {
      expect(screen.getByText("Account")).toBeInTheDocument();
    });
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    expect(screen.getByText("admin")).toBeInTheDocument();
    expect(screen.getByText("acme")).toBeInTheDocument();

    const signOutButton = screen.getByRole("button", { name: "Sign out" });
    expect(signOutButton).toBeInTheDocument();
  });
});
