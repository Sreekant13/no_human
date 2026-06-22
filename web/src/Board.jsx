import { useState, useRef, useEffect } from "react";
import SlideOver from "./SlideOver.jsx";

// "Awaiting You" and "Escalated" are pinned first and rendered loud.
// The 5 transient pipeline stages collapse into one "In Progress" lane
// with a per-card sub-status pill so the board fits a 1440px screen.
const LANES = [
  { key: "awaiting_you",  label: "Awaiting You", accent: "var(--c-awaiting)",  statuses: ["awaiting_approval", "awaiting_input"], loud: true },
  { key: "escalated",     label: "Escalated",    accent: "var(--c-escalated)", statuses: ["escalated", "failed"],                 loud: true },
  { key: "pending",       label: "Intake",       accent: "var(--c-intake)",    statuses: ["pending"] },
  { key: "in_progress",   label: "In Progress",  accent: "var(--c-building)",  statuses: ["context", "planning", "implementing", "reviewing", "testing"], showSubStatus: true },
  { key: "parked",        label: "Parked",       accent: "var(--c-context)",   statuses: ["blocked", "paused_quota"] },
  { key: "done",          label: "Done",         accent: "var(--c-done)",      statuses: ["done"] },
];

export default function Board({ tasks }) {
  const [selectedId, setSelectedId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const triggerRef = useRef(null);
  const prevUpdatedAtRef = useRef(null);

  // Re-fetch the SlideOver whenever the selected task's updated_at changes via WS
  useEffect(() => {
    if (!selectedId) return;
    const selected = tasks.find((t) => t.id === selectedId);
    const stamp = selected?.updated_at ?? null;
    if (stamp && stamp !== prevUpdatedAtRef.current) {
      prevUpdatedAtRef.current = stamp;
      setRefreshKey((k) => k + 1);
    }
  }, [tasks, selectedId]);

  function openTask(id, domNode) {
    triggerRef.current = domNode;
    prevUpdatedAtRef.current = null; // reset so first open always fetches
    setSelectedId(id);
  }

  function closeTask() {
    setSelectedId(null);
    prevUpdatedAtRef.current = null;
    // Restore focus to the card that triggered the panel
    triggerRef.current?.focus();
    triggerRef.current = null;
  }

  return (
    <>
      <div className="nh-board">
        {LANES.map((lane) => (
          <Lane
            key={lane.key}
            lane={lane}
            tasks={tasks.filter((t) => lane.statuses.includes(t.status))}
            onSelect={openTask}
          />
        ))}
      </div>
      {selectedId && (
        <SlideOver
          taskId={selectedId}
          onClose={closeTask}
          refreshKey={refreshKey}
        />
      )}
    </>
  );
}

function Lane({ lane, tasks, onSelect }) {
  return (
    <div className={`lane${lane.loud ? " lane-loud" : ""}`}>
      <div className="lane-header">
        <div className="lane-dot" style={{ background: lane.accent }} />
        <div className="lane-title">{lane.label}</div>
        {tasks.length > 0 && <div className="lane-count">{tasks.length}</div>}
      </div>
      <div className="lane-body">
        {tasks.length === 0 ? (
          <div className="lane-empty">—</div>
        ) : (
          tasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              accent={lane.accent}
              isAwaiting={lane.key === "awaiting_you"}
              showSubStatus={lane.showSubStatus}
              onClick={(e) => onSelect(task.id, e.currentTarget)}
            />
          ))
        )}
      </div>
    </div>
  );
}

function TaskCard({ task, accent, isAwaiting, showSubStatus, onClick }) {
  const age = relativeTime(task.updated_at || task.created_at);
  return (
    <div
      className={`task-card${isAwaiting ? " awaiting" : ""}`}
      style={{ "--lane-accent": accent }}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onClick(e)}
    >
      <div className="card-id">{task.id.slice(0, 8)}</div>
      <div className="card-title">{task.title}</div>
      <div className="card-meta">
        <span className="card-source">{task.source}</span>
        {showSubStatus && (
          <span className={`card-substatus substatus-${task.status}`}>{task.status}</span>
        )}
        {task.pr_url && (
          task.pr_url.startsWith("http") ? (
            <a
              className="card-pr-badge"
              href={task.pr_url}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
            >PR</a>
          ) : (
            <span className="card-pr-badge">PR</span>
          )
        )}
        <span className="card-age">{age}</span>
      </div>
    </div>
  );
}

function relativeTime(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "<1m";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}
