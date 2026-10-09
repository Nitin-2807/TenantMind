/**
 * E2E setup: point the API client at the live backend *before* the client
 * modules are imported (API_BASE is read from the environment at import time).
 *
 * The default for a local dev stack is http://localhost:8000; override with
 * BACKEND_URL when the backend listens somewhere else.
 */

const backendUrl = (process.env.BACKEND_URL ?? "http://localhost:8000").replace(/\/+$/, "");
process.env.NEXT_PUBLIC_API_BASE_URL = `${backendUrl}/api/v1`;

/**
 * Web-storage shim for the `node` test environment (see vitest.e2e.config.ts).
 *
 * The API client and session store read `window.localStorage` defensively
 * (`typeof window === "undefined"` guards), and the suite writes an expired
 * token through `window.localStorage` directly. This suite otherwise needs no
 * DOM, so instead of pulling in jsdom — whose `FormData`/`File` cannot be
 * serialized by Node's undici fetch, silently breaking multipart uploads — a
 * small in-memory `Storage` stands in for the browser.
 */

class InMemoryStorage implements Storage {
  private readonly data = new Map<string, string>();

  get length(): number {
    return this.data.size;
  }

  clear(): void {
    this.data.clear();
  }

  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.data.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.data.delete(key);
  }

  setItem(key: string, value: string): void {
    this.data.set(key, value);
  }
}

const storage = new InMemoryStorage();

Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  value: storage,
  writable: true,
});

const windowShim = {
  localStorage: storage,
  dispatchEvent: (): boolean => true,
};

Object.defineProperty(globalThis, "window", {
  configurable: true,
  value: windowShim,
  writable: true,
});