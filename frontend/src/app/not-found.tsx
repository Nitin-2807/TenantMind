import Link from "next/link";

import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center bg-background p-6">
      <div className="w-full max-w-md rounded-xl border border-border bg-surface p-8 text-center">
        <p className="text-sm font-medium text-primary">404</p>
        <h1 className="mt-2 text-lg font-semibold text-foreground">Page not found</h1>
        <p className="mt-2 text-sm text-muted">
          The page you&apos;re looking for doesn&apos;t exist or may have moved.
        </p>
        <div className="mt-5 flex justify-center gap-2">
          <Link href="/dashboard">
            <Button variant="outline">Go to dashboard</Button>
          </Link>
        </div>
      </div>
    </div>
  );
}