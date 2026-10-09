"use client";

/**
 * Global error boundary (Next 16: the retry callback re-renders the segment).
 * Deliberately shows no stack traces or raw error details — just a friendly,
 * recoverable message.
 */

import { useEffect } from "react";

import { Button } from "@/components/ui/button";

export default function GlobalError({
  error,
  retry,
}: {
  error: Error & { digest?: string };
  retry: () => void;
}) {
  useEffect(() => {
    // Minimal, no-token logging. Error details are never rendered to the user.
    console.error("TenantMind error boundary", { digest: error.digest });
  }, [error]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-6">
      <div
        role="alert"
        className="w-full max-w-md rounded-xl border border-border bg-surface p-8 text-center"
      >
        <h1 className="text-lg font-semibold text-foreground">Something went wrong</h1>
        <p className="mt-2 text-sm text-muted">
          An unexpected error occurred while loading this page. Please try again — if the
          problem persists, contact your organization&apos;s administrator.
        </p>
        <Button className="mt-5" onClick={retry}>
          Try again
        </Button>
      </div>
    </div>
  );
}