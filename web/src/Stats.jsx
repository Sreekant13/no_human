import { useEffect, useMemo, useState } from "react";
import { fetchMetrics, fetchRepos, fetchRepoUnderstanding, searchEvents } from "./api.js";
import { estimateCost, fmtTokens } from "./cost.js";
import { northStarTiles } from "./northStar.js";
import { profileRows, profileStatus } from "./repoView.js";
import { kindLabel, groupByTask } from "./searchView.js";

// ── Helpers ──────────────────────────────────────────────────────────────────

function fmtDuration(seconds) {
  if (seconds < 60) return "<1m";
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.round((seconds % 3600) / 60);
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
  }
  const d = Math.floor(seconds / 86400);
  const h = Math.round((seconds % 86400) / 3600);
  return h > 0 ? `${d}d ${h}h` : `${d}d`;
}

function parseTS(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : d;
}

// Compute all stats from the task list
function computeStats(tasks) {
  const now = new Date();
  const thirtyDaysAgo = new Date(now.getTime() - 30 * 86400 * 1000);
  const sevenDaysAgo = new Date(now.getTime() - 7 * 86400 * 1000);

  const doneTasks = tasks.filter(t => t.status === "done");
  // An operator-cancelled task ends in FAILED status but is not a capability
  // failure — keep it out of the failure count and the success-rate denominator.
  const cancelledTasks = tasks.filter(t => t.status === "failed" && t.cancelled);
  const failedTasks = tasks.filter(t => t.status === "failed" && !t.cancelled);

  // Tasks with valid timestamps
  const doneWithTimes = doneTasks
    .map(t => ({
      created: parseTS(t.created_at),
      finished: parseTS(t.updated_at),
      kind: t.kind || "feature",
    }))
    .filter(t => t.created && t.finished);

  // Completion durations in seconds
  const durations = doneWithTimes
    .map(t => (t.finished.getTime() - t.created.getTime()) / 1000)
    .filter(d => d > 0);

  // Average duration
  const avgDuration = durations.length > 0
    ? durations.reduce((a, b) => a + b, 0) / durations.length
    : 0;

  // Median duration
  const sortedDurations = [...durations].sort((a, b) => a - b);
  const medianDuration = sortedDurations.length > 0
    ? sortedDurations[Math.floor(sortedDurations.length / 2)]
    : 0;

  // Completed in last 30 days
  const last30 = doneWithTimes.filter(t => t.finished >= thirtyDaysAgo);

  // Completed in last 7 days
  const last7 = doneWithTimes.filter(t => t.finished >= sevenDaysAgo);

  // Average tasks per day
  let tasksPerDay = 0;
  if (doneWithTimes.length > 0) {
    const earliest = new Date(Math.min(...doneWithTimes.map(t => t.finished.getTime())));
    const spanDays = Math.max(1, (now.getTime() - earliest.getTime()) / 86400000);
    tasksPerDay = doneWithTimes.length / spanDays;
  }

  // Daily histogram for last 30 days (for sparkline)
  const dailyCounts = [];
  for (let i = 29; i >= 0; i--) {
    const dayStart = new Date(now.getTime() - (i + 1) * 86400000);
    const dayEnd = new Date(now.getTime() - i * 86400000);
    const count = doneWithTimes.filter(t => t.finished >= dayStart && t.finished < dayEnd).length;
    const label = dayStart.toLocaleDateString("en-US", { month: "short", day: "numeric" });
    dailyCounts.push({ label, count });
  }

  // Kind breakdown
  const kindMap = {};
  for (const t of doneWithTimes) {
    kindMap[t.kind] = (kindMap[t.kind] || 0) + 1;
  }
  const kindBreakdown = Object.entries(kindMap)
    .map(([kind, count]) => ({ kind, count }))
    .sort((a, b) => b.count - a.count);

  // Success rate
  const totalTerminal = doneTasks.length + failedTasks.length;
  const successRate = totalTerminal > 0 ? (doneTasks.length / totalTerminal) * 100 : 0;

  // Token tracking across all tasks
  // Burn = fresh + cache-read tokens (the same definition as the drawer and
  // board — tokens_used alone hides 90%+ of real spend, C1).
  const burnOf = (t) => (t.total_tokens || 0) + (t.total_cache_read || 0);
  const totalTokens = tasks.reduce((s, t) => s + burnOf(t), 0);
  const avgTokensPerTask = doneTasks.length > 0
    ? tasks.filter(t => burnOf(t) > 0).reduce((s, t) => s + burnOf(t), 0)
      / Math.max(1, tasks.filter(t => burnOf(t) > 0).length)
    : 0;

  // SDLC metrics — backward-compatible (handles missing fields)
  const withAttempts = doneTasks.filter(t => t.attempts?.length > 0);
  const firstAttemptRate = withAttempts.length > 0
    ? (withAttempts.filter(t => t.attempts.length === 1 && t.attempts[0].review_passed).length / withAttempts.length) * 100
    : 0;
  const avgAttempts = withAttempts.length > 0
    ? withAttempts.reduce((s, t) => s + t.attempts.length, 0) / withAttempts.length
    : 0;
  const specCoverage = tasks.length > 0
    ? (tasks.filter(t => t.context?.spec).length / tasks.length) * 100
    : 0;
  let stagnationCount = 0;
  for (const t of tasks) {
    if (t.attempts?.length >= 2) {
      const a1 = t.attempts[t.attempts.length - 2];
      const a2 = t.attempts[t.attempts.length - 1];
      const r1 = a1.review_checklist?.items;
      const r2 = a2.review_checklist?.items;
      if (r1?.length && r2?.length) {
        const p1 = r1.filter(it => it.passed).length;
        const p2 = r2.filter(it => it.passed).length;
        if (p1 === p2 && p2 < r2.length) stagnationCount++;
      }
    }
  }

  return {
    totalCompleted: doneTasks.length,
    totalFailed: failedTasks.length,
    totalCancelled: cancelledTasks.length,
    totalTasks: tasks.length,
    tasksPerDay,
    avgDuration,
    medianDuration,
    last30Count: last30.length,
    last7Count: last7.length,
    dailyCounts,
    kindBreakdown,
    successRate,
    totalTokens,
    totalTokensFresh: tasks.reduce((s, t) => s + (t.total_tokens || 0), 0),
    totalTokensCache: tasks.reduce((s, t) => s + (t.total_cache_read || 0), 0),
    avgTokensPerTask,
    firstAttemptRate,
    avgAttempts,
    specCoverage,
    stagnationCount,
  };
}

