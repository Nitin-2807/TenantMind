import type { DocumentStatus } from "@/lib/types";

import { Badge } from "@/components/ui/badge";

/**
 * Document ingestion status, never communicated by color alone: each state
 * carries its own text and icon (PMP §10 states: processing / ready / failed).
 */
export function StatusBadge({ status }: { status: DocumentStatus }) {
  switch (status) {
    case "ready":
      return (
        <Badge tone="success">
          <span aria-hidden="true">✓</span> Ready
        </Badge>
      );
    case "failed":
      return (
        <Badge tone="danger">
          <span aria-hidden="true">✕</span> Failed
        </Badge>
      );
    case "processing":
      return (
        <Badge tone="warning">
          <SpinnerDot /> Processing
        </Badge>
      );
  }
}

function SpinnerDot() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-2.5 w-2.5 animate-spin rounded-full border border-warning border-t-transparent align-middle"
    />
  );
}