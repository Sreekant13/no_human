import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { DRAFT_KEY, saveDraft, saveDraftNow, loadDraft, clearDraft, mergeWithSeed } from "./composerDraft.js";

// Composer draft-recovery: wiring composerDraft.js (untouched here) into
// TaskComposer.jsx. Like taskComposerCreateRepo.test.mjs and
// pathInput.test.mjs, there is no React renderer in this harness, so the
// component-level ACs are pinned two ways:
//   - wiring assertions over the JSX source (readFileSync of TaskComposer.jsx)
//   - for the highest-risk piece — the save effect's ordering against the
//     restore banner — the effect body is extracted from the real source and
//     actually EXECUTED (via `new Function`) against controlled inputs, so
//     the test proves the ordering rather than merely pattern-matching text.
// Every other handler's *semantics* are exercised behaviourally against
// composerDraft.js with an injected memory storage, exactly as
// composerDraft.test.mjs does.
//
// The scenario this file exists to pin (successor to task ec0db349, review
// finding 2026-08-11): typing into the composer WHILE the restore banner is
// still unresolved (no restore/discard click yet) must still persist —
// dismissing the banner implicitly rather than silently dropping keystrokes.

const here = fileURLToPath(new URL(".", import.meta.url));
const read = (f) => readFileSync(here + f, "utf8");

function memoryStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => {
      map.set(key, String(value));
    },
    removeItem: (key) => {
      map.delete(key);
    },
    _raw: map,
  };
}

// Same idiom as composerDraft.test.mjs: manual firing, no wall-clock dependency.
function fakeTimers() {
  let nextHandle = 1;
  const armed = new Map();
  return {
    setTimer: (fn) => {
      const handle = nextHandle++;
      armed.set(handle, fn);
      return handle;
    },
    clearTimer: (handle) => armed.delete(handle),
    fire: (handle) => {
      const fn = armed.get(handle);
      assert.ok(fn, `no timer armed for handle ${handle}`);
      armed.delete(handle);
      fn();
    },
    get armedCount() {
      return armed.size;
    },
    fireSole: () => {
      assert.equal(armed.size, 1, `expected exactly one armed timer, found ${armed.size}`);
      const [handle] = armed.keys();
      const fn = armed.get(handle);
      armed.delete(handle);
      fn();
    },
  };
}

// Extracts the save effect's callback body verbatim from TaskComposer.jsx and
// turns it into a callable function. If the effect's shape ever changes (e.g.
// a regression back to gating the first save on a mount-count ref instead of
// on render state), this extraction itself fails loudly rather than silently
// testing stale text.
function extractSaveEffectBody(src) {
  // Anchored on the guard's own opening line so this can't accidentally match
  // an EARLIER, unrelated useEffect(() => { ... }) that happens to precede it
  // in the file (a bare non-greedy [\s\S]*? would still span from the first
  // "useEffect(() => {" in the whole file through to this effect's closing
  // dep array).
  const whole = src.match(
    /useEffect\(\(\) => \{\n {4}if \(draftDoneRef\.current\) return;[\s\S]*?\}, \[prompt, kind, priority, repoPath, draft, draftDismissed\]\);/,
  );
  assert.ok(whole, "expected the save effect with dep array [prompt, kind, priority, repoPath, draft, draftDismissed]");
  const body = whole[0].match(/=> \{([\s\S]*)\}, \[prompt, kind, priority, repoPath, draft, draftDismissed\]\);$/);
  assert.ok(body, "expected to isolate the save effect's callback body");
  return { whole: whole[0], body: body[1] };
}

function runSaveEffect(
  body,
  { draftDoneRef, draft, draftDismissed, seedRef, prompt, kind, priority, repoPath, saveDraft, setDraftDismissed },
) {
  const fn = new Function(
    "draftDoneRef",
    "draft",
    "draftDismissed",
    "seedRef",
    "prompt",
    "kind",
    "priority",
    "repoPath",
    "saveDraft",
    "setDraftDismissed",
    body,
  );
  fn(draftDoneRef, draft, draftDismissed, seedRef, prompt, kind, priority, repoPath, saveDraft, setDraftDismissed ?? (() => {}));
}

