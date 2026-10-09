import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

/**
 * E2E integration suite: exercises the *real* API client and session store
 * against a live backend (no browser automation). Requires the backend stack
 * (Postgres + Qdrant + Redis) to be running; the suite skips cleanly when the
 * health endpoint is unreachable.
 *
 * Run: BACKEND_URL=http://localhost:8000 npm run test:e2e
 *
 * Environment: `node`, not `jsdom`. This suite is an API client exercise — it
 * needs no DOM — and it runs on Node's fetch, which is undici. undici cannot
 * serialize jsdom's `FormData`/`File` into a multipart body, so a jsdom
 * environment silently sends uploads with an empty `file` part (the backend
 * answers 422 "Field required"). Node's native `FormData`/`File` serialize
 * exactly like a browser's. `e2e/setup.ts` supplies the two `window`/storage
 * globals the client needs.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  test: {
    environment: "node",
    globals: true,
    setupFiles: ["./e2e/setup.ts"],
    include: ["e2e/**/*.test.ts"],
    // E2E talks to a real server; give it room.
    testTimeout: 90_000,
    hookTimeout: 90_000,
    css: false,
    restoreMocks: true,
    clearMocks: true,
    // Never part of the normal `npm run test` gate.
    watch: false,
  },
});