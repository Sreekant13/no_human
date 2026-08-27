// error.html's routing/rendering logic, exercised WITHOUT a real renderer.
//
// uiPages.test.mjs already covers this page in real Electron (focus rings,
// computed [hidden] display) — but that suite needs the `electron` devDependency
// physically installed, which is not guaranteed in every environment this runs
// in. The reason -> remediation-block mapping and the detail-text rendering are
// plain data-flow (no computed style question), so they are tested here by
// running the page's OWN <script> text in a `vm` sandbox with a minimal fake
// `document` — the real script, not a reimplementation, so this cannot drift
// from what actually ships.
import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";

const IDS = ["steps-packaged", "steps-stopfailed", "steps-dev", "steps-timeout",
  "steps-cli-missing", "steps-not-logged-in", "token-link", "retry-status",
  "detail-text", "detail-body"];

function extractScript() {
  const html = fs.readFileSync(new URL("./error.html", import.meta.url), "utf8");
  const m = html.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(m, "error.html must contain an inline <script> block");
  return m[1];
}

/** Run the page's real script against a query string; returns the fake elements. */
function route(queryString) {
  const elements = new Map();
  for (const id of IDS) {
    elements.set(id, {
      id, hidden: true, textContent: "", _attrs: {},
      setAttribute(k, v) { this._attrs[k] = v; },
      getAttribute(k) { return this._attrs[k] ?? null; },
      removeAttribute(k) { delete this._attrs[k]; },
      addEventListener() {},
    });
  }
  const document = {
    // error.html only ever looks up ids that exist on the shipped page —
    // returning null for a genuine miss (rather than fabricating one) keeps
    // this failing loudly if the script starts asking for something new.
    getElementById: (id) => elements.get(id) ?? null,
    querySelector: () => ({ addEventListener() {} }),
  };
  const sandbox = { document, location: { search: queryString }, URLSearchParams,
    setTimeout: () => 0, clearTimeout: () => {} };
  vm.createContext(sandbox);
  vm.runInContext(extractScript(), sandbox);
  return elements;
}

test("error.html: reason=backend-cli-missing shows the CLI-missing remediation, not generic advice", () => {
  const els = route("?reason=backend-cli-missing&packaged=1");
  assert.equal(els.get("steps-cli-missing").hidden, false,
    "the claude-CLI-specific remediation must be shown");
  assert.equal(els.get("steps-packaged").hidden, true,
    "the generic packaged text must not also show");
  assert.equal(els.get("steps-dev").hidden, true);
  assert.equal(els.get("steps-stopfailed").hidden, true);
});

test("error.html: reason=backend-not-logged-in shows the setup-token remediation", () => {
  const els = route("?reason=backend-not-logged-in&packaged=0");
  assert.equal(els.get("steps-not-logged-in").hidden, false,
    "the not-logged-in remediation must be shown");
  assert.equal(els.get("steps-dev").hidden, true,
    "the generic dev text must not also show");
});

test("error.html: the captured detail text is rendered verbatim, not swallowed", () => {
  const detail = "coding backend unavailable: the claude CLI was not found.";
  const els = route(`?reason=backend-cli-missing&packaged=1&detail=${encodeURIComponent(detail)}`);
  assert.equal(els.get("detail-text").hidden, false,
    "a captured detail message must be surfaced on the page");
  assert.equal(els.get("detail-body").textContent, detail,
    "the actual nh-start diagnostic text must reach the page verbatim");
});

test("error.html: no detail param means the detail block stays hidden", () => {
  const els = route("?reason=spawn-timeout&packaged=1");
  assert.equal(els.get("detail-text").hidden, true);
});

// --- spawn-timeout: a slow boot, never a credential accusation -------------

test("error.html: reason=spawn-timeout shows the honest 'taking longer' block, not the credential copy", () => {
  // The bug: a plain spawn-timeout fell through to steps-packaged, whose copy
  // says the server "refuses to start without a working Claude credential" — a
  // false accusation for a server that is merely booting slowly.
  const els = route("?reason=spawn-timeout&packaged=1");
  assert.equal(els.get("steps-timeout").hidden, false,
    "the non-accusatory 'still trying to connect' block must be shown");
  assert.equal(els.get("steps-packaged").hidden, true,
    "the credential-accusing packaged copy must NOT show for a plain timeout");
  assert.equal(els.get("steps-cli-missing").hidden, true);
  assert.equal(els.get("steps-not-logged-in").hidden, true);
});

test("error.html: spawn-timeout is the same slow-boot story for a developer, not steps-dev", () => {
  // The shell spawns the server in dev too, so a timeout there is the same slow
  // boot — the honest 'taking longer' block, not "start it in a terminal".
  const els = route("?reason=spawn-timeout&packaged=0");
  assert.equal(els.get("steps-timeout").hidden, false);
  assert.equal(els.get("steps-dev").hidden, true);
  assert.equal(els.get("steps-packaged").hidden, true);
});

// --- existing behaviours must survive untouched ---------------------------

test("error.html: existing reasons still route to their own copy", () => {
  assert.equal(route("?reason=stop-failed&packaged=1").get("steps-stopfailed").hidden, false);
  // A GENERIC reason (not one of the classified cases) still falls through to
  // the dev/packaged split — exercised here with load-failed, since spawn-timeout
  // now has its own block above.
  assert.equal(route("?reason=load-failed&packaged=0").get("steps-dev").hidden, false);
  assert.equal(route("?reason=load-failed&packaged=1").get("steps-packaged").hidden, false);
});

test("error.html: the token-reentry link still hides only for nh-not-found", () => {
  assert.equal(route("?reason=nh-not-found&packaged=1").get("token-link").hidden, true);
  assert.equal(route("?reason=spawn-timeout&packaged=1").get("token-link").hidden, false);
  assert.equal(route("?reason=backend-cli-missing&packaged=1").get("token-link").hidden, false);
});
