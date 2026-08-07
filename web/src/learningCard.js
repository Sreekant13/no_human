// What a learning-queue card must say before a human clicks Confirm.
//
// `GET /api/learnings` returns every column of the row — origin, project,
// project_scope, evidence — and the web card rendered four of them (id, type,
// title, content) and dropped the rest on the floor. The CLI prints all of it,
// so the two surfaces asked the operator to make the same decision with
// different facts in front of them.
//
// The one that matters is BLAST RADIUS. A memory with no project and no
// project_scope is GLOBAL: confirming it injects it into every project's rules,
// forever, and `nh learnings` says so in yellow at the moment of deciding. The
// web card said nothing at all, so a one-click confirm of a global rule looked
// exactly like a one-click confirm of a rule scoped to one repo.
//
// Pure functions, `node --test`'d, same shape as learningGroups.js.

// A learning's blast radius. `kind` is "global" or "project"; `label` is what
// the card shows; `detail` is the secondary line (checkout path / scope hash),
// or null when there is nothing honest to add.
//
// Mirrors `nh learnings` (cli/commands.py): a row is GLOBAL only when it has
// NEITHER key, because that is exactly what `Store.list_memories` matches with
// `project IS NULL AND project_scope IS NULL`. A row with a project_scope but
// no path is scoped by remote identity — it has a blast radius, it just cannot
// name a directory for it, and calling that "global" would be a false alarm on
// the one warning that has to stay believable.
export function learningScope(item) {
  const project = String((item && item.project) || "").trim();
  const scopeId = String((item && item.project_scope) || "").trim();
  if (!project && !scopeId) {
    return {
      kind: "global",
      label: "GLOBAL — applies to every project",
      detail: null,
    };
  }
  return {
    kind: "project",
    label: project || "(by remote identity)",
    // Truncated like the CLI's, so two checkouts of one repo are visibly ONE
    // scope without pasting 64 hex characters into a card.
    detail: scopeId ? scopeId.slice(0, 16) : null,
  };
}

// One line summarising a proposal's structured B3 evidence — what happened, in
// which task, citing the event — or null where the row genuinely does not
// record it. A port of `_learning_evidence_line`; the two must agree, because
// an operator who triages in the app and audits in the terminal is entitled to
// read the same sentence.
//
// Accepts the raw column (a JSON string) or an already-parsed object, and
// returns null rather than throwing on anything malformed: this is a caption,
// and a caption must never be able to blank a card.
export function learningEvidence(raw) {
  if (!raw) return null;
  let ev = raw;
  if (typeof raw === "string") {
    try {
      ev = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!ev || typeof ev !== "object" || Array.isArray(ev)) return null;
  const kind = String(ev.kind || "");
  if (kind === "supervisor_correction") {
    const tasks = (ev.task_ids || []).slice(0, 4).map((t) => String(t).slice(0, 8));
    return (
      `supervisor correction x${ev.count ?? "?"}` +
      (tasks.length ? ` · task(s) ${tasks.join(", ")}` : "")
    );
  }
  if (kind === "review_finding") {
    const n = (ev.findings || []).length;
    return (
      `review finding · task ${String(ev.task_id || "?").slice(0, 8)}` +
      ` · attempt ${ev.attempt ?? "?"}` +
      ` · round ${ev.review_round || "?"}` +
      (n ? ` · ${n} finding(s)` : "")
    );
  }
  if (kind === "task_outcome") {
    return `task outcome · task ${String(ev.task_id || "?").slice(0, 8)} · ${ev.status || "?"}`;
  }
  const what = String(ev.what || "");
  if (!kind && !what) return null;
  return `${kind || "recorded"}: ${what}`.slice(0, 120);
}

// WHICH PRODUCER proposed this, or null where the row predates the column.
// NULL is printed as nothing rather than guessed at — `origin` was added with
// no default precisely so a pre-existing row could say "unknown" honestly, and
// a card that renders "unknown" as "review" undoes that.
export function learningOrigin(item) {
  const o = String((item && item.origin) || "").trim();
  return o || null;
}

// Free-text filter over a learning list. 329 pending rows do not triage one
// click at a time and they do not triage by scrolling either.
//
// Matches title, content, type, origin and project — every field the card can
// show — so what the operator reads is what they can search. Case-insensitive,
// substring, and every whitespace-separated term must match (AND), which is how
// a two-word query narrows instead of widening. An empty query returns the
// input unchanged rather than an empty list.
export function filterLearnings(items, query) {
  const terms = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return items || [];
  return (items || []).filter((it) => {
    const hay = [it.title, it.content, it.type, it.origin, it.project]
      .map((v) => String(v || ""))
      .join(" ")
      .toLowerCase();
    return terms.every((t) => hay.includes(t));
  });
}

// The most learnings one "Confirm selected" may send.
//
// Each confirm is its own request and the store serialises writes, so a
// thousand-row batch is a thousand queued writes against the same connection
// the running task needs. A cap keeps the click bounded and the progress
// readout meaningful; the operator can simply click again.
export const BULK_CONFIRM_CAP = 50;

// The ids a bulk confirm will actually send: the current selection, narrowed to
// what is VISIBLE, in display order, capped.
//
// Narrowing to the visible list is the safety property, not a convenience. A
// selection made under one filter survives in state when the filter changes, so
// without this "Confirm selected" could activate rules the operator cannot see
// on screen — which for a global rule is exactly the one-click blast radius
// this module exists to surface.
export function bulkConfirmIds(visibleItems, selectedIds, cap = BULK_CONFIRM_CAP) {
  const chosen = selectedIds instanceof Set ? selectedIds : new Set(selectedIds || []);
  return (visibleItems || [])
    .map((it) => it && it.id)
    .filter((id) => id && chosen.has(id))
    .slice(0, Math.max(0, cap));
}
