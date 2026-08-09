import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { renderMarkdown, docPage } from "./docRender.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));

test("headings render at the right level", () => {
  const h = renderMarkdown("# Title\n\n## Sub");
  assert.match(h, /<h1>Title<\/h1>/);
  assert.match(h, /<h2>Sub<\/h2>/);
});

test("fenced code is escaped and NOT re-parsed for markup", () => {
  const h = renderMarkdown("```bash\nnh doctor <x> **not bold**\n```");
  assert.match(h, /<pre><code class="lang-bash">/);
  assert.match(h, /nh doctor &lt;x&gt; \*\*not bold\*\*/); // literal, escaped
  assert.doesNotMatch(h, /<strong>/);
});

test("inline code, bold and safe links render", () => {
  const h = renderMarkdown("Run `nh init`, it is **required**, see [docs](https://x.io).");
  assert.match(h, /<code>nh init<\/code>/);
  assert.match(h, /<strong>required<\/strong>/);
  assert.match(h, /<a href="https:\/\/x\.io">docs<\/a>/);
});

test("a javascript: link becomes plain label text, never an anchor or js href", () => {
  const h = renderMarkdown("[x](javascript:alert(1))");
  assert.doesNotMatch(h, /<a /);
  assert.doesNotMatch(h, /javascript:/i);
  assert.match(h, /<p>x/); // the label text is shown (no anchor, no js href)
});

test("a relative doc link renders as its label, not raw [text](path) markdown", () => {
  // The fixture points at a REAL anchor: the repo-wide doc-anchor guard
  // (tests/test_doc_anchors.py) scans every file for `<doc>.md#anchor` and
  // fails on one that would not resolve — a made-up anchor here broke it.
  const h = renderMarkdown(
    "see [INSTALLER.md#verify-your-install-is-real](INSTALLER.md#verify-your-install-is-real) for more",
  );
  assert.doesNotMatch(h, /<a /); // not linkified (unsafe/unresolvable scheme)
  assert.doesNotMatch(h, /\[INSTALLER/); // no raw markdown syntax left
  assert.match(h, /INSTALLER\.md#verify-your-install-is-real/); // the label text is shown
});

test("bold spanning a code span renders — the shipped quickstart's own pattern", () => {
  const h = renderMarkdown("**If you opened a `.dmg` and dragged it, stop.**");
  assert.match(
    h,
    /<strong>If you opened a <code>\.dmg<\/code> and dragged it, stop\.<\/strong>/,
  );
  assert.doesNotMatch(h, /\*\*/);
});

test("italics render, and ** inside a code span stays literal", () => {
  const h = renderMarkdown('SmartScreen says *"protected"*. See `a ** b`.');
  assert.match(h, /<em>&quot;protected&quot;<\/em>/);
  assert.match(h, /<code>a \*\* b<\/code>/);
  assert.doesNotMatch(h, /<strong>/);
});

test("a bare asterisk in prose is not italicised", () => {
  const h = renderMarkdown("2 * 3 * 4 stays flat, and a lone * too");
  assert.doesNotMatch(h, /<em>/);
});

test("raw HTML in the source is escaped, not emitted", () => {
  const h = renderMarkdown("a <script>evil()</script> b");
  assert.doesNotMatch(h, /<script>/);
  assert.match(h, /&lt;script&gt;/);
});

test("lists and horizontal rules render", () => {
  const h = renderMarkdown("- one\n- two\n\n---\n\n1. a\n2. b");
  assert.match(h, /<ul><li>one<\/li><li>two<\/li><\/ul>/);
  assert.match(h, /<hr>/);
  assert.match(h, /<ol><li>a<\/li><li>b<\/li><\/ol>/);
});

test("docPage is self-contained: no external stylesheet/script/font/CDN", () => {
  const html = docPage("no_human Quickstart", "# Hi");
  assert.match(html, /<!doctype html>/i);
  assert.match(html, /<title>no_human Quickstart<\/title>/);
  assert.doesNotMatch(html, /<link[^>]+href=/i); // no external stylesheet
  assert.doesNotMatch(html, /<script/i); // no script at all
  assert.doesNotMatch(html, /https?:\/\//); // no CDN/font/remote asset
});

test("the SHIPPED quickstart.md has no multi-line list items — the renderer splits them", () => {
  // The renderer is line-oriented: a continuation line under a list item ends
  // the list and becomes a paragraph, so "1. a\n   b\n2. c" renders as THREE
  // blocks numbered 1 / (text) / 1. An independent review caught a commit
  // adding exactly that to the doc a menu opens — and the artefact test below
  // could not see it, because no raw markdown survives. This lint catches the
  // class at the source: every list item must be a single source line.
  const md = readFileSync(path.join(here, "..", "docs", "quickstart.md"), "utf8");
  const lines = md.split("\n");
  let fence = false;
  let prevList = false;
  const offenders = [];
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (l.trim().startsWith("```")) { fence = !fence; prevList = false; continue; }
    if (fence) continue;
    const isList = /^\s*(\d+\.|[-*])\s+/.test(l);
    const isCont = /^\s{2,}\S/.test(l) && !isList;
    if (isCont && prevList) offenders.push(`${i + 1}: ${l.slice(0, 60)}`);
    prevList = isList || (isCont && prevList);
  }
  assert.deepEqual(offenders, [], "multi-line list items split the rendered list");
});

test("the SHIPPED quickstart.md renders without leaving raw markdown artefacts", () => {
  const md = readFileSync(path.join(here, "..", "docs", "quickstart.md"), "utf8");
  const html = renderMarkdown(md);
  // Strip rendered code blocks first: a shell comment ("# do X") INSIDE a
  // ```code fence``` is legitimately a line starting with '#' in the output and
  // must not be mistaken for an unrendered markdown heading.
  const outsideCode = html
    .replace(/<pre><code[\s\S]*?<\/code><\/pre>/g, "")
    .replace(/<code[^>]*>[\s\S]*?<\/code>/g, "");
  assert.doesNotMatch(outsideCode, /^#{1,6}\s/m); // no heading markdown survived
  assert.doesNotMatch(outsideCode, /```/); // no unclosed/leaked fence
  assert.doesNotMatch(outsideCode, /\*\*/); // no unrendered bold
  assert.doesNotMatch(outsideCode, /(^|\s)\*\S[^*]*\*/); // no unrendered italics
  assert.doesNotMatch(outsideCode, /\]\(/); // no raw link markdown
  assert.match(html, /<pre><code/); // it has code blocks
  assert.match(html, /<h1>/); // and a rendered title
  assert.match(html, /<strong>/); // the doc's bold actually rendered
});
