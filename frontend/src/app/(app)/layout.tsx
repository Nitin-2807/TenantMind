/**
 * Protected application shell. All authenticated pages live under this route
 * group; the backend remains the authorization authority (§10), and these
 * guards only decide which *screens* a signed-in user sees.
 */

import type { ReactNode } from "react";

import { RequireAuth } from "@/lib/auth/route-guard";
import { AppShell } from "@/components/layout/app-shell";

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <RequireAuth>
      <AppShell>{children}</AppShell>
    </RequireAuth>
  );
}