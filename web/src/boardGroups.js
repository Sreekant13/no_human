// U4: the FAILED lane is a graveyard — one stubborn task retried N ways
// buries the board under near-identical cards (live: 5 of 9 lifetime tasks
// share one title). Collapse same-title failed cards to the NEWEST one plus
// a count; the older ones stay reachable through it. Pure, node --test'd.

export function groupFailedByTitle(tasks) {
  const byTitle = new Map();
  for (const t of tasks) {
    const key = (t.title || "").trim() || t.id;
    const g = byTitle.get(key);
    if (!g) byTitle.set(key, { newest: t, older: [] });
    else if ((t.created_at || "") > (g.newest.created_at || "")) {
      g.older.push(g.newest);
      g.newest = t;
    } else {
      g.older.push(t);
    }
  }
  return [...byTitle.values()].map(({ newest, older }) => ({
    task: newest,
    collapsedCount: older.length,
    olderIds: older
      .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
      .map((t) => t.id),
  }));
}
