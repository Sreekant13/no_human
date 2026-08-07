import test from "node:test";
import assert from "node:assert/strict";
import {
  isImported, startableIssues, selectableIds, initialSelection, toggleId,
  selectAll, clearSelection, startIds, startIssues, selectionState, rowId,
  startLabel, multiStartNotice, queueProgress,
  initialQueue, startQueue, backlogQueueReducer, queueHead, queueNotice,
  queueRemaining,
} from "./backlogSelection.js";

// Real behavioural tests over the selection algebra — the Backlog page's
// checkboxes are a thin binding over these, so every rule that keeps a bulk
// start honest is provable here without a renderer.
//
// The stakes: a pre-ticked backlog is one click from starting an operator's
// whole sprint. The Forge panel shipped exactly that bug once.

const LIST = [
  { tracker: "jira", key: "PROJ-1", summary: "Fix retry loop", status: "To Do" },
  { tracker: "jira", key: "PROJ-2", summary: "Add index", status: "In Progress" },
  { tracker: "jira", key: "PROJ-3", summary: "Docs", status: "To Do", imported: { task_id: "t1", status: "done", count: 1 } },
  { tracker: "jira", key: "PROJ-4", summary: "Flaky test", status: "To Do" },
];

// A selection names a row by tracker AND key — see rowId. Written out here so
// the assertions below read as what the checkboxes actually hold.
const id = (k) => `jira:${k}`;

// ── Rule 1: nothing is ever pre-checked ────────────────────────────────────

test("initialSelection is empty — a freshly loaded backlog has nothing ticked", () => {
  assert.deepEqual(initialSelection(), []);
});

test("a freshly loaded backlog starts NOTHING, however many tickets it holds", () => {
  // The mutation this pins: seeding the selection from the list (e.g.
  // `selectableIds(issues)`) would make this array non-empty and one click
  // would start three tasks.
  assert.deepEqual(startIds(initialSelection(), LIST), []);
  assert.deepEqual(startIssues(initialSelection(), LIST), []);
  assert.equal(startLabel(startIds(initialSelection(), LIST).length), "Start tasks");
  assert.equal(selectionState(initialSelection(), LIST), "none");
});

test("initialSelection returns a fresh array each call — no shared mutable seed", () => {
  const a = initialSelection();
  a.push("PROJ-1");
  assert.deepEqual(initialSelection(), []);
});

// ── Rule 2: select all / clear ─────────────────────────────────────────────

test("selectAll picks every STARTABLE ticket, in list order", () => {
  assert.deepEqual(selectAll(LIST), [id("PROJ-1"), id("PROJ-2"), id("PROJ-4")]);
});

test("selectAll never sweeps in an already-imported ticket", () => {
  assert.ok(!selectAll(LIST).includes(id("PROJ-3")));
  assert.deepEqual(startIds(selectAll(LIST), LIST), [id("PROJ-1"), id("PROJ-2"), id("PROJ-4")]);
});

test("clearSelection empties a full selection", () => {
  const all = selectAll(LIST);
  assert.equal(all.length, 3);
  assert.deepEqual(clearSelection(), []);
  assert.deepEqual(startIds(clearSelection(), LIST), []);
});

test("selectionState reports none / some / all / empty distinctly", () => {
  assert.equal(selectionState([], LIST), "none");
  assert.equal(selectionState([id("PROJ-1")], LIST), "some");
  assert.equal(selectionState(selectAll(LIST), LIST), "all");
  // A list with nothing startable is neither "none" nor "all" — Select all
  // would be a no-op control that looks live.
  assert.equal(selectionState([], [LIST[2]]), "empty");
  assert.equal(selectionState([], []), "empty");
});

test("selecting every startable row reads as 'all' even though an imported row is unticked", () => {
  // The mutation this pins: comparing against issues.length instead of the
  // startable count would leave Select all permanently enabled.
  assert.equal(selectionState([id("PROJ-1"), id("PROJ-2"), id("PROJ-4")], LIST), "all");
});

// ── toggling ──────────────────────────────────────────────────────────────

