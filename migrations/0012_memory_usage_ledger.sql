-- Memory lifecycle A: the injection -> outcome ledger.
--
-- `memories.last_used_at` (S2) already answers "was this rule ever fetched
-- into a prompt", but nothing joins that injection to what the task it rode
-- along with actually did — so usefulness was unmeasurable: 33 of 561 rows
-- had ever been stamped, and even a stamped row could not say whether the
-- tasks it was injected into tended to succeed or fail.
--
-- ONE ROW PER INJECTION, not an aggregate on `memories`. `Orchestrator.
-- _load_active_memories` (the one chokepoint, S2/W3.4) can inject the same
-- memory into many tasks, and a later terminal-state handler needs to find
-- and fill exactly the rows for ONE task without recomputing which memories
-- that task used — an append-only ledger gives it a `task_id` to filter on;
-- a running total on `memories` alone could not.
--
-- `task_outcome` starts NULL and is filled by a SEPARATE terminal handler
-- (`Orchestrator.run_task`'s finalizer), never at injection time: the
-- outcome of a task is not known until the task ends, and a resumed task's
-- earlier injections are exactly as valid a signal as its later ones — they
-- get the SAME final label, not a partial one written mid-flight.
--
-- CORRELATIONAL, NOT CAUSAL — read every count this table produces with that
-- label attached (`nh learnings --usage`, the Learnings/Rules UI rows). A
-- rule injected into a task that failed did not necessarily cause the
-- failure; it was merely present. This table records presence and outcome,
-- nothing about causation.
CREATE TABLE IF NOT EXISTS memory_uses (
  id           TEXT PRIMARY KEY,
  memory_id    TEXT NOT NULL REFERENCES memories(id),
  task_id      TEXT NOT NULL,
  -- Nullable: the implement-path injection (`_drive`, before its attempt
  -- loop starts) happens before that task's first `attempts` row exists, so
  -- there is genuinely no attempt to name yet. The review-path injection
  -- (`_review_pr`) DOES have one by the time it calls in, and records it.
  attempt_id   TEXT,
  injected_at  TEXT NOT NULL,
  task_outcome TEXT,
  created_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memory_uses_memory ON memory_uses(memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_uses_task ON memory_uses(task_id);
