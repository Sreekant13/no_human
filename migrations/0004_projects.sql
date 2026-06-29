-- Projects: a named group of repos (multi-repo tasks pick a project, not a repo).
-- repo_paths is a JSON array of absolute paths; primary_repo is the default
-- repo_path for single-repo tasks and the first repo the agent works in.
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  repo_paths TEXT NOT NULL DEFAULT '[]',   -- JSON array of absolute paths
  primary_repo TEXT,                       -- one of the repo_paths entries
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
