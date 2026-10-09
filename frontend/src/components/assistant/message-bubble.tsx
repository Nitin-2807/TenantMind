"use client";

import type { Citation, FeedbackValue } from "@/lib/types";
import { CitationsPanel } from "./citations-panel";
import { FeedbackControls } from "./feedback-controls";
import { Spinner } from "@/components/ui/spinner";

interface MessageBubbleProps {
  role: "user" | "assistant";
  content: string;
  grounded?: boolean | null;
  contextTruncated?: boolean | null;
  citations?: Citation[];
  queryId?: string | null;
  completed?: boolean;
  inFlight?: boolean;
  feedback?: FeedbackValue | null;
  isError?: boolean;
  errorMessage?: string | null;
}

export function MessageBubble({
  role,
  content,
  grounded,
  contextTruncated,
  citations = [],
  queryId,
  completed = false,
  inFlight = false,
  feedback = null,
  isError = false,
  errorMessage = null,
}: MessageBubbleProps) {
  if (role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-lg bg-primary px-4 py-2 text-primary-foreground">
          <p className="whitespace-pre-wrap text-sm">{content}</p>
        </div>
      </div>
    );
  }

  // Assistant message
  return (
    <div className="flex justify-start" aria-live="polite">
      <div className="max-w-[80%] rounded-lg border border-border bg-surface px-4 py-2">
        {isError ? (
          <p className="text-sm text-danger">{errorMessage || "Something went wrong"}</p>
        ) : inFlight && content === "" ? (
          <div className="flex items-center gap-2">
            <Spinner size="sm" />
            <span className="text-sm text-muted">Searching your documents…</span>
          </div>
        ) : (
          <>
            {grounded === false && (
              <div className="mb-2 rounded-md bg-warning/10 px-2 py-1 text-xs text-warning">
                We couldn&apos;t find documents matching this question. Try rephrasing, or
                upload more documents.
              </div>
            )}
            {contextTruncated === true && (
              <div className="mb-2 text-xs text-warning">
                Context was truncated — the answer may be incomplete.
              </div>
            )}
            <p className="whitespace-pre-wrap text-sm text-foreground">{content}</p>
            <CitationsPanel citations={citations} />
            {completed && queryId && (
              <FeedbackControls queryId={queryId} initialFeedback={feedback} />
            )}
          </>
        )}
      </div>
    </div>
  );
}
