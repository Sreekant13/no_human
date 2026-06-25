import { useState, useRef, useEffect } from "react";
import SlideOver from "./SlideOver.jsx";

// Lanes are organized by WHAT ACTION THE HUMAN NEEDS TO TAKE:
//   Needs You  — tasks waiting for a human decision (approve, reply, or advise)
//   Working    — agent is actively processing (pending through testing)
//   Waiting    — auto-resolvable (will wake on its own, no human needed)
//   Failed     — terminal (separate from resolvable states)
//   Done       — completed
//
// "blocked" tasks are split: those WITH a wake_condition go to Waiting
// (they'll auto-resolve), those WITHOUT go to Needs You (human must act).
const LANES = [
  { key: "needs_you",    label: "Needs You",    accent: "var(--c-awaiting)",  statuses: ["awaiting_approval", "awaiting_input", "escalated"], loud: true, needsYou: true },
  { key: "working",      label: "Working",      accent: "var(--c-building)",  statuses: ["pending", "context", "planning", "implementing", "reviewing", "testing"], showSubStatus: true },
  { key: "waiting",      label: "Waiting",      accent: "var(--c-context)",   statuses: ["blocked", "paused_quota"], autoWait: true },
  { key: "failed",       label: "Failed",       accent: "var(--c-escalated)", statuses: ["failed"] },
  { key: "done",         label: "Done",         accent: "var(--c-done)",      statuses: ["done"] },
];

// "blocked" tasks are routed dynamically: if the blocker has a wake_condition
// the task will auto-resolve → Waiting lane. Otherwise → Needs You lane.
function routeTask(task) {
  if (task.status === "blocked") {
    return task.blocker_wake_condition ? "waiting" : "needs_you";
  }
  for (const lane of LANES) {
    if (lane.statuses.includes(task.status)) return lane.key;
  }
  return "working";
}

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
            tasks={tasks.filter((t) => routeTask(t) === lane.key)}
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
    <div className={`lane lane-${lane.key}${lane.loud ? " lane-loud" : ""}`}>
      <div className="lane-header">
        <div className="lane-dot" style={{ background: lane.accent }} />
        <div className="lane-title">{lane.label}</div>
        {tasks.length > 0 && <div className="lane-count">{tasks.length}</div>}
      </div>
      <div className="lane-body">
        {tasks.length === 0 ? (
          <div className="lane-empty">·</div>
        ) : (
          tasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              accent={lane.accent}
              isAwaiting={!!lane.needsYou}
              showSubStatus={lane.showSubStatus}
              onClick={(e) => onSelect(task.id, e.currentTarget)}
            />
          ))
        )}
      </div>
    </div>
  );
}

const STALE_STATUSES = new Set(["context", "planning", "implementing", "reviewing", "testing", "awaiting_approval", "awaiting_input", "blocked"]);
const STALE_THRESHOLD_S = 16 * 3600;
const ACTIVE_STATUSES = new Set(["context", "planning", "implementing", "reviewing", "testing"]);

// Human-readable action hint for "Needs You" tasks
function actionHint(task) {
  if (task.status === "awaiting_approval") return "review & approve PR";
  if (task.status === "awaiting_input") return "answer question";
  if (task.status === "escalated") return "advise or split task";
  if (task.status === "blocked") return "answer question";
  return null;
}

function TaskCard({ task, accent, isAwaiting, showSubStatus, onClick }) {
  const ageMs = Date.now() - new Date(task.updated_at || task.created_at).getTime();
  const ageSec = ageMs / 1000;
  const age = relativeTime(task.updated_at || task.created_at);
  const isStale = STALE_STATUSES.has(task.status) && ageSec > STALE_THRESHOLD_S;
  const priority = task.priority ?? "medium";

  const isActive = ACTIVE_STATUSES.has(task.status);

  let cardCls = "task-card";
  if (isAwaiting) cardCls += " awaiting";
  if (isStale) cardCls += " stale";
  if (isActive) cardCls += " active-working";

  return (
    <div
      className={cardCls}
      style={{ "--lane-accent": accent }}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onClick(e)}
    >
      {isActive && <div className="card-active-pulse" title="agent is working on this" />}
      <div className="card-id">{task.id.slice(0, 8)}</div>
      <div className="card-title">{task.title}</div>
      {task.description_short && (
        <div className="card-description">{task.description_short}</div>
      )}
      {task.blocker_question && isAwaiting && (
        <div className="card-blocker-q">{task.blocker_question}</div>
      )}
      {isAwaiting && actionHint(task) && (
        <div className="card-action-hint">{actionHint(task)}</div>
      )}
      <div className="card-meta">
        {task.repo_name && <span className="card-repo">{task.repo_name}</span>}
        <span className="card-source">{task.source}</span>
        {task.kind && task.kind !== "feature" && (
          <span className={`card-kind kind-${task.kind}`}>{task.kind}</span>
        )}
        {showSubStatus && (
          <span className={`card-substatus substatus-${task.status}`}>{task.status}</span>
        )}
        {task.attempt_count > 0 && (
          <span className="card-attempts">
            att {task.attempt_count}{task.last_turns != null ? ` · ${task.last_turns}t` : ""}
          </span>
        )}
        {priority === "high" && <span className="card-priority card-priority-high">HI</span>}
        {priority === "low"  && <span className="card-priority card-priority-low">LO</span>}
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
