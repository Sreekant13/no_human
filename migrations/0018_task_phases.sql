-- 0018: PER-PHASE TIMELINE for a task's life.
--
-- WHY THIS TABLE EXISTS. Until now a task carried `created_at`, `updated_at`
-- and nothing in between: a user watching a task could not see which phase it
-- was in (intake/plan/code/test/review/pr), nor how long it actually RAN
-- versus how long it had merely existed (wall-clock includes parked time,
-- overnight waits, review queues). One row per phase entry gives the drawer a
-- timeline and lets `active_seconds` sum only the time a phase was open.
--
-- LIFECYCLE. `open_phase(task_id, attempt, phase)` inserts a row with
-- `started_at` and closes any still-open phase of that task first with
-- `outcome='superseded'`. `close_phase(task_id, outcome, reason)` stamps
-- `ended_at`/`outcome`/`reason` on the currently-open row. A parked task has
-- NO open phase (parking closes it), so parked time never lands inside a phase
-- row and `active_seconds` — Σ(ended_at−started_at), open rows counted to now —
-- excludes it by construction.
--
-- The orchestrator writes these rows (D1.2, a separate PR); until that lands
-- the table is EMPTY and `phases_for` returns [] / `active_seconds` returns 0.
-- IF NOT EXISTS because `_migrate` executescripts every *.sql on every connect.
CREATE TABLE IF NOT EXISTS task_phases (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL,
  attempt    INTEGER NOT NULL,
  phase      TEXT NOT NULL CHECK(phase IN ('intake','plan','code','test','review','pr')),
  started_at TEXT NOT NULL,
  ended_at   TEXT,
  outcome    TEXT,
  reason     TEXT
);
CREATE INDEX IF NOT EXISTS task_phases_task ON task_phases(task_id, id);
