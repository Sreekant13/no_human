-- 1.4 (Playbooks): a reusable, operator-authored playbook for a recurring
-- task shape (migration, Stripe integration, data ingestion, …). Injected into
-- the coder prompt ONLY when its trigger matches the task text (keyword
-- substring, like knowledge triggers), so it is inert until authored and never
-- pollutes unrelated tasks. The Postconditions ("done = these are TRUE") are the
-- sleeper lever on merged-PR quality; Forbidden reinforces the never-* rails;
-- Required-from-user is gathered up front to cut clarification loops.
CREATE TABLE IF NOT EXISTS playbooks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  trigger_keywords TEXT,               -- JSON array of keywords; NULL/[] = never auto-matches
  procedure TEXT,                      -- step-by-step approach
  postconditions TEXT,                 -- JSON array: what must be TRUE when done
  forbidden TEXT,                      -- JSON array: hard-stop actions
  required_from_user TEXT,             -- JSON array: gather these up front
  project TEXT,                        -- NULL = global; else scoped to a repo path
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
