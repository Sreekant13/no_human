-- 0010: PR OUTCOME telemetry.
--
-- WHY THIS TABLE EXISTS. The autonomy metric counted a task as a success once
-- it reached AWAITING_APPROVAL or DONE — i.e. once a PR EXISTED. Nothing ever
-- asked what happened to that PR afterwards, so "success" and "opened a pull
-- request" were the same number wearing two names. This table records the
-- forge's answer to the question the old metric never asked.
--
-- WHAT IT DOES NOT RECORD, deliberately: any judgement of quality. There is no
-- score column and there must never be one — an evidence-based pass/fail gate
-- is the project's review mechanism and a numeric self-score is forbidden.
-- `attributes` is the forward slot for later, MEASURED facts (a cost figure, a
-- reviewer verdict id, a revert SHA) so adding them needs no second migration
-- and no rewrite of this one.
--
-- GRAIN is (task_id, pr_url), not task_id: a multi-repo task opens one PR per
-- repo (see core/multi_repo.py) and each has its own fate. Callers that need a
-- per-task answer roll the rows up through `vcs.pr_outcome.rollup`, which is
-- the ONE place that rule is written.
CREATE TABLE IF NOT EXISTS pr_outcomes (
  task_id      TEXT NOT NULL,
  pr_url       TEXT NOT NULL,
  -- Forge identifiers, parsed once at record time by vcs.pr_watcher.parse_pr_url
  -- so a later refresh never has to re-guess them. "" / NULL where the remote
  -- is a local bare repo (the bench sandbox), which has no forge at all.
  forge        TEXT NOT NULL DEFAULT '',   -- github | gitlab | local | ''
  forge_host   TEXT NOT NULL DEFAULT '',   -- github.com, a GHE host, ...
  repo_slug    TEXT NOT NULL DEFAULT '',   -- owner/repo (URL-encoded for GitLab)
  pr_number    INTEGER,
  -- merged | closed_unmerged | open | unknown. DEFAULT and fail-state are both
  -- 'unknown': an outcome nobody could determine must never read as a success.
  outcome      TEXT NOT NULL DEFAULT 'unknown',
  -- WHY the outcome says what it says, as a short machine-readable token
  -- (vcs.pr_outcome.EVIDENCE_*). A verdict with no evidence cannot be audited,
  -- and two of these tokens ('content_on_base', 'forge_closed_ship_unverified')
  -- exist precisely because a forge "CLOSED" is ambiguous in this repo.
  outcome_evidence TEXT NOT NULL DEFAULT '',
  -- pass | fail | pending | unknown, from the PR head's checks. Empty check
  -- list (no gh, no CI configured) is 'unknown', never 'pass'.
  ci_status    TEXT NOT NULL DEFAULT 'unknown',
  -- live      — observed against the forge (or recorded at PR-open time).
  -- backfill  — reconstructed after the fact for a historical row. Marked so a
  --             reconstruction can never be read as a live measurement.
  observed_source TEXT NOT NULL DEFAULT 'live',
  opened_at    TEXT,   -- when no_human opened the PR (ISO-8601)
  checked_at   TEXT,   -- when `outcome` was last refreshed against the forge
  attributes   TEXT NOT NULL DEFAULT '{}',  -- JSON; forward slot, see above
  PRIMARY KEY (task_id, pr_url)
);
CREATE INDEX IF NOT EXISTS idx_pr_outcomes_outcome ON pr_outcomes(outcome);
