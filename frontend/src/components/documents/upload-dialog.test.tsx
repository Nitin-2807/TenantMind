/**
 * Upload dialog tests: client-side validation must reject bad files without
 * ever hitting the network, and a valid upload must post multipart form data
 * with `allowed_roles` serialized as a JSON array.
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";

import { UploadDialog } from "@/components/documents/upload-dialog";
import type { Document } from "@/lib/types";
import { jsonResponse, mockFetchByUrl } from "@/test/utils";

function makeDoc(overrides: Partial<Document> = {}): Document {
  return {
    id: "doc-1",
    filename: "notes.txt",
    status: "processing",
    allowed_roles: null,
    content_type: "text/plain",
    size_bytes: 128,
    chunk_count: 0,
    error: null,
    created_at: "2026-01-05T10:30:00Z",
    updated_at: "2026-01-05T10:30:00Z",
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderDialog() {
  const onUploaded = vi.fn();
  const onClose = vi.fn();
  render(<UploadDialog onClose={onClose} onUploaded={onUploaded} />);
  return { onUploaded, onClose };
}

function requireFormData(value: FormData | undefined): FormData {
  if (!value) throw new Error("expected the upload to carry FormData");
  return value;
}

it("rejects an oversized file without calling the API", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  renderDialog();
  const user = userEvent.setup();

  const oversized = new File(
    [new Uint8Array(10 * 1024 * 1024 + 1)],
    "huge.txt",
    { type: "text/plain" },
  );
  await user.upload(screen.getByLabelText("File"), oversized);
  await user.click(screen.getByRole("button", { name: "Upload" }));

  expect(screen.getByRole("alert")).toHaveTextContent(
    "That file exceeds the 10 MB upload limit.",
  );
  expect(fetchMock).not.toHaveBeenCalled();
});

it("rejects a bad filename without calling the API", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  renderDialog();
  const user = userEvent.setup();

  const badName = new File(["x"], ".notes.txt", { type: "text/plain" });
  await user.upload(screen.getByLabelText("File"), badName);
  await user.click(screen.getByRole("button", { name: "Upload" }));

  expect(screen.getByRole("alert")).toHaveTextContent(
    "Choose a file whose name does not begin with a dot.",
  );
  expect(fetchMock).not.toHaveBeenCalled();
});

it("posts multipart form data with allowed_roles as a JSON array", async () => {
  let captured: FormData | undefined;
  mockFetchByUrl((url, init) => {
    if (init?.method === "POST" && String(url).endsWith("/api/v1/documents")) {
      captured = init.body as FormData;
      return jsonResponse({ status: 201, body: makeDoc({ allowed_roles: ["editor"] }) });
    }
    throw new Error(`Unexpected request: ${String(url)}`);
  });

  const { onUploaded } = renderDialog();
  const user = userEvent.setup();

  await user.upload(
    screen.getByLabelText("File"),
    new File(["hello"], "notes.txt", { type: "text/plain" }),
  );
  await user.selectOptions(screen.getByLabelText("Visible to"), "editor");
  await user.click(screen.getByRole("button", { name: "Upload" }));

  await waitFor(() => expect(onUploaded).toHaveBeenCalledTimes(1));
  expect(onUploaded).toHaveBeenCalledWith(
    expect.objectContaining({ id: "doc-1", status: "processing" }),
  );

  const form = requireFormData(captured);
  expect(form.get("allowed_roles")).toBe(JSON.stringify(["editor"]));
  expect((form.get("file") as File | null)?.name).toBe("notes.txt");
});

it("omits allowed_roles entirely when visibility is Everyone", async () => {
  let captured: FormData | undefined;
  mockFetchByUrl((url, init) => {
    if (init?.method === "POST" && String(url).endsWith("/api/v1/documents")) {
      captured = init.body as FormData;
      return jsonResponse({ status: 201, body: makeDoc() });
    }
    throw new Error(`Unexpected request: ${String(url)}`);
  });

  const { onUploaded } = renderDialog();
  const user = userEvent.setup();

  await user.upload(
    screen.getByLabelText("File"),
    new File(["hello"], "notes.txt", { type: "text/plain" }),
  );
  await user.click(screen.getByRole("button", { name: "Upload" }));

  await waitFor(() => expect(onUploaded).toHaveBeenCalledTimes(1));
  const form = requireFormData(captured);
  expect(form.get("allowed_roles")).toBeNull();
  expect((form.get("file") as File | null)?.name).toBe("notes.txt");
});

it("closes via Cancel without uploading anything", async () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const { onClose, onUploaded } = renderDialog();

  await userEvent.setup().click(screen.getByRole("button", { name: "Cancel" }));

  expect(onClose).toHaveBeenCalledTimes(1);
  expect(onUploaded).not.toHaveBeenCalled();
  expect(fetchMock).not.toHaveBeenCalled();
});
