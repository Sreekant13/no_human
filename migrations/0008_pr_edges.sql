-- 2.2 Stacked-PR ordered merge: a dependency DAG over PRs so a chain of
-- dependent PRs can be merged in the correct order (a child PR must not merge
-- before its parent, or it merges code that references an un-merged base).
-- The AGENT never merges: it opens the PRs and stops. This table records the
-- ORDER; a human executes the merges via `nh merge-stack`.
-- A row means: child_pr depends on parent_pr (parent must merge first).
CREATE TABLE IF NOT EXISTS pr_edges (
  child_pr TEXT NOT NULL,              -- PR/MR URL or ref that depends on the parent
  parent_pr TEXT NOT NULL,             -- PR/MR URL or ref that must merge first
  project TEXT,                        -- optional repo path scope
  created_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (child_pr, parent_pr)
);
