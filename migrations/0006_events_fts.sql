-- W3.3: full-text recall over the failure/fix record. The intake and CI-fix
-- prompts should be able to say "a similar failure was fixed in task X" —
-- LIKE over 4k+ JSON blobs cannot rank or tokenize. Plain FTS5 (content
-- stored — rows are short event texts) keyed by rowid = task_events.id;
-- the trigger keeps it current and the backfill is idempotent because this
-- script runs on every connect (rowid NOT IN dedupe needs stored content —
-- contentless FTS5 cannot answer it).
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(text);

CREATE TRIGGER IF NOT EXISTS task_events_fts_insert
AFTER INSERT ON task_events
WHEN json_extract(NEW.data, '$.kind') IN (
  'attempt_failed', 'escalated', 'pr_ci_red', 'ci_gate_fail', 'review',
  'blocked', 'tamper'
)
BEGIN
  INSERT INTO events_fts(rowid, text)
  VALUES (NEW.id, COALESCE(json_extract(NEW.data, '$.text'), ''));
END;

INSERT INTO events_fts(rowid, text)
SELECT e.id, COALESCE(json_extract(e.data, '$.text'), '')
FROM task_events e
WHERE json_extract(e.data, '$.kind') IN (
  'attempt_failed', 'escalated', 'pr_ci_red', 'ci_gate_fail', 'review',
  'blocked', 'tamper'
)
AND e.id NOT IN (SELECT rowid FROM events_fts);
