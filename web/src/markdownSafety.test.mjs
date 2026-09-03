import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, writeFileSync, mkdirSync, unlinkSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { transformWithEsbuild } from "vite";

// The board renders untrusted tracker / PR / CI / transcript text through
// Markdown.jsx. Its XSS safety is NOT our own code — it rests on defaults of
// the `react-markdown` + `remark-gfm` dependencies (raw HTML down-converted to
// text because no rehype plugin re-enables it; a default URL transform that
// drops javascript:/data: schemes) AND on Markdown.jsx passing nothing that
// overrides them. Both halves can regress silently: a dependency bump, or one
// prop (`urlTransform`, `rehypePlugins`) added to Markdown.jsx. So these tests
// render THE BOARD'S OWN COMPONENT — Markdown.jsx compiled at test time with
// vite's own esbuild (a declared devDependency) — not a bare
// react-markdown, and a props allowlist pins what Markdown.jsx may pass.

const here = dirname(fileURLToPath(import.meta.url));
const MARKDOWN_JSX = join(here, "Markdown.jsx");

async function loadBoardMarkdown() {
  // Compile the real component and import it from inside node_modules, so its
  // bare imports (react-markdown, remark-gfm) resolve exactly as the board's
  // build resolves them. The temp module lives under node_modules/.cache
  // (never tracked) and is removed afterwards.
  const src = readFileSync(MARKDOWN_JSX, "utf8");
  const out = await transformWithEsbuild(src, MARKDOWN_JSX, { loader: "jsx", jsx: "automatic", format: "esm" });
  // relative imports are relative to Markdown.jsx, not to the cache directory
  // relative imports are relative to Markdown.jsx, not to the cache directory;
  // a side-effect import of a build-time asset (css/svg/png...) is vite's
  // business, not the renderer's, and node cannot load it - dropped here
  const code = out.code
    .replace(/^\s*import\s+(["'])[^"']+\.(css|scss|less|svg|png|jpe?g|gif|webp|ico)\1;?\s*$/gm, "")
    .replace(/(from\s+|import\s+)(["'])(\.\.?\/[^"']+)\2/g,
      (_, kw, q, rel) => `${kw}${q}${join(here, rel)}${q}`);
  const dir = join(here, "..", "node_modules", ".cache");
  mkdirSync(dir, { recursive: true });
  const file = join(dir, `markdownSafety-${process.pid}.mjs`);
  writeFileSync(file, code);
  try {
    return (await import(`${file}?t=${Date.now()}`)).default;
  } finally {
    unlinkSync(file);
  }
}

const Markdown = await loadBoardMarkdown();
const render = (md) => renderToStaticMarkup(React.createElement(Markdown, null, md));
const hrefs = (html) => [...html.matchAll(/href=("|')([^"']*)\1/gi)].map((m) => m[2]);

test("the board component is what is under test (a link carries its target/rel props)", () => {
  const html = render("[docs](https://example.com/path)");
  assert.match(html, /href=("|')https:\/\/example\.com\/path\1/i, `expected the https link to render, got: ${html}`);
  assert.match(html, /target="_blank"/, `Markdown.jsx's anchor override not applied: ${html}`);
});

test("raw HTML in markdown is escaped, not rendered as a live element", () => {
  const html = render('before <img src=x onerror="alert(1)"> after');
  assert.ok(!/<img\b[^>]*onerror/i.test(html), `live img rendered: ${html}`);
  assert.ok(html.includes("&lt;img"), `raw HTML not escaped: ${html}`);
  const iframe = render('x <iframe src="https://evil.test/"></iframe> y');
  assert.ok(!/<iframe\b/i.test(iframe), `live iframe rendered: ${iframe}`);
});

test("a javascript: link is neutralized (no javascript: href reaches the DOM)", () => {
  const html = render("[click me](javascript:alert(document.domain))");
  assert.ok(!/href=("|')javascript:/i.test(html), `javascript: href rendered: ${html}`);
});

test("a data: link is neutralized", () => {
  const html = render("[x](data:text/html,<script>alert(1)</script>)");
  assert.ok(!/href=("|')data:/i.test(html), `data: href rendered: ${html}`);
});

test("a data: image src is neutralized (the CSP allows img-src data:, so the renderer must not pass one through)", () => {
  // no space in the destination: a payload that does not parse as an image
  // asserts nothing (there is no <img> to sanitise)
  const html = render("![x](data:image/svg+xml;base64,PHN2Zz4=)");
  assert.match(html, /<img\b/i, `expected the image to parse, got: ${html}`);
  assert.ok(!/src=("|')data:/i.test(html), `data: img src rendered: ${html}`);
});

test("text is output-encoded: markup characters render as escaped entities, not live markup", () => {
  const html = render('<script>alert(1)</script> & "q" <b>bold</b>');
  assert.ok(!/<script\b/i.test(html), `live <script> rendered: ${html}`);
  assert.ok(!/<b>/.test(html), `raw <b> rendered: ${html}`);
  assert.ok(html.includes("&lt;script&gt;"), `angle brackets not encoded: ${html}`);
});

test("control characters cannot break out of a parsed link or its text", () => {
  // The link MUST parse (a payload that fails to parse tests nothing), so the
  // control characters go where CommonMark allows them: percent-encoded in
  // the destination, raw in the link text and surrounding prose.
  const ctrl = String.fromCharCode(0x2028, 0x2029, 0x00, 0x1f);
  const html = render(`a${ctrl}b [l${ctrl}nk](https://ok.test/p%E2%80%A8ath%00x) c`);
  const found = hrefs(html);
  assert.equal(found.length, 1, `expected exactly one parsed link, got ${JSON.stringify(found)} in ${JSON.stringify(html)}`);
  assert.ok(found[0].startsWith("https://ok.test/p"), `href tampered: ${JSON.stringify(found[0])}`);
  const rawCtrl = [...found[0]].some((ch) => [0x2028, 0x2029, 0x00, 0x1f].includes(ch.charCodeAt(0)));
  assert.ok(!rawCtrl, `raw control character inside href: ${JSON.stringify(found[0])}`);
  assert.ok(!/<script|onerror=|javascript:/i.test(html), `dangerous construct from control chars: ${JSON.stringify(html)}`);
});

test("Markdown.jsx passes ReactMarkdown only the allowlisted props (nothing that re-enables HTML or rewrites URLs)", () => {
  const src = readFileSync(MARKDOWN_JSX, "utf8");
  // EVERY <ReactMarkdown ...> in the file is held to the allowlist: a second
  // renderer added beside the first would otherwise escape it. The opening
  // tag ends at the first `>` OUTSIDE any {...} expression — the `components`
  // prop contains JSX with its own `>`s, so a lazy regex would stop early and
  // miss a prop written after it.
  const tags = [];
  for (let start = src.indexOf("<ReactMarkdown"); start >= 0; start = src.indexOf("<ReactMarkdown", start + 1)) {
    let depth = 0, end = -1;
    for (let i = start; i < src.length; i++) {
      const ch = src[i];
      if (ch === "{") depth++;
      else if (ch === "}") depth--;
      else if (ch === ">" && depth === 0) { end = i; break; }
    }
    assert.ok(end > start, "ReactMarkdown opening tag is unterminated");
    tags.push(src.slice(start + "<ReactMarkdown".length, end));
  }
  assert.ok(tags.length >= 1, "ReactMarkdown opening tag not found in Markdown.jsx");
  const allowed = new Set(["remarkPlugins", "components"]);
  for (const tag of tags) {
    // top-level props only: names followed by `=` at brace depth 0; a spread
    // at depth 0 could smuggle any prop past this allowlist and is refused
    const props = [];
    let depth = 0;
    for (const m of tag.matchAll(/\{\s*\.\.\.|\{|\}|(?:^|\s)([A-Za-z_][\w]*)\s*=/g)) {
      if (m[0].startsWith("{") && m[0].length > 1) { assert.ok(depth > 0, "no prop spread on ReactMarkdown"); depth++; }
      else if (m[0] === "{") depth++;
      else if (m[0] === "}") depth--;
      else if (depth === 0) props.push(m[1]);
    }
    const extra = props.filter((p) => !allowed.has(p));
    assert.deepEqual(extra, [], `Markdown.jsx passes ReactMarkdown props outside the allowlist: ${extra.join(", ")} (rehypePlugins, urlTransform, skipHtml, allowElement, allowedElements, disallowedElements, unwrapDisallowed, remarkRehypeOptions each change what untrusted text can do — extend this test deliberately if one is ever needed)`);
    assert.match(tag, /remarkPlugins=\{\[remarkGfm\]\}/, "remarkPlugins must be exactly [remarkGfm]");
  }
  assert.ok(!/rehype/i.test(src), "Markdown.jsx must not import or use any rehype plugin");
  assert.ok(!/dangerouslySetInnerHTML/.test(src), "Markdown.jsx must not use dangerouslySetInnerHTML");
});

test("Markdown.jsx is the only react-markdown consumer under web/ (a second renderer would escape the allowlist above)", () => {
  const offenders = [];
  const walk = (dir) => {
    for (const ent of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, ent.name);
      if (ent.isDirectory()) { if (!["node_modules", "dist", ".cache"].includes(ent.name)) walk(full); continue; }
      if (!/\.(jsx?|[cm]js|tsx?|[cm]ts)$/.test(ent.name) || /\.test\.(jsx?|[cm]js|tsx?|[cm]ts)$/.test(ent.name)) continue;
      const text = readFileSync(full, "utf8");
      if (/["']react-markdown["']|<ReactMarkdown\b/.test(text) && full !== MARKDOWN_JSX) offenders.push(full);
    }
  };
  walk(join(here, ".."));  // the whole web/ tree, not only src/: a shim one directory up is still bundled
  assert.deepEqual(offenders, [], `react-markdown is used outside Markdown.jsx: ${offenders.join(", ")}`);
});
