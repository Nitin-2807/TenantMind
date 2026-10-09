"use client";

/**
 * Root route: bounce to the right starting screen based on session state.
 * The backend remains the authority — this only picks the entry point.
 */

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth/auth-provider";
import { Spinner } from "@/components/ui/spinner";

export default function Home() {
  const { status } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (status === "authenticated") router.replace("/dashboard");
    else if (status === "anonymous") router.replace("/login");
  }, [status, router]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background" role="status" aria-label="Loading">
      <Spinner size="lg" />
    </div>
  );
}