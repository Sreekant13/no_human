// True only when there has never been a single task filed: nothing in any
// board lane AND nothing in the done/failed outcomes. Callers must only pass
// a real outcomeCount (0 counts as real) — an unresolved/unknown count must
// never be coerced to 0 here, or a board still loading its first fetch would
// flash this as first-run. See Board.jsx's `tasksLoaded` gate.
export function isFirstRun(tasks, outcomeCount) {
  return (!tasks || tasks.length === 0) && (!outcomeCount || outcomeCount === 0);
}
