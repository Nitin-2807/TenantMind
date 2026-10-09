/**
 * Small shared helpers for the documents screens. Plain functions only — no
 * component code — so pages and dialogs can share the same role rules and the
 * same friendly-error mapping.
 */

import { ApiError } from "@/lib/errors";
import type { UserRole } from "@/lib/types";

/**
 * Friendly message for an API failure: the backend's own `detail` when it has
 * one, otherwise a generic sentence. Never a stack trace or raw payload.
 */
export function friendlyMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.status === 429) return "Too many requests. Please try again shortly.";
    if (error.detail) return error.detail;
    return fallback;
  }
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

/**
 * Role model (backend enforces; this only decides which controls to show):
 * upload = admin + editor, delete = admin, viewing = everyone.
 */
export function canUpload(role: UserRole | null | undefined): boolean {
  return role === "admin" || role === "editor";
}

export function canDelete(role: UserRole | null | undefined): boolean {
  return role === "admin";
}
