-- 0009 (Phase 2C CI.6): the post-PR gate's failure event was renamed
-- `ci_gate_fail` -> `ci_gate_fail` in the vendor-neutral sweep. Migration 0006
-- created the FTS insert trigger keyed on the OLD kind, and `CREATE TRIGGER IF
-- NOT EXISTS` will NOT update the trigger on a database that already has it --
-- so a pre-existing install would silently stop FTS-indexing gate-failure
-- events. Drop and recreate the trigger with the new kind list. Idempotent:
-- this whole script runs on every connect (executescript). The one-time
-- backfill of already-stored `ci_gate_fail` events is handled by 0006's own
-- idempotent INSERT..SELECT (it re-runs each connect), so it is not repeated
-- here.
DROP TRIGGER IF EXISTS task_events_fts_insert;
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
