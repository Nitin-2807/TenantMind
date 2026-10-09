"use client";

import Link from "next/link";

import type { Citation } from "@/lib/types";

interface CitationsPanelProps {
  citations: Citation[];
}

export function CitationsPanel({ citations }: CitationsPanelProps) {
  if (!citations || citations.length === 0) {
    return null;
  }

  return (
    <div className="mt-3 rounded-lg border border-border bg-surface p-3">
      <h4 className="mb-2 text-sm font-medium text-foreground">Sources</h4>
      <ul className="space-y-1">
        {citations.map((citation, index) => {
          const pageInfo =
            citation.page !== null && citation.page !== undefined
              ? ` (p. ${citation.page})`
              : "";
          return (
            <li key={`${citation.document_id}-${citation.chunk_index}-${index}`}>
              <Link
                href={`/documents/${citation.document_id}`}
                className="text-sm text-primary hover:underline"
              >
                {citation.filename}
                {pageInfo} [chunk {citation.chunk_index}]
              </Link>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
