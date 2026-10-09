import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Chat } from "./chat";
import { ToasterProvider } from "@/components/ui/toaster";

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

interface ControlledStream {
  response: Response;
  /** Push one SSE chunk. Callers pace delivery, like a real stream. */
  send: (chunk: string) => void;
  close: () => void;
}

/**
 * A stream the test paces frame-by-frame, so intermediate UI states (e.g.
 * "Searching…") are deterministically observable instead of racing the
 * event loop.
 */
function createControlledSseStream(): ControlledStream {
  const encoder = new TextEncoder();
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }),
    send(chunk) {
      controller?.enqueue(encoder.encode(chunk));
    },
    close() {
      controller?.close();
    },
  };
}

function renderChat() {
  return render(
    <ToasterProvider>
      <Chat />
    </ToasterProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Chat", () => {
  it("renders full lifecycle: start -> answer -> citations -> done", async () => {
    const stream = createControlledSseStream();
    const mockFetch = vi.fn().mockResolvedValueOnce(stream.response);
    vi.stubGlobal("fetch", mockFetch);

    const user = userEvent.setup();
    renderChat();

    const textarea = screen.getByLabelText(/Ask a question/i);
    await user.type(textarea, "What is X?");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    // No frame has been delivered yet, so the placeholder is showing.
    expect(screen.getByText(/Searching your documents/i)).toBeInTheDocument();

    stream.send('event: start\ndata: {"query_id":"q1"}\n\n');
    stream.send(
      'event: answer\ndata: {"query_id":"q1","answer":"Hello","grounded":true,"context_truncated":false}\n\n',
    );

    // The answer appears as it streams in.
    await waitFor(() => {
      expect(screen.getByText("Hello")).toBeInTheDocument();
    });

    stream.send(
      'event: citations\ndata: {"query_id":"q1","citations":[{"document_id":"d1","filename":"doc.pdf","chunk_index":0,"page":1}]}\n\n',
    );
    stream.send('event: done\ndata: {"query_id":"q1","cache_hit":false}\n\n');
    stream.close();

    // Citations arrive on their own frame.
    await waitFor(() => {
      expect(screen.getByText("Sources")).toBeInTheDocument();
    });
    expect(screen.getByText(/doc.pdf/)).toBeInTheDocument();

    // Feedback controls render only after `done`.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Helpful" })).toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Not helpful" })).toBeInTheDocument();

    // InFlight should be false - send button enabled
    expect(screen.getByRole("button", { name: /Send/i })).not.toBeDisabled();
  });

  it("grounded:false renders honest no-match notice", async () => {
    const chunks = [
      'event: start\ndata: {"query_id":"q2"}\n\n',
      'event: answer\ndata: {"query_id":"q2","answer":"We found nothing.","grounded":false,"context_truncated":false}\n\n',
      'event: done\ndata: {"query_id":"q2","cache_hit":false}\n\n',
    ];
    const mockFetch = vi.fn().mockResolvedValueOnce(createSseStream(chunks));
    vi.stubGlobal("fetch", mockFetch);

    const user = userEvent.setup();
    renderChat();

    const textarea = screen.getByLabelText(/Ask a question/i);
    await user.type(textarea, "Question");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/couldn't find documents matching this question/i),
      ).toBeInTheDocument();
    });
  });

  it("shows context_truncated notice when true", async () => {
    const chunks = [
      'event: start\ndata: {"query_id":"q3"}\n\n',
      'event: answer\ndata: {"query_id":"q3","answer":"Answer","grounded":true,"context_truncated":true}\n\n',
      'event: done\ndata: {"query_id":"q3","cache_hit":false}\n\n',
    ];
    const mockFetch = vi.fn().mockResolvedValueOnce(createSseStream(chunks));
    vi.stubGlobal("fetch", mockFetch);

    const user = userEvent.setup();
    renderChat();

    const textarea = screen.getByLabelText(/Ask a question/i);
    await user.type(textarea, "Question");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/Context was truncated/i),
      ).toBeInTheDocument();
    });
  });

  it("handles error event with rate_limited message", async () => {
    const chunks = [
      'event: start\ndata: {"query_id":"q4"}\n\n',
      'event: error\ndata: {"query_id":"q4","code":"rate_limited","detail":"Too many"}\n\n',
    ];
    const mockFetch = vi.fn().mockResolvedValueOnce(createSseStream(chunks));
    vi.stubGlobal("fetch", mockFetch);

    const user = userEvent.setup();
    renderChat();

    const textarea = screen.getByLabelText(/Ask a question/i);
    await user.type(textarea, "Question");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/Too many requests. Please try again shortly./i),
      ).toBeInTheDocument();
    });

    // Chat should still be usable
    expect(screen.getByRole("button", { name: /Send/i })).not.toBeDisabled();
  });

  it("XSS safety: renders as escaped text", async () => {
    const xss = '<img src=x onerror="alert(1)">';
    const chunks = [
      'event: start\ndata: {"query_id":"x1"}\n\n',
      `event: answer\ndata: {"query_id":"x1","answer":${JSON.stringify(xss)},"grounded":true,"context_truncated":false}\n\n`,
      'event: done\ndata: {"query_id":"x1","cache_hit":false}\n\n',
    ];
    const mockFetch = vi.fn().mockResolvedValueOnce(createSseStream(chunks));
    vi.stubGlobal("fetch", mockFetch);

    const user = userEvent.setup();
    renderChat();

    const textarea = screen.getByLabelText(/Ask a question/i);
    await user.type(textarea, "Question");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await waitFor(() => {
      expect(screen.getByText(xss)).toBeInTheDocument();
    });

    // Should not create img element
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("stop button aborts and shows no error", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode('event: start\ndata: {"query_id":"s1"}\n\n'),
        );
        // Keep stream open
      },
    });
    const response = new Response(body, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    const mockFetch = vi.fn().mockResolvedValueOnce(response);
    vi.stubGlobal("fetch", mockFetch);

    const user = userEvent.setup();
    renderChat();

    const textarea = screen.getByLabelText(/Ask a question/i);
    await user.type(textarea, "Question");
    await user.click(screen.getByRole("button", { name: /Send/i }));

    await waitFor(() => {
      expect(screen.getByText(/Searching your documents/i)).toBeInTheDocument();
    });

    const stopBtn = screen.getByRole("button", { name: /Stop/i });
    await user.click(stopBtn);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /Stop/i })).not.toBeInTheDocument();
    });

    // No error message
    expect(
      screen.queryByText(/Too many requests. Please try again shortly./i),
    ).not.toBeInTheDocument();
  });
});
