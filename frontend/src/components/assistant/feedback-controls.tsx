"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toaster";
import { queryApi } from "@/lib/api";
import type { FeedbackValue } from "@/lib/types";

interface FeedbackControlsProps {
  queryId: string | null;
  initialFeedback?: FeedbackValue | null;
  onFeedbackChange?: (feedback: FeedbackValue) => void;
}

export function FeedbackControls({
  queryId,
  initialFeedback = null,
  onFeedbackChange,
}: FeedbackControlsProps) {
  const [feedback, setFeedback] = useState<FeedbackValue | null>(
    initialFeedback,
  );
  const [sending, setSending] = useState(false);
  const { toast } = useToast();

  const handleFeedback = async (value: FeedbackValue) => {
    if (!queryId || sending) return;

    setSending(true);
    try {
      await queryApi.submitFeedback(queryId, value);
      setFeedback(value);
      onFeedbackChange?.(value);
    } catch (error) {
      toast({
        variant: "error",
        title: "Failed to submit feedback",
        description:
          error instanceof Error ? error.message : "Please try again.",
      });
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="mt-2 flex items-center gap-2">
      <span className="sr-only">Feedback</span>
      <Button
        variant={feedback === "up" ? "primary" : "ghost"}
        size="sm"
        onClick={() => handleFeedback("up")}
        disabled={sending || !queryId}
        aria-label="Helpful"
      >
        👍 Helpful
      </Button>
      <Button
        variant={feedback === "down" ? "danger" : "ghost"}
        size="sm"
        onClick={() => handleFeedback("down")}
        disabled={sending || !queryId}
        aria-label="Not helpful"
      >
        👎 Not helpful
      </Button>
      {feedback && !sending && (
        <span className="text-xs text-muted">Thanks for your feedback</span>
      )}
    </div>
  );
}
