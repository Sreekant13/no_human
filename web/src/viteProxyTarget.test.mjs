import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

// D2-hermetic: `ui_evidence` walks used to boot the dev server against
// vite.config.js's HARDCODED proxy target ("http://127.0.0.1:8420"), which is
// the operator's real, live `nh serve` board - a walk step clicking
// Save/Reset-to-defaults could PUT into the real ~/.no_human/config.yaml.
// vite.config.js now reads the target from process.env.VITE_API_TARGET,
// defaulting to that same literal so the customer path is byte-identical.
// These assertions import the real config module (not a source-text regex
// match, unlike this directory's JSX tests) - vite.config.js is plain ESM and
// its only imports ("vite", "@vitejs/plugin-react") are real devDependencies.

const CONFIG_PATH = join(dirname(fileURLToPath(import.meta.url)), "..", "vite.config.js");

test("defaults to 127.0.0.1:8420 when VITE_API_TARGET is unset", async () => {
  delete process.env.VITE_API_TARGET;
  const mod = await import(`${CONFIG_PATH}?case=default`);
  const { proxy } = mod.default.server;
  assert.equal(proxy["/api"], "http://127.0.0.1:8420");
  assert.equal(proxy["/ws"].target, "ws://127.0.0.1:8420");
  assert.equal(proxy["/ws"].ws, true);
});

test("VITE_API_TARGET overrides both the http and ws proxy targets", async () => {
  process.env.VITE_API_TARGET = "http://127.0.0.1:39111";
  try {
    // Node's ESM loader caches a module per exact specifier string; a
    // cache-busting query re-evaluates vite.config.js under the new env var
    // instead of returning the previous test's already-imported module.
    const mod = await import(`${CONFIG_PATH}?case=override`);
    const { proxy } = mod.default.server;
    assert.equal(proxy["/api"], "http://127.0.0.1:39111");
    assert.equal(proxy["/ws"].target, "ws://127.0.0.1:39111");
    assert.equal(proxy["/ws"].ws, true);
  } finally {
    delete process.env.VITE_API_TARGET;
  }
});

test("the build block is untouched", async () => {
  delete process.env.VITE_API_TARGET;
  const mod = await import(`${CONFIG_PATH}?case=build`);
  const { build } = mod.default;
  assert.equal(build.outDir, "dist");
  assert.equal(build.assetsDir, "assets");
  assert.equal(build.assetsInlineLimit, 0);
});
