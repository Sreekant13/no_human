import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8420",
      "/ws": { target: "ws://127.0.0.1:8420", ws: true },
    },
  },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    // Never inline assets as data: URIs — the CSP is font-src 'self', and
    // Vite's 4KB default base64-inlined two Plex Mono subsets that the
    // policy then blocked (PR #107 review, medium).
    assetsInlineLimit: 0,
  },
});
