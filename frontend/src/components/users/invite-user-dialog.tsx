"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { useToast } from "@/components/ui/toaster";
import { usersApi } from "@/lib/api";
import type { UserRole } from "@/lib/types";
import { validateEmail, validateNewPassword } from "@/lib/validation";
import { ApiError } from "@/lib/errors";

interface InviteUserDialogProps {
  open: boolean;
  onClose: () => void;
  onInviteSuccess: () => void;
}

export function InviteUserDialog({ open, onClose, onInviteSuccess }: InviteUserDialogProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("viewer");
  const [submitting, setSubmitting] = useState(false);
  const [errors, setErrors] = useState<{ email?: string | null; password?: string | null; general?: string | null }>({});
  const { toast } = useToast();

  const resetForm = () => {
    setEmail("");
    setPassword("");
    setRole("viewer");
    setErrors({});
  };

  const handleClose = () => {
    if (submitting) return;
    resetForm();
    onClose();
  };

  const validateForm = () => {
    const emailError = validateEmail(email);
    const passwordError = validateNewPassword(password);
    const newErrors: typeof errors = {
      email: emailError,
      password: passwordError,
    };
    setErrors(newErrors);
    return !emailError && !passwordError;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!validateForm()) return;

    setSubmitting(true);
    setErrors({});

    try {
      await usersApi.inviteUser({ email, password, role });
      toast({
        variant: "success",
        title: "User invited",
        description: `An invitation has been sent to ${email}.`,
      });
      resetForm();
      onInviteSuccess();
      onClose();
    } catch (error) {
      let generalError: string | null = null;
      if (error instanceof ApiError) {
        // Map 422 detail to a friendly inline message as per spec
        generalError = error.detail || "Failed to invite user.";
      } else if (error instanceof Error) {
        generalError = error.message;
      } else {
        generalError = "Failed to invite user.";
      }
      setErrors({ general: generalError });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={handleClose}
      title="Invite user"
      description="Invite a new user to this workspace."
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {errors.general ? (
          <div role="alert" className="rounded-lg border border-danger/20 bg-danger/10 p-3 text-sm text-danger">
            {errors.general}
          </div>
        ) : null}
        <Field label="Email" htmlFor="invite-email" error={errors.email}>
          <Input
            id="invite-email"
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="user@example.com"
            disabled={submitting}
            autoComplete="email"
          />
        </Field>
        <Field label="Password" htmlFor="invite-password" error={errors.password} hint="Must be at least 12 characters">
          <Input
            id="invite-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Enter password"
            disabled={submitting}
            autoComplete="new-password"
          />
        </Field>
        <Field label="Role" htmlFor="invite-role">
          <Select
            id="invite-role"
            value={role}
            onChange={(e) => setRole(e.target.value as UserRole)}
            disabled={submitting}
          >
            <option value="viewer">Viewer</option>
            <option value="editor">Editor</option>
            <option value="admin">Admin</option>
          </Select>
        </Field>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={handleClose} disabled={submitting}>
            Cancel
          </Button>
          <Button type="submit" loading={submitting} >
            Invite
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
