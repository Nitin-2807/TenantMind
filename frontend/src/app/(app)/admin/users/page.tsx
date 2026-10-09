"use client";

import { useCallback, useEffect, useState } from "react";

import { RequireRole } from "@/lib/auth/route-guard";
import { useAuth } from "@/lib/auth/auth-provider";
import { usersApi } from "@/lib/api";
import type { User } from "@/lib/types";
import { ApiError } from "@/lib/errors";

import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Spinner } from "@/components/ui/spinner";
import { UsersIcon } from "@/components/icons";
import { InviteUserDialog } from "@/components/users/invite-user-dialog";
import { DeleteUserDialog } from "@/components/users/delete-user-dialog";
import { UsersTable } from "@/components/users/users-table";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; users: User[] };

function friendlyMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail || "The request failed.";
  if (error instanceof Error) return error.message;
  return "The request failed.";
}

function UsersContent() {
  const { user: currentUser } = useAuth();
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [inviteOpen, setInviteOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [userToDelete, setUserToDelete] = useState<User | null>(null);

  const fetchUsers = useCallback(async () => {
    try {
      const response = await usersApi.listUsers();
      setState({ status: "ready", users: response.items });
    } catch (error) {
      setState({ status: "error", message: friendlyMessage(error) });
    }
  }, []);

  // Initial load — promise continuation so no state is set synchronously from
  // the effect body (react-hooks/set-state-in-effect). Event handlers reuse
  // fetchUsers below.
  useEffect(() => {
    let cancelled = false;
    usersApi
      .listUsers()
      .then((response) => {
        if (cancelled) return;
        setState({ status: "ready", users: response.items });
      })
      .catch((error) => {
        if (cancelled) return;
        setState({ status: "error", message: friendlyMessage(error) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleRetry = () => {
    setState({ status: "loading" });
    fetchUsers();
  };

  const handleDeleteClick = (user: User) => {
    setUserToDelete(user);
    setDeleteOpen(true);
  };

  const handleDeleteSuccess = () => {
    fetchUsers();
  };

  const handleInviteSuccess = () => {
    fetchUsers();
  };

  return (
    <div className="container mx-auto max-w-6xl space-y-6 px-4 py-6">
      <PageHeader
        title="Administration"
        description="Manage users and access"
        actions={
          <Button onClick={() => setInviteOpen(true)} aria-label="Invite user">
            Invite user
          </Button>
        }
      />

      {state.status === "loading" && (
        <div className="flex items-center justify-center py-12">
          <Spinner size="md" />
        </div>
      )}
      {state.status === "error" && (
        <ErrorState message={state.message} retry={handleRetry} />
      )}
      {state.status === "ready" && (
        <>
          {state.users.length === 0 ? (
            <EmptyState
              icon={<UsersIcon className="h-8 w-8" />}
              title="No users yet"
              description="Invite users to get started."
            />
          ) : (
            <UsersTable
              users={state.users}
              currentUserId={currentUser?.id ?? ""}
              onRefresh={fetchUsers}
              onDelete={handleDeleteClick}
            />
          )}
        </>
      )}

      <InviteUserDialog
        open={inviteOpen}
        onClose={() => setInviteOpen(false)}
        onInviteSuccess={handleInviteSuccess}
      />

      <DeleteUserDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onDeleteSuccess={handleDeleteSuccess}
        user={userToDelete}
      />
    </div>
  );
}

export default function AdminUsersPage() {
  return (
    <RequireRole role="admin">
      <UsersContent />
    </RequireRole>
  );
}
