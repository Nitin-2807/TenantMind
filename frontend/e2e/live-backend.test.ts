/**
 * Live-backend E2E (no browser stack).
 *
 * Exercises the *real* API client and session store against a running TenantMind
 * backend (Postgres + Qdrant + Redis + uvicorn). Each run registers a unique
 * tenant so runs are independent. When the backend is unreachable the whole
 * suite skips with one clear message instead of failing.
 *
 * Run: cd frontend && BACKEND_URL=http://localhost:8000 npm run test:e2e
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import * as adminApi from "@/lib/api/admin";
import * as authApi from "@/lib/api/auth";
import * as documentsApi from "@/lib/api/documents";
import * as queryApi from "@/lib/api/query";
import * as usersApi from "@/lib/api/users";
import { PermissionError } from "@/lib/errors";
import { clearStoredSession, getAccessToken, setSession } from "@/lib/auth/tokens";
import type { Document, SSEStreamEvent, User } from "@/lib/types";

const BACKEND_URL = (process.env.BACKEND_URL ?? "http://localhost:8000").replace(/\/+$/, "");
const ADMIN_PASSWORD = "e2e-admin-password-123";
const VIEWER_PASSWORD = "e2e-viewer-password-123";

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function backendReachable(): Promise<boolean> {
  try {
    const response = await fetch(`${BACKEND_URL}/health`);
    return response.ok;
  } catch {
    return false;
  }
}

const reachable = await backendReachable();
const describeLive = reachable ? describe : describe.skip;

describeLive("TenantMind live backend E2E", () => {
  const slug = `e2e-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
  const adminEmail = `admin-${slug}@example.com`;
  const viewerEmail = `viewer-${slug}@example.com`;

  let adminUser: User;
  let uploaded: Document;
  let queryId: string;

  beforeAll(async () => {
    clearStoredSession();
    const registration = await authApi.registerTenant({
      name: `E2E ${slug}`,
      slug,
      admin_email: adminEmail,
      admin_password: ADMIN_PASSWORD,
    });
    // register-tenant signs the first admin in: persist the returned tokens.
    setSession(registration.user, registration.tokens);
    adminUser = registration.user;
  });

  afterAll(async () => {
    // Best-effort cleanup: remove the uploaded document if the current session
    // may (admin). Tenants themselves have no delete endpoint.
    if (uploaded) {
      try {
        await documentsApi.deleteDocument(uploaded.id);
      } catch {
        // Ignore — orphaned docs in an E2E tenant are harmless.
      }
    }
    clearStoredSession();
  });

  it("register-tenant returns tokens and persists a session", () => {
    expect(adminUser.email).toBe(adminEmail);
    expect(adminUser.role).toBe("admin");
    expect(getAccessToken()).not.toBeNull();
  });

  it("logs the admin back in with tenant slug + email + password", async () => {
    clearStoredSession();
    const login = await authApi.login({
      tenant_slug: slug,
      email: adminEmail,
      password: ADMIN_PASSWORD,
    });
    expect(login.user.email).toBe(adminEmail);
    setSession(login.user, login.tokens);
  });

  it("uploads a document and waits for it to be ready", async () => {
    const file = new File(
      ["# Rent policy\n\nRent is due on the 1st of every month.\nPayments are accepted until the 5th.\n"],
      "rent-policy.md",
      { type: "text/markdown" },
    );
    uploaded = await documentsApi.uploadDocument(file);
    expect(uploaded.filename).toBe("rent-policy.md");
    expect(["processing", "ready"]).toContain(uploaded.status);

    // Poll until the ingestion pipeline finishes (or fails — say so honestly).
    const deadline = Date.now() + 90_000;
    for (;;) {
      const current = await documentsApi.getDocument(uploaded.id);
      if (current.status === "ready") {
        uploaded = current;
        break;
      }
      if (current.status === "failed") {
        throw new Error(`document ingestion failed: ${current.error}`);
      }
      if (Date.now() > deadline) {
        throw new Error(`document still processing after 90s (status ${current.status})`);
      }
      await sleep(2_000);
    }
    expect(uploaded.chunk_count).toBeGreaterThan(0);
  });

  it("the list endpoint reflects the document", async () => {
    const list = await documentsApi.listDocuments();
    expect(list.items.some((d) => d.id === uploaded.id)).toBe(true);
  });

  it("askQuestion returns a grounded answer with citations", async () => {
    const answer = await queryApi.askQuestion("When is rent due?");
    expect(answer.answer.length).toBeGreaterThan(0);
    expect(answer.grounded).toBe(true);
    expect(answer.citations.length).toBeGreaterThan(0);
    expect(answer.citations[0].document_id).toBe(uploaded.id);
  });

  it("streams the full SSE lifecycle start → answer → citations → done", async () => {
    const events: SSEStreamEvent[] = [];
    await queryApi.streamQuery("When is rent due?", {
      onEvent: (event) => events.push(event),
    });

    expect(events.map((e) => e.type)).toEqual(["start", "answer", "citations", "done"]);
    const start = events[0];
    if (start.type === "start") queryId = start.query_id;
    const answer = events[1];
    if (answer.type === "answer") {
      expect(answer.grounded).toBe(true);
      expect(answer.answer.length).toBeGreaterThan(0);
    }
    const citations = events[2];
    if (citations.type === "citations") {
      expect(citations.citations.length).toBeGreaterThan(0);
    }
  });

  it("feedback reaches history (up vote)", async () => {
    const history = await queryApi.getHistory(20, 0);
    const item = history.items.find((h) => h.id === queryId);
    expect(item).toBeDefined();

    const feedback = await queryApi.submitFeedback(queryId, "up");
    expect(feedback.query_id).toBe(queryId);
    expect(feedback.feedback).toBe("up");

    const updated = await queryApi.getHistory(20, 0);
    expect(updated.items.find((h) => h.id === queryId)?.feedback).toBe("up");
  });

  it("admin usage reflects the queries and stays honest (empty breakdown tolerated)", async () => {
    const usage = await adminApi.getUsage();
    expect(usage.total_queries).toBeGreaterThanOrEqual(2);
    expect(usage.total_answered).toBeGreaterThanOrEqual(2);
    expect(Number.isFinite(usage.cache_hit_rate)).toBe(true);
    // day/user breakdowns may be empty for a brand-new tenant — check shape only.
    expect(Array.isArray(usage.queries_by_day)).toBe(true);
    expect(Array.isArray(usage.queries_by_user)).toBe(true);
  });

  it("an expired access token is refreshed and the request retried", async () => {
    // JWT with exp in 2001 — the client's proactive refresh must swap it.
    const expired =
      "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1c2VyLTEiLCJleHAiOjEwMDAwMDAwMDB9.c2ln";
    window.localStorage.setItem("tm.access_token", expired);

    const list = await documentsApi.listDocuments();
    expect(Array.isArray(list.items)).toBe(true);
    expect(getAccessToken()).not.toBe(expired);
  });

  it("viewers can read documents but are rejected from admin usage (403)", async () => {
    const invited = await usersApi.inviteUser({
      email: viewerEmail,
      password: VIEWER_PASSWORD,
      role: "viewer",
    });
    expect(invited.role).toBe("viewer");

    const viewerLogin = await authApi.login({
      tenant_slug: slug,
      email: viewerEmail,
      password: VIEWER_PASSWORD,
    });
    setSession(viewerLogin.user, viewerLogin.tokens);

    const docs = await documentsApi.listDocuments();
    expect(docs.items.some((d) => d.id === uploaded.id)).toBe(true);

    await expect(adminApi.getUsage()).rejects.toBeInstanceOf(PermissionError);
  });
});