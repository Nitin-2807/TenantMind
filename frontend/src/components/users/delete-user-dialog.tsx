"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { usersApi } from "@/lib/api";
import { ApiError } from "@/lib/errors";
import { useToast } from "@/components/ui/toaster";

interface DeleteUserDialogProps {
  open: boolean;
  onClose: () => void;
  onDeleteSuccess: () => void;
  user: { id: string; email: string } | null;
}

export function DeleteUserDialog({ open, onClose, onDeleteSuccess, user }: DeleteUserDialogProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { toast } = useToast();

  const handleClose = () => {
    if (submitting) return;
    setError(null);
    onClose();
  };

  const handleConfirm = async () => {
    if (!user) return;
    setSubmitting(true);
    setError(null);

    try {
      await usersApi.deleteUser(user.id);
      toast({
        variant: "success",
        title: "User removed",
        description: `${user.email} has been removed.`,
      });
      onDeleteSuccess();
      onClose();
    } catch (err) {
      let message = "Failed to delete user.";
      if (err instanceof ApiError) {
        message = err.detail || message;
      } else if (err instanceof Error) {
        message = err.message;
      }
      setError(message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      title="Remove access"
      description={user ? `This will remove access for ${user.email}.` : undefined}
    >
      <div className="space-y-4">
        <p className="text-sm text-muted">
          {user
            ? `Are you sure you want to remove access for ${user.email}? This action cannot be undone.`
            : "Are you sure you want to remove this user?"}
        </p>
        {error ? (
          <div role="alert" className="rounded-lg border border-danger/20 bg-danger/10 p-3 text-sm text-danger">
            {error}
          </div>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={handleClose} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="danger" onClick={handleConfirm} loading={submitting}>
            Remove
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
