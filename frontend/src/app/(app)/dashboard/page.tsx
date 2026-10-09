"use client";

/**
 * Dashboard.
 *
 * Real data only — every number comes from the backend:
 * - `GET /documents`          → document inventory
 * - `GET /query/history`      → recent activity
 * - `GET /admin/usage`        → usage summary (administrator only)
 *
 * No fabricated metrics. Empty states explain what to do next.
 */

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { adminApi, documentsApi, queryApi } from "@/lib/api";
import { useAuth } from "@/lib/auth/auth-provider";
import { ApiError } from "@/lib/errors";
import type { Document, HistoryItem, UsageSummary } from "@/lib/types";

import { CardsPreview } from "@/components/dashboard/cards";
import { RecentActivity } from "@/components/dashboard/recent-activity";
import { Usage } from "@/components/dashboard/usage";
import { ErrorState } from "@/components/ui/error-state";
import { Spinner } from "@/components/ui/spinner";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | {
      status: "ready";
      documents: Document[];
      recent: HistoryItem[];
      usage: UsageSummary | null; // null for non-admins (not fetched).
    };

function friendlyMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail || "The request failed.";
  if (error instanceof Error) return error.message;
  return "The request failed.";
}

export default function DashboardPage() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const [state, setState] = useState<LoadState>({ status: "loading" });

  // Pure loader: returns the next state; never calls setState itself so it can
  // run from an effect or an event handler without triggering the
  // set-state-in-effect lint rule.
  const fetchAll = useCallback(async (): Promise<LoadState> => {
    const [docs, history] = await Promise.all([
      documentsApi.listDocuments(),
      queryApi.getHistory(5, 0),
    ]);
    let usage: UsageSummary | null = null;
    if (isAdmin) {
      usage = await adminApi.getUsage().catch((error: unknown) => {
        // Usage is secondary on the dashboard; skip it rather than fail the page.
        if (error instanceof ApiError && error.status === 403) return null;
        throw error;
      });
    }
    return {
      status: "ready",
      documents: docs.items,
      recent: history.items,
      usage,
    };
  }, [isAdmin]);

  useEffect(() => {
    let cancelled = false;
    fetchAll()
      .then((next) => {
        if (!cancelled) setState(next);
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ status: "error", message: friendlyMessage(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [fetchAll]);

  const retry = () => {
    setState({ status: "loading" });
    fetchAll()
      .then(setState)
      .catch((error: unknown) => setState({ status: "error", message: friendlyMessage(error) }));
  };

  return (
    <div>
      <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-foreground">Dashboard</h1>
          <p className="mt-1 text-sm text-muted">
            An overview of your workspace, straight from the live API.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Link href="/assistant">
            <span className="inline-flex h-10 items-center justify-center gap-2 rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary-hover">
              Ask a question
            </span>
          </Link>
        </div>
      </div>

      {state.status === "loading" ? (
        <div
          className="flex min-h-[40vh] items-center justify-center"
          role="status"
          aria-label="Loading dashboard"
        >
          <Spinner size="lg" />
        </div>
      ) : null}

      {state.status === "error" ? (
        <ErrorState
          title="Could not load the dashboard"
          message={state.message}
          retry={retry}
        />
      ) : null}

      {state.status === "ready" ? (
        <div className="space-y-6">
          <CardsPreview documents={state.documents} />
          {state.usage ? <Usage usage={state.usage} /> : null}
          <RecentActivity items={state.recent} />
        </div>
      ) : null}
    </div>
  );
}