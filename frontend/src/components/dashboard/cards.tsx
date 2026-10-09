import Link from "next/link";

import type { Document } from "@/lib/types";

import { Card, CardContent } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { DocumentsIcon } from "@/components/icons";

function count(documents: Document[], predicate: (document: Document) => boolean): number {
  return documents.filter(predicate).length;
}

export function CardsPreview({ documents }: { documents: Document[] }) {
  if (documents.length === 0) {
    return (
      <EmptyState
        icon={<DocumentsIcon className="h-8 w-8" />}
        title="No documents yet"
        description="Upload a document to build your knowledge base, then ask the AI assistant anything about it."
        action={
          <Link href="/documents">
            <span className="inline-flex h-9 items-center rounded-lg bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary-hover">
              Upload a document
            </span>
          </Link>
        }
      />
    );
  }

  const total = documents.length;
  const ready = count(documents, (d) => d.status === "ready");
  const processing = count(documents, (d) => d.status === "processing");
  const failed = count(documents, (d) => d.status === "failed");

  const stats: Array<{ label: string; value: number; tone: string }> = [
    { label: "Total documents", value: total, tone: "text-foreground" },
    { label: "Ready to search", value: ready, tone: "text-success" },
    { label: "Processing", value: processing, tone: "text-warning" },
    { label: "Failed", value: failed, tone: "text-danger" },
  ];

  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      {stats.map((stat) => (
        <Card key={stat.label}>
          <CardContent className="px-5 py-4">
            <p className="text-sm text-muted">{stat.label}</p>
            <p className={`mt-1 text-2xl font-semibold ${stat.tone}`}>{stat.value}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}