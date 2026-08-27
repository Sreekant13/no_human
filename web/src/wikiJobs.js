// Pure reducer for the wizard's background wiki-generation jobs. Extracted from
// Onboarding.jsx so the polling logic is unit-testable without a browser.
//
// A job travels queued → running → done | failed. The wizard polls while it is
// not terminal and folds each row into a per-repo map keyed by repo path.

// Keep polling only until the job reaches a terminal state.
export function shouldPoll(job) {
  return !!job && (job.status === "queued" || job.status === "running");
}

// Fold one job row into the per-repo map. The POST response is `{job_id}` with
// no status (reads as "queued"); the GET response is the full row
// (`{id, status, error, files, ...}`). Missing fields fall back to the previous
// value so a poll never erases what an earlier response established.
export function nextJobState(state, repoPath, row) {
  const prev = state[repoPath] || {};
  return {
    ...state,
    [repoPath]: {
      jobId: row.job_id ?? row.id ?? prev.jobId ?? null,
      status: row.status ?? prev.status ?? "queued",
      error: row.error ?? null,
      files: row.files ?? prev.files ?? null,
    },
  };
}
