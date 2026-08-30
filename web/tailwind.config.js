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
      // Tailwind's type scale is in `rem`, and this app sets `html { font-size:
      // 14px }` (styles.css) — not the 16px the default scale assumes. Measured in
      // the built bundle: text-xs resolved to 10.5px, under the 12px functional-text
      // floor that src/typeFloor.test.mjs holds the plain CSS to. Only `xs` fell
      // through (sm 12.25px, base 14px, lg 15.75px), so only `xs` is redefined —
      // in px, so it cannot drift with the root again. The line-height moves with
      // it: the default xs box is 1rem/14px, which is 1.17 on a 12px glyph.
      fontSize: {
        xs: ["12px", { lineHeight: "1.35" }],
      },
    },
  },
  plugins: [],
};
