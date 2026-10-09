"use client";

import { useCallback, useRef, useState } from "react";

import { RateLimitError } from "@/lib/errors";
import { streamQuery, type StreamHandlers } from "@/lib/api/query";
import type { Citation, SSEStreamEvent } from "@/lib/types";

export interface QueryStreamState {
  queryId: string | null;
  answer: string;
  grounded: boolean | null;
  contextTruncated: boolean | null;
  citations: Citation[];
  inFlight: boolean;
  completed: boolean;
  error: string | null;
  cacheHit: boolean | null;
}

/**
 * The final result of a single `ask` call, captured from the actual stream
 * events (the component's `state` closure is stale at completion time, so
 * consumers must read the outcome instead of the closed-over state).
 */
export interface QueryStreamOutcome {
  cancelled: boolean;
  queryId: string | null;
  answer: string;
  grounded: boolean | null;
  contextTruncated: boolean | null;
  citations: Citation[];
  completed: boolean;
  cacheHit: boolean | null;
  error: string | null;
}

export interface UseQueryStreamReturn {
  state: QueryStreamState;
  ask: (question: string) => Promise<QueryStreamOutcome>;
  abort: () => void;
}

const initialState: QueryStreamState = {
  queryId: null,
  answer: "",
  grounded: null,
  contextTruncated: null,
  citations: [],
  inFlight: false,
  completed: false,
  error: null,
  cacheHit: null,
};

export function useQueryStream(): UseQueryStreamReturn {
  const [state, setState] = useState<QueryStreamState>(initialState);
  const abortControllerRef = useRef<AbortController | null>(null);

  const abort = useCallback(() => {
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;
  }, []);

  const ask = useCallback(
    async (question: string) => {
      // Abort any in-flight request
      abortControllerRef.current?.abort();

      const controller = new AbortController();
      abortControllerRef.current = controller;

      setState({
        queryId: null,
        answer: "",
        grounded: null,
        contextTruncated: null,
        citations: [],
        inFlight: true,
        completed: false,
        error: null,
        cacheHit: null,
      });

      const outcome: QueryStreamOutcome = {
        cancelled: false,
        queryId: null,
        answer: "",
        grounded: null,
        contextTruncated: null,
        citations: [],
        completed: false,
        cacheHit: null,
        error: null,
      };

      const handleEvent = (event: SSEStreamEvent) => {
        switch (event.type) {
          case "start":
            outcome.queryId = event.query_id;
            setState((prev) => ({
              ...prev,
              queryId: event.query_id,
            }));
            break;
          case "answer":
            outcome.answer += event.answer;
            outcome.grounded = event.grounded;
            outcome.contextTruncated = event.context_truncated;
            setState((prev) => ({
              ...prev,
              answer: prev.answer + event.answer,
              grounded: event.grounded,
              contextTruncated: event.context_truncated,
            }));
            break;
          case "citations":
            outcome.citations = event.citations;
            setState((prev) => ({
              ...prev,
              citations: event.citations,
            }));
            break;
          case "done":
            outcome.completed = true;
            outcome.cacheHit = event.cache_hit;
            setState((prev) => ({
              ...prev,
              inFlight: false,
              completed: true,
              cacheHit: event.cache_hit,
            }));
            abortControllerRef.current = null;
            break;
          case "error":
            // Error event is terminal; streamQuery will reject after calling onEvent
            setState((prev) => ({
              ...prev,
              inFlight: false,
              completed: false,
            }));
            break;
          default:
            break;
        }
      };

      const handlers: StreamHandlers = {
        onEvent: handleEvent,
        signal: controller.signal,
      };

      try {
        await streamQuery(question, handlers);
        abortControllerRef.current = null;
        return outcome;
      } catch (error) {
        // AbortError is user cancellation - do not show error UI
        if (error instanceof DOMException && error.name === "AbortError") {
          outcome.cancelled = true;
          setState((prev) => ({
            ...prev,
            inFlight: false,
            completed: false,
            error: null,
          }));
          abortControllerRef.current = null;
          return outcome;
        }

        // Map typed errors to friendly messages
        let errorMessage: string;
        const err = error as Error & {
          status?: number;
          code?: string;
          retryAfter?: number;
        };
        if (
          error instanceof RateLimitError ||
          err?.name === "RateLimitError" ||
          err?.status === 429 ||
          err?.code === "rate_limited" ||
          err?.retryAfter !== undefined ||
          (error instanceof Error &&
            error.message &&
            (error.message.includes("Too many requests") ||
              error.message.includes("rate limited")))
        ) {
          errorMessage = "Too many requests. Please try again shortly.";
        } else if (error instanceof Error) {
          errorMessage = error.message || "Something went wrong. Please try again.";
        } else {
          errorMessage = "Something went wrong. Please try again.";
        }

        outcome.error = errorMessage;
        setState((prev) => ({
          ...prev,
          inFlight: false,
          completed: false,
          error: errorMessage,
        }));
        abortControllerRef.current = null;
        return outcome;
      }
    },
    [],
  );

  return { state, ask, abort };
}