test("toggleId adds then removes, and is stable under repetition", () => {
  let sel = toggleId([], id("PROJ-1"), LIST);
  assert.deepEqual(sel, [id("PROJ-1")]);
  sel = toggleId(sel, id("PROJ-2"), LIST);
  assert.deepEqual(sel, [id("PROJ-1"), id("PROJ-2")]);
  sel = toggleId(sel, id("PROJ-1"), LIST);
  assert.deepEqual(sel, [id("PROJ-2")]);
});

test("toggleId refuses to add an imported ticket, and refuses one that isn't listed", () => {
  assert.deepEqual(toggleId([], id("PROJ-3"), LIST), []);
  assert.deepEqual(toggleId([], id("NOPE-9"), LIST), []);
  // Removing always works, even for an id the current list would refuse to
  // add — otherwise a stale tick could never be cleared.
  assert.deepEqual(toggleId([id("NOPE-9")], id("NOPE-9"), LIST), []);
});

// ── Rule 3: a stale selection cannot smuggle an unlisted key ───────────────

test("a key that is no longer listed is dropped from the start set", () => {
  // The ticket was closed in Jira, or the operator narrowed the search after
  // ticking it. Either way it is not on screen, so it must not be started.
  const stale = [id("PROJ-1"), id("GONE-42")];
  assert.deepEqual(startIds(stale, LIST), [id("PROJ-1")]);
  assert.deepEqual(startIssues(stale, LIST).map((i) => i.key), ["PROJ-1"]);
  // …and the count the operator reads is the count that gets started.
  assert.equal(startLabel(startIds(stale, LIST).length), "Start 1 task");
});

test("the whole selection going stale starts nothing at all", () => {
  assert.deepEqual(startIds([id("GONE-1"), id("GONE-2")], LIST), []);
  assert.equal(selectionState([id("GONE-1"), id("GONE-2")], LIST), "none");
});

test("start order follows the LIST, not the click order", () => {
  // Selected bottom-up; the queue still drains top-down, matching what is on
  // screen — a queue that runs in click order is unreadable next to the list.
  assert.deepEqual(startIds([id("PROJ-4"), id("PROJ-1")], LIST), [id("PROJ-1"), id("PROJ-4")]);
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
  assert.deepEqual(startIds([id("PROJ-1"), id("PROJ-3")], LIST), [id("PROJ-1")]);
  assert.deepEqual(startIssues([id("PROJ-1"), id("PROJ-3")], LIST).map((i) => i.key), ["PROJ-1"]);
});

test("a ticket that BECAME imported between renders drops out of an existing selection", () => {
  const before = [id("PROJ-1"), id("PROJ-2")];
  const after = LIST.map((i) => (i.key === "PROJ-2"
    ? { ...i, imported: { task_id: "t9", status: "implementing", count: 1 } } : i));
  assert.deepEqual(startIds(before, LIST), [id("PROJ-1"), id("PROJ-2")]);
  assert.deepEqual(startIds(before, after), [id("PROJ-1")]);
});

test("startableIssues / selectableIds drop rows with no key at all", () => {
  const junk = [{ summary: "no key" }, null, { key: "OK-1", summary: "fine" }];
  assert.deepEqual(selectableIds(junk), [id("OK-1")]);
  assert.equal(startableIssues(junk).length, 1);
});

// ── two trackers, one list: a key alone is not an identity ────────────────

test("rowId names the tracker as well as the key, defaulting to jira", () => {
  assert.equal(rowId({ tracker: "linear", key: "NO-1" }), "linear:NO-1");
  assert.equal(rowId({ tracker: "jira", key: "NO-1" }), "jira:NO-1");
  assert.equal(rowId({ key: "NO-1" }), "jira:NO-1", "a row with no tracker is the Jira row");
  assert.equal(rowId({}), null);
  assert.equal(rowId(null), null);
});

