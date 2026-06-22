import { useState } from "react";
import SlideOver from "./SlideOver.jsx";

const LANES = [
  { key: "pending",            label: "Intake",      accent: "var(--c-intake)",   statuses: ["pending"] },
  { key: "context",            label: "Context",     accent: "var(--c-context)",  statuses: ["context", "planning"] },
  { key: "building",           label: "Building",    accent: "var(--c-building)", statuses: ["implementing"] },
  { key: "review",             label: "Review",      accent: "var(--c-review)",   statuses: ["reviewing"] },
  { key: "testing",            label: "Testing",     accent: "var(--c-testing)",  statuses: ["testing"] },
  { key: "parked",             label: "Parked",      accent: "var(--c-context)",  statuses: ["blocked", "paused_quota"] },
  { key: "awaiting_you",       label: "Awaiting You",accent: "var(--c-awaiting)", statuses: ["awaiting_approval", "awaiting_input"] },
  { key: "done",               label: "Done",        accent: "var(--c-done)",     statuses: ["done"] },
  { key: "escalated",          label: "Escalated",   accent: "var(--c-escalated)",statuses: ["escalated", "failed"] },
];

export default function Board({ tasks }) {
  const [selectedId, setSelectedId] = useState(null);

  return (
    <>
      <div className="nh-board">
        {LANES.map((lane) => (
          <Lane
            key={lane.key}
            lane={lane}
            tasks={tasks.filter((t) => lane.statuses.includes(t.status))}
            onSelect={setSelectedId}
          />
        ))}
      </div>
      {selectedId && (
        <SlideOver
          taskId={selectedId}
          onClose={() => setSelectedId(null)}
        />
      )}
    </>
  );
}

function Lane({ lane, tasks, onSelect }) {
  return (
    <div className="lane">
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
              onClick={() => onSelect(task.id)}
            />
          ))
        )}
      </div>
    </div>
  );
}

function TaskCard({ task, accent, isAwaiting, onClick }) {
  const age = relativeTime(task.updated_at || task.created_at);
  return (
    <div
      className={`task-card${isAwaiting ? " awaiting" : ""}`}
      style={{ "--lane-accent": accent }}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => e.key === "Enter" && onClick()}
    >
      <div className="card-id">{task.id.slice(0, 8)}</div>
      <div className="card-title">{task.title}</div>
      <div className="card-meta">
        <span className="card-source">{task.source}</span>
        {task.pr_url && <span className="card-pr-badge">PR</span>}
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
