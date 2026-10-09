"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { PageHeader } from "@/components/layout/page-header";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Spinner } from "@/components/ui/spinner";
import { HistoryItem } from "@/components/history/history-item";
import { queryApi } from "@/lib/api";
import type { HistoryItem as HistoryItemType } from "@/lib/types";
import { HistoryIcon } from "@/components/icons";

export default function HistoryPage() {
  const [items, setItems] = useState<HistoryItemType[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [limit] = useState(20);
  const [offset, setOffset] = useState(0);
  const [hasOlder, setHasOlder] = useState(false);
  const [hasNewer, setHasNewer] = useState(false);

  const fetchHistory = async (newOffset: number) => {
    setLoading(true);
    setError(null);
    try {
      const response = await queryApi.getHistory(limit, newOffset);
      setItems(response.items);
      setOffset(newOffset);
      // Has older if we got full limit items, or more items than returned? 
      // Based on typical pagination, if items.length === limit, maybe more exist
      setHasOlder(response.items.length === limit);
      setHasNewer(newOffset > 0);
    } catch (err) {
      const message =
        err instanceof Error
          ? err.message
          : "Failed to load history. Please try again.";
      setError(message);
    } finally {
      setLoading(false);
    }
  };

  // Initial load — run via promise continuation so no state is set
  // synchronously from the effect body (react-hooks/set-state-in-effect).
  useEffect(() => {
    let cancelled = false;
    queryApi
      .getHistory(limit, 0)
      .then((response) => {
        if (cancelled) return;
        setItems(response.items);
        setOffset(0);
        // Has older if we got a full page — more rows may exist.
        setHasOlder(response.items.length === limit);
        setHasNewer(false);
      })
      .catch((err) => {
        if (cancelled) return;
        const message =
          err instanceof Error
            ? err.message
            : "Failed to load history. Please try again.";
        setError(message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [limit]);

  const handleOlder = () => {
    fetchHistory(offset + limit);
  };

  const handleNewer = () => {
    fetchHistory(Math.max(0, offset - limit));
  };

  const handleRetry = () => {
    fetchHistory(offset);
  };

  return (
    <div className="container mx-auto max-w-4xl px-4 py-6">
      <PageHeader
        title="Query History"
        description="Your past questions and answers"
      />

      {loading && items.length === 0 ? (
        <div className="flex items-center justify-center py-12">
          <Spinner size="md" />
        </div>
      ) : error ? (
        <ErrorState message={error} retry={handleRetry} />
      ) : items.length === 0 ? (
        <EmptyState
          icon={<HistoryIcon className="h-10 w-10" />}
          title="No questions yet"
          description="Start asking questions to build your history"
          action={
            <Button>
              <Link href="/assistant">Ask your first question</Link>
            </Button>
          }
        />
      ) : (
        <div className="space-y-4">
          {items.map((item) => (
            <HistoryItem key={item.id} item={item} />
          ))}

          {(hasOlder || hasNewer) && (
            <div className="flex items-center justify-between pt-4">
              <Button
                variant="ghost"
                onClick={handleNewer}
                disabled={!hasNewer || loading}
              >
                Newer
              </Button>
              <Button
                variant="ghost"
                onClick={handleOlder}
                disabled={!hasOlder || loading}
              >
                Older
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
