-- Verification receipts: what the coder session SUBMITTED TO THE SHELL to check
-- its own work, captured mechanically from the PostToolUse hook.
--
-- SUBMITTED, NOT EXECUTED. This header said "ACTUALLY executed" for one round
-- after the module and the rendered limits list had both been corrected. A row
-- records that a command LINE went to the shell and what came back; the check
-- named in that line may never have been reached (`agent/verification_receipts.
-- py` names ten driven shapes). Nothing in this table asserts otherwise, and
-- there is deliberately no column that could.
--
-- WHY A TABLE AND NOT A JSON COLUMN ON `attempts`.
-- `TaskStore.update_attempt` builds `UPDATE attempts SET <col> = :<col>` — a
-- whole-column REPLACE with no merge. Receipts arrive ONE AT A TIME while the
-- session runs, so a JSON column would need a read-modify-write on every
-- captured tool call, interleaved with every other writer of that row
-- (`test_results`, the token counters, `ci_status`, `pr_url`). Two writers
-- racing on the same row is exactly how a sibling branch lost data. An
-- append-only INSERT has no such window: a receipt cannot be clobbered by a
-- later update to an unrelated column, and the rows carry their own order.
--
-- WHY THERE IS NO `verdict` COLUMN. There was one - pass/fail/unknown, derived
-- from the exit status the harness reported - and six independent reviews failed
-- the branch on it, each through a different shell construct that hands back a
-- zero the checked program never earned. Of 292 verification commands mined from
-- real transcripts, only 6 (2.1%) could justify a PASS. The verdict is gone
-- rather than dormant: a row stores the command and the text that came back, and
-- the human reads `1 failed, 42 passed` for themselves.
--
-- The excerpt is stored ALREADY bounded and ALREADY redacted (see
-- `agent/verification_receipts.py`); nothing downstream is trusted to do it.
CREATE TABLE IF NOT EXISTS verification_receipts (
  id            TEXT PRIMARY KEY,
  attempt_id    TEXT NOT NULL REFERENCES attempts(id),
  seq           INTEGER NOT NULL,          -- order of submission within the attempt
  kind          TEXT NOT NULL,             -- test|lint|typecheck|build|http|e2e
  command       TEXT NOT NULL,             -- the command line AS RECORDED: redacted,
                                           -- and shortened in the middle past 400 chars
  output_excerpt TEXT NOT NULL DEFAULT '', -- what came back: bounded, redacted
  output_bytes  INTEGER NOT NULL DEFAULT 0,-- size of the ORIGINAL output, so truncation is stated in real numbers
  truncated     INTEGER NOT NULL DEFAULT 0,-- 0/1
  created_at    TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_verification_receipts_attempt
  ON verification_receipts(attempt_id, seq);