test("two trackers holding the SAME key are two different rows", () => {
  // Jira and Linear both mint PROJ-1-shaped keys. Keying the selection on the
  // key alone would tick both boxes at once and start two tasks from one
  // click — and the board agrees with rowId here: dedupe is on
  // (source, external_id), never external_id alone.
  const both = [
    { tracker: "jira", key: "NO-1", summary: "the Jira one" },
    { tracker: "linear", key: "NO-1", summary: "the Linear one" },
  ];
  assert.deepEqual(selectAll(both), ["jira:NO-1", "linear:NO-1"]);
  const onlyLinear = toggleId([], "linear:NO-1", both);
  assert.deepEqual(startIds(onlyLinear, both), ["linear:NO-1"]);
  assert.deepEqual(startIssues(onlyLinear, both).map((i) => i.summary), ["the Linear one"]);
  assert.equal(selectionState(onlyLinear, both), "some", "ticking one must not read as all");
});

test("an imported Jira ticket does not take its Linear namesake out of the start", () => {
  const both = [
    { tracker: "jira", key: "NO-1", imported: { task_id: "t1", status: "done", count: 1 } },
    { tracker: "linear", key: "NO-1" },
  ];
  assert.deepEqual(selectAll(both), ["linear:NO-1"]);
  assert.deepEqual(startIds(selectAll(both), both), ["linear:NO-1"]);
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
  assert.match(n, /scopes each one with you/,
    "the intake flow must be named as INTERACTIVE - a reader must not think N>1 is a silent batch");
  assert.match(n, /Cancelling one keeps the rest/,
    "the promise must match what cancelling does — it drops THIS ticket, not the run");
});

test("queueProgress locates the operator in the queue, and stays silent for a single start", () => {
  assert.equal(queueProgress(0, 1, "PROJ-1"), null);
  assert.equal(queueProgress(0, 3, "PROJ-1"), "Ticket 1 of 3 · PROJ-1");
  assert.equal(queueProgress(2, 3, "PROJ-4"), "Ticket 3 of 3 · PROJ-4");
  assert.equal(queueProgress(1, 2, null), "Ticket 2 of 2");
});

// ── the queue: what happens AFTER Start ───────────────────────────────────
//
// This is the half that had no coverage at all. It lived inline in App.jsx as
// a ref flipped between two branches of one `onClose` handler, and inverting
// that flag reduced "start 10 tickets" to "start 1, silently drop 9" with the
// whole suite still green. Every assertion below is written so that the
// corresponding one-character mutation in backlogQueueReducer turns it red.

const TEN = Array.from({ length: 10 }, (_, i) => ({ key: `NO-${i + 1}`, summary: `t${i + 1}` }));

const drain = (state, n, action = { type: "next" }) => {
  let s = state;
  for (let i = 0; i < n; i++) s = backlogQueueReducer(s, action);
  return s;
};

test("nothing is queued until Start — initialQueue is empty and has no head", () => {
  assert.deepEqual(initialQueue(), { queue: [], total: 0 });
  assert.equal(queueHead(initialQueue()), null);
  assert.equal(queueNotice(initialQueue()), null);
});

test("start queues EVERY picked ticket, in list order, and records the total", () => {
  const s = backlogQueueReducer(initialQueue(), { type: "start", issues: TEN });
  assert.equal(s.total, 10, "the total must be the number the operator picked");
  assert.deepEqual(s.queue.map((i) => i.key), TEN.map((i) => i.key));
  assert.equal(queueHead(s).key, "NO-1", "the head is the first picked ticket");
});

test("start drops keyless junk rather than queueing a ticket that cannot be fetched", () => {
  const s = startQueue([{ key: "NO-1" }, null, { summary: "no key" }, { key: "NO-2" }]);
  assert.deepEqual(s.queue.map((i) => i.key), ["NO-1", "NO-2"]);
  assert.equal(s.total, 2, "the total counts what will actually be started");
  assert.deepEqual(startQueue([]), initialQueue());
});

test("ten started tickets are ALL walked — one `next` per ticket, none dropped", () => {
  // The regression this exists for: an advance that fires once and then clears
  // starts 1 ticket and silently discards 9.
  let s = backlogQueueReducer(initialQueue(), { type: "start", issues: TEN });
  const seen = [];
  while (queueHead(s)) {
    seen.push(queueHead(s).key);
    s = backlogQueueReducer(s, { type: "next" });
  }
  assert.deepEqual(seen, TEN.map((i) => i.key), "every picked ticket must reach the intake flow");
  assert.equal(seen.length, 10);
  assert.deepEqual(s, initialQueue(), "the run is over and leaves no stale total behind");
});

