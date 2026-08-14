import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// The server loads its backend ONCE and never reloads it, so merging a fix to
// main does not change what the running process does — a task can be judged by
// code that was superseded hours earlier, and the failure is then charged to
// the ticket. The backend records the loaded sha per attempt and exposes a
// staleness note; this pins the rest of the chain to the board.
//
// The startup log line is NOT a substitute and this test exists because of
// that: the condition matters precisely on a server that has been up a long
// time, which is exactly when the boot line has scrolled away. A signal only
// reachable by `curl` is the same silence in a different place.
//
// Every link is read from ITS OWN SOURCE. A test that restates the field name
// passes happily while a rename breaks the banner.

const SRC = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(SRC, p), "utf8");

const FIELD = "loaded_code_stale";
const COLUMN = "loaded_code_version";

const app = read("App.jsx");
const css = read("styles.css");
const api = read("../../src/no_human/api/app.py");
const db = read("../../src/no_human/core/db.py");

test("the attempt row actually carries the loaded sha", () => {
  assert.ok(
    new RegExp(`"${COLUMN}":\\s*"TEXT"`).test(db),
    `${COLUMN} must be a real attempts column (db.py att_wanted)`,
  );
  assert.ok(
    /loaded_code_version[^)]*\) VALUES/.test(db),
    "create_attempt must write it — a declared column nothing writes is null forever",
  );
});

test("the endpoint the board polls emits both fields", () => {
  assert.ok(api.includes(`"${FIELD}":`), `/api/worker/status must emit ${FIELD}`);
  assert.ok(api.includes('"loaded_code":'), "and the descriptor it refers to");
});

test("the board renders the staleness banner", () => {
  assert.ok(
    app.includes(`workerStatus?.${FIELD} &&`),
    `App.jsx must render on ${FIELD} — the whole point is a visible surface`,
  );
  assert.ok(
    /className="nh-alarm nh-stale"/.test(app),
    "it needs its own tone class",
  );
});

test("the banner is advisory, not an alarm", () => {
  // Red is reserved for the unattended promise breaking (worker down, watcher
  // down). Stale code is common during active development; painting it red
  // trains the operator to ignore red, and nothing here blocks a claim.
  const banner = app.match(/<div className="nh-alarm nh-stale"[^>]*/)?.[0];
  assert.ok(banner, "the banner must exist to be checked");
  assert.ok(banner.includes('role="status"'), "advisory, so role=status");
  assert.ok(!banner.includes('role="alert"'), "must not be an alert");
});

test("the tone class is defined and themed for both modes", () => {
  assert.ok(/\.nh-alarm\.nh-stale\s*\{/.test(css), "styles.css must define it");
  const rule = css.match(/\.nh-alarm\.nh-stale\s*\{([^}]*)\}/)[1];
  // Tailwind preflight is off: a bare `border` shorthand paints nothing, so
  // the re-tint must use border-color over .nh-alarm's existing border-style.
  assert.ok(/border-color:/.test(rule), "re-tint with border-color");
  assert.ok(!/\bborder:\s/.test(rule), "a `border` shorthand would paint nothing");
  // Every token it reads must exist in BOTH themes or light mode reuses the
  // dark hue (the bug class themeVars.test.mjs guards).
  const lightBlock = css.match(/\[data-theme="light"\]\s*\{([^}]*)\}/)?.[1];
  assert.ok(lightBlock, 'the [data-theme="light"] rule must exist');
  for (const token of rule.match(/var\((--[a-z0-9-]+)\)/g) || []) {
    const name = token.slice(4, -1);
    assert.ok(
      lightBlock.includes(`${name}:`),
      `${name} is used by .nh-stale but never overridden for the light theme`,
    );
  }
});
