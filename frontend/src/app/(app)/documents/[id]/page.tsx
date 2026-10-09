"use client";

/**
 * Document detail — metadata only.
 *
 * The backend returns no extracted text for a document, so this screen shows
 * the honest facts it does return: status, size, chunks, access, timestamps,
 * and the failure reason when ingestion failed. No fabricated preview.
 *
 * Loading: `getDocument(id)` for everyone. While a document is `processing`,
 * an administrator additionally polls the admin status endpoint (cheaper,
 * admin-only); editors and viewers keep re-fetching `getDocument(id)`.
 */

import Link from "next/link";
import { Suspense, use, useCallback, useEffect, useState, type ReactNode } from "react";

import { documentsApi } from "@/lib/api";
import { useAuth } from "@/lib/auth/auth-provider";
import { ApiError } from "@/lib/errors";
import { formatBytes, formatDateTime } from "@/lib/format";
import type { Document } from "@/lib/types";
import { useDocumentPolling } from "@/hooks/use-document-status";

import { friendlyMessage } from "@/components/documents/shared";
import { Badge } from "@/components/ui/badge";
import { ErrorState } from "@/components/ui/error-state";
import { PageHeader } from "@/components/layout/page-header";
import { Spinner } from "@/components/ui/spinner";
import { StatusBadge } from "@/components/ui/status-badge";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; document: Document };

const backLinkClass =
  "inline-flex items-center text-sm font-medium text-muted transition-colors hover:text-foreground";

function BackLink() {
  return (
    <Link href="/documents" className={backLinkClass}>
      <span aria-hidden="true">← </span>
      Back to documents
    </Link>
  );
}

function LoadingBlock() {
  return (
    <div
      className="flex min-h-[40vh] items-center justify-center"
      role="status"
      aria-label="Loading document"
    >
      <Spinner size="lg" />
    </div>
  );
}

function MetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 px-4 py-3">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="text-right text-sm text-foreground">{children}</dd>
    </div>
  );
}

/** Friendly, honest message for a failed detail load (404/403 are special). */
function documentErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return "This document no longer exists, or the link is out of date.";
    }
    if (error.status === 403) {
      return "You don't have permission to view this document.";
    }
  }
  return friendlyMessage(error, "Could not load this document.");
}

export default function DocumentDetailPage({ params }: PageProps<"/documents/[id]">) {
  return (
    <Suspense fallback={<LoadingBlock />}>
      <DocumentDetail params={params} />
    </Suspense>
  );
}

function DocumentDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";

  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    documentsApi
      .getDocument(id)
      .then((document) => {
        if (!cancelled) setState({ status: "ready", document });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: "error", message: documentErrorMessage(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const current = state.status === "ready" ? state.document : null;

  // Status refresh while processing: administrators use the admin status
  // endpoint; everyone else re-fetches the document itself (the admin
  // endpoint would 403 for editors/viewers).
  const refresh = useCallback(async (): Promise<void> => {
    try {
      if (isAdmin) {
        const status = await documentsApi.getDocumentAdminStatus(id);
        setState((previous) =>
          previous.status === "ready"
            ? {
                status: "ready",
                document: {
                  ...previous.document,
                  status: status.status,
                  chunk_count: status.chunk_count,
                  error: status.error,
                  updated_at: status.updated_at,
                },
              }
            : previous,
        );
      } else {
        const document = await documentsApi.getDocument(id);
        setState({ status: "ready", document });
      }
    } catch {
      // Transient poll failure: keep the last known state.
    }
  }, [id, isAdmin]);

  useDocumentPolling(current?.status === "processing", refresh);

  if (state.status === "loading") return <LoadingBlock />;

  if (state.status === "error") {
    return (
      <div>
        <ErrorState title="Could not load this document" message={state.message}>
          <Link href="/documents" className={`${backLinkClass} mt-4`}>
            Back to documents
          </Link>
        </ErrorState>
      </div>
    );
  }

  const document = state.document;
  const restricted = document.allowed_roles !== null;
  const visibleTo =
    restricted && document.allowed_roles && document.allowed_roles.length > 0
      ? document.allowed_roles.join(", ")
      : "Everyone";

  return (
    <div>
      <div className="mb-4">
        <BackLink />
      </div>

      <PageHeader
        title={document.filename}
        actions={
          <div className="flex items-center gap-2">
            {restricted ? <Badge tone="neutral">Restricted</Badge> : null}
            <StatusBadge status={document.status} />
          </div>
        }
      />

      {document.status === "failed" && document.error ? (
        <div
          role="alert"
          className="mb-6 rounded-xl border border-danger/30 bg-danger/5 p-4"
        >
          <p className="text-sm font-semibold text-danger">Processing failed</p>
          <p className="mt-1 text-sm text-muted">{document.error}</p>
        </div>
      ) : null}

      <dl className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-surface">
        <MetaRow label="Content type">{document.content_type}</MetaRow>
        <MetaRow label="Size">{formatBytes(document.size_bytes)}</MetaRow>
        <MetaRow label="Chunks">{document.chunk_count}</MetaRow>
        <MetaRow label="Visible to">{visibleTo}</MetaRow>
        <MetaRow label="Created">{formatDateTime(document.created_at)}</MetaRow>
        <MetaRow label="Updated">{formatDateTime(document.updated_at)}</MetaRow>
      </dl>
    </div>
  );
}
