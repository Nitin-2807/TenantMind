"use client";

import { useState } from "react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FeedbackControls } from "@/components/assistant/feedback-controls";
import { formatDateTime, formatLatency } from "@/lib/format";
import type { HistoryItem as HistoryItemType } from "@/lib/types";

interface HistoryItemProps {
  item: HistoryItemType;
}

export function HistoryItem({ item }: HistoryItemProps) {
  const [expanded, setExpanded] = useState(false);
  const [feedback, setFeedback] = useState(item.feedback);

  const isError = item.status === "error";

  return (
    <div className="rounded-lg border border-border bg-surface">
      <div className="p-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-medium text-foreground line-clamp-2">
              {item.question}
            </h3>
            <p className="mt-1 text-xs text-muted">
              {formatDateTime(item.created_at)}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {isError ? (
              <Badge tone="danger">error</Badge>
            ) : (
              <Badge tone={item.grounded ? "success" : "warning"}>
                {item.grounded ? "success" : "ungrounded"}
              </Badge>
            )}
            {item.cache_hit && <Badge tone="brand">cached</Badge>}
            {item.latency_ms !== null && (
              <span className="text-xs text-muted">
                {formatLatency(item.latency_ms)}
              </span>
            )}
          </div>
        </div>

        {feedback && (
          <div className="mt-2 text-xs text-muted">
            Your feedback: {feedback === "up" ? "👍 Helpful" : "👎 Not helpful"}
          </div>
        )}

        <div className="mt-3">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "Hide details" : "View details"}
          </Button>
        </div>

        {expanded && (
          <div className="mt-4 space-y-4">
            <div>
              <h4 className="text-xs font-medium text-muted">Answer</h4>
              <p className="mt-1 whitespace-pre-wrap text-sm text-foreground">
                {item.answer}
              </p>
            </div>

            {!isError && item.citations && item.citations.length > 0 && (
              <div>
                <h4 className="text-xs font-medium text-muted">Sources</h4>
                <ul className="mt-1 space-y-1">
                  {item.citations.map((citation, idx) => {
                    const pageInfo =
                      citation.page !== null ? ` (p. ${citation.page})` : "";
                    return (
                      <li key={`${citation.document_id}-${idx}`}>
                        <Link
                          href={`/documents/${citation.document_id}`}
                          className="text-sm text-primary hover:underline"
                        >
                          {citation.filename}
                          {pageInfo}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {!isError && item.id && (
              <div>
                <FeedbackControls
                  queryId={item.id}
                  initialFeedback={feedback}
                  onFeedbackChange={(newFeedback) => setFeedback(newFeedback)}
                />
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
