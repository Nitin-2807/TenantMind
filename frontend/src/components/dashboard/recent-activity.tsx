import Link from "next/link";

import { formatDateTime } from "@/lib/format";
import type { HistoryItem } from "@/lib/types";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ChatIcon } from "@/components/icons";

export function RecentActivity({ items }: { items: HistoryItem[] }) {
  if (items.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Recent questions</CardTitle>
          <CardDescription>Questions asked against your knowledge base.</CardDescription>
        </CardHeader>
        <CardContent>
          <EmptyState
            icon={<ChatIcon className="h-8 w-8" />}
            title="No questions yet"
            description="Ask the AI assistant a question about your documents — answered questions appear here."
            action={
              <Link href="/assistant">
                <span className="inline-flex h-9 items-center rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary-hover">
                  Ask your first question
                </span>
              </Link>
            }
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>Recent questions</CardTitle>
          <CardDescription>Questions asked against your knowledge base.</CardDescription>
        </div>
        <Link
          href="/history"
          className="text-sm font-medium text-primary hover:text-primary-hover"
        >
          View all
        </Link>
      </CardHeader>
      <CardContent>
        <ul className="divide-y divide-border">
          {items.map((item) => (
            <li key={item.id} className="flex items-center justify-between gap-4 py-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium text-foreground">{item.question}</p>
                <p className="mt-0.5 text-xs text-muted">{formatDateTime(item.created_at)}</p>
              </div>
              <GroundedBadge grounded={item.grounded} failed={item.status === "error"} />
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}

function GroundedBadge({ grounded, failed }: { grounded: boolean; failed: boolean }) {
  if (failed) return <Badge tone="danger">Failed</Badge>;
  return grounded ? (
    <Badge tone="success">Answered from documents</Badge>
  ) : (
    <Badge tone="neutral">No documents matched</Badge>
  );
}