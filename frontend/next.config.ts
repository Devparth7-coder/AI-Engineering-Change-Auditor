import type { NextConfig } from "next";

// The browser never talks to GitLab and never holds credentials: every request
// goes to this app's own origin and is proxied to the backend, which is the only
// component that knows GITLAB_TOKEN / GITLAB_URL.
const BACKEND_URL = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${BACKEND_URL}/api/:path*` },
    ];
  },
  reactStrictMode: true,
  turbopack: {
    // REQUIRED for Tailwind v4: without this loader the CSS module fails to
    // compile, the client bundle never evaluates, and pages silently never
    // hydrate (links degrade to full navigations, effects never run).
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
