"use client";

import { useRouter } from "next/navigation";

import { RequireAuth } from "@/lib/auth/route-guard";
import { useAuth } from "@/lib/auth/auth-provider";
import { getTenantSlug } from "@/lib/auth/tokens";
import { formatDateTime } from "@/lib/format";

import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

const roleTone: Record<string, "brand" | "neutral" | "success"> = {
  admin: "brand",
  editor: "success",
  viewer: "neutral",
};

function SettingsContent() {
  const { user, logout } = useAuth();
  const router = useRouter();
  const tenantSlug = getTenantSlug();

  const handleSignOut = async () => {
    await logout();
    router.replace("/login");
  };

  if (!user) return null;

  return (
    <div className="container mx-auto max-w-4xl space-y-6 px-4 py-6">
      <PageHeader title="Settings" description="Manage your account and workspace" />

      <Card>
        <CardHeader>
          <CardTitle>Account</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <p className="text-sm text-muted">Email</p>
            <p className="text-sm font-medium text-foreground">{user.email}</p>
          </div>
          <div>
            <p className="text-sm text-muted">Role</p>
            <Badge tone={roleTone[user.role]} className="capitalize">
              {user.role}
            </Badge>
          </div>
          <div>
            <p className="text-sm text-muted">Member since</p>
            <p className="text-sm font-medium text-foreground">{formatDateTime(user.created_at)}</p>
          </div>
          <div>
            <p className="text-sm text-muted">Workspace slug</p>
            <p className="text-sm font-medium text-foreground">{tenantSlug || "—"}</p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Workspace capabilities</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted">
            This product currently provides: documents, assistant, history, and the admin area (users and usage).
            Capabilities like billing, SSO, and API key management are not part of this version.
          </p>
        </CardContent>
      </Card>

      <div className="flex justify-start">
        <Button variant="outline" onClick={() => void handleSignOut()}>
          Sign out
        </Button>
      </div>
    </div>
  );
}

export default function SettingsPage() {
  return (
    <RequireAuth>
      <SettingsContent />
    </RequireAuth>
  );
}
