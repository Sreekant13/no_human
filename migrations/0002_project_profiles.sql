-- Per-repo build/test/CI profile (ProjectProfile). The repo's .no_human/project.yml
-- is the human-confirmable source of truth; this table mirrors it for the daemon,
-- keyed by absolute repo path.
CREATE TABLE IF NOT EXISTS project_profiles (
  repo_path TEXT PRIMARY KEY,
  ecosystem TEXT,
  install_cmd TEXT,
  test_cmd TEXT,
  lint_cmd TEXT,
  confirmed INTEGER DEFAULT 0,
  data TEXT NOT NULL,                 -- full ProjectProfile JSON (ci, provenance, etc.)
  updated_at TEXT DEFAULT (datetime('now'))
);
