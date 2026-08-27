-- 0017: WIKI GENERATION JOBS.
--
-- WHY THIS TABLE EXISTS. Wiki generation was a foreground `await` inside the
-- onboarding endpoint: a bounded Agent SDK session that can take minutes. The
-- wizard blocked on it, and if the user navigated away (or the page unmounted,
-- or the server restarted) the result died with the request — the user saw a
-- spinner, then nothing. This table makes the generation a persisted job the
-- board can poll: POST returns 202 + a job id, the row records the outcome, and
-- the result survives wizard unmount and server restart.
--
-- LIFECYCLE. status is queued → running → done | failed. `error` carries the
-- failure reason (including docs_gen's "failed to parse wiki JSON …: <excerpt>"
-- so the user can see WHY). `files` is a JSON array of the written paths on a
-- done job. queued/running rows left behind by a restart are marked failed at
-- startup (wiki_jobs.resume_unfinished), mirroring the scheduler's orphan
-- recovery: a job nobody is running must not read as still in flight.
CREATE TABLE IF NOT EXISTS wiki_jobs (
  id          TEXT PRIMARY KEY,
  repo_path   TEXT NOT NULL,
  status      TEXT NOT NULL CHECK(status IN ('queued','running','done','failed')),
  error       TEXT,
  files       TEXT,
  created_at  TEXT NOT NULL,
  started_at  TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_wiki_jobs_status ON wiki_jobs(status);
