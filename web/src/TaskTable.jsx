import { useMemo } from "react";
import { costOf, fmtCost, fmtTokens, totalBurn } from "./cost.js";

// The task table, extracted from Stats.jsx so the outcome screens (5D: Done / Failed) show the
// SAME table instead of a second one that would drift out of step with it.
//
// `onSelect` is optional: on Stats the table is a read-out, and on an outcome screen every row
// opens the task drawer — the same drawer the board cards open, with the same keyboard contract
// (Enter must open it, and preventDefault is load-bearing — see Board.jsx).

const STATUS_DOT = {
  done: "var(--green)",
  failed: "var(--red)",
  implementing: "var(--accent-500)",
  reviewing: "var(--amber)",
  testing: "var(--purple)",
};

function parseTS(ts) {
  if (!ts) return null;
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : d;
}

function fmtDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds % 3600) / 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

export default function TaskTable({ tasks, onSelect = null, emptyHint = null }) {
  const rows = useMemo(() => {
    return tasks
      .map((t) => {
        const created = parseTS(t.created_at);
        const updated = parseTS(t.updated_at);
        const duration =
          created && updated ? (updated.getTime() - created.getTime()) / 1000 : null;
        return { ...t, duration, _sortKey: updated ? updated.getTime() : 0 };
      })
      .sort((a, b) => b._sortKey - a._sortKey);
  }, [tasks]);

  if (rows.length === 0) {
    return emptyHint ? <div className="outcome-empty">{emptyHint}</div> : null;
  }

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
          {rows.map((t) => (
            <tr
              key={t.id}
              className={`stats-tr${onSelect ? " stats-tr-clickable" : ""}`}
              {...(onSelect
                ? {
                    role: "button",
                    tabIndex: 0,
                    "aria-label": `Open ${t.title}`,
                    onClick: (e) => onSelect(t.id, e.currentTarget),
                    onKeyDown: (e) => {
                      if (e.target !== e.currentTarget) return;
                      if (e.key === "Enter" || e.key === " ") {
                        // Without preventDefault the drawer opens, autofocuses its close button
                        // in the same event flush, and Enter's default activation clicks it —
                        // the drawer opens and shuts within one keypress (Board.jsx, B2).
                        e.preventDefault();
                        onSelect(t.id, e.currentTarget);
                      }
                    },
                  }
                : {})}
            >
              <td className="stats-td stats-td-name" title={t.title}>
                {t.title}
                {t._collapsed > 0 && (
                  <span
                    className="stats-collapsed-note"
                    title="Older runs with this title — open the row to see its attempts"
                  >
                    +{t._collapsed} older
                  </span>
                )}
              </td>
              <td className="stats-td stats-td-status">
                <span
                  className="stats-status-dot"
                  style={{ background: STATUS_DOT[t.status] || "var(--text-dim)" }}
                />
                {t.cancelled ? "cancelled" : t.status.replace(/_/g, " ")}
              </td>
              <td className="stats-td stats-td-mono">
                {t.duration != null && t.duration > 0 ? fmtDuration(t.duration) : "—"}
              </td>
              <td className="stats-td stats-td-project" title={t.repo_name || undefined}>
                {t.repo_name || "—"}
              </td>
              <td className="stats-td">
                <span className="stats-runner-badge runner-claude">
                  Claude Code
                </span>
              </td>
              <td className="stats-td stats-td-mono stats-td-right">
                {fmtTokens(
                  totalBurn({
                    used: t.total_tokens,
                    creation: t.total_cache_creation,
                    read: t.total_cache_read,
                  }) || null,
                )}
              </td>
              <td className="stats-td stats-td-mono stats-td-right">
                {fmtCost(
                  costOf({
                    used: t.total_tokens,
                    creation: t.total_cache_creation,
                    read: t.total_cache_read,
                  })
                  + costOf({
                    used: t.total_review_tokens,
                    creation: t.total_review_cache_creation,
                    read: t.total_review_cache_read,
                  }),
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
