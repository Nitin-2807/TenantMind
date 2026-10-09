"use client";

import { useCallback, useEffect, useState } from "react";

import { RequireRole } from "@/lib/auth/route-guard";
import { adminApi } from "@/lib/api";
import type { UsageSummary } from "@/lib/types";
import { ApiError } from "@/lib/errors";
import { formatLatency, formatPercent } from "@/lib/format";

import { PageHeader } from "@/components/layout/page-header";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorState } from "@/components/ui/error-state";
import { Spinner } from "@/components/ui/spinner";
import { UsageBreakdown } from "@/components/admin/usage-breakdown";

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; usage: UsageSummary };

function friendlyMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail || "The request failed.";
  if (error instanceof Error) return error.message;
  return "The request failed.";
}

function UsageContent() {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  const fetchUsage = useCallback(async () => {
    try {
      const usage = await adminApi.getUsage();
      setState({ status: "ready", usage });
    } catch (error) {
      setState({ status: "error", message: friendlyMessage(error) });
    }
  }, []);

  // Initial load — promise continuation so no state is set synchronously from
  // the effect body (react-hooks/set-state-in-effect). Event handlers reuse
  // fetchUsage below.
  useEffect(() => {
    let cancelled = false;
    adminApi
      .getUsage()
      .then((usage) => {
        if (cancelled) return;
        setState({ status: "ready", usage });
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
    fetchUsage();
  };

  const renderStats = (usage: UsageSummary) => {
    const stats: Array<{ label: string; value: string; hint?: string }> = [
      { label: "Total questions", value: String(usage.total_queries) },
      { label: "Answered", value: String(usage.total_answered) },
      { label: "Failed", value: String(usage.total_failed) },
      { label: "Last 24 hours", value: String(usage.queries_last_24h) },
      {
        label: "Average latency",
        value: formatLatency(usage.avg_latency_ms),
        hint: "per question, end to end",
      },
      {
        label: "Cache hit rate",
        value: formatPercent(usage.cache_hit_rate),
        hint: "identical questions served from cache",
      },
      { label: "Feedback: helpful", value: String(usage.feedback_up) },
      { label: "Feedback: not helpful", value: String(usage.feedback_down) },
    ];

    return (
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {stats.map((stat) => (
          <Card key={stat.label}>
            <CardContent className="px-5 py-4">
              <p className="text-sm text-muted">{stat.label}</p>
              <p className="mt-1 text-2xl font-semibold text-foreground">{stat.value}</p>
              {stat.hint ? <p className="mt-0.5 text-xs text-muted">{stat.hint}</p> : null}
            </CardContent>
          </Card>
        ))}
      </div>
    );
  };

  return (
    <div className="container mx-auto max-w-6xl space-y-6 px-4 py-6">
      <PageHeader title="Usage" description="Usage statistics" />

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
          {renderStats(state.usage)}
          <UsageBreakdown
            queriesByDay={state.usage.queries_by_day}
            queriesByUser={state.usage.queries_by_user}
          />
        </>
      )}
    </div>
  );
}

export default function AdminUsagePage() {
  return (
    <RequireRole role="admin">
      <UsageContent />
    </RequireRole>
  );
}
