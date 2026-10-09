/**
 * Friendly error messages for the auth screens.
 *
 * The backend's `detail` can be a technical string; the login form maps every
 * failure to copy a person can act on. Registration surfaces `detail` for the
 * cases where the backend already writes user-facing text (409 slug taken,
 * 422 field problems) and falls back to a friendly sentence otherwise.
 * Tokens are never part of any message.
 */

import { ApiError, NetworkError } from "@/lib/errors";

const NETWORK_MESSAGE = "Could not reach the server. Check your connection and try again.";
const RATE_LIMIT_MESSAGE = "Too many requests. Please try again shortly.";
const LOGIN_FALLBACK = "We couldn't sign you in right now. Please try again.";
const REGISTER_FALLBACK = "We couldn't create your workspace. Check the details and try again.";

function isNetworkError(error: unknown): boolean {
  return error instanceof NetworkError || (error instanceof Error && error.name === "NetworkError");
}

/** Message for a failed sign-in attempt. */
export function loginErrorMessage(error: unknown): string {
  if (isNetworkError(error)) return NETWORK_MESSAGE;
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return "We couldn't sign you in. Check the workspace slug, email, and password.";
    }
    if (error.status === 429) return RATE_LIMIT_MESSAGE;
    if (error.detail) return error.detail;
  }
  return LOGIN_FALLBACK;
}

/** Message for a failed workspace registration. */
export function registerErrorMessage(error: unknown): string {
  if (isNetworkError(error)) return NETWORK_MESSAGE;
  if (error instanceof ApiError) {
    if (error.status === 429) return RATE_LIMIT_MESSAGE;
    // 409 (slug taken) and 422 (field rules) carry backend-written detail we
    // can surface; anything else gets the friendly fallback first.
    if ((error.status === 409 || error.status === 422) && error.detail) return error.detail;
    if (error.status === 409) return "That workspace URL is already in use. Pick another one.";
    if (error.detail && error.status < 500) return error.detail;
  }
  return REGISTER_FALLBACK;
}
