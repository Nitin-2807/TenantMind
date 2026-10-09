"use client";

import { PageHeader } from "@/components/layout/page-header";
import { Chat } from "@/components/assistant/chat";

export default function AssistantPage() {
  return (
    <div className="container mx-auto max-w-4xl px-4 py-6">
      <PageHeader
        title="AI Assistant"
        description="Ask questions about your documents"
      />
      <Chat />
    </div>
  );
}
