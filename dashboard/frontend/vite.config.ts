import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // `assetsInlineLimit: 0`: Vite would otherwise inline the small font subsets as `data:` URIs,
  // which the Content-Security-Policy (`font-src 'self'`, see the Caddyfile) blocks.
  build: { outDir: "dist", sourcemap: false, assetsInlineLimit: 0 },
  // Local dev proxy (production goes through Caddy). Only relevant for `npm run dev`.
  server: { proxy: { "/api": "http://localhost:8088", "/ws": { target: "ws://localhost:8088", ws: true } } },
});
