import test from "node:test";
import assert from "node:assert/strict";
import { archiveBadge, archivedCount, visibleMemories } from "./memoryArchive.js";

// ── archiveBadge ─────────────────────────────────────────────────────────── //

test("a live row (archived falsy, no superseded_by) gets no badge", () => {
  assert.equal(archiveBadge({ id: "a", archived: 0, superseded_by: null }), null);
  assert.equal(archiveBadge({ id: "a" }), null);
});

test("a legacy row with archived NULL/undefined gets no badge", () => {
  assert.equal(archiveBadge({ id: "a", archived: null }), null);
  assert.equal(archiveBadge({ id: "a", archived: undefined }), null);
});

test("null/undefined item gets no badge", () => {
  assert.equal(archiveBadge(null), null);
  assert.equal(archiveBadge(undefined), null);
});

test("archived: 1 gets an Archived badge", () => {
  const b = archiveBadge({ id: "a", archived: 1, superseded_by: null });
  assert.equal(b.label, "Archived");
});

test("a superseded row gets Superseded, not Archived — superseded wins", () => {
  const b = archiveBadge({ id: "a", archived: 1, superseded_by: "deadbeef1234" });
  assert.equal(b.label, "Superseded");
  assert.match(b.title, /deadbeef/);
});

// ── visibleMemories ──────────────────────────────────────────────────────── //

test("archived rows are hidden by default", () => {
  const live = { id: "1", archived: 0 };
  const archived = { id: "2", archived: 1 };
  const out = visibleMemories([live, archived]);
  assert.deepEqual(out.map((i) => i.id), ["1"]);
});

test("showArchived: true reveals archived rows too", () => {
  const live = { id: "1", archived: 0 };
  const archived = { id: "2", archived: 1 };
  const out = visibleMemories([live, archived], { showArchived: true });
  assert.deepEqual(out.map((i) => i.id).sort(), ["1", "2"]);
});

test("a dismissed id is never shown, even with showArchived: true", () => {
  const archived = { id: "2", archived: 1 };
  const out = visibleMemories([archived], { showArchived: true, dismissedIds: ["2"] });
  assert.deepEqual(out, []);
});

test("empty/undefined input returns []", () => {
  assert.deepEqual(visibleMemories(undefined), []);
  assert.deepEqual(visibleMemories([]), []);
});

// ── archivedCount ────────────────────────────────────────────────────────── //

test("archivedCount counts only archived rows", () => {
  const items = [{ id: "1", archived: 0 }, { id: "2", archived: 1 }, { id: "3", archived: 1 }];
  assert.equal(archivedCount(items), 2);
});

test("archivedCount on empty/undefined input is 0", () => {
  assert.equal(archivedCount([]), 0);
  assert.equal(archivedCount(undefined), 0);
});
