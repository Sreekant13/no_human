// Presentation logic for the auto-discovered repository list served by
// GET /api/repos/discover. Kept out of the two surfaces that render it
// (Onboarding.jsx and TaskComposer.jsx) so the wording and the honesty rules
// below are asserted once, in discoveredRepos.test.mjs, rather than pinned by
// regex over JSX in two places.

export const DIRTY_TEXT = "uncommitted changes";

// Rows the picker shows, most-load-bearing first. `tone` maps to the caller's
// own styling: "warn" for anything that should give a user pause before
// pointing an agent at the repo, "muted" for context.
export function repoBadges(repo) {
  const r = repo || {};
  if (!r.is_git) return [{ key: "nogit", text: "not a git repo", tone: "warn" }];

  const out = [];
  if (r.branch) {
    out.push({
      key: "branch",
      text: r.detached ? `detached at ${r.branch}` : r.branch,
      tone: "neutral",
    });
  }
  if (r.dirty) {
    out.push({ key: "dirty", text: DIRTY_TEXT, tone: "warn" });
  } else if (r.dirty_scan === "partial") {
    // The server proved the tracked files clean but ran out of budget before
    // it could walk the working tree for untracked files. Saying "clean" here
    // would report a guess as a reading.
    out.push({
      key: "scan",
      text: "no tracked edits, untracked files not checked",
      tone: "muted",
    });
  } else if (r.dirty_scan === "unavailable") {
    out.push({ key: "scan", text: "status unavailable", tone: "muted" });
  } else if (r.dirty_scan === "unknown") {
    out.push({ key: "scan", text: "branch and status not checked", tone: "muted" });
  }
  if (r.ecosystem) out.push({ key: "eco", text: r.ecosystem, tone: "muted" });
  return out;
}

// One line under the list: the cap note, an empty-state, a warning about scan
// roots that were refused, or the fact that the search ran out of time.
// "" means there is nothing to say.
export function discoveryMessage(res) {
  if (!res) return "";
  const scanned = res.roots_scanned || [];
  const refused = res.roots_refused || [];
  const repos = res.repos || [];

  if (res.capped && res.note) return res.note;

  const parts = [];
  if (repos.length === 0) {
    // NAME the searched roots, never just count them. An operator with a
    // machine full of repos read "Searched 1 folder and found no repositories"
    // and concluded the page was broken — the folder was a sandbox home their
    // repos were never under, which the path itself would have shown at a
    // glance (2026-08-09). A count hides exactly the fact that matters.
    parts.push(
      scanned.length === 0
        ? "No standard clone folders found under your home directory - type a repository path instead."
        : `Searched ${scanned.join(", ")} and found no repositories - type a repository path instead.`,
    );
  }
  if (res.walk_truncated) {
    // A short list that LOOKS complete is the failure mode here: the user
    // concludes the repo is not there and stops looking.
    parts.push(
      "The search stopped early, so some folders were not reached - type a repository path to use one of them.",
    );
  }
  if (refused.length) {
    // Not always operator-configured: a conventional root can be a symlink out
    // of home (`~/Code -> /Volumes/BigDisk`), and the wording has to cover it.
    parts.push(
      `Skipped ${refused.length} scan ${refused.length === 1 ? "root" : "roots"} pointing outside your home directory.`,
    );
  }
  return parts.join(" ");
}

// The older single-root scan (POST /api/onboarding/repos/detect) returns only
// path/name/ecosystem. Widen its rows to the discovery shape so one renderer
// serves both surfaces - without inventing a branch or a clean working tree
// that scan never measured. `dirty_scan: "unknown"` is what makes the gap
// visible on the row instead of reading as "clean".
export function fromDetectedRepo(r) {
  return {
    path: r.path,
    name: r.name,
    ecosystem: r.ecosystem || "",
    is_git: true,
    branch: "",
    detached: false,
    dirty: false,
    dirty_scan: "unknown",
  };
}

export function filterRepos(repos, query) {
  const list = Array.isArray(repos) ? repos : [];
  const q = (query || "").trim().toLowerCase();
  if (!q) return list;
  return list.filter(
    (r) =>
      (r.name || "").toLowerCase().includes(q) ||
      (r.path || "").toLowerCase().includes(q),
  );
}
