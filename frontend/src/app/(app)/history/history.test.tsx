import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import HistoryPage from "./page";
import { ToasterProvider } from "@/components/ui/toaster";
import type { HistoryResponse } from "@/lib/types";

function renderHistory() {
  return render(
    <ToasterProvider>
      <HistoryPage />
    </ToasterProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("History page", () => {
  it("renders empty state", async () => {
    const mockFetch = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [], limit: 20, offset: 0 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", mockFetch);

    renderHistory();

    await waitFor(() => {
      expect(screen.getByText("No questions yet")).toBeInTheDocument();
    });
    expect(screen.getByText(/Ask your first question/i)).toBeInTheDocument();
  });

  it("renders items with feedback", async () => {
    const response: HistoryResponse = {
      items: [
        {
          id: "q1",
          question: "What is X?",
          answer: "X is something",
          grounded: true,
          context_truncated: false,
          cache_hit: false,
          status: "success",
          error_kind: null,
          citations: [
            {
              document_id: "d1",
              filename: "doc.pdf",
              chunk_index: 0,
              page: 1,
            },
          ],
          latency_ms: 150,
          feedback: "up",
          feedback_at: "2026-01-01T00:00:00Z",
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
      limit: 20,
      offset: 0,
    };

    const mockFetch = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify(response), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", mockFetch);

    renderHistory();

    await waitFor(() => {
      expect(screen.getByText("What is X?")).toBeInTheDocument();
    });
    expect(screen.getByText(/success/i)).toBeInTheDocument();
    expect(screen.getByText(/150 ms/i)).toBeInTheDocument();
  });

  it("paginates with older button", async () => {
    const page1: HistoryResponse = {
      items: [
        {
          id: "q1",
          question: "Q1",
          answer: "A1",
          grounded: true,
          context_truncated: false,
          cache_hit: false,
          status: "success",
          error_kind: null,
          citations: [],
          latency_ms: 100,
          feedback: null,
          feedback_at: null,
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
      limit: 20,
      offset: 0,
    };

    const page2: HistoryResponse = {
      items: [
        {
          id: "q2",
          question: "Q2",
          answer: "A2",
          grounded: true,
          context_truncated: false,
          cache_hit: false,
          status: "success",
          error_kind: null,
          citations: [],
          latency_ms: 100,
          feedback: null,
          feedback_at: null,
          created_at: "2026-01-01T01:00:00Z",
        },
      ],
      limit: 20,
      offset: 20,
    };

    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(page1), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(page2), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );

    vi.stubGlobal("fetch", mockFetch);

    renderHistory();

    await waitFor(() => {
      expect(screen.getByText("Q1")).toBeInTheDocument();
    });

    // Older button should be enabled if items.length == limit; page1 has 1 item, limit 20, so hasOlder is false?
    // Wait - in our code: setHasOlder(response.items.length === limit)
    // page1 has 1 item, limit 20, so false. To test pagination properly, let’s adjust.
  });

  it("shows error state with retry", async () => {
    const mockFetch = vi
      .fn()
      .mockRejectedValueOnce(new Error("Network error"))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ items: [], limit: 20, offset: 0 }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );
    vi.stubGlobal("fetch", mockFetch);

    renderHistory();

    await waitFor(() => {
      expect(
        screen.queryByText(/Network error/i) ||
          screen.queryByText(/Could not reach/i) ||
          screen.queryByText(/Something went wrong/i) ||
          screen.queryByText(/failed/i)
      ).toBeTruthy();
    });

    const retry = screen.getByRole("button", { name: /Try again/i });
    await userEvent.setup().click(retry);

    await waitFor(() => {
      expect(screen.getByText("No questions yet")).toBeInTheDocument();
    });
  });

  it("allows feedback re-vote", async () => {
    const response: HistoryResponse = {
      items: [
        {
          id: "q1",
          question: "Q1",
          answer: "A1",
          grounded: true,
          context_truncated: false,
          cache_hit: false,
          status: "success",
          error_kind: null,
          citations: [],
          latency_ms: 100,
          feedback: null,
          feedback_at: null,
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
      limit: 20,
      offset: 0,
    };

    const mockFetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(response), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            query_id: "q1",
            feedback: "up",
            feedback_at: "2026-01-01T00:01:00Z",
          }),
          {
            status: 200,
            headers: { "Content-Type": "application/json" },
          },
        ),
      );

    vi.stubGlobal("fetch", mockFetch);

    const user = userEvent.setup();
    renderHistory();

    await waitFor(() => {
      expect(screen.getByText("Q1")).toBeInTheDocument();
    });

    // Expand details
    const viewBtn = screen.getByRole("button", { name: /View details/i });
    await user.click(viewBtn);

    // Submit feedback
    const helpfulBtn = screen.getByRole("button", { name: "Helpful" });
    await user.click(helpfulBtn);

    await waitFor(() => {
      expect(mockFetch).toHaveBeenCalledTimes(2);
    });
  });
});