test("`next` advances by exactly ONE ticket and keeps the total steady", () => {
  const s0 = startQueue(TEN);
  const s1 = backlogQueueReducer(s0, { type: "next" });
  assert.equal(s1.queue.length, 9, "exactly one ticket leaves the queue");
  assert.equal(s1.total, 10, "the total is the size of the RUN — it does not shrink with the queue");
  assert.equal(queueHead(s1).key, "NO-2");
  assert.equal(queueHead(s0).key, "NO-1", "the previous state is not mutated in place");
});

test("cancelling ticket 2 of 10 keeps the other 8 — one Escape is not a run-wide discard", () => {
  // The P0-3 defect verbatim: `onClose` fires on Escape (TaskComposer binds
  // useEscapeKey(onClose)), and it used to clear the queue AND the total. The
  // Backlog page has already cleared its checkboxes by then, so those 8 tickets
  // were unrecoverable without re-ticking every one from memory.
  const onTicket2 = backlogQueueReducer(startQueue(TEN), { type: "next" });
  const cancelled = backlogQueueReducer(onTicket2, { type: "next" });
  assert.equal(cancelled.queue.length, 8, "the 8 tickets behind the cancelled one survive");
  assert.equal(queueHead(cancelled).key, "NO-3");
  assert.deepEqual(cancelled.queue.map((i) => i.key),
    ["NO-3", "NO-4", "NO-5", "NO-6", "NO-7", "NO-8", "NO-9", "NO-10"]);
});

test("created and cancelled are the SAME transition — the queue does not care which happened", () => {
  // Deliberate: they were two branches, and the difference between them was
  // where nine tickets went missing. Whichever it was, this ticket is done
  // with and the ones behind it are not.
  const s = startQueue(TEN);
  assert.deepEqual(
    backlogQueueReducer(s, { type: "next" }),
    backlogQueueReducer(s, { type: "next" }),
  );
});

test("`stop` is the ONLY way to lose the rest of the run, and it loses all of it", () => {
  const s = drain(startQueue(TEN), 3);
  assert.equal(s.queue.length, 7);
  assert.deepEqual(backlogQueueReducer(s, { type: "stop" }), initialQueue());
});

test("an unknown action leaves the queue exactly as it was", () => {
  const s = startQueue(TEN);
  assert.equal(backlogQueueReducer(s, { type: "wat" }), s);
  assert.equal(backlogQueueReducer(s, undefined), s);
  assert.deepEqual(backlogQueueReducer(undefined, { type: "next" }), initialQueue());
});

test("the position readout follows the head through the whole run", () => {
  let s = startQueue(TEN);
  assert.equal(queueNotice(s), "Ticket 1 of 10 · NO-1");
  s = backlogQueueReducer(s, { type: "next" });
  assert.equal(queueNotice(s), "Ticket 2 of 10 · NO-2");
  s = drain(s, 7);
  assert.equal(queueNotice(s), "Ticket 9 of 10 · NO-9");
  s = backlogQueueReducer(s, { type: "next" });
  assert.equal(queueNotice(s), "Ticket 10 of 10 · NO-10");
  assert.equal(queueNotice(backlogQueueReducer(s, { type: "next" })), null, "no readout once the run is over");
});

test("a single-ticket start shows no queue readout and no stop button", () => {
  const s = startQueue([{ key: "NO-1" }]);
  assert.equal(queueNotice(s), null, "'Ticket 1 of 1' is noise, not information");
  assert.equal(queueRemaining(s), 0, "there is no 'rest' to stop");
});

test("queueRemaining names how many tickets `stop` would discard — never the head itself", () => {
  const s = startQueue(TEN);
  assert.equal(queueRemaining(s), 9, "the ticket on screen is not part of 'the rest'");
  assert.equal(queueRemaining(drain(s, 8)), 1);
  assert.equal(queueRemaining(drain(s, 9)), 0, "on the last ticket there is nothing left to stop");
  assert.equal(queueRemaining(initialQueue()), 0);
});
