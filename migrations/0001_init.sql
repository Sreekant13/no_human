-- no_human schema (PLAN.md Part 9, SQLite flavor).
-- Phase 0 ships tasks + attempts. memories/sessions/notifications arrive in
-- later phases; their tables are created here so the store is forward-stable.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,                 -- uuid
  external_id TEXT,
  source TEXT NOT NULL,                -- freeform | tracker | jira | github | ...
  title TEXT NOT NULL,
  description TEXT,
  requirements TEXT,                   -- JSON array
  acceptance_criteria TEXT,            -- JSON array
  repo_path TEXT,                      -- local target repo
  status TEXT NOT NULL DEFAULT 'pending',
  -- pending|context|planning|implementing|reviewing|testing|awaiting_approval|
  -- blocked|awaiting_input|paused_quota|done|failed|escalated
  blocker TEXT,                        -- JSON: Part 22 Blocker record
  wake_check_at TEXT,                  -- next parked-task watcher re-eval (ISO)
  priority TEXT DEFAULT 'medium',
  context TEXT,                        -- JSON
  plan TEXT,                           -- JSON
  config TEXT,                         -- JSON snapshot
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_wake ON tasks(wake_check_at);

CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  attempt_number INTEGER NOT NULL,
  branch_name TEXT,
  commit_sha TEXT,
  pr_url TEXT,
  diff TEXT,
  review_checklist TEXT,               -- JSON: items + evidence + pass/fail
  review_passed INTEGER,               -- 0/1 (no numeric score)
  test_results TEXT,                   -- JSON {passed,failed,errors,tamper_flag}
  ci_pipeline_id TEXT,
  ci_pipeline_url TEXT,
  ci_status TEXT,
  failure_reason TEXT,
  turns_used INTEGER,
  tokens_used INTEGER,                 -- against subscription quota
  started_at TEXT DEFAULT (datetime('now')),
  completed_at TEXT,
  status TEXT DEFAULT 'in_progress'    -- in_progress|succeeded|failed
);

CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,                  -- rule|skill|fact|anti_pattern
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  file_path TEXT,
  tags TEXT,                           -- JSON array
  project TEXT,                        -- NULL = global
  source TEXT,                         -- user|proposed|confirmed
  confirmed INTEGER DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id),
  attempt_id TEXT REFERENCES attempts(id),
  summary TEXT,
  learnings TEXT,                      -- JSON
  turns INTEGER,
  tokens_used INTEGER,
  started_at TEXT DEFAULT (datetime('now')),
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
  id TEXT PRIMARY KEY,
  task_id TEXT REFERENCES tasks(id),
  channel TEXT NOT NULL,
  event TEXT NOT NULL,
  message TEXT NOT NULL,
  sent_at TEXT,
  acknowledged_at TEXT
);
