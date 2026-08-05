import test from "node:test";
import assert from "node:assert/strict";
import {
  isImported, startableIssues, selectableKeys, initialSelection, toggleKey,
  selectAll, clearSelection, startKeys, startIssues, selectionState,
  startLabel, multiStartNotice, queueProgress,
} from "./backlogSelection.js";

// Real behavioural tests over the selection algebra — the Backlog page's
// checkboxes are a thin binding over these, so every rule that keeps a bulk
// start honest is provable here without a renderer.
//
// The stakes: a pre-ticked backlog is one click from starting an operator's
// whole sprint. The Forge panel shipped exactly that bug once.

const LIST = [
  { key: "PROJ-1", summary: "Fix retry loop", status: "To Do" },
  { key: "PROJ-2", summary: "Add index", status: "In Progress" },
  { key: "PROJ-3", summary: "Docs", status: "To Do", imported: { task_id: "t1", status: "done", count: 1 } },
  { key: "PROJ-4", summary: "Flaky test", status: "To Do" },
];

// ── Rule 1: nothing is ever pre-checked ────────────────────────────────────

test("initialSelection is empty — a freshly loaded backlog has nothing ticked", () => {
  assert.deepEqual(initialSelection(), []);
});

test("a freshly loaded backlog starts NOTHING, however many tickets it holds", () => {
  // The mutation this pins: seeding the selection from the list (e.g.
  // `selectableKeys(issues)`) would make this array non-empty and one click
  // would start three tasks.
  assert.deepEqual(startKeys(initialSelection(), LIST), []);
  assert.deepEqual(startIssues(initialSelection(), LIST), []);
  assert.equal(startLabel(startKeys(initialSelection(), LIST).length), "Start tasks");
  assert.equal(selectionState(initialSelection(), LIST), "none");
});

test("initialSelection returns a fresh array each call — no shared mutable seed", () => {
  const a = initialSelection();
  a.push("PROJ-1");
  assert.deepEqual(initialSelection(), []);
});

// ── Rule 2: select all / clear ─────────────────────────────────────────────

test("selectAll picks every STARTABLE ticket, in list order", () => {
  assert.deepEqual(selectAll(LIST), ["PROJ-1", "PROJ-2", "PROJ-4"]);
});

test("selectAll never sweeps in an already-imported ticket", () => {
  assert.ok(!selectAll(LIST).includes("PROJ-3"));
  assert.deepEqual(startKeys(selectAll(LIST), LIST), ["PROJ-1", "PROJ-2", "PROJ-4"]);
});

test("clearSelection empties a full selection", () => {
  const all = selectAll(LIST);
  assert.equal(all.length, 3);
  assert.deepEqual(clearSelection(), []);
  assert.deepEqual(startKeys(clearSelection(), LIST), []);
});

test("selectionState reports none / some / all / empty distinctly", () => {
  assert.equal(selectionState([], LIST), "none");
  assert.equal(selectionState(["PROJ-1"], LIST), "some");
  assert.equal(selectionState(selectAll(LIST), LIST), "all");
  // A list with nothing startable is neither "none" nor "all" — Select all
  // would be a no-op control that looks live.
  assert.equal(selectionState([], [LIST[2]]), "empty");
  assert.equal(selectionState([], []), "empty");
});

test("selecting every startable row reads as 'all' even though an imported row is unticked", () => {
  // The mutation this pins: comparing against issues.length instead of the
  // startable count would leave Select all permanently enabled.
  assert.equal(selectionState(["PROJ-1", "PROJ-2", "PROJ-4"], LIST), "all");
});

// ── toggling ──────────────────────────────────────────────────────────────

test("toggleKey adds then removes, and is stable under repetition", () => {
  let sel = toggleKey([], "PROJ-1", LIST);
  assert.deepEqual(sel, ["PROJ-1"]);
  sel = toggleKey(sel, "PROJ-2", LIST);
  assert.deepEqual(sel, ["PROJ-1", "PROJ-2"]);
  sel = toggleKey(sel, "PROJ-1", LIST);
  assert.deepEqual(sel, ["PROJ-2"]);
});

test("toggleKey refuses to add an imported ticket, and refuses one that isn't listed", () => {
  assert.deepEqual(toggleKey([], "PROJ-3", LIST), []);
  assert.deepEqual(toggleKey([], "NOPE-9", LIST), []);
  // Removing always works, even for a key the current list would refuse to
  // add — otherwise a stale tick could never be cleared.
  assert.deepEqual(toggleKey(["NOPE-9"], "NOPE-9", LIST), []);
});

