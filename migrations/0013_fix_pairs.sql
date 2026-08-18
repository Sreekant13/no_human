-- Fix pairs: "this machine hit this exact error before — and here is what
-- overcame it."
--
-- main-6cec2140 (2026-08-07) put 12 of 26 failures in the burn-then-quit
-- class: 25–194 turns of work, then an escalation on a task whose expected
-- outcome was delivery. The cheapest deterministic counter (measured at scale
-- by deja-vu on agent transcripts: 1,082 errors followed by a resolving
-- command, 81% never recurred) is to remember failure signatures and what
-- later overcame them, and to hand that history to the next attempt BEFORE it
-- retries blind or gives up.
--
-- ONE ROW PER (signature, task) FRICTION EVENT. A row is open friction while
-- `resolution` is NULL and becomes a usable fix pair when a later success on
-- the same task fills it. The signature is `core.bounds.error_signature` —
-- the SAME normalization the StuckDetector already uses, so "the same error"
-- means the same thing in both places.
--
-- EVIDENCE, NEVER AN INSTRUCTION. Consumers inject a resolved row as "this
-- signature was overcome before, in task X, this way — history, not a
-- guaranteed fix" (the deja-vu framing, kept verbatim on the prompt side).
-- Nothing here auto-applies anything.
CREATE TABLE IF NOT EXISTS fix_pairs (
  id               TEXT PRIMARY KEY,
  sig              TEXT NOT NULL,      -- bounds.error_signature(failure detail)
  repo_path        TEXT,               -- same-repo hits rank first at lookup
  error_excerpt    TEXT NOT NULL,      -- first ~300 chars of the failing detail
  task_id          TEXT NOT NULL,      -- the task that hit the friction
  resolution       TEXT,               -- NULL until the task later succeeds
  resolved_task_id TEXT,               -- task whose success filled `resolution`
  created_at       TEXT DEFAULT (datetime('now')),
  resolved_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_fix_pairs_sig ON fix_pairs(sig);
CREATE INDEX IF NOT EXISTS idx_fix_pairs_task ON fix_pairs(task_id);
