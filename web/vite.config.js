import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// A walk booted by the harness (`ui_evidence.py`) must talk to its own
// throwaway backend, never the operator's live :8420 board — VITE_API_TARGET
// lets that hermetic boot repoint the proxy without touching this file.
// Unset (every customer install) resolves to today's literal, byte-for-byte.
const apiTarget = process.env.VITE_API_TARGET || "http://127.0.0.1:8420";
const wsTarget = apiTarget.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": apiTarget,
      "/ws": { target: wsTarget, ws: true },
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
