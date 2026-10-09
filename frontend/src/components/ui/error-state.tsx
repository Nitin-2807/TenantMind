"use client";

import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";

export function ErrorState({
  title = "Something went wrong",
  message,
  retry,
  className,
  children,
}: {
  title?: string;
  message?: string;
  retry?: () => void;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <div
      role="alert"
      className={`flex flex-col items-center justify-center rounded-xl border border-border bg-surface px-6 py-10 text-center ${className ?? ""}`}
    >
      <div aria-hidden="true" className="mb-2 text-2xl">
        ⚠
      </div>
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {message ? <p className="mt-1 max-w-md text-sm text-muted">{message}</p> : null}
      {children}
      {retry ? (
        <Button variant="outline" size="sm" className="mt-4" onClick={retry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}