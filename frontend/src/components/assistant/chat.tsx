"use client";

import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useQueryStream } from "@/hooks/use-query-stream";
import { Composer } from "./composer";
import { MessageBubble } from "./message-bubble";
import type { Citation, FeedbackValue } from "@/lib/types";

interface Message {
  id: string;
  role: "user" | "assistant";
  content: string;
  queryId?: string | null;
  grounded?: boolean | null;
  contextTruncated?: boolean | null;
  citations?: Citation[];
  completed?: boolean;
  inFlight?: boolean;
  feedback?: FeedbackValue | null;
  isError?: boolean;
  errorMessage?: string | null;
}

export function Chat() {
  const [messages, setMessages] = useState<Message[]>([]);
  const { state, ask, abort } = useQueryStream();
  const composerRef = useRef<HTMLTextAreaElement>(null);
  // The assistant message currently mirroring the live stream state.
  const [activeAssistantId, setActiveAssistantId] = useState<string | null>(null);

  const handleAsk = async (question: string) => {
    const userMessageId = `user-${Date.now()}`;
    const assistantMessageId = `assistant-${Date.now()}`;
    setActiveAssistantId(assistantMessageId);

    // Add user message
    setMessages((prev) => [
      ...prev,
      {
        id: userMessageId,
        role: "user",
        content: question,
      },
    ]);

    // Add assistant placeholder (rendered live from stream state while active)
    setMessages((prev) => [
      ...prev,
      {
        id: assistantMessageId,
        role: "assistant",
        content: "",
        inFlight: true,
        citations: [],
      },
    ]);

    // Wait for the stream to finish; `ask` resolves with the final outcome.
    // The `state` closed over here would be stale — never sync from it.
    const outcome = await ask(question);

    // Persist the final stream outcome into the assistant message.
    setMessages((prev) => {
      const updated = [...prev];
      const idx = updated.findIndex((m) => m.id === assistantMessageId);
      if (idx >= 0) {
        updated[idx] = {
          ...updated[idx],
          inFlight: false,
          completed: outcome.completed,
          queryId: outcome.queryId,
          content: outcome.answer,
          grounded: outcome.grounded,
          contextTruncated: outcome.contextTruncated,
          citations: outcome.citations,
          isError: !!outcome.error,
          errorMessage: outcome.error,
        };
      }
      return updated;
    });
    setActiveAssistantId(null);

    // Focus composer after response
    setTimeout(() => {
      composerRef.current?.focus();
    }, 0);
  };

  const handleAbort = () => {
    abort();
    // Mark last assistant message as not in flight
    setMessages((prev) => {
      const updated = [...prev];
      for (let i = updated.length - 1; i >= 0; i--) {
        if (updated[i].role === "assistant" && updated[i].inFlight) {
          updated[i] = {
            ...updated[i],
            inFlight: false,
            completed: false,
          };
          break;
        }
      }
      return updated;
    });
    setTimeout(() => {
      composerRef.current?.focus();
    }, 0);
  };

  const isInFlight = state.inFlight;

  return (
    <div className="flex h-[calc(100vh-12rem)] flex-col rounded-lg border border-border bg-background">
      <div className="flex-1 overflow-y-auto p-4">
        <div className="flex flex-col gap-4">
          {messages.length === 0 ? (
            <div className="flex h-full items-center justify-center text-center">
              <div>
                <h3 className="text-base font-semibold text-foreground">
                  Ask a question
                </h3>
                <p className="mt-1 text-sm text-muted">
                  Get answers from your documents
                </p>
              </div>
            </div>
          ) : (
            messages.map((message) => {
              // The active assistant message mirrors the live stream state so
              // tokens and citations appear as they arrive (a post-hoc sync
              // would read a stale `state` closure instead).
              const live =
                message.id === activeAssistantId
                  ? {
                      inFlight: state.inFlight,
                      completed: state.completed,
                      queryId: state.queryId,
                      content: state.answer,
                      grounded: state.grounded,
                      contextTruncated: state.contextTruncated,
                      citations: state.citations,
                      isError: !!state.error,
                      errorMessage: state.error,
                    }
                  : {};
              return (
                <MessageBubble
                  key={message.id}
                  role={message.role}
                  content={message.content}
                  grounded={message.grounded}
                  contextTruncated={message.contextTruncated}
                  citations={message.citations}
                  queryId={message.queryId}
                  completed={message.completed}
                  inFlight={message.inFlight}
                  feedback={message.feedback}
                  isError={message.isError}
                  errorMessage={message.errorMessage}
                  {...live}
                />
              );
            })
          )}
        </div>
      </div>
      {isInFlight && (
        <div className="border-t border-border px-4 py-2">
          <Button variant="ghost" size="sm" onClick={handleAbort}>
            Stop
          </Button>
        </div>
      )}
      <Composer
        onSubmit={handleAsk}
        disabled={isInFlight}
        loading={isInFlight}
      />
    </div>
  );
}
