// Render a bundled Markdown doc (quickstart.md / configuration.md) to a
// self-contained, dark-first HTML page shown IN-APP.
//
// Why this exists: the Help → "no_human Quickstart" item used to
// `shell.openPath()` the bundled `.md`, which hands the raw file to whatever
// macOS app owns `.md` — so the user saw raw Markdown in TextEdit. This turns
// the same bundled file into a rendered page, still fully offline (no network,
// no CDN, no external stylesheet) so it works on first run exactly when a
// quickstart is read.
//
// The renderer is deliberately a SMALL, dependency-free subset — headings,
// fenced + inline code, bold, italic, links, unordered/ordered lists,
// horizontal rules and paragraphs — which covers everything the menu-wired
// docs/quickstart.md actually uses (a test renders the shipped file and fails
// on any leftover markup). It is NOT a general CommonMark implementation;
// keeping it tiny keeps the lean-stack constraint (no markdown dependency)
// and keeps it auditable. Everything is HTML-escaped before any markup is
// emitted, and code — fenced or inline — is escaped and never re-parsed.
//
// KNOWN LIMITATION (deliberate, not a bug): no tables and no multi-line list
// items — docs/configuration.md (not menu-wired today) would render its table
// as plain paragraphs. Wire a doc through openDocWindow only after extending
// the shipped-doc artefact test in docRender.test.mjs to cover that doc.

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Inline pass. The WHOLE string is HTML-escaped first, so every raw <, >, "
// and & from the source is inert before any markup is emitted — every tag and
// attribute delimiter below comes from these templates, never from input.
// One left-to-right scan in which the EARLIEST match wins: a code span that
// opens before a ** protects its contents (`a ** b` stays literal), while
// bold whose CONTENT contains a paired code span still renders — the content
// is re-scanned by the same rules, and because the bold/italic grammar
// forbids a bare * inside, recursion is bounded and code stays atomic.
const INLINE = [
  "`([^`]+)`", // 1: code span — contents literal, never re-parsed
  "\\*\\*((?:[^*`]|`[^`]*`)+?)\\*\\*", // 2: bold; may contain code spans
  "\\*([^*`\\s](?:[^*`]*[^*`\\s])?)\\*", // 3: italic; no *, `, or edge spaces
  "\\[([^\\]]+)\\]\\(([^)]+)\\)", // 4=label, 5=href
].join("|");

function renderEscaped(s) {
  const re = new RegExp(INLINE, "g");
  let out = "";
  let last = 0;
  let m;
  while ((m = re.exec(s)) !== null) {
    out += s.slice(last, m.index);
    if (m[1] !== undefined) out += `<code>${m[1]}</code>`;
    else if (m[2] !== undefined) out += `<strong>${renderEscaped(m[2])}</strong>`;
    else if (m[3] !== undefined) out += `<em>${renderEscaped(m[3])}</em>`;
    else if (/^(https?:\/\/|#)/i.test(m[5]))
      // href/label come from the escaped string: no raw ", <, > can exist in
      // them, so the attribute cannot be broken out of. Scheme allowlist:
      // only http(s) and in-doc anchors become clickable.
      out += `<a href="${m[5]}">${renderEscaped(m[4])}</a>`;
    // A relative/other-scheme target (e.g. INSTALLER.md#…, javascript:) is
    // not safely resolvable from a data: doc window, so it is NOT linkified.
    // Render just the LABEL as text — raw "[label](path)" markdown on screen
    // would defeat the whole point of rendering.
    else out += renderEscaped(m[4]);
    last = re.lastIndex;
  }
  return out + s.slice(last);
}

// Code spans are atomic; bold/italic/links may wrap them. Escape-then-scan —
// no placeholders, so nothing can collide with real content.
function renderInline(text) {
  return renderEscaped(escapeHtml(text));
}

// Block-level pass. Line-oriented, which is enough for the two shipped docs.
export function renderMarkdown(md) {
  const lines = String(md).replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  let para = [];

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${renderInline(para.join(" "))}</p>`);
      para = [];
    }
  };

  while (i < lines.length) {
    const line = lines[i];

    // fenced code block — contents are literal, escaped, never re-parsed
    if (/^```/.test(line.trim())) {
      flushPara();
      const lang = line.trim().slice(3).trim();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        buf.push(lines[i]);
        i++;
      }
      i++; // consume closing fence
      const cls = lang ? ` class="lang-${escapeHtml(lang)}"` : "";
      out.push(`<pre><code${cls}>${escapeHtml(buf.join("\n"))}</code></pre>`);
      continue;
    }

    // headings
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      flushPara();
      const level = h[1].length;
      out.push(`<h${level}>${renderInline(h[2].trim())}</h${level}>`);
      i++;
      continue;
    }

    // horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushPara();
      out.push("<hr>");
      i++;
      continue;
    }

    // unordered list
    if (/^\s*[-*]\s+/.test(line)) {
      flushPara();
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i++;
      }
      out.push(
        `<ul>${items.map((it) => `<li>${renderInline(it)}</li>`).join("")}</ul>`,
      );
      continue;
    }

    // ordered list
    if (/^\s*\d+\.\s+/.test(line)) {
      flushPara();
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      out.push(
        `<ol>${items.map((it) => `<li>${renderInline(it)}</li>`).join("")}</ol>`,
      );
      continue;
    }

    // blank line ends a paragraph
    if (line.trim() === "") {
      flushPara();
      i++;
      continue;
    }

    para.push(line.trim());
    i++;
  }
  flushPara();
  return out.join("\n");
}

// Wrap rendered body in a full, self-contained dark-first HTML document. No
// external stylesheet, font, or script — it must render on a machine with no
// network on first run.
export function docPage(title, md) {
  const body = renderMarkdown(md);
  const safeTitle = escapeHtml(String(title || "no_human"));
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${safeTitle}</title>
<style>
  :root { color-scheme: dark light; }
  body {
    margin: 0; padding: 48px 20px; background: #0e1320; color: #e7edf7;
    font: 16px/1.65 -apple-system, "Helvetica Neue", Arial, sans-serif;
    display: flex; justify-content: center;
  }
  main { width: 100%; max-width: 760px; }
  h1,h2,h3,h4 { line-height: 1.25; color: #fff; margin: 1.8em 0 .5em; }
  h1 { font-size: 2rem; margin-top: 0; }
  h2 { font-size: 1.45rem; border-bottom: 1px solid #223; padding-bottom: .2em; }
  h3 { font-size: 1.15rem; }
  a { color: #4C9AFF; }
  p, li { color: #c9d4e6; }
  code {
    font: .9em ui-monospace, "SF Mono", Menlo, monospace;
    background: #1a2233; color: #dfe8f7; padding: .12em .38em; border-radius: 5px;
  }
  pre {
    background: #0a0f1a; border: 1px solid #223; border-radius: 10px;
    padding: 14px 16px; overflow-x: auto;
  }
  pre code { background: none; padding: 0; color: #d5e0f2; }
  hr { border: none; border-top: 1px solid #223; margin: 2em 0; }
  ul, ol { padding-left: 1.4em; }
  li { margin: .3em 0; }
  @media (prefers-color-scheme: light) {
    body { background: #fff; color: #1a2233; }
    h1,h2,h3,h4 { color: #0b1220; } h2 { border-bottom-color: #e5e9f0; }
    p, li { color: #33405a; } code { background: #f0f3f8; color: #1a2233; }
    pre { background: #f6f8fb; border-color: #e5e9f0; } pre code { color: #1a2233; }
    hr { border-top-color: #e5e9f0; }
  }
</style></head>
<body><main>${body}</main></body></html>`;
}
