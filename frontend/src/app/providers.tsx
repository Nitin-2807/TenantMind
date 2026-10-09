"use client";

/**
 * Client-side providers for the whole app: authentication state and toasts.
 */

import type { ReactNode } from "react";

import { AuthProvider } from "@/lib/auth/auth-provider";
import { ToasterProvider } from "@/components/ui/toaster";

export function Providers({ children }: { children: ReactNode }) {
  return (
    <AuthProvider>
      <ToasterProvider>{children}</ToasterProvider>
    </AuthProvider>
  );
}