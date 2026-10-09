import type { ReactNode } from "react";

/**
 * Unauthenticated route group: a centered, single-column shell with the
 * TenantMind brand mark (same pattern as the sidebar) and room for one card.
 * The `(app)` group owns the protected shell; everything here is pre-auth.
 */
export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen flex-col bg-background">
      <header className="flex justify-center px-4 pt-8 sm:pt-12">
        <h1 className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-sm font-bold text-primary-foreground"
          >
            T
          </span>
          <span className="text-lg font-semibold tracking-tight text-foreground">TenantMind</span>
        </h1>
      </header>
      <main className="flex flex-1 items-center justify-center px-4 py-8 sm:px-6">
        <div className="w-full max-w-md">{children}</div>
      </main>
    </div>
  );
}
