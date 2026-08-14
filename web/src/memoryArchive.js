// Rules/Skills UI: client-side badge + filter logic for archived/superseded
// rows (Memory lifecycle C part B). The server decides which rows are
// archived (the 45-day sweep, a supersede-on-confirm, a manual retire, or a
// restore) — this module only turns that state into a badge and a visibility
// filter, the same client-side-labelling idiom as `learningRetire.js`.
//
// Pure functions, `node --test`'d, same shape as learningRetire.js/learningCard.js.

// `archived`/`superseded_by` predate this feature on legacy rows as NULL,
// which `list_memories`'s own `archived IS NULL OR archived = 0` clause
// treats as "live" — this mirrors that exactly so the badge and the SQL
// filter never disagree about the same row.
export function archiveBadge(item) {
  if (!item) return null;
  if (item.superseded_by) {
    return { label: "Superseded", title: `superseded by ${String(item.superseded_by).slice(0, 8)}` };
  }
  if (item.archived) {
    return { label: "Archived" };
  }
  return null;
}

// Live rows always shown; archived rows only when showArchived; a dismissed
// id never shown regardless (client-side "not now" — writes nothing
// server-side, exactly retireCandidates's dismissal contract). Missing input -> [].
export function visibleMemories(items, { showArchived = false, dismissedIds = [] } = {}) {
  const dismissed = new Set(dismissedIds || []);
  return (items || [])
    .filter((it) => it && !dismissed.has(it.id))
    .filter((it) => showArchived || !it.archived);
}

// The "Show archived (N)" toggle label — counts only archived rows.
export function archivedCount(items) {
  return (items || []).filter((it) => it && it.archived).length;
}
