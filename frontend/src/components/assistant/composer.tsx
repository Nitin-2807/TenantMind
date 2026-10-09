"use client";

import {
  forwardRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type Ref,
} from "react";

import { Button } from "@/components/ui/button";
import { validateQuestion } from "@/lib/validation";

interface ComposerProps {
  onSubmit: (question: string) => void;
  disabled?: boolean;
  loading?: boolean;
}

export const Composer = forwardRef(function Composer(
  { onSubmit, disabled, loading }: ComposerProps,
  ref: Ref<HTMLTextAreaElement>,
) {
  const [question, setQuestion] = useState("");
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const validationError = validateQuestion(question);
    if (validationError) {
      setError(validationError);
      return;
    }
    setError(null);
    onSubmit(question.trim());
    setQuestion("");
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e as unknown as FormEvent);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="border-t border-border p-4">
      <div className="flex flex-col gap-2">
        <label htmlFor="assistant-question" className="sr-only">
          Ask a question
        </label>
        <textarea
          ref={ref}
          id="assistant-question"
          aria-label="Ask a question about your documents"
          className="min-h-[80px] w-full resize-y rounded-lg border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-ring"
          placeholder="Ask a question about your documents..."
          value={question}
          onChange={(e) => {
            setQuestion(e.target.value);
            if (error) setError(null);
          }}
          onKeyDown={handleKeyDown}
          disabled={disabled || loading}
          maxLength={500}
        />
        {error && <p className="text-xs text-danger">{error}</p>}
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted">
            {question.length}/500 characters
          </span>
          <Button type="submit" disabled={disabled || loading} loading={loading}>
            Send
          </Button>
        </div>
      </div>
    </form>
  );
});
