-- Persisted task events: the scheduler's in-memory per-task event log is lost
-- on server restart. This table stores a durable copy so completed tasks
-- retain their Activity/System tab history after a restart.
CREATE TABLE IF NOT EXISTS task_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  ts REAL NOT NULL,
  data TEXT NOT NULL   -- JSON-encoded event dict (kind, text, tool_name, ...)
);
CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, ts);
