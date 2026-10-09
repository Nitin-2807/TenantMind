import type { NextConfig } from "next";

/**
 * TenantMind frontend configuration.
 *
 * The browser never talks to the FastAPI backend directly: every API call is a
 * same-origin request to this Next.js server, which proxies it to the backend
 * through the rewrites below. That keeps the application same-origin (no CORS
 * configuration needed on the backend) and lets the API base stay `/api/v1`,
 * exactly as the backend mounts it.
 *
 * - `BACKEND_URL` is a *server-side only* variable used by the proxy. It is
 *   never exposed to the browser (it has no `NEXT_PUBLIC_` prefix).
 * - `NEXT_PUBLIC_API_BASE_URL` (optional, read by the API client) lets a
 *   deployment point the client at a full backend URL instead of the proxy.
 */
const backendUrl = process.env.BACKEND_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  /* config options here */
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  experimental: {
    // Backend default upload limit is 10MB; buffer 15MB so multipart uploads
    // near the limit are not truncated by the proxy.
    proxyClientMaxBodySize: "15mb",
  },
  async rewrites() {
    return [
      {
        source: "/api/v1/:path*",
        destination: `${backendUrl}/api/v1/:path*`,
      },
      // Health probes live at the application root (backend design decision).
      {
        source: "/health",
        destination: `${backendUrl}/health`,
      },
      {
        source: "/health/ready",
        destination: `${backendUrl}/health/ready`,
      },
    ];
  },
};

export default nextConfig;