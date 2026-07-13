/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  // Task 5.0 Step-0 spike: the Preflight base reset can clash with the existing
  // reset in styles.css. This value is set by the pixel-stability test (Board +
  // Stats, light + dark, vs the no-Tailwind baseline) — do NOT flip it blind.
  corePlugins: { preflight: false },
  theme: {
    extend: {
      // Token bridge (Task 5.0): Tailwind color utilities resolve to the SAME
      // CSS variables the plain-CSS screens use, so light/dark stay driven by the
      // [data-theme] blocks in styles.css — one source of truth, no `dark:`
      // variant, no second palette. Keep the HEX values in styles.css; never
      // rewrite them here.
      colors: {
        base: "var(--base)",
        card: "var(--bg-card)",
        panel: "var(--bg-panel)",
        hover: "var(--bg-hover)",
        // 'border' would collide with Tailwind's border-* utilities; expose the
        // border token as 'line' → border-line / bg-line.
        line: "var(--border)",
        text: "var(--text)",
        "text-hi": "var(--text-hi)",
        "text-muted": "var(--text-muted)",
        "text-dim": "var(--text-dim)",
        accent: "var(--accent-500)",
        "accent-600": "var(--accent-600)",
      },
      fontFamily: {
        mono: "var(--font-mono)",
        ui: "var(--font-ui)",
        display: "var(--font-display)",
      },
    },
  },
  plugins: [],
};
