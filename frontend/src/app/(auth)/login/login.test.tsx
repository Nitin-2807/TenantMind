import { render, screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import LoginPage from "./page";

import { AuthProvider } from "@/lib/auth/auth-provider";
import { jsonResponse, mockFetchByUrl, seedSession, TEST_TOKENS, TEST_USER } from "@/test/utils";

const { push, replace } = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
}));

function renderLogin() {
  return render(
    <AuthProvider>
      <LoginPage />
    </AuthProvider>,
  );
}

async function fillValidCredentials(user: UserEvent): Promise<void> {
  await user.type(screen.getByLabelText("Workspace slug"), " acme ");
  await user.type(screen.getByLabelText("Email"), "admin@example.com");
  await user.type(screen.getByLabelText("Password"), "correct-horse");
}

beforeEach(() => {
  push.mockClear();
  replace.mockClear();
});

it("validates every field on submit without calling the API", async () => {
  const user = userEvent.setup();
  const fetchMock = mockFetchByUrl(() => jsonResponse({ status: 500 }));
  renderLogin();

  expect(screen.getByLabelText("Workspace slug")).toBeInTheDocument();
  expect(screen.getByLabelText("Email")).toBeInTheDocument();
  expect(screen.getByLabelText("Password")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByText("Slug is required.")).toBeInTheDocument();
  expect(screen.getByText("Email is required.")).toBeInTheDocument();
  expect(screen.getByText("Password is required.")).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();

  // Bad shapes: typed values trigger live re-validation, and re-submitting
  // runs validation again in our handler (the form is `noValidate`, so no
  // native bubble preempts the inline errors).
  await user.type(screen.getByLabelText("Workspace slug"), "Not A Slug!");
  await user.type(screen.getByLabelText("Email"), "not-an-email");
  await user.type(screen.getByLabelText("Password"), "correct-horse");

  expect(
    screen.getByText("Use lowercase letters, numbers, and dashes (e.g. acme-corp)."),
  ).toBeInTheDocument();
  expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
  expect(screen.queryByText("Password is required.")).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Sign in" }));
  expect(
    screen.getByText("Use lowercase letters, numbers, and dashes (e.g. acme-corp)."),
  ).toBeInTheDocument();
  expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();

  // Fixing a field clears its error on change.
  await user.clear(screen.getByLabelText("Workspace slug"));
  await user.type(screen.getByLabelText("Workspace slug"), "acme");
  expect(
    screen.queryByText("Use lowercase letters, numbers, and dashes (e.g. acme-corp)."),
  ).not.toBeInTheDocument();
  expect(fetchMock).not.toHaveBeenCalled();
});

it("posts the login payload and navigates to the dashboard", async () => {
  const user = userEvent.setup();
  const fetchMock = mockFetchByUrl(() =>
    jsonResponse({ status: 200, body: { user: TEST_USER, tokens: TEST_TOKENS } }),
  );
  renderLogin();

  await fillValidCredentials(user);
  await user.click(screen.getByRole("button", { name: "Sign in" }));

  await waitFor(() => expect(push).toHaveBeenCalledWith("/dashboard"));
  expect(fetchMock).toHaveBeenCalledTimes(1);

  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toBe("/api/v1/auth/login");
  expect(JSON.parse(String(init?.body))).toEqual({
    tenant_slug: "acme",
    email: "admin@example.com",
    password: "correct-horse",
  });
  // Tokens from the response are persisted, never rendered.
  expect(screen.queryByText(TEST_TOKENS.access_token)).not.toBeInTheDocument();
});

it("shows a friendly message for rejected credentials (401)", async () => {
  const user = userEvent.setup();
  mockFetchByUrl(() => jsonResponse({ status: 401, body: { detail: "Invalid credentials" } }));
  renderLogin();

  await fillValidCredentials(user);
  await user.click(screen.getByRole("button", { name: "Sign in" }));

  expect(
    await screen.findByText(
      "We couldn't sign you in. Check the workspace slug, email, and password.",
    ),
  ).toBeInTheDocument();
  // The backend's raw detail is not surfaced.
  expect(screen.queryByText("Invalid credentials")).not.toBeInTheDocument();
  expect(push).not.toHaveBeenCalled();
});

it("explains an expired session with a dismissible banner", async () => {
  const user = userEvent.setup();
  window.localStorage.setItem("tm.session_expired", "1");
  renderLogin();

  expect(
    screen.getByText("Your session has expired. Please sign in again."),
  ).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Dismiss notice" }));
  expect(
    screen.queryByText("Your session has expired. Please sign in again."),
  ).not.toBeInTheDocument();
});

it("redirects an already signed-in user to the dashboard", () => {
  seedSession();
  renderLogin();

  expect(replace).toHaveBeenCalledWith("/dashboard");
});
