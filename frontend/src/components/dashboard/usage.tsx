import { formatLatency, formatPercent } from "@/lib/format";
import type { UsageSummary } from "@/lib/types";

import { Card, CardContent } from "@/components/ui/card";

/**
 * Administrator usage summary (GET /admin/usage). All fields come straight
 * from the backend; nothing is computed or presented on this screen that the
 * API does not already return.
 */
export function Usage({ usage }: { usage: UsageSummary }) {
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
}