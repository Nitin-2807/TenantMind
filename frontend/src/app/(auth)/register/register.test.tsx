import { render, screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import RegisterPage from "./page";

import { AuthProvider } from "@/lib/auth/auth-provider";
import { jsonResponse, mockFetchByUrl, TEST_TOKENS, TEST_USER } from "@/test/utils";

const { push, replace } = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
}));

const TEST_TENANT = {
  id: "tenant-1",
  name: "Acme Corp",
  slug: "acme-corp",
  created_at: "2026-01-01T00:00:00Z",
} as const;

function renderRegister() {
  return render(
    <AuthProvider>
      <RegisterPage />
    </AuthProvider>,
  );
}

async function fillValidRegistration(user: UserEvent): Promise<void> {
  await user.type(screen.getByLabelText("Organization name"), TEST_TENANT.name);
  await user.type(screen.getByLabelText("Workspace slug"), TEST_TENANT.slug);
  await user.type(screen.getByLabelText("Admin email"), "admin@example.com");
  await user.type(screen.getByLabelText("Admin password"), "correct-horse-battery");
}

beforeEach(() => {
  push.mockClear();
  replace.mockClear();
});

it("validates required fields and the 12-character password rule without the API", async () => {
  const user = userEvent.setup();
  const fetchMock = mockFetchByUrl(() => jsonResponse({ status: 500 }));
  renderRegister();

  await user.click(screen.getByRole("button", { name: "Create workspace" }));

  expect(await screen.findByText("Organization name is required.")).toBeInTheDocument();
  expect(screen.getByText("Slug is required.")).toBeInTheDocument();
  expect(screen.getByText("Email is required.")).toBeInTheDocument();
  expect(screen.getByText("Password is required.")).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();

  // New passwords need 12+ characters; the error shows inline as soon as the
  // field has been validated.
  await user.type(screen.getByLabelText("Organization name"), "Acme Corp");
  await user.type(screen.getByLabelText("Workspace slug"), "acme-corp");
  await user.type(screen.getByLabelText("Admin email"), "admin@example.com");
  await user.type(screen.getByLabelText("Admin password"), "shortpw");

  expect(screen.getByText("Password must be at least 12 characters.")).toBeInTheDocument();
  // …and the other errors cleared as each field became valid.
  expect(screen.queryByText("Organization name is required.")).not.toBeInTheDocument();
  expect(screen.queryByText("Slug is required.")).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Create workspace" }));
  expect(screen.getByText("Password must be at least 12 characters.")).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("submits the registration payload and navigates to the dashboard", async () => {
  const user = userEvent.setup();
  const fetchMock = mockFetchByUrl(() =>
    jsonResponse({
      status: 201,
      body: { tenant: TEST_TENANT, user: TEST_USER, tokens: TEST_TOKENS },
    }),
  );
  renderRegister();

  await fillValidRegistration(user);
  await user.click(screen.getByRole("button", { name: "Create workspace" }));

  await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
  expect(fetchMock).toHaveBeenCalledTimes(1);

  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toBe("/api/v1/auth/register-tenant");
  expect(JSON.parse(String(init?.body))).toEqual({
    name: "Acme Corp",
    slug: "acme-corp",
    admin_email: "admin@example.com",
    admin_password: "correct-horse-battery",
  });
  // Tokens are persisted by the auth provider, never rendered.
  expect(screen.queryByText(TEST_TOKENS.access_token)).not.toBeInTheDocument();
});

it("surfaces the backend reason when the slug is already taken (409)", async () => {
  const user = userEvent.setup();
  mockFetchByUrl(() =>
    jsonResponse({ status: 409, body: { detail: "That workspace slug is already in use." } }),
  );
  renderRegister();

  await fillValidRegistration(user);
  await user.click(screen.getByRole("button", { name: "Create workspace" }));

  expect(await screen.findByText("That workspace slug is already in use.")).toBeInTheDocument();
  expect(push).not.toHaveBeenCalled();
});
