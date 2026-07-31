// The Updates panel must never offer an action it cannot perform.
//
// The two failures worth pinning: offering "Download" in a browser (where
// there is no shell to download into), and offering it in an unsigned build
// (where macOS will refuse the install). Both would look fine in a screenshot.
import test from "node:test";
import assert from "node:assert/strict";
import { TONES, updateNotice } from "./updateNotice.js";

test("every branch returns a known tone and a non-empty title", () => {
  const cases = [
    { inShell: false, current: "0.1.0" },
    { inShell: true, current: "0.1.0" },
    { inShell: true, current: "0.1.0", update: { mode: "available", latest: "0.2.0" } },
    { inShell: true, current: "0.1.0", update: { mode: "unavailable", latest: "0.2.0" } },
    { inShell: true, current: "0.1.0", update: { mode: "downloading", percent: 10 } },
    { inShell: true, current: "0.1.0", update: { mode: "downloaded", latest: "0.2.0" } },
    { inShell: true, current: "0.1.0", update: { mode: "up-to-date" } },
    { inShell: true, current: "0.1.0", update: { mode: "failed", error: "x" } },
  ];
  for (const c of cases) {
    const n = updateNotice(c);
    assert.ok(TONES.includes(n.tone), `unknown tone ${n.tone}`);
    assert.ok(n.title && n.title.trim().length > 0, "every state needs a title");
    assert.ok(Array.isArray(n.actions));
  }
});

test("a browser is never offered an in-app download", () => {
  const n = updateNotice({ inShell: false, current: "0.1.0" });
  assert.deepEqual(n.actions, [],
    "there is no shell to install into — the only route is pip");
  assert.match(n.detail, /pip install --upgrade no-human/);
});

test("an available update offers download AND later, and downloads nothing yet", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "available", latest: "0.2.0" },
  });
  assert.deepEqual(n.actions, ["download", "later"],
    "the operator asked to be informed and then choose");
  assert.match(n.title, /0\.2\.0/);
  assert.match(n.detail, /Nothing downloads until you choose/);
});

test("an UNSIGNED build still announces the update but offers no install", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "unavailable", latest: "0.2.0",
              message: "not code-signed, download manually" },
  });
  assert.match(n.title, /0\.2\.0 is available/,
    "the user must still learn a new version exists");
  assert.equal(n.actions.includes("download"), false,
    "offering an install macOS will refuse is worse than offering none");
  assert.equal(n.tone, "warn");
  assert.match(n.detail, /code-signed|manually/i, "it must say why");
});

test("a downloaded update offers install and later, never an auto-restart", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "downloaded", latest: "0.2.0" },
  });
  assert.deepEqual(n.actions, ["install", "later"]);
  assert.equal(n.tone, "ok");
});

test("download progress reports the real percentage, never a fabricated one", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "downloading", percent: 37 },
  });
  assert.match(n.title, /37%/);
  // A missing percent must read as 0, not as NaN% or a guess.
  const missing = updateNotice({
    inShell: true, current: "0.1.0", update: { mode: "downloading" },
  });
  assert.match(missing.title, /0%/);
  assert.equal(missing.title.includes("NaN"), false);
});

test("an unknown version is rendered as unknown, never invented", () => {
  for (const current of [null, undefined, ""]) {
    const n = updateNotice({ inShell: true, current });
    assert.equal(n.version, "unknown");
    assert.equal(n.title.includes("null"), false);
    assert.equal(n.title.includes("undefined"), false);
  }
});

test("a failed check surfaces the reason and offers a retry", () => {
  const n = updateNotice({
    inShell: true, current: "0.1.0",
    update: { mode: "failed", error: "getaddrinfo ENOTFOUND" },
  });
  assert.equal(n.tone, "error");
  assert.match(n.detail, /ENOTFOUND/, "the real cause must reach the user");
  assert.deepEqual(n.actions, ["check"]);
});

test("the default state explains the policy rather than showing nothing", () => {
  const n = updateNotice({ inShell: true, current: "0.1.0" });
  assert.match(n.detail, /once a day/);
  assert.deepEqual(n.actions, ["check"]);
});

test("it never throws on a malformed payload", () => {
  for (const update of [null, {}, { mode: "nonsense" }, { mode: null }]) {
    assert.doesNotThrow(() => updateNotice({ inShell: true, current: "0.1.0", update }));
  }
  assert.doesNotThrow(() => updateNotice());
});
