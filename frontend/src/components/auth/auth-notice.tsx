"use client";

import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/**
 * Banner used by the auth screens: a form-level error (`role="alert"`) or an
 * informational note such as the session-expiry explanation (`role="status"`),
 * optionally dismissible. The message is always plain text — React renders it
 * as a text node, so nothing here can inject raw HTML.
 */
export function AuthNotice({
  tone = "info",
  message,
  onDismiss,
}: {
  tone?: "info" | "error";
  message: ReactNode;
  onDismiss?: () => void;
}) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn(
        "flex items-start gap-3 rounded-lg border px-4 py-3 text-sm",
        tone === "error"
          ? "border-danger/40 bg-danger/10 text-danger"
          : "border-primary/40 bg-primary/10 text-foreground",
      )}
    >
      <p className="flex-1">{message}</p>
      {onDismiss ? (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss notice"
          className="-m-1 flex h-6 w-6 shrink-0 items-center justify-center rounded text-base leading-none text-muted transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span aria-hidden="true">&times;</span>
        </button>
      ) : null}
    </div>
  );
}