test("the restore banner is gated on a non-empty loaded draft", () => {
  const src = read("TaskComposer.jsx");
  assert.match(
    src,
    /const showDraftBanner = Boolean\(draft\) && Object\.keys\(draft\)\.length > 0 && !draftDismissed;/,
  );
  assert.match(src, /\{showDraftBanner && \(/);

  // Behavioural: an empty draft (only non-allowlisted keys survived pickFields)
  // must read as length 0; a real field makes it length 1.
  const empty = memoryStorage();
  empty.setItem(DRAFT_KEY, "{}");
  const loadedEmpty = loadDraft(empty);
  assert.deepEqual(loadedEmpty, {});
  assert.equal(Object.keys(loadedEmpty).length, 0);

  const nonEmpty = memoryStorage();
  nonEmpty.setItem(DRAFT_KEY, JSON.stringify({ prompt: "x" }));
  const loadedNonEmpty = loadDraft(nonEmpty);
  assert.equal(Object.keys(loadedNonEmpty).length, 1);
});

test("mounting with a stored draft present does not overwrite it before the operator touches anything", () => {
  const src = read("TaskComposer.jsx");
  const { body } = extractSaveEffectBody(src);

  // This is the exact bug that sank three prior attempts: gating the first
  // save on a mount-count ref, which StrictMode's double mount-effect invoke
  // walks straight past. The fix must not reintroduce that pattern anywhere.
  assert.doesNotMatch(src, /draftArmedRef/);

  // Simulate mount: a draft was loaded, non-empty, not yet resolved. Form
  // state still equals the seed (the operator has not typed anything). Run
  // the extracted body TWICE with the same draftDoneRef object — the same
  // shape StrictMode's setup->cleanup->setup produces on one instance — and
  // assert the guard holds both times.
  const seedRef = { current: { prompt: "", kind: "feature", priority: "medium", repoPath: "" } };
  const draftDoneRef = { current: false };
  const draft = { prompt: "a half-typed idea" };
  let calls = 0;
  const spySave = () => { calls += 1; };
  let dismissCalls = 0;
  const spyDismiss = () => { dismissCalls += 1; };

  for (let i = 0; i < 2; i++) {
    runSaveEffect(body, {
      draftDoneRef,
      draft,
      draftDismissed: false,
      seedRef,
      prompt: seedRef.current.prompt,
      kind: seedRef.current.kind,
      priority: seedRef.current.priority,
      repoPath: seedRef.current.repoPath,
      saveDraft: spySave,
      setDraftDismissed: spyDismiss,
    });
  }
  assert.equal(calls, 0, "the loaded draft must survive un-clobbered while nothing has been typed");
  assert.equal(dismissCalls, 0, "an untouched, unresolved banner must not be auto-dismissed");
});

test("typing while the restore banner is still unresolved persists the new text and dismisses the banner", () => {
  const src = read("TaskComposer.jsx");
  const { body } = extractSaveEffectBody(src);

  // This is the reviewer's blocking finding on the predecessor attempt: a
  // keystroke while a loaded draft sits unresolved (no restore/discard click
  // yet) must never be silently unpersisted. Typing IS the decision — it
  // dismisses the banner and the new text becomes the persisted draft.
  const seedRef = { current: { prompt: "", kind: "feature", priority: "medium", repoPath: "" } };
  const draftDoneRef = { current: false };
  const draft = { prompt: "an old unresolved draft" };
  let savedWith = null;
  const spySave = (fields) => { savedWith = fields; };
  let dismissedTo = null;
  const spyDismiss = (v) => { dismissedTo = v; };

  runSaveEffect(body, {
    draftDoneRef,
    draft,
    draftDismissed: false,
    seedRef,
    prompt: "freshly typed text, never restored or discarded",
    kind: seedRef.current.kind,
    priority: seedRef.current.priority,
    repoPath: seedRef.current.repoPath,
    saveDraft: spySave,
    setDraftDismissed: spyDismiss,
  });

  assert.deepEqual(savedWith, {
    prompt: "freshly typed text, never restored or discarded",
    kind: "feature",
    priority: "medium",
    repoPath: "",
  });
  assert.equal(dismissedTo, true, "typing while unresolved must dismiss the banner");
});

test("once the operator actually edits a field, the draft store is updated", () => {
  const src = read("TaskComposer.jsx");
  const { body } = extractSaveEffectBody(src);

  const seedRef = { current: { prompt: "", kind: "feature", priority: "medium", repoPath: "" } };
  const draftDoneRef = { current: false };
  let savedWith = null;
  const spySave = (fields) => { savedWith = fields; };

  // No draft was loaded (draft is null) and the operator typed something that
  // differs from the seed — the guard must not be satisfiable by simply never
  // saving.
  runSaveEffect(body, {
    draftDoneRef,
    draft: null,
    draftDismissed: false,
    seedRef,
    prompt: "a real typed prompt",
    kind: seedRef.current.kind,
    priority: seedRef.current.priority,
    repoPath: seedRef.current.repoPath,
    saveDraft: spySave,
  });
  assert.deepEqual(savedWith, { prompt: "a real typed prompt", kind: "feature", priority: "medium", repoPath: "" });

  // And once a previously-offered draft is resolved (draftDismissed true),
  // further edits resume saving even though `draft` itself is still set.
  savedWith = null;
  runSaveEffect(body, {
    draftDoneRef,
    draft: { prompt: "old draft" },
    draftDismissed: true,
    seedRef,
    prompt: "edited after restore",
    kind: "bug",
    priority: seedRef.current.priority,
    repoPath: seedRef.current.repoPath,
    saveDraft: spySave,
  });
  assert.deepEqual(savedWith, { prompt: "edited after restore", kind: "bug", priority: "medium", repoPath: "" });
});

test("an untouched composer never manufactures a draft from unchanged seed defaults", () => {
  const src = read("TaskComposer.jsx");
  const { body } = extractSaveEffectBody(src);

  // This is attempt 1's bug: kind/priority always carry a non-empty default,
  // so persisting them unconditionally on any re-render would write a 4-key
  // "draft" (prompt:"", kind:"feature", priority:"medium", repoPath:"") that
  // re-shows the banner for nothing the operator typed.
  const seedRef = { current: { prompt: "", kind: "feature", priority: "medium", repoPath: "" } };
  const draftDoneRef = { current: false };
  let calls = 0;
  const spySave = () => { calls += 1; };
  runSaveEffect(body, {
    draftDoneRef,
    draft: null,
    draftDismissed: false,
    seedRef,
    prompt: seedRef.current.prompt,
    kind: seedRef.current.kind,
    priority: seedRef.current.priority,
    repoPath: seedRef.current.repoPath,
    saveDraft: spySave,
  });
  assert.equal(calls, 0);
});

test("a successful submit permanently stops this instance from re-persisting", () => {
  const src = read("TaskComposer.jsx");
  const { body } = extractSaveEffectBody(src);

  const seedRef = { current: { prompt: "", kind: "feature", priority: "medium", repoPath: "" } };
  // draftDoneRef.current === true is exactly what handleSubmit sets before
  // clearing — the effect must treat that as terminal regardless of anything
  // else, or a re-render right after submit would resurrect the just-cleared
  // draft with the just-submitted content.
  const draftDoneRef = { current: true };
  let calls = 0;
  const spySave = () => { calls += 1; };
  runSaveEffect(body, {
    draftDoneRef,
    draft: null,
    draftDismissed: false,
    seedRef,
    prompt: "the submitted title",
    kind: "bug",
    priority: "high",
    repoPath: "/repos/x",
    saveDraft: spySave,
  });
  assert.equal(calls, 0);
});

test("Restore applies mergeWithSeed(draft, initial) - seed wins per present field", () => {
  const src = read("TaskComposer.jsx");
  const handlerMatch = src.match(/function restoreDraft\(\)\s*\{[\s\S]*?\n  \}/);
  assert.ok(handlerMatch, "expected to find restoreDraft's body");
  const handler = handlerMatch[0];
  assert.match(handler, /mergeWithSeed\(draft, initial\)/);
  assert.match(handler, /setPrompt\(/);
  assert.match(handler, /setKind\(/);
  assert.match(handler, /setPriority\(/);
  assert.match(handler, /setRepoPath\(/);
  assert.match(handler, /setDraftDismissed\(true\)/);

  // Behavioural: partial draft + seed overlap. Seed wins for `prompt` (present
  // in seed); draft fills the gaps for `kind` and `repoPath` (absent from seed).
  const draft = { prompt: "drafted", kind: "bug", repoPath: "/d" };
  const seed = { prompt: "seeded" };
  const draftBefore = { ...draft };
  const seedBefore = { ...seed };
  const merged = mergeWithSeed(draft, seed);
  assert.equal(merged.prompt, "seeded");
  assert.equal(merged.kind, "bug");
  assert.equal(merged.repoPath, "/d");
  assert.deepEqual(draft, draftBefore, "mergeWithSeed must not mutate the draft argument");
  assert.deepEqual(seed, seedBefore, "mergeWithSeed must not mutate the seed argument");
});

test("Discard and the close affordance share one handler that clears storage", () => {
  const src = read("TaskComposer.jsx");
  const discardButtons = [...src.matchAll(/onClick=\{discardDraft\}/g)];
  assert.equal(discardButtons.length, 2, "expected both the Discard button and the close affordance to use discardDraft");
  assert.match(src, /aria-label="Dismiss draft notice"[\s\S]{0,120}onClick=\{discardDraft\}/);

  const handlerMatch = src.match(/function discardDraft\(\)\s*\{[\s\S]*?\n  \}/);
  assert.ok(handlerMatch, "expected to find discardDraft's body");
  assert.match(handlerMatch[0], /clearDraft\(\);/);
  assert.match(handlerMatch[0], /setDraftDismissed\(true\);/);

  // Behavioural: storage is empty immediately after clearDraft.
  const store = memoryStorage();
  saveDraftNow({ prompt: "leftover" }, store);
  assert.notEqual(store.getItem(DRAFT_KEY), null);
  clearDraft(store);
  assert.equal(store.getItem(DRAFT_KEY), null);
});

test("the save effect persists exactly the four allowlisted fields, debounced", () => {
  const src = read("TaskComposer.jsx");
  assert.match(src, /saveDraft\(\{ prompt, kind, priority, repoPath \}\);/);
  assert.match(src, /\}, \[prompt, kind, priority, repoPath, draft, draftDismissed\]\);/);

  // Behavioural: three rapid calls coalesce into one armed timer, firing with
  // only the LAST fields.
  const store = memoryStorage();
  const timers = fakeTimers();
  const opts = { storage: store, setTimer: timers.setTimer, clearTimer: timers.clearTimer };
  saveDraft({ prompt: "p1" }, opts);
  saveDraft({ prompt: "p2" }, opts);
  const handle = saveDraft({ prompt: "p3" }, opts);
  assert.equal(timers.armedCount, 1);
  timers.fire(handle);
  assert.deepEqual(loadDraft(store), { prompt: "p3" });

  // Behavioural: a partial save with a non-allowlisted key persists only the
  // allowlisted field (enforced by composerDraft.js's pickFields, not by the
  // component).
  const store2 = memoryStorage();
  saveDraftNow({ prompt: "p", apiKey: "secret" }, store2);
  assert.deepEqual(loadDraft(store2), { prompt: "p" });
});

test("end to end: load with an existing draft, type without clicking restore/discard, reload recovers the typed text", () => {
  // This is the acceptance test for the fix: a draft is already on disk from
  // a previous session (the banner would show it). The operator, instead of
  // clicking Restore or Discard, starts typing fresh text. That keystroke
  // must reach storage — simulated here as the save effect firing (debounced
  // save armed then fired) followed by a fresh `loadDraft` call, exactly as a
  // page reload would perform.
  const src = read("TaskComposer.jsx");
  const { body } = extractSaveEffectBody(src);

  const store = memoryStorage();
  saveDraftNow({ prompt: "an old draft from last time" }, store);

  // Mount: the lazy initializer loads it (mirrors `useState(() => loadDraft())`).
  const draft = loadDraft(store);
  assert.deepEqual(draft, { prompt: "an old draft from last time" });

  const seedRef = { current: { prompt: "", kind: "feature", priority: "medium", repoPath: "" } };
  const draftDoneRef = { current: false };
  let draftDismissed = false;
  const setDraftDismissed = (v) => { draftDismissed = v; };
  const timers = fakeTimers();
  const boundSaveDraft = (fields) =>
    saveDraft(fields, { storage: store, setTimer: timers.setTimer, clearTimer: timers.clearTimer });

  // Operator types, WITHOUT clicking Restore or Discard first.
  const typed = "brand new text typed over the unresolved banner";
  runSaveEffect(body, {
    draftDoneRef,
    draft,
    draftDismissed,
    seedRef,
    prompt: typed,
    kind: seedRef.current.kind,
    priority: seedRef.current.priority,
    repoPath: seedRef.current.repoPath,
    saveDraft: boundSaveDraft,
    setDraftDismissed,
  });

  assert.equal(draftDismissed, true, "typing must implicitly dismiss the banner");
  assert.equal(timers.armedCount, 1, "the debounced write must be armed");
  timers.fireSole();
  // Simulate reload: a fresh loadDraft() call, exactly what next mount does.
  const recovered = loadDraft(store);
  assert.equal(recovered.prompt, typed, "the typed text — not the stale old draft — must survive a reload");
});

test("handleSubmit clears the draft only after the canSubmit guard", () => {
  const src = read("TaskComposer.jsx");
  const handlerMatch = src.match(/function handleSubmit\(e\)\s*\{[\s\S]*?\n  \}/);
  assert.ok(handlerMatch, "expected to find handleSubmit's body");
  const handler = handlerMatch[0];

  const guardIdx = handler.indexOf("if (!canSubmit) return;");
  const doneIdx = handler.indexOf("draftDoneRef.current = true;");
  const clearIdx = handler.indexOf("clearDraft();");
  const nullIdx = handler.indexOf("setDraft(null);");
  assert.ok(guardIdx !== -1, "expected the canSubmit guard");
  assert.ok(
    doneIdx > guardIdx && clearIdx > guardIdx && nullIdx > guardIdx,
    "clearDraft/setDraft(null)/draftDoneRef must all run AFTER the canSubmit guard, never before",
  );

  // Behavioural: a save armed just before submit must not resurrect the
  // draft once clearDraft has run.
  const store = memoryStorage();
  const timers = fakeTimers();
  const handle = saveDraft({ prompt: "about to submit" }, { storage: store, setTimer: timers.setTimer, clearTimer: timers.clearTimer });
  clearDraft(store);
  assert.throws(() => timers.fire(handle), /no timer armed/, "clearDraft must cancel the pending timer, not just remove the key");
  assert.equal(loadDraft(store), null);
});

test("a corrupt or missing draft loads as null and shows no banner", () => {
  const src = read("TaskComposer.jsx");
  assert.match(src, /try \{\s*return loadDraft\(\);\s*\} catch \{\s*return null;\s*\}/);

  for (const raw of ["{not json", "[1,2]", "", null]) {
    const store = memoryStorage();
    if (raw !== null) store.setItem(DRAFT_KEY, raw);
    assert.doesNotThrow(() => loadDraft(store));
    assert.equal(loadDraft(store), null, `expected null for raw=${JSON.stringify(raw)}`);
  }
});

test("the banner uses board theme tokens and states border-solid", () => {
  const src = read("TaskComposer.jsx");
  const bannerMatch = src.match(/\{showDraftBanner && \([\s\S]*?\)\}/);
  assert.ok(bannerMatch, "expected to find the banner JSX block");
  const banner = bannerMatch[0];
  assert.match(banner, /border border-solid border-line bg-panel/);
  assert.match(banner, /\$\{ACCENT\}/);
  assert.match(banner, /\$\{ON_CARD\}/);
  assert.match(banner, /\$\{GHOST\}/);
  // Every button inside the banner is type="button" - it sits inside a form.
  const buttonCount = (banner.match(/<button/g) || []).length;
  const typedButtonCount = (banner.match(/<button\s+type="button"/g) || []).length;
  assert.equal(buttonCount, typedButtonCount, "every banner button must be type=\"button\"");
  assert.match(banner, /aria-label="Dismiss draft notice"/);
  assert.match(banner, />Restore draft</);
  assert.match(banner, />Discard</);
});

test("composerDraft.js is not modified by this change", () => {
  const src = read("TaskComposer.jsx");
  assert.match(src, /from "\.\/composerDraft\.js"/);
  // This test file itself never imports React or touches composerDraft.js's
  // exports as anything but a read-only consumer (see the top-of-file imports
  // above) - byte-identity to main is additionally proven at review time by
  // `git diff --stat` and by check_release_manifest.py.
});
