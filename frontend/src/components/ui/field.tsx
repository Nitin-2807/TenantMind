import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/** Form field wrapper: label + control + error/hint. */
export function Field({
  label,
  htmlFor,
  error,
  hint,
  className,
  children,
}: {
  label?: string;
  htmlFor?: string;
  error?: string | null;
  hint?: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("space-y-1", className)}>
      {label ? (
        <label htmlFor={htmlFor} className="mb-1.5 block text-sm font-medium text-foreground">
          {label}
        </label>
      ) : null}
      {children}
      {hint && !error ? <p className="text-xs text-muted">{hint}</p> : null}
      {error ? (
        <p role="alert" className="text-xs text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}