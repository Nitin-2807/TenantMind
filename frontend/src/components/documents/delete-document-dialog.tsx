"use client";

/**
 * Delete confirmation. Destructive, so it names the file, states the
 * consequence plainly, and requires an explicit confirm click. The backend
 * authorizes the DELETE (administrators only) — any refusal (e.g. 403) is
 * surfaced here with the backend's own reason.
 */

import { useState } from "react";

import { documentsApi } from "@/lib/api";
import type { Document } from "@/lib/types";

import { friendlyMessage } from "@/components/documents/shared";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { useToast } from "@/components/ui/toaster";

export interface DeleteDocumentDialogProps {
  document: Document;
  onClose: () => void;
  /** Called after the backend confirmed the deletion (refresh the list). */
  onDeleted: () => void;
}

export function DeleteDocumentDialog({
  document,
  onClose,
  onDeleted,
}: DeleteDocumentDialogProps) {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDelete = async () => {
    setBusy(true);
    setError(null);
    try {
      await documentsApi.deleteDocument(document.id);
      toast({
        variant: "success",
        title: "Document deleted",
        description: document.filename,
      });
      onDeleted();
    } catch (deleteError) {
      setError(
        friendlyMessage(deleteError, "The document could not be deleted. Please try again."),
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Delete document?"
      description={`"${document.filename}" — This removes it from the knowledge base immediately.`}
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="danger" onClick={handleDelete} loading={busy}>
            Delete document
          </Button>
        </>
      }
    >
      {error ? (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      ) : null}
    </Dialog>
  );
}
