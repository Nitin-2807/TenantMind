"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { getTenantSlug } from "@/lib/auth/tokens";
import type { User } from "@/lib/types";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { MenuIcon } from "@/components/icons";

const roleTone: Record<User["role"], "brand" | "neutral" | "success"> = {
  admin: "brand",
  editor: "success",
  viewer: "neutral",
};

export function Topbar({
  user,
  onLogout,
  onMenuClick,
}: {
  user: User;
  onLogout: () => Promise<void> | void;
  onMenuClick: () => void;
}) {
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);
  const tenantSlug = getTenantSlug();

  const handleLogout = async () => {
    setSigningOut(true);
    try {
      await onLogout();
    } finally {
      // Logout clears the session; navigate to login regardless of outcome.
      router.replace("/login");
    }
  };

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-border bg-surface/80 px-4 backdrop-blur sm:px-6">
      {/* Mobile: brand + menu; the brand doubles as the home link. */}
      <div className="flex items-center gap-2 lg:hidden">
        <Button
          variant="ghost"
          size="sm"
          onClick={onMenuClick}
          aria-label="Open navigation menu"
          className="h-9 w-9 px-0"
        >
          <MenuIcon className="h-5 w-5" />
        </Button>
        <span className="text-sm font-semibold text-foreground">TenantMind</span>
      </div>

      {/* Desktop: tenant context */}
      <div className="hidden items-center gap-2 lg:flex">
        <span className="text-sm text-muted">
          {tenantSlug ? (
            <>
              Workspace: <span className="font-medium text-foreground">{tenantSlug}</span>
            </>
          ) : (
            "TenantMind"
          )}
        </span>
      </div>

      <div className="flex items-center gap-2">
        <span className="hidden text-sm text-muted sm:inline">{user.email}</span>
        <Badge tone={roleTone[user.role]} className="capitalize">
          {user.role}
        </Badge>
        <Button
          variant="outline"
          size="sm"
          onClick={() => void handleLogout()}
          loading={signingOut}
          aria-label="Sign out"
        >
          Sign out
        </Button>
      </div>
    </header>
  );
}