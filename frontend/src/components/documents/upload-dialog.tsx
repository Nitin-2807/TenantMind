"use client";

/**
 * Upload dialog: one file at a time, validated with the shared client rules
 * (filename shape + 10 MB cap — the backend re-validates and stays
 * authoritative). The tenant is never sent; it comes from the bearer token.
 *
 * "Visible to" maps to the backend's `allowed_roles` form field: `Everyone`
 * omits the field entirely (null = every authenticated user in the tenant),
 * a specific role sends a JSON array like `["editor"]` — the API client
 * serializes it.
 */

import { useId, useState, type FormEvent } from "react";

import { documentsApi } from "@/lib/api";
import type { Document, UserRole } from "@/lib/types";
import { VALIDATION, validateUploadFile } from "@/lib/validation";

import { friendlyMessage } from "@/components/documents/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Select } from "@/components/ui/select";

interface VisibilityOption {
  value: string;
  label: string;
  /** `null` = everyone (the field is omitted from the form). */
  roles: UserRole[] | null;
}

const VISIBILITY_OPTIONS: VisibilityOption[] = [
  { value: "everyone", label: "Everyone", roles: null },
  { value: "admin", label: "Administrators", roles: ["admin"] },
  { value: "editor", label: "Editors", roles: ["editor"] },
  { value: "viewer", label: "Viewers", roles: ["viewer"] },
];

const ACCEPT = VALIDATION.allowedUploadExtensions.join(",");

export interface UploadDialogProps {
  onClose: () => void;
  /** Called after the backend accepted the file (status may be `processing`). */
  onUploaded: (document: Document) => void;
}

export function UploadDialog({ onClose, onUploaded }: UploadDialogProps) {
  const formId = useId();
  const fileId = useId();
  const visibilityId = useId();

  const [file, setFile] = useState<File | null>(null);
  const [visibility, setVisibility] = useState("everyone");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!file) {
      setError("Choose a file to upload.");
      return;
    }
    const fileError = validateUploadFile(file);
    if (fileError) {
      setError(fileError);
      return;
    }

    const selected = VISIBILITY_OPTIONS.find((option) => option.value === visibility);
    setBusy(true);
    setError(null);
    try {
      const uploaded = await documentsApi.uploadDocument(file, selected?.roles ?? undefined);
      onUploaded(uploaded);
    } catch (uploadError) {
      setError(friendlyMessage(uploadError, "The upload failed. Please try again."));
    } finally {
      setBusy(false);
    }
  };

  // Enter-to-submit (a11y) routes through the form; the footer button calls
  // the same routine directly.
  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void submit();
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Upload document"
      description="Add a file to your team's knowledge base."
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} loading={busy}>
            Upload
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={handleSubmit} className="space-y-4">
        <Field
          label="File"
          htmlFor={fileId}
          error={error}
          hint={`${VALIDATION.uploadMaxBytes / (1024 * 1024)} MB max — ${VALIDATION.allowedUploadExtensions.join(", ")}`}
        >
          <input
            id={fileId}
            type="file"
            accept={ACCEPT}
            className="block w-full text-sm text-foreground file:mr-3 file:rounded-lg file:border file:border-border file:bg-surface file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-foreground hover:file:bg-background"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              setError(null);
            }}
          />
        </Field>
        <Field label="Visible to" htmlFor={visibilityId} hint="Who can view this document.">
          <Select
            id={visibilityId}
            value={visibility}
            onChange={(event) => setVisibility(event.target.value)}
          >
            {VISIBILITY_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </Field>
      </form>
    </Dialog>
  );
}
