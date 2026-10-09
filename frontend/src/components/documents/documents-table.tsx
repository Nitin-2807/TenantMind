"use client";

/**
 * Responsive documents list: a real `<table>` on desktop and a compact card
 * list on small screens (only one is ever visible; both render the same data
 * and the same actions).
 *
 * All strings from the backend are rendered as plain React text — filenames
 * are untrusted input and are never injected as HTML.
 */

import Link from "next/link";

import type { Document } from "@/lib/types";

import { TrashIcon } from "@/components/icons";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui/status-badge";
import { formatBytes, formatDateTime } from "@/lib/format";

export interface DocumentsTableProps {
  documents: Document[];
  /** Delete is an administrator-only action; the backend enforces it too. */
  canDelete: boolean;
  onDelete: (document: Document) => void;
}

function isRestricted(document: Document): boolean {
  return document.allowed_roles !== null;
}

function RestrictedBadge() {
  return <Badge tone="neutral">Restricted</Badge>;
}

function RowActions({
  document,
  canDelete,
  onDelete,
}: Pick<DocumentsTableProps, "canDelete" | "onDelete"> & { document: Document }) {
  return (
    <div className="flex items-center justify-end gap-1">
      <Link
        href={`/documents/${document.id}`}
        aria-label={`View ${document.filename}`}
        className="inline-flex h-8 items-center rounded-lg px-3 text-sm font-medium text-foreground transition-colors hover:bg-background"
      >
        View
      </Link>
      {canDelete ? (
        <Button
          variant="ghost"
          size="sm"
          aria-label={`Delete ${document.filename}`}
          onClick={() => onDelete(document)}
        >
          <TrashIcon className="h-4 w-4" />
          Delete
        </Button>
      ) : null}
    </div>
  );
}

export function DocumentsTable({ documents, canDelete, onDelete }: DocumentsTableProps) {
  return (
    <div>
      {/* Desktop */}
      <div className="hidden overflow-hidden rounded-xl border border-border bg-surface md:block">
        <table aria-label="Documents" className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted">
              <th scope="col" className="px-4 py-3 font-medium">Name</th>
              <th scope="col" className="px-4 py-3 font-medium">Status</th>
              <th scope="col" className="px-4 py-3 font-medium">Size</th>
              <th scope="col" className="px-4 py-3 font-medium">Created</th>
              <th scope="col" className="px-4 py-3 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {documents.map((document) => (
              <tr key={document.id}>
                <td className="px-4 py-3">
                  <span className="block font-medium text-foreground">{document.filename}</span>
                  {isRestricted(document) ? (
                    <span className="mt-1 inline-block"><RestrictedBadge /></span>
                  ) : null}
                </td>
                <td className="px-4 py-3"><StatusBadge status={document.status} /></td>
                <td className="px-4 py-3 text-muted">{formatBytes(document.size_bytes)}</td>
                <td className="px-4 py-3 text-muted">{formatDateTime(document.created_at)}</td>
                <td className="px-4 py-3">
                  <RowActions
                    document={document}
                    canDelete={canDelete}
                    onDelete={onDelete}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Mobile / tablet */}
      <ul aria-label="Documents" className="space-y-3 md:hidden">
        {documents.map((document) => (
          <li
            key={document.id}
            className="rounded-xl border border-border bg-surface p-4"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-foreground">{document.filename}</p>
                {isRestricted(document) ? (
                  <span className="mt-1 inline-block"><RestrictedBadge /></span>
                ) : null}
              </div>
              <StatusBadge status={document.status} />
            </div>
            <dl className="mt-3 space-y-1 text-xs text-muted">
              <div className="flex justify-between gap-3">
                <dt>Size</dt>
                <dd>{formatBytes(document.size_bytes)}</dd>
              </div>
              <div className="flex justify-between gap-3">
                <dt>Created</dt>
                <dd>{formatDateTime(document.created_at)}</dd>
              </div>
            </dl>
            <div className="mt-3 border-t border-border pt-2">
              <RowActions document={document} canDelete={canDelete} onDelete={onDelete} />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