// ── Sparkline (pure SVG) ─────────────────────────────────────────────────────

function Sparkline({ data, width = 320, height = 64 }) {
  if (!data || data.length === 0) return null;
  const max = Math.max(...data.map(d => d.count), 1);
  const barW = Math.max(1, (width - (data.length - 1) * 2) / data.length);
  const gap = 2;

  return (
    <svg
      className="stats-sparkline"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      role="img"
      aria-label="Daily completions chart"
    >
      {data.map((d, i) => {
        const barH = Math.max(1, (d.count / max) * (height - 4));
        const x = i * (barW + gap);
        const y = height - barH;
        return (
          <g key={i}>
            <rect
              x={x}
              y={y}
              width={barW}
              height={barH}
              rx={2}
              className={`stats-sparkline-bar${d.count > 0 ? " has-data" : ""}`}
            />
            <title>{`${d.label}: ${d.count} task${d.count !== 1 ? "s" : ""}`}</title>
          </g>
        );
      })}
    </svg>
  );
}

// ── Kind breakdown bar ───────────────────────────────────────────────────────

const KIND_COLORS = {
  feature: "var(--accent-500)",
  bugfix: "var(--red)",
  ci_fix: "var(--amber)",
  test_gap: "var(--purple)",
  investigation: "var(--cyan)",
  code_review: "var(--green)",
};

