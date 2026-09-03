import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// The board renders untrusted tracker / PR / CI / transcript text through
// react-markdown (Markdown.jsx). Its XSS safety is NOT our own code — it rests
// on default behaviors of the `react-markdown` + `remark-gfm` dependencies:
// output-encoding of text, no raw-HTML parsing (rehype-raw is not enabled), and
// a default URL sanitizer that drops dangerous link schemes (javascript:/data:).
// A dependency upgrade could silently change any of these. These tests render
// the REAL dependencies, so they fail the moment that safety regresses from a
// bump — which is the whole point of pinning it here rather than trusting the
// version number. The source-guard test additionally catches a local change
// that would re-open raw HTML.

function render(md) {
  return renderToStaticMarkup(
    React.createElement(ReactMarkdown, { remarkPlugins: [remarkGfm] }, md),
  );
}

test("raw HTML in markdown is escaped, not rendered as a live element", () => {
  const html = render('before <img src=x onerror="alert(1)"> after');
  assert.ok(!/<img\b[^>]*onerror/i.test(html), `live img rendered: ${html}`);
  assert.ok(html.includes("&lt;img"), `raw HTML not escaped: ${html}`);
});

test("a javascript: link is neutralized (no javascript: href reaches the DOM)", () => {
  const html = render("[click me](javascript:alert(document.domain))");
  assert.ok(!/href=("|')javascript:/i.test(html), `javascript: href rendered: ${html}`);
});

test("a data: link is neutralized", () => {
  const html = render("[x](data:text/html,<script>alert(1)</script>)");
  assert.ok(!/href=("|')data:/i.test(html), `data: href rendered: ${html}`);
});

test("a bare javascript: scheme in text is not autolinked into an href", () => {
  const html = render("look at javascript:alert(1) here");
  assert.ok(!/href=("|')javascript:/i.test(html), `bare scheme autolinked: ${html}`);
});

test("positive control: a real https link still renders (sanitizer is not just dropping every link)", () => {
  const html = render("[docs](https://example.com/path)");
  assert.ok(
    /href=("|')https:\/\/example\.com\/path\1/i.test(html),
    `expected https link to render, got: ${html}`,
  );
});

test("text is output-encoded: markup characters render as escaped entities, not live markup", () => {
  const html = render('<script>alert(1)</script> & "q" <b>bold</b>');
  assert.ok(!/<script\b/i.test(html), `live <script> rendered: ${html}`);
  assert.ok(!/<b>/.test(html), `raw <b> rendered: ${html}`);
  assert.ok(html.includes("&lt;script&gt;"), `angle brackets not encoded: ${html}`);
});

test("control characters are inert: no break-out of rendered text or a link href", () => {
  // U+2028/U+2029 (line/paragraph separators), U+0000 (null), U+001F — built
  // from char codes so the source stays pure ASCII. In react-markdown's
  // text/URL context these are inert: they cannot form markup or break a href.
  const ctrl = String.fromCharCode(0x2028, 0x2029, 0x00, 0x1f);
  const payload = `a${ctrl}b [lnk](https://ok.test/${ctrl}path)`;
  const html = render(payload);
  assert.ok(
    !/<script|onerror=|javascript:/i.test(html),
    `dangerous construct from control chars: ${JSON.stringify(html)}`,
  );
  const m = html.match(/href=("|')([^"']*)\1/i);
  if (m) {
    assert.ok(
      m[2].startsWith("https://ok.test/"),
      `href tampered by control chars: ${JSON.stringify(m[2])}`,
    );
  }
});

test("Markdown.jsx enables no raw-HTML rendering and no unsafe DOM sink", () => {
  const src = readFileSync(new URL("./Markdown.jsx", import.meta.url), "utf8");
  assert.ok(!/rehype-raw/.test(src), "Markdown.jsx must not enable rehype-raw");
  assert.ok(
    !/dangerouslySetInnerHTML/.test(src),
    "Markdown.jsx must not use dangerouslySetInnerHTML",
  );
});