// ── Rule 3: a stale selection cannot smuggle an unlisted key ───────────────

test("a key that is no longer listed is dropped from the start set", () => {
  // The ticket was closed in Jira, or the operator narrowed the search after
  // ticking it. Either way it is not on screen, so it must not be started.
  const stale = ["PROJ-1", "GONE-42"];
  assert.deepEqual(startKeys(stale, LIST), ["PROJ-1"]);
  assert.deepEqual(startIssues(stale, LIST).map((i) => i.key), ["PROJ-1"]);
  // …and the count the operator reads is the count that gets started.
  assert.equal(startLabel(startKeys(stale, LIST).length), "Start 1 task");
});

test("the whole selection going stale starts nothing at all", () => {
  assert.deepEqual(startKeys(["GONE-1", "GONE-2"], LIST), []);
  assert.equal(selectionState(["GONE-1", "GONE-2"], LIST), "none");
});

test("start order follows the LIST, not the click order", () => {
  // Selected bottom-up; the queue still drains top-down, matching what is on
  // screen — a queue that runs in click order is unreadable next to the list.
  assert.deepEqual(startKeys(["PROJ-4", "PROJ-1"], LIST), ["PROJ-1", "PROJ-4"]);
});

// ── Rule 4: an imported ticket is never in the start set ───────────────────

test("isImported reads the endpoint's `imported` block", () => {
  assert.equal(isImported({ key: "A", imported: { task_id: "t", status: "done", count: 1 } }), true);
  assert.equal(isImported({ key: "A" }), false);
  assert.equal(isImported({ key: "A", imported: null }), false);
  assert.equal(isImported(undefined), false);
});

test("an imported ticket held in the selection is still excluded from the start", () => {
  // The defence in depth that matters: even if a tick survives a refresh that
  // turned the ticket into an imported one (it was started moments ago in
  // another tab), the poller's (source=jira, external_id) dedup is not relied
  // on to save us — no second task is requested in the first place.
  assert.deepEqual(startKeys(["PROJ-1", "PROJ-3"], LIST), ["PROJ-1"]);
  assert.deepEqual(startIssues(["PROJ-1", "PROJ-3"], LIST).map((i) => i.key), ["PROJ-1"]);
});

test("a ticket that BECAME imported between renders drops out of an existing selection", () => {
  const before = ["PROJ-1", "PROJ-2"];
  const after = LIST.map((i) => (i.key === "PROJ-2"
    ? { ...i, imported: { task_id: "t9", status: "implementing", count: 1 } } : i));
  assert.deepEqual(startKeys(before, LIST), ["PROJ-1", "PROJ-2"]);
  assert.deepEqual(startKeys(before, after), ["PROJ-1"]);
});

test("startableIssues / selectableKeys drop rows with no key at all", () => {
  const junk = [{ summary: "no key" }, null, { key: "OK-1", summary: "fine" }];
  assert.deepEqual(selectableKeys(junk), ["OK-1"]);
  assert.equal(startableIssues(junk).length, 1);
});

// ── the copy that has to be true ──────────────────────────────────────────

test("startLabel names the count and pluralises it", () => {
  assert.equal(startLabel(0), "Start tasks");
  assert.equal(startLabel(1), "Start 1 task");
  assert.equal(startLabel(2), "Start 2 tasks");
  assert.equal(startLabel(19), "Start 19 tasks");
});

test("multiStartNotice warns only when N > 1, and says what actually happens", () => {
  assert.equal(multiStartNotice(0), null);
  assert.equal(multiStartNotice(1), null);
  const n = multiStartNotice(3);
  assert.match(n, /^3 tickets/);
  assert.match(n, /one at a time/, "N>1 is a queue through the same flow, not a silent batch");
  assert.match(n, /five questions/, "the intake flow must be named — it is interactive");
  assert.match(n, /Cancelling stops the rest/);
});

test("queueProgress locates the operator in the queue, and stays silent for a single start", () => {
  assert.equal(queueProgress(0, 1, "PROJ-1"), null);
  assert.equal(queueProgress(0, 3, "PROJ-1"), "Ticket 1 of 3 · PROJ-1");
  assert.equal(queueProgress(2, 3, "PROJ-4"), "Ticket 3 of 3 · PROJ-4");
  assert.equal(queueProgress(1, 2, null), "Ticket 2 of 2");
});
