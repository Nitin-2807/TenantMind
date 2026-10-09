import { describe, expect, it, vi, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";

import { useQueryStream } from "./use-query-stream";
import { RateLimitError } from "@/lib/errors";

function createSseStream(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("useQueryStream", () => {
  it("streams answer, citations, then done", async () => {
    const chunks = [
      'event: start\ndata: {"query_id":"q1"}\n\n',
      'event: answer\ndata: {"query_id":"q1","answer":"Hello ","grounded":true,"context_truncated":false}\n\n',
      'event: answer\ndata: {"query_id":"q1","answer":"world","grounded":true,"context_truncated":false}\n\n',
      'event: citations\ndata: {"query_id":"q1","citations":[{"document_id":"d1","filename":"doc.pdf","chunk_index":0,"page":1}]}\n\n',
      'event: done\ndata: {"query_id":"q1","cache_hit":false}\n\n',
    ];
    const mockFetch = vi.fn().mockResolvedValueOnce(createSseStream(chunks));
    vi.stubGlobal("fetch", mockFetch);

    const { result } = renderHook(() => useQueryStream());

    expect(result.current.state.inFlight).toBe(false);
    await act(async () => {
      await result.current.ask("What is X?");
    });

    expect(result.current.state.queryId).toBe("q1");
    expect(result.current.state.answer).toBe("Hello world");
    expect(result.current.state.grounded).toBe(true);
    expect(result.current.state.contextTruncated).toBe(false);
    expect(result.current.state.citations).toEqual([
      { document_id: "d1", filename: "doc.pdf", chunk_index: 0, page: 1 },
    ]);
    expect(result.current.state.inFlight).toBe(false);
    expect(result.current.state.completed).toBe(true);
    expect(result.current.state.cacheHit).toBe(false);
    expect(result.current.state.error).toBeNull();
  });

  it("aborts and treats as cancellation (no error)", async () => {
    const encoder = new TextEncoder();
    // Simulate slow stream that never closes
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode('event: start\ndata: {"query_id":"q1"}\n\n'),
        );
        // Never close or enqueue more
      },
    });
    const response = new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    const mockFetch = vi.fn().mockResolvedValueOnce(response);
    vi.stubGlobal("fetch", mockFetch);

    const { result } = renderHook(() => useQueryStream());

    act(() => {
      result.current.ask("What is X?");
    });

    // Wait for start
    await vi.waitFor(() => {
      expect(result.current.state.queryId).toBe("q1");
      expect(result.current.state.inFlight).toBe(true);
    });

    act(() => {
      result.current.abort();
    });

    await vi.waitFor(() => {
      expect(result.current.state.inFlight).toBe(false);
      expect(result.current.state.error).toBeNull();
    });
  });

  it("handles grounded:false with context_truncated", async () => {
    const chunks = [
      'event: start\ndata: {"query_id":"q2"}\n\n',
      'event: answer\ndata: {"query_id":"q2","answer":"We found nothing.","grounded":false,"context_truncated":true}\n\n',
      'event: done\ndata: {"query_id":"q2","cache_hit":false}\n\n',
    ];
    const mockFetch = vi.fn().mockResolvedValueOnce(createSseStream(chunks));
    vi.stubGlobal("fetch", mockFetch);

    const { result } = renderHook(() => useQueryStream());

    await act(async () => {
      await result.current.ask("Question");
    });

    expect(result.current.state.grounded).toBe(false);
    expect(result.current.state.contextTruncated).toBe(true);
    expect(result.current.state.inFlight).toBe(false);
    expect(result.current.state.completed).toBe(true);
  });

  it("handles error event (rate_limited) as friendly message", async () => {
    const chunks = [
      'event: start\ndata: {"query_id":"q3"}\n\n',
      'event: error\ndata: {"query_id":"q3","code":"rate_limited","detail":"Too many"}\n\n',
    ];
    const mockFetch = vi.fn().mockResolvedValueOnce(createSseStream(chunks));
    vi.stubGlobal("fetch", mockFetch);

    const { result } = renderHook(() => useQueryStream());

    await act(async () => {
      await result.current.ask("Question");
    });

    expect(result.current.state.error).toBe("Too many requests. Please try again shortly.");
    expect(result.current.state.inFlight).toBe(false);
    expect(result.current.state.completed).toBe(false);
  });

  it("handles error from fetch rejection with RateLimitError", async () => {
    const error = new RateLimitError("Too many requests. Please try again shortly.", 10);
    // Force the error to have the exact shape as in the test
    const mockFetch = vi.fn().mockRejectedValueOnce(error);
    vi.stubGlobal("fetch", mockFetch);

    const { result } = renderHook(() => useQueryStream());

    await act(async () => {
      await result.current.ask("Question");
    });

    // The hook should set error state appropriately
    expect(result.current.state.error).toBe("Too many requests. Please try again shortly.");
    expect(result.current.state.inFlight).toBe(false);
  });

  it("handles network/fetch errors with AbortError", async () => {
    const abortError = new DOMException("Aborted", "AbortError");
    const mockFetch = vi.fn().mockRejectedValueOnce(abortError);
    vi.stubGlobal("fetch", mockFetch);

    const { result } = renderHook(() => useQueryStream());

    await act(async () => {
      await result.current.ask("Question");
    });

    expect(result.current.state.error).toBeNull();
    expect(result.current.state.inFlight).toBe(false);
    expect(result.current.state.completed).toBe(false);
  });

  it("XSS safety note: answer is stored as-is (rendering component must escape)", async () => {
    const xss = '<img src=x onerror="alert(1)">';
    const chunks = [
      'event: start\ndata: {"query_id":"x1"}\n\n',
      `event: answer\ndata: {"query_id":"x1","answer":${JSON.stringify(xss)},"grounded":true,"context_truncated":false}\n\n`,
      'event: done\ndata: {"query_id":"x1","cache_hit":false}\n\n',
    ];
    const mockFetch = vi.fn().mockResolvedValueOnce(createSseStream(chunks));
    vi.stubGlobal("fetch", mockFetch);

    const { result } = renderHook(() => useQueryStream());

    await act(async () => {
      await result.current.ask("Question");
    });

    expect(result.current.state.answer).toBe(xss);
  });
});
