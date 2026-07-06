import { useMemo } from "react";

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
  const failedTasks = tasks.filter(t => t.status === "failed");

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

  return {
    totalCompleted: doneTasks.length,
    totalFailed: failedTasks.length,
    totalTasks: tasks.length,
    tasksPerDay,
    avgDuration,
    medianDuration,
    last30Count: last30.length,
    last7Count: last7.length,
    dailyCounts,
    kindBreakdown,
    successRate,
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

function fmtTokens(n) {
  if (n == null) return "\u2014";
  if (n < 1000) return `${n}`;
  if (n < 1000000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1000000).toFixed(2)}M`;
}

// Rough cost estimate for subscription tokens (Claude subscription pricing).
// This is purely indicative — actual billing depends on the plan.
function estimateCost(tokens) {
  if (tokens == null || tokens === 0) return "\u2014";
  // ~$0.003 per 1k input tokens as a rough proxy for blended usage
  const est = (tokens / 1000) * 0.003;
  if (est < 0.01) return "<$0.01";
  return `$${est.toFixed(2)}`;
}

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
              <td className="stats-td stats-td-mono stats-td-right">{fmtTokens(t.total_tokens)}</td>
              <td className="stats-td stats-td-mono stats-td-right">{estimateCost(t.total_tokens)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Main Stats Component ─────────────────────────────────────────────────────

export default function Stats({ tasks }) {
  const stats = useMemo(() => computeStats(tasks), [tasks]);

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

        <div className="stats-card">
          <div className="stats-card-label">Success Rate</div>
          <div className="stats-card-value">
            {stats.totalCompleted + stats.totalFailed > 0
              ? `${stats.successRate.toFixed(0)}%`
              : "\u2014"}
          </div>
          <div className="stats-card-sub">
            {stats.totalCompleted} done &middot; {stats.totalFailed} failed
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

      {/* Task table */}
      <div className="stats-section">
        <h3 className="stats-section-title">All Tasks</h3>
        <div className="stats-section-sub">{tasks.length} tasks</div>
        <TaskTable tasks={tasks} />
      </div>
    </div>
  );
}
