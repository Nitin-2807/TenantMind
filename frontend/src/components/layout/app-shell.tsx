"use client";

/**
 * Application shell: role-aware sidebar (fixed on desktop, drawer on mobile),
 * topbar with identity/session controls, and the routed page content.
 */

import { useState, type ReactNode } from "react";

import { useAuth } from "@/lib/auth/auth-provider";
import type { UserRole } from "@/lib/types";

import { cn } from "@/lib/cn";
import { SidebarContent } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout } = useAuth();
  const [mobileOpen, setMobileOpen] = useState(false);

  if (!user) return null; // Guarded by RequireAuth in the layout.

  const role = user.role as UserRole;
  const closeMobile = () => setMobileOpen(false);

  return (
    <div className="min-h-screen bg-background">
      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-64 border-r border-border bg-surface lg:block">
        <SidebarContent role={role} />
      </aside>

      {/* Mobile drawer */}
      {mobileOpen ? (
        <div className="fixed inset-0 z-50 lg:hidden" role="presentation">
          <div
            className="absolute inset-0 bg-black/50"
            onClick={closeMobile}
            aria-hidden="true"
          />
          <div className="absolute inset-y-0 left-0 w-64 border-r border-border bg-surface shadow-xl">
            <SidebarContent role={role} onNavigate={closeMobile} />
          </div>
        </div>
      ) : null}

      <div className={cn("flex min-h-screen flex-col", "lg:pl-64")}>
        <Topbar user={user} onLogout={logout} onMenuClick={() => setMobileOpen(true)} />
        <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-6 sm:px-6 lg:px-8">
          {children}
        </main>
      </div>
    </div>
  );
}