"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
import { TrashIcon } from "@/components/icons";
import { useToast } from "@/components/ui/toaster";
import { usersApi } from "@/lib/api";
import type { User, UserRole } from "@/lib/types";
import { formatDateTime } from "@/lib/format";
import { ApiError } from "@/lib/errors";

interface UsersTableProps {
  users: User[];
  currentUserId: string;
  onRefresh: () => void;
  onDelete: (user: User) => void;
}

export function UsersTable({ users, currentUserId, onRefresh, onDelete }: UsersTableProps) {
  const [changingRoleId, setChangingRoleId] = useState<string | null>(null);
  const { toast } = useToast();

  const handleRoleChange = async (userId: string, newRole: UserRole) => {
    setChangingRoleId(userId);
    try {
      await usersApi.changeUserRole(userId, newRole);
      toast({
        variant: "success",
        title: "Role updated",
        description: "User role has been updated.",
      });
      onRefresh();
    } catch (err) {
      let message = "Failed to change role.";
      if (err instanceof ApiError) {
        message = err.detail || message;
      } else if (err instanceof Error) {
        message = err.message;
      }
      toast({
        variant: "error",
        title: "Failed to change role",
        description: message,
      });
    } finally {
      setChangingRoleId(null);
    }
  };

  if (users.length === 0) {
    return null;
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Users</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead className="border-b border-border bg-surface">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-muted">
                  Email
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-muted">
                  Role
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-muted">
                  Joined
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-muted">
                  Status
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium uppercase tracking-wider text-muted">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {users.map((user) => {
                const isCurrentUser = user.id === currentUserId;
                return (
                  <tr key={user.id} className="hover:bg-surface/50">
                    <td className="px-4 py-3 text-sm text-foreground">{user.email}</td>
                    <td className="px-4 py-3 text-sm text-foreground">
                      <Select
                        value={user.role}
                        onChange={(e) => handleRoleChange(user.id, e.target.value as UserRole)}
                        disabled={isCurrentUser || changingRoleId === user.id}
                        className="h-8 w-32"
                        aria-label={`Change role for ${user.email}`}
                      >
                        <option value="admin">Admin</option>
                        <option value="editor">Editor</option>
                        <option value="viewer">Viewer</option>
                      </Select>
                    </td>
                    <td className="px-4 py-3 text-sm text-muted">{formatDateTime(user.created_at)}</td>
                    <td className="px-4 py-3 text-sm text-muted">
                      {user.is_active ? "Active" : "Inactive"}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => onDelete(user)}
                        disabled={isCurrentUser}
                        aria-label={`Remove ${user.email}`}
                        className="h-8 w-8 p-0"
                      >
                        <TrashIcon className="h-4 w-4" />
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </CardContent>
    </Card>
  );
}
