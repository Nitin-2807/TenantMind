"use client";

/**
 * Documents list — the knowledge-base browser.
 *
 * Data comes only from `GET /documents` through the API client (the backend
 * already filters to what this caller may see). Distinct loading / error /
 * empty / ready states, role-aware controls, and status polling while any
 * document is still `processing`.
 *
 * Role model (backend enforces; this mirrors it in the UI):
 *   upload = admin + editor, delete = admin, viewing = everyone.
 */

import { useCallback, useEffect, useState } from "react";

import { documentsApi } from "@/lib/api";
import { useAuth } from "@/lib/auth/auth-provider";
import type { Document } from "@/lib/types";
import { useProcessingDocuments } from "@/hooks/use-document-status";

import { DeleteDocumentDialog } from "@/components/documents/delete-document-dialog";
import { DocumentsTable } from "@/components/documents/documents-table";
import { canDelete, canUpload, friendlyMessage } from "@/components/documents/shared";
import { UploadDialog } from "@/components/documents/upload-dialog";
import { DocumentsIcon, UploadIcon } from "@/components/icons";
import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Spinner } from "@/components/ui/spinner";
import { useToast } from "@/components/ui/toaster";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; documents: Document[] };

export default function DocumentsPage() {
  const { user } = useAuth();
  const { toast } = useToast();
  const uploadAllowed = canUpload(user?.role);
  const deleteAllowed = canDelete(user?.role);

  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [uploadOpen, setUploadOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Document | null>(null);

  const fetchList = useCallback(async (): Promise<Document[]> => {
    const response = await documentsApi.listDocuments();
    return response.items;
  }, []);

  // Initial load: drives the loading → ready/error state machine.
  useEffect(() => {
    let cancelled = false;
    fetchList()
      .then((documents) => {
        if (!cancelled) setState({ status: "ready", documents });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({ status: "error", message: friendlyMessage(error, "Could not load documents.") });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [fetchList]);

  // Background refresh (after a mutation, and every status poll): a transient
  // failure keeps what's on screen rather than replacing it with an error page.
  const refresh = useCallback(async (): Promise<void> => {
    try {
      const documents = await fetchList();
      setState({ status: "ready", documents });
    } catch {
      // Keep the current view; the next poll or manual action will retry.
    }
  }, [fetchList]);

  const retry = () => {
    setState({ status: "loading" });
    fetchList()
      .then((documents) => setState({ status: "ready", documents }))
      .catch((error: unknown) =>
        setState({ status: "error", message: friendlyMessage(error, "Could not load documents.") }),
      );
  };

  const documents = state.status === "ready" ? state.documents : null;
  // Re-poll every few seconds while anything is still processing.
  useProcessingDocuments(documents, refresh);

  const handleUploaded = (document: Document) => {
    setUploadOpen(false);
    toast({
      variant: "info",
      title: "Uploaded — processing…",
      description: document.filename,
    });
    void refresh();
  };

  const handleDeleted = () => {
    setDeleteTarget(null);
    void refresh();
  };

  return (
    <div>
      <PageHeader
        title="Documents"
        description="Browse your knowledge base: file status, access, and details."
        actions={
          uploadAllowed ? (
            <Button onClick={() => setUploadOpen(true)}>
              <UploadIcon className="h-4 w-4" />
              Upload document
            </Button>
          ) : null
        }
      />

      {state.status === "loading" ? (
        <div
          className="flex min-h-[40vh] items-center justify-center"
          role="status"
          aria-label="Loading documents"
        >
          <Spinner size="lg" />
        </div>
      ) : null}

      {state.status === "error" ? (
        <ErrorState
          title="Could not load documents"
          message={state.message}
          retry={retry}
        />
      ) : null}

      {state.status === "ready" && state.documents.length === 0 ? (
        <EmptyState
          icon={<DocumentsIcon className="h-8 w-8" />}
          title={uploadAllowed ? "Upload your first document" : "No documents yet"}
          description={
            uploadAllowed
              ? "Add a .txt, .md, .pdf, or .docx file to start building your knowledge base."
              : "Documents shared with your role will show up here."
          }
          action={
            uploadAllowed ? (
              <Button onClick={() => setUploadOpen(true)}>Upload document</Button>
            ) : undefined
          }
        />
      ) : null}

      {state.status === "ready" && state.documents.length > 0 ? (
        <DocumentsTable
          documents={state.documents}
          canDelete={deleteAllowed}
          onDelete={setDeleteTarget}
        />
      ) : null}

      {/* Mounted only while open: a fresh mount resets file/selection/errors. */}
      {uploadOpen ? (
        <UploadDialog onClose={() => setUploadOpen(false)} onUploaded={handleUploaded} />
      ) : null}
      {deleteTarget ? (
        <DeleteDocumentDialog
          document={deleteTarget}
          onClose={() => setDeleteTarget(null)}
          onDeleted={handleDeleted}
        />
      ) : null}
    </div>
  );
}
