"use client";

/**
 * Document ingestion polling (PMP §10).
 *
 * While any document on screen is still `processing`, the list/detail view
 * re-fetches from the backend every few seconds so the status badges converge
 * on the server's truth without a manual reload. The moment nothing is
 * processing, the timer is cleared — the backend stays the only source of
 * status, this hook only decides *when* to ask again.
 *
 * Written for fake timers in tests: it uses a single chained `setTimeout`
 * (never an interval), so a slow fetch can never stack overlapping requests,
 * and cleanup cancels the pending timer on unmount or when polling stops.
 */

import { useCallback, useEffect, useRef } from "react";

import type { Document } from "@/lib/types";

/** How often to re-fetch while something is still processing. */
export const DOCUMENT_POLL_INTERVAL_MS = 3000;

export interface DocumentPolling {
  /**
   * Re-fetch immediately (e.g. right after an upload or delete). Resolves when
   * the refresh settles; a failed refresh leaves the current view untouched.
   */
  poll: () => Promise<void>;
}

/**
 * Poll `refresh` every {@link DOCUMENT_POLL_INTERVAL_MS} while `enabled`, and
 * stop (clearing the pending timer) as soon as it flips to `false` or the
 * component unmounts.
 */
export function useDocumentPolling(
  enabled: boolean,
  refresh: () => void | Promise<void>,
): DocumentPolling {
  const refreshRef = useRef(refresh);

  // Keep the latest refresh callback without re-arming the timer chain.
  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  const poll = useCallback(async (): Promise<void> => {
    await refreshRef.current();
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;

    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function run(): Promise<void> {
      try {
        await refreshRef.current();
      } catch {
        // A transient poll failure must not break the chain or blank the
        // screen; keep showing the last known state and try again later.
      }
      if (!cancelled) schedule();
    }

    function schedule(): void {
      timer = setTimeout(() => {
        void run();
      }, DOCUMENT_POLL_INTERVAL_MS);
    }

    schedule();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [enabled]);

  return { poll };
}

/**
 * Convenience wrapper for the documents list: polls while any document in the
 * current view is `processing`. Pass `null` while the list is loading/errored.
 */
export function useProcessingDocuments(
  documents: Document[] | null,
  refresh: () => void | Promise<void>,
): DocumentPolling {
  const anyProcessing =
    documents !== null && documents.some((doc) => doc.status === "processing");
  return useDocumentPolling(anyProcessing, refresh);
}