function KindBar({ breakdown, total }) {
  if (breakdown.length === 0) return null;
  return (
    <div className="stats-kind-bar-wrap">
      <div className="stats-kind-bar">
        {breakdown.map(({ kind, count }) => (
          <div
            key={kind}
            className="stats-kind-segment"
            style={{
              width: `${(count / total) * 100}%`,
              background: KIND_COLORS[kind] || "var(--text-dim)",
            }}
            title={`${kind}: ${count}`}
          />
        ))}
      </div>
      <div className="stats-kind-legend">
        {breakdown.map(({ kind, count }) => (
          <div key={kind} className="stats-kind-legend-item">
            <span
              className="stats-kind-dot"
              style={{ background: KIND_COLORS[kind] || "var(--text-dim)" }}
            />
            <span className="stats-kind-label">{kind}</span>
            <span className="stats-kind-count">{count}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Token cost estimate ──────────────────────────────────────────────────────

// fmtTokens/estimateCost come from cost.js — the ONE home (a local copy here
// silently shadowed the blended cache-read pricing and kept est. fresh-only).

// ── Task table ───────────────────────────────────────────────────────────────

const STATUS_DOT = {
  done: "var(--green)",
  failed: "var(--red)",
  implementing: "var(--accent-500)",
  reviewing: "var(--amber)",
  testing: "var(--purple)",
};

function TaskTable({ tasks }) {
  const rows = useMemo(() => {
    return tasks
      .map(t => {
        const created = parseTS(t.created_at);
        const updated = parseTS(t.updated_at);
        const duration = created && updated ? (updated.getTime() - created.getTime()) / 1000 : null;
        return { ...t, duration, _sortKey: updated ? updated.getTime() : 0 };
      })
      .sort((a, b) => b._sortKey - a._sortKey);
  }, [tasks]);

  if (rows.length === 0) return null;

  return (
    <div className="stats-table-wrap">
      <table className="stats-table">
        <thead>
          <tr>
            <th className="stats-th stats-th-name">Task</th>
            <th className="stats-th">Status</th>
            <th className="stats-th">Duration</th>
            <th className="stats-th">Project</th>
            <th className="stats-th">Runner</th>
            <th className="stats-th stats-th-right">Tokens</th>
            <th className="stats-th stats-th-right">Est. Cost</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(t => (
            <tr key={t.id} className="stats-tr">
              <td className="stats-td stats-td-name" title={t.title}>
                {t.title}
              </td>
              <td className="stats-td">
                <span className="stats-status-dot" style={{ background: STATUS_DOT[t.status] || "var(--text-dim)" }} />
                {t.status}
              </td>
              <td className="stats-td stats-td-mono">
                {t.duration != null && t.duration > 0 ? fmtDuration(t.duration) : "\u2014"}
              </td>
              <td className="stats-td">{t.repo_name || "\u2014"}</td>
              <td className="stats-td">
                <span className={`stats-runner-badge runner-${t.backend || "claude"}`}>
                  {t.backend === "devin" ? "Devin" : "Claude Code"}
                </span>
              </td>
              <td className="stats-td stats-td-mono stats-td-right">{fmtTokens((t.total_tokens || 0) + (t.total_cache_read || 0) || null)}</td>
              <td className="stats-td stats-td-mono stats-td-right">{estimateCost(t.total_tokens, t.total_cache_read)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Main Stats Component ─────────────────────────────────────────────────────

// C3-G3: what no_human understands about a repo — its onboarded profile, the
// cached repo map, and matched playbooks. Read-only; NO indexing, NO RAG. Lets
// the operator see what the coder sees, without opening a task.
function RepoUnderstanding() {
  const [repos, setRepos] = useState([]);
  const [selected, setSelected] = useState("");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => { fetchRepos().then(setRepos); }, []);
  useEffect(() => {
    if (!selected) { setData(null); setLoading(false); return; }
    let ignore = false;   // drop a stale response when the repo is switched fast
    setLoading(true);
    fetchRepoUnderstanding(selected).then((d) => {
      if (ignore) return;
      setData(d);
      setLoading(false);
    });
    return () => { ignore = true; };
  }, [selected]);

  if (repos.length === 0) return null;   // nothing onboarded yet — no empty widget

  return (
    <div className="stats-section" data-testid="repo-understanding">
      <h3 className="stats-section-title">Repository Understanding</h3>
      <div className="stats-section-sub">
        What no_human knows about a repo — profile, structure map, and playbooks
      </div>
      <select
        className="repo-understanding-select"
        data-testid="repo-understanding-select"
        value={selected}
        onChange={(e) => setSelected(e.target.value)}
      >
        <option value="">Select a repo&hellip;</option>
        {repos.map((r) => (
          <option key={r.repo_path} value={r.repo_path}>{r.name}</option>
        ))}
      </select>

      {loading && <div className="stats-section-sub">Loading&hellip;</div>}

      {data && (
        <div className="repo-understanding-body">
          <div className="repo-understanding-status">{profileStatus(data.profile)}</div>

          {profileRows(data.profile).length > 0 && (
            <table className="repo-understanding-profile">
              <tbody>
                {profileRows(data.profile).map((row) => (
                  <tr key={row.label}>
                    <td className="repo-understanding-key">{row.label}</td>
                    <td className="repo-understanding-val">{row.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {data.playbooks.length > 0 && (
            <div className="repo-understanding-playbooks">
              <div className="repo-understanding-subhead">
                Playbooks ({data.playbooks.length})
              </div>
              <ul>
                {data.playbooks.map((p, i) => (
                  <li key={i}>{p.title}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="repo-understanding-subhead">Structure map</div>
          {data.repo_map
            ? <pre className="repo-understanding-map">{data.repo_map}</pre>
            : <div className="stats-section-sub">
                No cached map (the repo path may be unavailable on this host).
              </div>}
        </div>
      )}
    </div>
  );
}

// C3-G4: cross-task search over the failure/fix record (events_fts). Debounced;
// results grouped by task. Answers "how was a failure like this handled before?"
function SessionSearch() {
  const [q, setQ] = useState("");
  const [results, setResults] = useState([]);
  const [searched, setSearched] = useState(false);

  useEffect(() => {
    const term = q.trim();
    if (!term) { setResults([]); setSearched(false); return; }
    let ignore = false;
    const h = setTimeout(() => {
      searchEvents(term).then((r) => {
        if (ignore) return;
        setResults(r);
        setSearched(true);
      });
    }, 250);   // debounce so each keystroke isn't a request
    return () => { ignore = true; clearTimeout(h); };
  }, [q]);

  const groups = groupByTask(results);

  return (
    <div className="stats-section" data-testid="session-search">
      <h3 className="stats-section-title">Search history</h3>
      <div className="stats-section-sub">
        Full-text search across every task's failures, reviews, and blockers
      </div>
      <input
        type="search"
        className="session-search-input"
        data-testid="session-search-input"
        placeholder="e.g. ImportError, timeout, tamper…"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      {searched && groups.length === 0 && (
        <div className="stats-section-sub">No matches.</div>
      )}
      {groups.length > 0 && (
        <ul className="session-search-results">
          {groups.map((g) => (
            <li key={g.task_id} className="session-search-group">
              <div className="session-search-task">
                <span className="session-search-taskid">{g.task_id.slice(0, 8)}</span>
                {g.task_title}
              </div>
              {g.hits.map((h, i) => (
                <div key={i} className="session-search-hit">
                  <span className="session-search-kind">{kindLabel(h.kind)}</span>
                  <span className="session-search-snippet">{h.snippet}</span>
                </div>
              ))}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function Stats({ tasks }) {
  const stats = useMemo(() => computeStats(tasks), [tasks]);
  const [metrics, setMetrics] = useState(null);
  useEffect(() => { fetchMetrics().then(setMetrics); }, [tasks.length]);
  const northStar = northStarTiles(metrics);

  if (tasks.length === 0) {
    return (
      <div className="stats-page">
        <div className="stats-empty">
          <div className="stats-empty-icon">&#9776;</div>
          <div className="stats-empty-title">No tasks yet</div>
          <div className="stats-empty-desc">
            Create your first task to start seeing stats here.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="stats-page">
      <div className="stats-header">
        <h2 className="stats-title">Performance</h2>
        <div className="stats-subtitle">
          {stats.totalTasks} total tasks &middot; {stats.totalCompleted} completed
        </div>
      </div>

      {/* North-star row (D1): the real delivery numbers from /api/metrics —
          "completed" above counts 0-token code reviews, these do not. */}
      {northStar.length > 0 && (
        <div className="stats-northstar">
          {northStar.map((t) => (
            <div key={t.label} className={`ns-tile ns-${t.tone}`}>
              <div className="ns-label">{t.label}</div>
              <div className="ns-value">{t.value}</div>
              <div className="ns-sub">{t.sub}</div>
            </div>
          ))}
        </div>
      )}

      {/* Primary metric cards */}
      <div className="stats-cards">
        <div className="stats-card stats-card-accent">
          <div className="stats-card-label">Avg. Tasks / Day</div>
          <div className="stats-card-value">
            {stats.totalCompleted > 0 ? stats.tasksPerDay.toFixed(1) : "\u2014"}
          </div>
          <div className="stats-card-sub">
            {stats.last7Count} in the last 7 days
          </div>
        </div>

        <div className="stats-card">
          <div className="stats-card-label">Avg. Completion Time</div>
          <div className="stats-card-value">
            {stats.avgDuration > 0 ? fmtDuration(stats.avgDuration) : "\u2014"}
          </div>
          <div className="stats-card-sub">
            median: {stats.medianDuration > 0 ? fmtDuration(stats.medianDuration) : "\u2014"}
          </div>
        </div>

        <div className="stats-card">
          <div className="stats-card-label">Last 30 Days</div>
          <div className="stats-card-value">{stats.last30Count}</div>
          <div className="stats-card-sub">
            tasks completed
          </div>
        </div>

        <div className={`stats-card${
          stats.totalCompleted + stats.totalFailed > 0
            ? (stats.successRate < 50 ? " stats-card-bad"
               : stats.successRate >= 80 ? " stats-card-good" : "")
            : ""}`}>
          <div className="stats-card-label">Success Rate</div>
          <div className="stats-card-value">
            {stats.totalCompleted + stats.totalFailed > 0
              ? `${stats.successRate.toFixed(0)}%`
              : "\u2014"}
          </div>
          <div className="stats-card-sub">
            {stats.totalCompleted} done &middot; {stats.totalFailed} failed
            {stats.totalCancelled > 0 && <> &middot; {stats.totalCancelled} cancelled</>}
          </div>
        </div>

        <div className="stats-card">
          <div className="stats-card-label">Token Usage</div>
          <div className="stats-card-value">{fmtTokens(stats.totalTokens)}</div>
          <div className="stats-card-sub">
            est. {estimateCost(stats.totalTokensFresh, stats.totalTokensCache)}
            {stats.avgTokensPerTask > 0 && ` · avg ${fmtTokens(stats.avgTokensPerTask)}/task`}
          </div>
        </div>

        {/* First-attempt-pass and spec-coverage only when they have data \u2014
            an empty "\u2014" tile is clutter, not information (D3 declutter). */}
        {stats.avgAttempts > 0 && (
          <div className="stats-card" data-testid="stat-first-attempt">
            <div className="stats-card-label">First-Attempt Pass</div>
            <div className="stats-card-value">{stats.firstAttemptRate.toFixed(0)}%</div>
            <div className="stats-card-sub">
              avg {stats.avgAttempts.toFixed(1)} attempts/task
            </div>
          </div>
        )}

        {stats.specCoverage > 0 && (
          <div className="stats-card" data-testid="stat-spec-coverage">
            <div className="stats-card-label">Spec Coverage</div>
            <div className="stats-card-value">{stats.specCoverage.toFixed(0)}%</div>
            <div className="stats-card-sub">
              {tasks.filter(t => t.context?.spec).length} of {tasks.length} tasks
            </div>
          </div>
        )}

        <div className="stats-card" data-testid="stat-stagnation">
          <div className="stats-card-label">Stagnation</div>
          <div className="stats-card-value">{stats.stagnationCount}</div>
          <div className="stats-card-sub">
            tasks with unchanged pass rate
          </div>
        </div>
      </div>

      {/* Daily completions chart */}
      <div className="stats-section">
        <h3 className="stats-section-title">Daily Completions</h3>
        <div className="stats-section-sub">Last 30 days</div>
        <div className="stats-chart-wrap">
          <Sparkline data={stats.dailyCounts} width={640} height={80} />
        </div>
      </div>

      {/* Kind breakdown */}
      {stats.kindBreakdown.length > 0 && (
        <div className="stats-section">
          <h3 className="stats-section-title">By Type</h3>
          <KindBar breakdown={stats.kindBreakdown} total={stats.totalCompleted} />
        </div>
      )}

      {/* Repository Understanding (C3-G3) */}
      <RepoUnderstanding />

      {/* Cross-task session search (C3-G4) */}
      <SessionSearch />

      {/* Task table */}
      <div className="stats-section">
        <h3 className="stats-section-title">All Tasks</h3>
        <div className="stats-section-sub">{tasks.length} tasks</div>
        <TaskTable tasks={tasks} />
      </div>
    </div>
  );
}
